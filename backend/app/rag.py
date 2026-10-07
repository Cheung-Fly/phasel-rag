"""文档入库 + 检索问答（阶段一核心链路）。

链路：上传文件 → 解析文本 → 递归切片 → 向量化 → 写入独立 Chroma 服务
     提问 → 向量检索 Top-K 片段 → 拼 Prompt → LLM 生成 → 带来源返回

入库前有两道「工作量」闸门（字符数 / 切片数），原因见 ingest() 的说明。
"""
import logging
import threading
import uuid
from pathlib import Path

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from .config import get_settings
from .llm import get_chat_model
from .vectorstore import get_vectorstore

logger = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".md", ".markdown", ".txt"}

SYSTEM_PROMPT = """你是一个严谨的私有知识库助手。请只依据下面提供的【参考资料】回答问题。

规则：
1. 如果参考资料中没有相关信息，直接回答"知识库中未找到相关内容"，不要编造。
2. 回答时在句末用 [来源N] 标注依据的片段编号。
3. 使用简体中文，条理清晰，必要时用列表。
4. 不要复述规则本身。

【参考资料】
{context}
"""


# ---------------------------------------------------------------- 入库并发闸门
_gate: threading.BoundedSemaphore | None = None
_gate_lock = threading.Lock()


def _ingest_gate() -> threading.BoundedSemaphore:
    """入库并发闸门，名额由 INGEST_MAX_CONCURRENT 决定（默认 1）。

    为什么需要闸门：embedding 是串行批量调用，一份文档要跑上百次请求。
    2 vCPU 上两个入库任务并行，只会互相抢 CPU 并把云端接口打到限流，
    最终两个都变慢 —— 串行反而更快更稳。这里用非阻塞获取 + 明确报错，
    而不是让第二个请求无限等待（那会变成一个看不见的排队，超时也难解释）。
    """
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = threading.BoundedSemaphore(
                    max(1, get_settings().ingest_max_concurrent)
                )
    return _gate


def _parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    # .md / .markdown / .txt 统一按 UTF-8 读，容错非法字节
    return path.read_text(encoding="utf-8", errors="replace")


