"""文档入库 + 检索问答（阶段一核心链路）。

链路：上传文件 → 解析文本 → 递归切片 → 向量化 → 写入独立 Chroma 服务
     提问 → 向量检索 Top-K 片段 → 拼 Prompt → LLM 生成 → 带来源返回

入库前有两道「工作量」闸门（字符数 / 切片数），原因见 ingest_stored_file() 的说明。

【2026-10-14 阶段 B：入库改走后台任务】
  上传接口不再同步等入库跑完（一份 1200 片的文档要 1~3 分钟，请求一直挂着，
  用户关掉页面就前功尽弃），而是「落盘 → 入队 → 立刻返回 task_id」。
  相应地，入库被拆成两半：
    store_upload()       —— 由接口调用，把字节流落到 uploads/ 并返回路径
    ingest_stored_file() —— 由 worker 调用，对已落盘的文件做解析/切片/向量化
    ingest()             —— 二者串起来的同步版本，保留为队列不可用时的降级路径
  拆开的另一个好处：跨「入队 → 执行」这段（可能几分钟、也可能跨进程重启）
  不再需要把文件字节留在内存里，任务只需要携带一个路径。
"""
import logging
import re
import threading
import uuid
import zipfile
from datetime import date
from pathlib import Path
from typing import Callable

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from .config import get_settings
from .llm import get_chat_model
from .rag_prompt import SYSTEM_PROMPT, current_date_iso
from .retrieval import invalidate_corpus_cache, retrieve
from .task_store import TaskCancelled
from .temporal import elapsed_years_answer
from .vectorstore import get_vectorstore

logger = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".md", ".markdown", ".txt", ".docx", ".pptx", ".xlsx"}
MARKITDOWN_FORMATS = {".docx", ".pptx", ".xlsx"}
MAX_OFFICE_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_OFFICE_ARCHIVE_ENTRIES = 10_000

# 公共知识库文档的特殊归属值。它不是任何一个真实 user_id，
# 所以「按 owner_user_id 精确匹配」的删除路径永远匹配不到它。
SHARED_OWNER_ID = "shared"

# 模型输出的来源标注，例如 [来源3]
_CITATION_RE = re.compile(r"\[来源(\d+)\]")


def _sanitize_citations(text: str, source_count: int) -> str:
    """剔除越界的 [来源N] 标注。

    提示词已明确要求只标注真实存在的来源编号，但实测中模型仍会编造不存在的编号：
    某次只有 4 条来源时写 [来源5]、只有 2 条来源时写 [来源8]。这类标注在前端无法
    对应到任何来源，会直接误导用户，因此返回前统一清除；越界标注一律删除并记日志，
    范围内的标注原样保留。
    """
    invalid: list[str] = []

    def _replace(match: "re.Match[str]") -> str:
        number = int(match.group(1))
        if 1 <= number <= source_count:
            return match.group(0)
        invalid.append(match.group(0))
        return ""

    cleaned = _CITATION_RE.sub(_replace, text)
    if invalid:
        logger.warning(
            "removed %d out-of-range citation marker(s) %s (sources=%d)",
            len(invalid),
            ", ".join(invalid),
            source_count,
        )
    return cleaned


# ---------------------------------------------------------------- 长期记忆（阶段 D）
def _user_facts_text(owner_user_id: str, question: str, top_k: int = 5) -> str:
    """检索与当前问题相关的长期记忆，拼成给 Prompt 用的一段文本。

    降级链：fact_store 不可用 / pgvector 未装 / embedding 失败 → 返回空串，
    对应 Prompt 里的【用户长期记忆】段落就是「（无）」，不影响主链路。
    """
    try:
        from .fact_store import get_fact_store

        facts = get_fact_store().search(owner_user_id, question, top_k)
    except Exception as exc:
        logger.warning("长期记忆检索失败（已忽略）: %s", exc)
        return "（无）"
    if not facts:
        return "（无）"
    return "\n".join(
        f"- [{f['fact_type']}] {f['fact']}" for f in facts
    )


