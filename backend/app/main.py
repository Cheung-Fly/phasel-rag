"""FastAPI 入口：阶段一对外接口。

包含：
  - 文档管理：上传 / 列表 / 删除
  - 问答：普通（一次性返回）+ 流式（SSE 逐字返回）
  - 记忆：查询 / 清空

存储拓扑（2026-10-07 起）：
    本进程只负责业务逻辑，三种数据各自独立成服务 ——
    向量 → 独立 Chroma 容器；会话存档 → PostgreSQL（真相源）；
    会话热缓存 → Redis。本进程挂了，数据一条都不会丢。

前端拓扑（2026-10-07 起）：
    ★ 本服务不再托管前端，一个 HTML/CSS/JS 都不发。
    前端是独立的 Vue3 + Vite 工程（./frontend），生产环境由 nginx 容器
    托管构建产物并把 /api、/health 反代回本服务。三点好处：
      1) 发布解耦：换前端不必重建/重启 API 镜像，反过来 API 升级也不
         影响静态页面能否打开（哪怕 API 挂了，页面仍能加载并显示错误）；
      2) 职责单一：本服务只处理接口，静态文件的缓存头、gzip、TLS
         交给擅长做这些的 nginx；
      3) 无同源妥协：浏览器访问的一直是 nginx 那一个源，
         不需要为了让前端调接口而放开 CORS。
    因此导入本模块时不会再去检查任何前端目录、也不会挂载 StaticFiles。
"""
import json
import logging
import uuid

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import rag
from .config import get_settings
from .memory import get_memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Phase 1 RAG API", version="0.2.0")

# CORS 白名单来自环境变量 CORS_ORIGINS（逗号分隔），默认留空。
# 留空是常态且是正确配置：开发期由 Vite dev server 把 /api 代理过来，
# 生产期由 nginx 把 /api 反代过来 —— 两种情况下浏览器看到的都是同源，
# 压根不会发起跨域请求。只有「前端与后端之间没有任何代理」时
# （例如前端单独部署到另一个域名）才需要在这里填白名单。
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

# 上传上限：第一道粗筛，只拦「文件体积」。
# 真正决定 embedding 成本与容器负载的是「切片数」，由 rag.ingest 里的两道闸门负责
# （见 config.py 中 ingest_max_chars / ingest_max_chunks 的说明）。
MAX_UPLOAD_BYTES = _settings.max_upload_mb * 1024 * 1024


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=64)


# ---------------------------------------------------------------- 基础
def _probe_chroma(s) -> str:
    """实时探测独立的 Chroma 服务。

    为什么要主动探一次向量库：vectorstore 是懒加载的 —— 只有真的上传或检索时
    才会建立连接。也就是说，主机名写错这类配置问题在"第一次使用"之前完全静默，
    /health 却一直报 ok。这里用 2 秒超时探一次，把问题暴露在健康检查里。
    """
    import urllib.request

    url = f"http://{s.chroma_host}:{s.chroma_port}/api/v2/heartbeat"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return "ok" if resp.status == 200 else f"http {resp.status}"
    except Exception as exc:
        return f"unreachable ({type(exc).__name__})"


@app.get("/health", tags=["基础"], summary="健康检查")
def health() -> dict:
    s = get_settings()
    chroma_status = _probe_chroma(s)
    return {
        # 向量库不可达时降级为 degraded：API 进程本身还能响应，
        # 但知识库功能已经不可用，不该报 ok。
        "status": "ok" if chroma_status == "ok" else "degraded",
        "llm_provider": s.llm_provider,
        "llm_model": s.llm_model,
        "embedding_model": s.embedding_model,
        # 由 MemoryStore 自报【运行态】而非读配置：原先 health 直接返回
        # 配置里有没有填 REDIS_URL，结果是"填了但 redis 包没装"时照样报 redis，
        # 属于谎报。现在只有真的 ping 通才会出现在这里。
        "memory_backend": get_memory().backend_summary(),
        "vector_store": f"chroma://{s.chroma_host}:{s.chroma_port}",
        "chroma_status": chroma_status,
        # 前端不再写死「最大 20MB」这类数字：上限是会随机型调整的配置，
        # 写死在前端就会出现「界面说 20MB、后端其实收 50MB」的假信息。
        # 这里把真实生效值暴露出来，前端读它来显示与预校验。
        "max_upload_mb": s.max_upload_mb,
        "ingest_max_chunks": s.ingest_max_chunks,
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
        # ★ 必须丢到线程池执行，不能直接调用。
        # rag.ingest 是同步函数，入库一份文档要串行发起上百次 embedding 请求
        # （每批限 10 行）。直接在 async 路由里同步调用会阻塞整个事件循环 ——
        # 期间所有接口（包括 /health 和其他人的问答）都会卡住，容器还会因此
        # 被健康检查判定为不健康。run_in_threadpool 是 Starlette 官方提供的
        # 处理同步代码的标准做法。
        return await run_in_threadpool(rag.ingest, data, file.filename or "unnamed.txt")
    except ValueError as exc:
        # 业务校验失败（类型不支持 / 超出字符或切片上限 / 已有任务在跑）
        # → 400，把可读原因原样告诉用户
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

    数据源是 PostgreSQL（真相源），所以容器重启、Redis 缓存过期都不会丢。
    只有 PostgreSQL 不可用时才会退回 Redis / 进程内，那时这个列表才是临时的。
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


# ---------------------------------------------------------------- 根路径
# 这里曾经挂载过静态前端（StaticFiles 挂在 /ui，根路径 307 跳到 /ui/）。
# 前端迁到 Vue3 + Vite、由独立 nginx 容器托管后，本服务不再发任何静态文件。
#
# 为什么根路径还要保留一个响应，而不是干脆让它 404：
#   直接访问 127.0.0.1:8000 的人（多半是在排查问题）需要立刻知道
#   「这是 API，不是网站 —— 页面在 nginx 那个端口上」。
#   一个 404 会让人以为服务没起来，然后去查一堆无关的东西。
#   返回一份自描述的 JSON 是最省事的指路牌。
@app.get("/", include_in_schema=False)
def root() -> dict:
    return {
        "service": "Phase 1 RAG API",
        "notice": "本服务只提供接口，不托管前端页面。",
        "frontend": "页面由独立的 nginx 容器提供（生产 8080，开发 Vite 5173）",
        "docs": "/docs",
        "health": "/health",
    }