def ingest(file_bytes: bytes, filename: str) -> dict:
    """把一份文档切块入库，返回统计信息。

    【为什么这里有两道「工作量」闸门（2026-10-07 按服务器规格新增）】
    原先只有 main.py 里的 MAX_UPLOAD_BYTES 在拦「文件体积」，而真正决定
    embedding 成本与容器负载的是「切片数量」，两者并不成正比：
    50MB 纯文本约等于 75000 个片段、7500 次 embedding 调用，足以打挂容器。
    （真实事故：一次测试上传 22MB 文本，被切成 33002 片后疯狂调用 embedding，
    容器健康检查随即失败。）因此在这里加两道闸：
      · 字符数上限：在切片之前先拦下明显过大的文件，省掉一次全量切片；
      · 切片数上限：作为硬约束兜底，直接对齐「embedding 调用次数」这个真实成本单位。
    """
    s = get_settings()
    Path(s.upload_dir).mkdir(parents=True, exist_ok=True)

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"不支持的文件类型 {suffix}，仅支持 {sorted(SUPPORTED)}")

    # ---- 闸门一：同时只允许一个入库任务 ----
    gate = _ingest_gate()
    if not gate.acquire(blocking=False):
        raise ValueError(
            "已有一个文档正在入库，请等它处理完再上传"
            f"（本机并发入库名额为 {s.ingest_max_concurrent}）"
        )

    stored: Path | None = None
    try:
        doc_id = uuid.uuid4().hex[:12]
        stored = Path(s.upload_dir) / f"{doc_id}{suffix}"
        stored.write_bytes(file_bytes)

        text = _parse_file(stored)
        if not text.strip():
            raise ValueError("文件解析后内容为空（可能是扫描版 PDF，需先 OCR）")

        # ---- 闸门二 a：字符数预检（在切片前拦下明显过大的文件）----
        if len(text) > s.ingest_max_chars:
            raise ValueError(
                f"文档解析后约 {len(text):,} 字符，超过本机上限 {s.ingest_max_chars:,}。"
                "向量化需要逐批调用云端 embedding 接口，超长文档会占用数十分钟"
                "并消耗大量调用配额，请拆分后分批上传。"
            )

        splitter = RecursiveCharacterTextSplitter(
            chunk_size=s.chunk_size,
            chunk_overlap=s.chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", ".", " ", ""],
        )
        chunks = splitter.split_text(text)

        # ---- 闸门二 b：切片数硬上限 ----
        if len(chunks) > s.ingest_max_chunks:
            approx_calls = len(chunks) // max(1, s.embedding_batch_size) + 1
            raise ValueError(
                f"该文档会被切成 {len(chunks):,} 个片段，超过本机上限 "
                f"{s.ingest_max_chunks:,}（约需 {approx_calls:,} 次 embedding 调用）。"
                "请拆分或精简后重试。"
            )

        docs = [
            Document(
                page_content=chunk,
                metadata={"doc_id": doc_id, "filename": filename, "chunk_index": i},
            )
            for i, chunk in enumerate(chunks)
        ]

        ids = [f"{doc_id}-{i}" for i in range(len(docs))]

        # ---- 显式分批入库 ----
        # 百炼的 OpenAI 兼容 embedding 接口单次最多接受 10 行输入。
        # langchain 的 chunk_size 已经设成 10，但这里再显式分批做双重保险：
        # 即使有人手改 .env 把 EMBEDDING_BATCH_SIZE 调大，也不会整批失败。
        store = get_vectorstore()
        batch = max(1, s.embedding_batch_size)
        total = len(docs)
        for start in range(0, total, batch):
            store.add_documents(docs[start:start + batch], ids=ids[start:start + batch])
            logger.info("  embedded %d/%d chunks", min(start + batch, total), total)

        logger.info("ingested %s -> %d chunks (doc_id=%s)", filename, total, doc_id)
        return {"doc_id": doc_id, "filename": filename, "chunks": total, "chars": len(text)}
    except Exception:
        # 入库失败（含被闸门拦下）时清掉刚落盘的原始文件，避免留下永远不会被
        # 引用的孤立文件 —— 此前排查时就在 uploads 里发现过两份这种"孤儿"。
        if stored is not None and stored.exists():
            try:
                stored.unlink()
                logger.info("已清理入库失败的残留文件 %s", stored.name)
            except OSError as exc:
                logger.warning("清理残留文件失败 %s: %s", stored, exc)
        raise
    finally:
        gate.release()


def list_documents() -> list[dict]:
    """按 doc_id 聚合已入库文档（Chroma 没有原生分组，取元数据自己汇总）。"""
    data = get_vectorstore().get(include=["metadatas"])
    seen: dict[str, dict] = {}
    for meta in data.get("metadatas") or []:
        if not meta:
            continue
        doc_id = meta.get("doc_id")
        if doc_id and doc_id not in seen:
            seen[doc_id] = {"doc_id": doc_id, "filename": meta.get("filename")}
    return list(seen.values())


def delete_document(doc_id: str) -> int:
    """删除文档：向量片段 + 落盘的原始文件。

    为什么必须连原始文件一起删：删除向量的逻辑很早就有了，但一直没删
    uploads 下的原文件，结果是每删一次文档就留下一个永远不会再被引用的
    「孤儿」。此前排查时就在 uploads 里发现过两份历史孤儿（一份 md、一份 pdf），
    只能靠人工比对文件名才认出来。这里补上，让 uploads 始终与知识库一致。
    """
    s = get_settings()
    store = get_vectorstore()
    matched = store.get(where={"doc_id": doc_id}, include=[])
    ids = matched.get("ids") or []
    if ids:
        store.delete(ids=ids)

    # 原始文件的命名规则是 {doc_id}{后缀}，但后缀可能随上传时的文件名变化，
    # 所以用 glob 匹配而不是猜扩展名。
    for path in Path(s.upload_dir).glob(f"{doc_id}.*"):
        try:
            path.unlink()
            logger.info("已删除原始文件 %s", path.name)
        except OSError as exc:
            logger.warning("删除原始文件失败 %s: %s", path, exc)

    return len(ids)


