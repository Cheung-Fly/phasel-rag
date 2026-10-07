"""FastAPI 入口：阶段一对外接口。

包含：
  - 文档管理：上传 / 列表 / 删除
  - 问答：普通（一次性返回）+ 流式（SSE 逐字返回）
  - 记忆：查询 / 清空
  - 前端：挂载在 /ui（纯 HTML/CSS/JS 三件套）
"""
import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import rag
from .config import get_settings
from .memory import get_memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Phase 1 RAG API", version="0.2.0")

# CORS 白名单来自环境变量 CORS_ORIGINS（逗号分隔），默认留空。
# 前端与 API 同源（/ui 由本服务托管），留空不影响正常访问；
# 只有需要 Vue3 dev server 等跨域直连时才填具体源。
# 安全前提：原先的 allow_origins=["*"] 会让任意网站借访客浏览器调本 API，
# 对外暴露前必须收紧到具体域名。
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 上传上限随容器内存上限一起放宽（1 GiB 机型时代是 20MB，现在默认 50MB）
MAX_UPLOAD_BYTES = _settings.max_upload_mb * 1024 * 1024


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=64)


# ---------------------------------------------------------------- 基础
@app.get("/health", tags=["基础"], summary="健康检查")
def health() -> dict:
    s = get_settings()
    return {
        "status": "ok",
        "llm_provider": s.llm_provider,
        "llm_model": s.llm_model,
        "embedding_model": s.embedding_model,
        "memory_backend": s.memory_backend(),
    }


# ---------------------------------------------------------------- 文档
@app.post("/api/documents/upload", tags=["文档"], summary="上传文档并入库")
async def upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 限制")
    try:
        return rag.ingest(data, file.filename or "unnamed.txt")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("ingest failed")
        raise HTTPException(500, f"入库失败：{exc}") from exc


@app.get("/api/documents", tags=["文档"], summary="列出已入库文档")
def documents() -> dict:
    return {"documents": rag.list_documents()}


@app.delete("/api/documents/{doc_id}", tags=["文档"], summary="删除文档")
def delete_document(doc_id: str) -> dict:
    deleted = rag.delete_document(doc_id)
    if deleted == 0:
        raise HTTPException(404, "未找到该文档")
    return {"deleted_chunks": deleted}


# ---------------------------------------------------------------- 问答
@app.post("/api/chat", tags=["问答"], summary="问答（一次性返回全部结果）")
def chat(req: AskRequest) -> dict:
    session_id = req.session_id or uuid.uuid4().hex[:16]
    try:
        return rag.answer(req.question, session_id)
    except Exception as exc:
        logger.exception("chat failed")
        raise HTTPException(500, f"生成失败：{exc}") from exc


@app.post("/api/chat/stream", tags=["问答"], summary="问答（SSE 流式逐字返回）")
def chat_stream(req: AskRequest, request: Request) -> StreamingResponse:
    """Server-Sent Events 流式问答。

    事件协议（每条 data 都是一个 JSON 对象，带 type 字段）：
        {"type":"meta",  "session_id":"..."}          会话建立
        {"type":"sources","sources":[...]}            命中的知识库片段
        {"type":"token", "text":"..."}                一个回答片段（可能多次）
        {"type":"done",  "session_id":"..."}          正常结束
        {"type":"error", "message":"..."}             出错

    为什么用 POST + fetch 而不是浏览器原生 EventSource：
        EventSource 只支持 GET，且不能带请求体。
        我们需要 POST 一个 JSON（含问题和 session_id），所以改用
        fetch + ReadableStream 手动解析 SSE。协议本身仍是标准的 SSE。
    """
    session_id = req.session_id or uuid.uuid4().hex[:16]

    def sse(payload: dict) -> str:
        # ensure_ascii=False 让中文直接输出，不必转成 \uXXXX
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def event_stream():
        try:
            yield sse({"type": "meta", "session_id": session_id})

            for event in rag.answer_stream(req.question, session_id):
                if event["type"] == "sources":
                    yield sse({"type": "sources", "sources": event["sources"]})
                else:
                    yield sse({"type": "token", "text": event["text"]})

            yield sse({"type": "done", "session_id": session_id})
        except Exception as exc:
            logger.exception("stream failed")
            yield sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            # 关掉各级缓冲：否则代理/浏览器会攒够一批才吐出来，失去"流式"意义
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 给 nginx 看的：不要缓冲
        },
    )


# ---------------------------------------------------------------- 记忆 / 会话
@app.get("/api/sessions", tags=["记忆"], summary="列出所有会话")
def sessions() -> dict:
    """给前端做会话列表用。

    注意：会话数据只是对话记忆。当前的 inproc 后端在容器重启后会清空，
    所以这个列表是「进程生命周期内」的。
    """
    return {"sessions": get_memory().list_sessions()}


@app.delete("/api/sessions", tags=["记忆"], summary="清空所有会话")
def clear_all_sessions() -> dict:
    return {"cleared": get_memory().clear_all()}


@app.get("/api/chat/{session_id}/history", tags=["记忆"], summary="查询会话历史")
def history(session_id: str) -> dict:
    return {"session_id": session_id, "history": get_memory().get_history(session_id)}


@app.delete("/api/chat/{session_id}/history", tags=["记忆"], summary="清空会话历史")
def clear_history(session_id: str) -> dict:
    get_memory().clear(session_id)
    return {"session_id": session_id, "cleared": True}


# ---------------------------------------------------------------- 前端
# 纯静态三件套，挂到 /ui。必须放在所有 API 路由之后注册，
# 否则 "/" 挂载会抢先匹配掉 API 路径。
_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC_DIR), html=True), name="ui")
    logger.info("前端已挂载: /ui  (目录 %s)", _STATIC_DIR)
else:
    logger.warning("静态目录不存在，未挂载前端: %s", _STATIC_DIR)