def _enqueue_fact_extraction(
    owner_user_id: str, session_id: str, user_msg: str, ai_msg: str
) -> None:
    """对话落库后，异步登记一次长期记忆抽取（阶段 D）。

    放在 rag 层而不是只放 main.py，是因为流式问答的答案在 rag.answer_stream
    内部才拼接完整 —— main.py 只能拿到生成器，拿不到最终答案文本。
    入队失败静默：长期记忆是增强，不是主链路的硬依赖。
    """
    try:
        from .task_store import get_task_store_or_none

        store = get_task_store_or_none()
        if store is None:
            return
        store.enqueue(
            "fact_extraction",
            owner_user_id,
            {"session_id": session_id, "user_msg": user_msg, "ai_msg": ai_msg},
        )
    except Exception as exc:
        logger.info("长期记忆抽取任务入队失败（已跳过）: %s", exc)


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
    if suffix in MARKITDOWN_FORMATS:
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_OFFICE_ARCHIVE_ENTRIES:
                    raise ValueError("Office 文档包含过多条目")
                if sum(entry.file_size for entry in entries) > MAX_OFFICE_ARCHIVE_BYTES:
                    raise ValueError("Office 文档解压后超过 100MB 限制")
        except zipfile.BadZipFile as exc:
            raise ValueError("Office 文档不是有效的文件") from exc
        from markitdown import MarkItDown

        return MarkItDown(enable_plugins=False).convert(str(path)).markdown
    # .md / .markdown / .txt 统一按 UTF-8 读，容错非法字节
    return path.read_text(encoding="utf-8", errors="replace")


def migrate_legacy_documents_to_shared() -> int:
    """Make previously global Chroma documents explicitly visible as shared."""
    collection = get_vectorstore()._collection
    offset = 0
    migrated = 0
    while True:
        page = collection.get(include=["metadatas"], limit=500, offset=offset)
        ids = page.get("ids") or []
        if not ids:
            break
        metadatas = page.get("metadatas") or []
        update_ids, update_metadatas = [], []
        for doc_id, metadata in zip(ids, metadatas):
            if not metadata or "owner_user_id" not in metadata:
                update_ids.append(doc_id)
                update_metadatas.append({**(metadata or {}), "owner_user_id": "shared"})
        if update_ids:
            collection.update(ids=update_ids, metadatas=update_metadatas)
            migrated += len(update_ids)
        offset += len(ids)
    if migrated:
        invalidate_corpus_cache()
        logger.info("Migrated %d legacy document chunks to shared visibility", migrated)
    return migrated


def _report(
    on_progress: Callable[[str, int, int], None] | None,
    stage: str,
    current: int = 0,
    total: int = 0,
) -> None:
    """进度上报。回调出错绝不能影响入库本身 —— 那只是个显示问题。"""
    if on_progress is None:
        return
    try:
        on_progress(stage, current, total)
    except Exception:
        logger.warning("进度回调失败（已忽略，不影响入库）", exc_info=True)


def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    """取消检查点。

    取消是【协作式】的：外部线程无法安全打断一段正卡在 embedding 调用里的
    代码，因此只能在检查点之间生效（目前是每批 embedding 之前）。
    选检查点位置时的取舍：太密会拖慢入库，太疏则「点了取消没反应」。
    一批 10 行的间隔在实测里大约 0.5~1.5 秒，体感足够灵敏。
    """
    if should_cancel is not None and should_cancel():
        raise TaskCancelled("任务已被要求停止")