def answer(question: str, session_id: str) -> dict:
    """检索 + 生成，返回答案与引用来源。"""
    s = get_settings()
    store = get_vectorstore()

    hits = store.similarity_search_with_relevance_scores(question, k=s.retrieval_top_k)

    if not hits:
        return {
            "answer": "知识库中未找到相关内容，请先上传文档。",
            "sources": [],
            "session_id": session_id,
        }

    context_parts, sources = [], []
    for idx, (doc, score) in enumerate(hits, start=1):
        context_parts.append(f"[来源{idx}] 文件：{doc.metadata.get('filename')}\n{doc.page_content}")
        sources.append({
            "index": idx,
            "filename": doc.metadata.get("filename"),
            "doc_id": doc.metadata.get("doc_id"),
            "chunk_index": doc.metadata.get("chunk_index"),
            "score": round(float(score), 4),
            "preview": doc.page_content[:160],
        })

    chain = (
        ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "历史对话：\n{history}\n\n当前问题：{question}"),
        ])
        | get_chat_model()
        | StrOutputParser()
    )

    # 历史只取最近 3 轮：够用即可，多取只是白烧 token（每轮都要重复计费）。
    # 完整历史一直在 PostgreSQL 里存档，需要更多上下文时改这个数字即可。
    from .memory import get_memory

    history = get_memory().get_history(session_id)[-3:]
    history_text = "\n".join(f"用户：{h['user']}\n助手：{h['ai']}" for h in history) or "（无）"

    result = chain.invoke({
        "context": "\n\n---\n\n".join(context_parts),
        "history": history_text,
        "question": question,
    })

    get_memory().append(session_id, question, result)
    return {"answer": result, "sources": sources, "session_id": session_id}


def answer_stream(question: str, session_id: str):
    """流式版问答：按事件逐段产出，供 SSE 接口使用。

    产出的事件序列：
        {"type": "sources", "sources": [...]}   先给来源（前端可立即展示）
        {"type": "token",   "text": "..."}      反复产出，每次一小段
        （调用方负责在结束后发送 done / error）

    为什么先发 sources 再发 token：
        sources 在检索后立刻就能确定，而 token 要等 LLM 生成。
        先发出去能让前端马上把「引用了哪些片段」显示出来，
        用户不必等到答案生成完才看到依据 —— 这是体验上的明显改善。
    """
    s = get_settings()
    store = get_vectorstore()
    hits = store.similarity_search_with_relevance_scores(question, k=s.retrieval_top_k)

    if not hits:
        yield {"type": "sources", "sources": []}
        # 知识库为空时不调用 LLM，直接给结论（省一次 API 调用，也杜绝瞎编）
        yield {"type": "token", "text": "知识库中未找到相关内容，请先上传文档。"}
        return

    context_parts, sources = [], []
    for idx, (doc, score) in enumerate(hits, start=1):
        context_parts.append(
            f"[来源{idx}] 文件：{doc.metadata.get('filename')}\n{doc.page_content}"
        )
        sources.append({
            "index": idx,
            "filename": doc.metadata.get("filename"),
            "doc_id": doc.metadata.get("doc_id"),
            "chunk_index": doc.metadata.get("chunk_index"),
            "score": round(float(score), 4),
            "preview": doc.page_content[:160],
        })

    yield {"type": "sources", "sources": sources}

    chain = (
        ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "历史对话：\n{history}\n\n当前问题：{question}"),
        ])
        | get_chat_model()
        | StrOutputParser()
    )

    from .memory import get_memory

    history = get_memory().get_history(session_id)[-3:]
    history_text = "\n".join(f"用户：{h['user']}\n助手：{h['ai']}" for h in history) or "（无）"

    # .stream() 逐块返回，而不是 .invoke() 等全部生成完
    collected: list[str] = []
    for chunk in chain.stream({
        "context": "\n\n---\n\n".join(context_parts),
        "history": history_text,
        "question": question,
    }):
        if chunk:
            collected.append(chunk)
            yield {"type": "token", "text": chunk}

    # 流结束后才写记忆：确保存进去的是完整答案
    get_memory().append(session_id, question, "".join(collected))
