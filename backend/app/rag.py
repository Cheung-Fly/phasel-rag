"""文档入库 + 检索问答（阶段一核心链路）。

链路：上传文件 → 解析文本 → 递归切片 → 向量化 → Chroma 入库
     提问 → 向量检索 Top-K 片段 → 拼 Prompt → LLM 生成 → 带来源返回
"""
import logging
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


def _parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    # .md / .markdown / .txt 统一按 UTF-8 读，容错非法字节
    return path.read_text(encoding="utf-8", errors="replace")


def ingest(file_bytes: bytes, filename: str) -> dict:
    """把一份文档切块入库，返回统计信息。"""
    s = get_settings()
    Path(s.upload_dir).mkdir(parents=True, exist_ok=True)

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"不支持的文件类型 {suffix}，仅支持 {sorted(SUPPORTED)}")

    doc_id = uuid.uuid4().hex[:12]
    stored = Path(s.upload_dir) / f"{doc_id}{suffix}"
    stored.write_bytes(file_bytes)

    text = _parse_file(stored)
    if not text.strip():
        raise ValueError("文件解析后内容为空（可能是扫描版 PDF，需先 OCR）")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=s.chunk_size,
        chunk_overlap=s.chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", ".", " ", ""],
    )
    chunks = splitter.split_text(text)

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
    for start in range(0, len(docs), batch):
        store.add_documents(docs[start:start + batch], ids=ids[start:start + batch])
        logger.info("  embedded %d/%d chunks", min(start + batch, len(docs)), len(docs))

    logger.info("ingested %s -> %d chunks (doc_id=%s)", filename, len(docs), doc_id)
    return {"doc_id": doc_id, "filename": filename, "chunks": len(docs), "chars": len(text)}


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
    store = get_vectorstore()
    matched = store.get(where={"doc_id": doc_id}, include=[])
    ids = matched.get("ids") or []
    if ids:
        store.delete(ids=ids)
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

    # 历史只取最近 3 轮，避免把上下文撑爆（1 GiB 内存 + 突发型 CPU 额度都要省）
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