def validate_upload(filename: str) -> str:
    """校验上传文件名，返回小写后缀。接口在入队前调用，好让用户立刻拿到错误。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"不支持的文件类型 {suffix}，仅支持 {sorted(SUPPORTED)}")
    return suffix


def stored_path_for(doc_id: str, filename: str) -> Path:
    return Path(get_settings().upload_dir) / f"{doc_id}{validate_upload(filename)}"


def store_upload(file_bytes: bytes, filename: str, doc_id: str) -> Path:
    """把上传的原始文件落盘，返回路径。

    为什么由接口提前落盘，而不是入队时把字节带上：入库是后台任务，从入队到
    真正执行可能隔着几分钟、甚至隔着一次 worker 重启。字节流留在内存里，
    重启即丢；落成文件后任务只需携带路径，重试也天然可行。
    """
    if not file_bytes:
        raise ValueError("空文件")
    path = stored_path_for(doc_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(file_bytes)
    return path


def discard_stored_file(stored: str | Path | None) -> bool:
    """删除一个已落盘的源文件，返回是否真的删掉了。

    【为什么单独提出来（2026-10-14 端到端验收发现真实泄漏后新增）】
    这个动作原先散落在三个地方，各写各的：
      · worker._discard_source_file   —— 任务执行失败 / 被取消
      · main.upload 的入队失败分支    —— 入队失败时回滚刚落盘的文件
      · rag.ingest 的降级分支         —— 同步入库失败时删残留
    只要漏掉一条路径就会留下孤儿，而「排队中取消」恰好就是被漏掉的那条：
    任务在 queued 状态被直接写成终态，worker 永远不会领取它，于是
    worker 里那段清理代码根本没机会执行 —— 实测在 uploads/ 遗留了
    250,988 字节的文件，且因为它没有任何向量片段、不出现在文档列表里，
    用户连手动删除的入口都没有。
    统一到这里之后，「谁删文件」只有一个实现，漏一条路径不再是可能的错误。

    注意：源文件只在任务【不会再重跑】时才该删。该不该留是任务层的知识，
    所以本函数只负责删，判断由调用方做。
    """
    if not stored:
        return False
    try:
        path = Path(stored)
        if path.exists():
            path.unlink()
            logger.info("已清理源文件 %s", path.name)
            return True
    except OSError as exc:
        logger.warning("清理源文件失败 %s: %s", stored, exc)
    return False


def purge_document_chunks(doc_id: str, owner_user_id: str) -> int:
    """只清理向量片段，不碰落盘的源文件。

    与 delete_document() 的区别正在于此：重试任务前要清掉上一次崩溃留下的
    半成品片段，但源文件必须留着，否则重试无从谈起。
    （进程内的异常会由 ingest_stored_file 自己回滚；被 SIGKILL 时不会 ——
    那条路径留下的残留只能靠这里清除。）
    """
    store = get_vectorstore()
    matched = store.get(
        where={"$and": [{"doc_id": doc_id}, {"owner_user_id": owner_user_id}]},
        include=[],
    )
    ids = matched.get("ids") or []
    if ids:
        store.delete(ids=ids)
        invalidate_corpus_cache()
        logger.warning("清理了 %d 个残留片段 (doc_id=%s)", len(ids), doc_id)
    return len(ids)


def ingest(
    file_bytes: bytes,
    filename: str,
    owner_user_id: str,
    *,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    doc_id: str | None = None,
) -> dict:
    """同步入库：落盘 → 处理 → 失败清理，返回统计信息。

    这是【降级路径】。正常情况上传接口会把任务入队，由 worker 调用
    ingest_stored_file()；只有任务队列不可用（PG 连不上）时接口才退回调用
    本函数，把入库重新变成一次同步等待 —— 那比「上传直接失败」要好。
    """
    resolved_doc_id = doc_id or uuid.uuid4().hex
    stored = store_upload(file_bytes, filename, resolved_doc_id)
    try:
        return ingest_stored_file(
            stored,
            filename,
            owner_user_id,
            resolved_doc_id,
            on_progress=on_progress,
            should_cancel=should_cancel,
        )
    except Exception:
        # 同步路径下，源文件的生命周期由本函数负责：失败就删，不留孤儿。
        # 后台任务路径不同 —— 那里由 worker 决定，因为「停机重排」需要保留文件。
        discard_stored_file(stored)
        raise


def ingest_stored_file(
    stored: Path,
    filename: str,
    owner_user_id: str,
    doc_id: str,
    *,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    """对已落盘的源文件执行「解析 → 切片 → 向量化」，返回统计信息。

    不负责删除源文件：成功时它要被保留（delete_document 会连它一起删），
    失败时是否清理取决于调用方 —— 同步路径删、后台任务路径看情况留。

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
    _report(on_progress, "解析文件")

    # ---- 闸门一：同时只允许一个入库任务 ----
    gate = _ingest_gate()
    if not gate.acquire(blocking=False):
        raise ValueError(
            "已有一个文档正在入库，请等它处理完再上传"
            f"（本机并发入库名额为 {s.ingest_max_concurrent}）"
        )

    store = None
    try:
        # 在真正开始烧 embedding 配额之前先看一眼取消位：一个排队很久才被领到的
        # 任务，用户很可能早就取消了（排队期间置的取消标记同样读得到）。
        _raise_if_cancelled(should_cancel)

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

        _report(on_progress, "切片中")
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
                metadata={
                    "doc_id": doc_id,
                    "filename": filename,
                    "chunk_index": i,
                    "owner_user_id": owner_user_id,
                },
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
            # ---- 取消检查点：每批之前查一次 ----
            # 放在 add_documents 之前而不是之后：取消请求到达时，用户要的是
            # 「别再往向量库写了」，而不是「写完这批再停」。
            _raise_if_cancelled(should_cancel)
            store.add_documents(docs[start:start + batch], ids=ids[start:start + batch])
            done = min(start + batch, total)
            _report(on_progress, "向量化", done, total)
            logger.info("  embedded %d/%d chunks", done, total)

        invalidate_corpus_cache()
        _report(on_progress, "完成", total, total)
        logger.info("ingested %s -> %d chunks (doc_id=%s)", filename, total, doc_id)
        return {"doc_id": doc_id, "filename": filename, "chunks": total, "chars": len(text)}
    except Exception:
        # 失败或取消都要把已写入的部分片段清掉：半份文档留在库里会以
        # 「来源」的形式出现在回答中，比没有更糟。
        if store is not None:
            try:
                partial_ids = store.get(
                    where={
                        "$and": [
                            {"doc_id": doc_id},
                            {"owner_user_id": owner_user_id},
                        ]
                    },
                    include=[],
                )["ids"]
                if partial_ids:
                    store.delete(ids=partial_ids)
                    logger.warning(
                        "已回滚入库失败文档 %s 的 %d 个部分写入片段",
                        doc_id,
                        len(partial_ids),
                    )
            except Exception as cleanup_error:
                logger.error(
                    "回滚入库失败文档 %s 的向量片段失败: %s",
                    doc_id,
                    cleanup_error,
                )
        invalidate_corpus_cache()
        # ★ 这里【不删】源文件（2026-10-14 阶段 B 的调整）。
        #   文件的生命周期归调用方：同步路径（ingest）失败即删，避免孤儿；
        #   后台任务路径则由 worker 决定 —— 若是 worker 停机导致的中断，
        #   文件必须留着，否则重新排队的任务一启动就发现源文件没了。
        #   原实现在这里直接 unlink，是「谁写的谁清理」；改成后台任务后
        #   这个假设不再成立，因此上移一层。
        raise
    finally:
        gate.release()


def list_documents(owner_user_id: str) -> list[dict]:
    """Return shared documents and documents owned by this user."""
    data = get_vectorstore().get(
        where={
            "$or": [
                {"owner_user_id": owner_user_id},
                {"owner_user_id": "shared"},
            ]
        },
        include=["metadatas"],
    )
    seen: dict[str, dict] = {}
    for meta in data.get("metadatas") or []:
        if not meta:
            continue
        doc_id = meta.get("doc_id")
        if doc_id and doc_id not in seen:
            seen[doc_id] = {
                "doc_id": doc_id,
                "filename": meta.get("filename"),
                "shared": meta.get("owner_user_id") == "shared",
            }
    return list(seen.values())


def _discard_document_files(doc_id: str) -> int:
    """删除某个 doc_id 对应的全部落盘源文件，返回实际删除的份数。

    源文件命名规则是 {doc_id}{后缀}，后缀随上传时的文件名变化，
    所以用 glob 匹配而不是猜扩展名。统一走 discard_stored_file()，
    保证「清理源文件」全项目只有一个实现（见该函数的说明）。
    """
    removed = 0
    for path in Path(get_settings().upload_dir).glob(f"{doc_id}.*"):
        if discard_stored_file(path):
            removed += 1
    return removed


def delete_document(
    doc_id: str,
    owner_user_id: str,
    *,
    allow_shared: bool = False,
) -> int:
    """删除文档：向量片段 + 落盘的原始文件。

    为什么必须连原始文件一起删：删除向量的逻辑很早就有了，但一直没删
    uploads 下的原文件，结果是每删一次文档就留下一个永远不会再被引用的
    「孤儿」。此前排查时就在 uploads 里发现过两份历史孤儿（一份 md、一份 pdf），
    只能靠人工比对文件名才认出来。这里补上，让 uploads 始终与知识库一致。

    【2026-10-15 修复两个真实缺陷 —— 实测各踩到一次，丢了一份源文件】

    缺陷一：删除范围与文件范围不一致，造成「删了一半」的破坏性中间态。
      向量按 (doc_id, owner_user_id) 精确匹配，文件却按 doc_id 无条件 glob。
      只要归属对不上，就会出现「向量一个没删掉、源文件已经没了」的结果 ——
      实测请求删除一个 owner 为 "shared" 的文档时，接口返回 404（匹配到 0 个
      片段），但 uploads/ 下的源文件当场被删走，文档列表里还留着它的向量，
      变成既删不掉又无法重新入库的死文档。
      现在：**只有在真的删掉了这个 doc_id 的全部片段之后，才允许动文件**。

    缺陷二：shared 文档谁都删不掉。
      归属过滤是精确相等，而 "shared" 不属于任何真实用户，于是公共知识库的
      文档在 UI 上永远返回 404。现在加 allow_shared 开关，只在本机单人模式
      （AUTH_MODE=local，请求者就是这台机器的主人）下由调用方打开 ——
      那种场景里 "shared" 就是用户自己上传的资料，删不掉才是错的。
      多用户（session）模式保持原语义：shared 文档不可由普通用户删除。
    """
    store = get_vectorstore()

    # 第一步按 doc_id 取全部片段（连元数据），不预设归属。必须先分清三种「删不到」：
    #   ① 这个 doc_id 根本不存在  → 顺手清掉可能残留的孤儿文件，返回 0
    #   ② 存在，但没有一段归你    → 什么都不动（尤其不能删文件），返回 0
    #   ③ 存在且有可删的片段      → 删片段；片段清空后才删源文件
    #
    # 归属判断刻意【取回来在本地做】，而不是写进 where：
    # 需要的条件形如 doc_id == X AND (owner == me OR owner == "shared")，
    # 而 Chroma 对「$or 嵌在 $and 里」的支持随版本变化，实测写复杂布尔条件
    # 容易在不同版本上行为不一致。一个 doc 的片段数很少，本地过滤零风险。
    snapshot = store.get(where={"doc_id": doc_id}, include=["metadatas"])
    all_ids = snapshot.get("ids") or []
    if not all_ids:
        _discard_document_files(doc_id)
        return 0

    allowed_owners = {owner_user_id}
    if allow_shared:
        allowed_owners.add(SHARED_OWNER_ID)

    deletable_ids = [
        chunk_id
        for chunk_id, meta in zip(all_ids, snapshot.get("metadatas") or [])
        if (meta or {}).get("owner_user_id") in allowed_owners
    ]

    if not deletable_ids:
        # 文档存在但没有删除权：保持原样。既不泄露它的存在性，
        # 也不做任何部分删除 —— 部分删除正是缺陷一的成因。
        return 0

    store.delete(ids=deletable_ids)
    invalidate_corpus_cache()

    # 只有把该 doc_id 的片段全部删净，才删源文件。否则会留下
    # 「有向量、没源文件」的文档：它无法重新入库，也修不回来。
    if len(set(deletable_ids)) >= len(all_ids):
        _discard_document_files(doc_id)

    return len(deletable_ids)



def answer(question: str, session_id: str, owner_user_id: str) -> dict:
    """检索 + 生成，返回答案与引用来源。"""
    s = get_settings()
    prompt_date = current_date_iso()
    store = get_vectorstore()

    retrieved = retrieve(
        store,
        question,
        s.retrieval_top_k,
        s.retrieval_candidate_k,
        s.retrieval_min_relevance,
        s.retrieval_min_lexical_coverage,
        s.retrieval_max_lexical_chunks,
        owner_user_id,
    )
    hits = [(doc, score) for doc, score, _, _ in retrieved]

    if not hits:
        from .memory import get_memory

        refusal = "知识库中未找到足够相关的内容，暂时无法可靠回答。"
        get_memory().append_for_user(owner_user_id, session_id, question, refusal)
        return {
            "answer": refusal,
            "sources": [],
            "session_id": session_id,
        }

    context_parts, sources = [], []
    for idx, (doc, score, _, lexical_coverage) in enumerate(retrieved, start=1):
        context_parts.append(f"[来源{idx}] 文件：{doc.metadata.get('filename')}\n{doc.page_content}")
        sources.append({
            "index": idx,
            "filename": doc.metadata.get("filename"),
            "doc_id": doc.metadata.get("doc_id"),
            "chunk_index": doc.metadata.get("chunk_index"),
            "score": round(float(score), 4),
            "lexical_coverage": round(lexical_coverage, 4),
            "preview": doc.page_content[:160],
        })

    from .memory import get_memory

    temporal_answer = elapsed_years_answer(
        question,
        [doc.page_content for doc, _ in hits],
        date.fromisoformat(prompt_date),
    )
    if temporal_answer:
        get_memory().append_for_user(owner_user_id, session_id, question, temporal_answer)
        _enqueue_fact_extraction(owner_user_id, session_id, question, temporal_answer)
        return {"answer": temporal_answer, "sources": sources, "session_id": session_id}

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
    history = get_memory().get_history_for_user(owner_user_id, session_id)[-3:]
    history_text = "\n".join(f"用户：{h['user']}\n助手：{h['ai']}" for h in history) or "（无）"

    result = chain.invoke({
        "current_date": prompt_date,
        "context": "\n\n---\n\n".join(context_parts),
        "history": history_text,
        "user_facts": _user_facts_text(owner_user_id, question),
        "question": question,
    })
    result = _sanitize_citations(result, len(sources))

    get_memory().append_for_user(owner_user_id, session_id, question, result)
    _enqueue_fact_extraction(owner_user_id, session_id, question, result)
    return {"answer": result, "sources": sources, "session_id": session_id}


def answer_stream(question: str, session_id: str, owner_user_id: str):
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
    prompt_date = current_date_iso()
    store = get_vectorstore()
    retrieved = retrieve(
        store,
        question,
        s.retrieval_top_k,
        s.retrieval_candidate_k,
        s.retrieval_min_relevance,
        s.retrieval_min_lexical_coverage,
        s.retrieval_max_lexical_chunks,
        owner_user_id,
    )
    hits = [(doc, score) for doc, score, _, _ in retrieved]

    if not hits:
        from .memory import get_memory

        refusal = "知识库中未找到足够相关的内容，暂时无法可靠回答。"
        get_memory().append_for_user(owner_user_id, session_id, question, refusal)
        yield {"type": "sources", "sources": []}
        # 无可信候选时不调用 LLM，避免低相关片段诱发编造。
        yield {"type": "token", "text": refusal}
        return

    context_parts, sources = [], []
    for idx, (doc, score, _, lexical_coverage) in enumerate(retrieved, start=1):
        context_parts.append(
            f"[来源{idx}] 文件：{doc.metadata.get('filename')}\n{doc.page_content}"
        )
        sources.append({
            "index": idx,
            "filename": doc.metadata.get("filename"),
            "doc_id": doc.metadata.get("doc_id"),
            "chunk_index": doc.metadata.get("chunk_index"),
            "score": round(float(score), 4),
            "lexical_coverage": round(lexical_coverage, 4),
            "preview": doc.page_content[:160],
        })

    yield {"type": "sources", "sources": sources}

    from .memory import get_memory

    temporal_answer = elapsed_years_answer(
        question,
        [doc.page_content for doc, _ in hits],
        date.fromisoformat(prompt_date),
    )
    if temporal_answer:
        get_memory().append_for_user(owner_user_id, session_id, question, temporal_answer)
        _enqueue_fact_extraction(owner_user_id, session_id, question, temporal_answer)
        yield {"type": "token", "text": temporal_answer}
        return

    chain = (
        ChatPromptTemplate.from_messages([
            ("system", SYSTEM_PROMPT),
            ("human", "历史对话：\n{history}\n\n当前问题：{question}"),
        ])
        | get_chat_model()
        | StrOutputParser()
    )

    history = get_memory().get_history_for_user(owner_user_id, session_id)[-3:]
    history_text = "\n".join(f"用户：{h['user']}\n助手：{h['ai']}" for h in history) or "（无）"

    # .stream() 逐块返回，而不是 .invoke() 等全部生成完。
    # 引用清洗：模型可能吐出越界的 [来源N]，而流式一旦下发，前端就无从拦截，
    # 所以把「可能是标注开头」的尾巴（最后一个 '[' 起）暂存，等后续片段补齐后
    # 再一并清洗下发，避免把半个标注或已越界的标注发出去。
    source_count = len(sources)
    collected: list[str] = []
    pending = ""
    for chunk in chain.stream({
        "current_date": prompt_date,
        "context": "\n\n---\n\n".join(context_parts),
        "history": history_text,
        "user_facts": _user_facts_text(owner_user_id, question),
        "question": question,
    }):
        if not chunk:
            continue
        pending += chunk
        cut = pending.rfind("[")
        if cut == -1:
            emit, pending = pending, ""
        else:
            emit, pending = pending[:cut], pending[cut:]
        if emit:
            emit = _sanitize_citations(emit, source_count)
            collected.append(emit)
            yield {"type": "token", "text": emit}

    if pending:
        tail = _sanitize_citations(pending, source_count)
        collected.append(tail)
        yield {"type": "token", "text": tail}

    # 流结束后才写记忆：确保存进去的是完整答案
    get_memory().append_for_user(owner_user_id, session_id, question, "".join(collected))
    _enqueue_fact_extraction(owner_user_id, session_id, question, "".join(collected))
