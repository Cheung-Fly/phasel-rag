"""后台任务执行器（阶段 B）—— 让助手能在你关掉页面之后继续干活。

启动方式：`python -m app.worker`（见 docker-compose.yml 的 rag-worker 服务）

【四条设计决定，以及为什么】
  1. 单进程、单任务串行执行。
     理由与 rag._ingest_gate 一致：2 vCPU 上并行入库只会互相抢 CPU、
     并把云端 embedding 接口打到限流，两个任务都变慢。需要更多并发时，
     正确做法是加「名额」（把 worker 数量或并发度做成配置），不是无脑复制进程。

  2. 队列在 PostgreSQL，不在内存，也不在 Redis。
     详见 task_store.py 的模块注释：Redis 是会被淘汰的热缓存，装不下
     「任务不能丢」这条要求。

  3. 崩溃恢复靠租约 + 心跳，而不是「启动即重置 running 任务」。
     后者在误开两个 worker 时会让同一份文档被入库两次。见 task_store 注释。

  4. 三种「停止」被刻意区分开，因为它们该有的后续动作完全不同：
        · 用户取消      → 终结为 cancelled，删掉源文件（不会重跑了）
        · worker 停机   → 放回队列（release），保留源文件，重启即续跑
        · 失去租约      → 什么都不写。任务已经归别的 worker，写下去会互相覆盖
     把三者混成一个「取消」是这类系统最常见的错误来源：要么任务莫名
     消失，要么同一份文档被入库两遍。

【与 API 进程的分工】
    API 只负责「落盘 + 入队 + 回报状态」，不执行任何长任务；
    本进程只负责「领取 + 执行 + 汇报」，不提供任何 HTTP 接口。
    两个进程共用同一个镜像与同一份 .env，因此共用同一套配置口径。
"""
import logging
import os
import signal
import socket
import threading
import time
from pathlib import Path

from . import rag
from .config import get_settings
from .task_store import (
    TaskCancelled,
    close_task_store,
    get_task_store,
    new_worker_id,
)

logger = logging.getLogger(__name__)

# 容器健康检查读这个文件的时间戳（见 docker-compose.yml 的 rag-worker）。
# 与数据库里的 task_worker_heartbeats 表是两回事，各有各的读者：
#   · 文件   → 给 docker 判断容器是否要重启（不依赖 PG，PG 挂了也能工作）
#   · 数据表 → 给 /health 与前端判断「任务为什么一直排队」（需要跨容器可见）
HEARTBEAT_FILE = Path("/tmp/rag-worker-heartbeat")


class _RunContext:
    """一次执行的上下文：进度上报（带节流）+ 三种停止信号。"""

    def __init__(self, store, task_id: str, worker_id: str) -> None:
        self._store = store
        self._task_id = task_id
        self._worker_id = worker_id
        # 执行方（rag.ingest_stored_file）通过 should_cancel 观察这个事件
        self._stopped = threading.Event()
        self._aborted = False
        self._shutting_down = False
        self._last_report: tuple[str, int, int] | None = None
        # 通知心跳线程退出
        self.done = threading.Event()

    # ---- 给执行方看的两个回调
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def progress(self, stage: str, current: int, total: int) -> None:
        # 节流：同一组 (阶段, 当前, 总数) 只写一次库。
        # 不是为了省带宽（一次 UPDATE 而已），而是避免在「无变化」时也
        # 反复触碰同一行 —— 那会让任务的 updated_at 失去「最后一次进展」的含义。
        key = (stage, current, total)
        if key == self._last_report:
            return
        self._last_report = key
        try:
            self._store.update_progress(
                self._task_id, self._worker_id, stage, current, total
            )
        except Exception:
            # 进度写不进去只是显示问题，绝不能让入库因此失败
            logger.warning("进度写库失败（已忽略）", exc_info=True)

    # ---- 由心跳线程 / 信号处理函数触发的三种停止
    def request_cancel(self) -> None:
        self._stopped.set()

    def request_shutdown(self) -> None:
        self._shutting_down = True
        self._stopped.set()

    def abort(self) -> None:
        self._aborted = True
        self._stopped.set()

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def shutting_down(self) -> bool:
        return self._shutting_down

    def finish(self) -> None:
        self.done.set()


def _execute_document_ingest(task: dict, ctx: _RunContext) -> dict:
    """任务类型 document_ingest 的执行体：把落盘的源文件入库。"""
    payload = task.get("payload") or {}
    missing = [k for k in ("stored_path", "doc_id", "filename") if not payload.get(k)]
    if missing:
        raise RuntimeError(f"任务数据缺少字段 {', '.join(missing)}，无法执行")

    stored = Path(payload["stored_path"])
    doc_id = payload["doc_id"]

    if task["attempts"] > 1:
        # 上一次执行被强杀（SIGKILL）时不会走异常回滚，向量库里可能留着
        # 半成品片段。重试前先清掉，否则重试成功后同一份文档会出现重复来源。
        # （异常路径不需要这一步 —— ingest_stored_file 自己会回滚。）
        rag.purge_document_chunks(doc_id, task["owner_user_id"])

    if not stored.exists():
        raise RuntimeError("源文件已不存在，无法入库，请重新上传")

    return rag.ingest_stored_file(
        stored,
        payload["filename"],
        task["owner_user_id"],
        doc_id,
        on_progress=ctx.progress,
        should_cancel=ctx.stopped,
    )


def _execute_fact_extraction(task: dict, ctx: _RunContext) -> dict:
    """任务类型 fact_extraction 的执行体：从一轮对话抽取长期记忆（阶段 D）。

    这是「增强」而非主链路：抽取失败（LLM 挂了、embedding 挂了）只影响
    长期记忆的完整性，绝不影响用户眼前这轮已经拿到手的回答。因此这里
    任何异常都转成「抽取 0 条」的成功返回，而不是让任务进入 failed 重试
    —— 记忆抽取不值得为它重烧配额、反复打扰。
    """
    from .fact_store import get_fact_store

    payload = task.get("payload") or {}
    user_msg = payload.get("user_msg") or ""
    ai_msg = payload.get("ai_msg") or ""
    source_session_id = payload.get("session_id") or ""
    source_message_id = payload.get("message_id")
    owner_user_id = task["owner_user_id"]
    if not user_msg or not source_session_id:
        raise RuntimeError("任务数据缺少 user_msg / session_id，无法抽取")

    store = get_fact_store()
    facts = store.extract(user_msg, ai_msg)
    written = store.upsert_facts(
        owner_user_id, facts, source_session_id, source_message_id
    )
    return {"extracted": len(facts), "written": written}


# 任务类型 → 执行体。阶段 C/D 增加站点操作、记忆整理等任务时在这里登记即可。
EXECUTORS = {
    "document_ingest": _execute_document_ingest,
    "fact_extraction": _execute_fact_extraction,
}


class Worker:
    """单任务串行的后台执行器。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = get_task_store()
        self.worker_id = new_worker_id(socket.gethostname(), os.getpid())
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current_ctx: _RunContext | None = None
        self._next_sweep_at = 0.0

    # ------------------------------------------------------------ 信号
    def request_stop(self, signum=None, frame=None) -> None:
        """SIGTERM / SIGINT：尽快退出，但不丢任务。

        这里不只是置个标志就完事 —— 还得通知正在跑的任务在下一个检查点停下。
        否则一份 3 分钟的文档会把退出拖到 docker 的 10 秒宽限期之后，
        结果还是被 SIGKILL，白白退化成「靠租约过期恢复」。
        """
        logger.info("收到停止信号，准备优雅退出（进行中的任务会放回队列）")
        self._stop.set()
        with self._lock:
            ctx = self._current_ctx
        if ctx is not None:
            ctx.request_shutdown()

    # ------------------------------------------------------------ 心跳
    def _touch_heartbeat_file(self) -> None:
        try:
            HEARTBEAT_FILE.write_text(str(time.time()), encoding="ascii")
        except OSError:
            logger.warning("心跳文件写入失败 %s", HEARTBEAT_FILE, exc_info=True)

    def _heartbeat_loop(self, task: dict, ctx: _RunContext) -> None:
        lease = self.settings.task_lease_seconds
        # 上限 5 秒：取消请求的感知延迟就是这个间隔（用户点了取消，
        # 要等下一次心跳才知道）。下限 2 秒是为了别把 PG 打得太勤。
        interval = max(2.0, min(lease / 4.0, 5.0))
        task_id = task["task_id"]
        while not ctx.done.wait(interval):
            try:
                cancel_requested = self.store.heartbeat(task_id, self.worker_id, lease)
                self.store.worker_heartbeat(self.worker_id)
                self._touch_heartbeat_file()
            except Exception:
                # 心跳失败通常意味着 PG 短暂不可达。此时【不】中断任务：
                # 入库本身不依赖 PG（向量写进 Chroma），让它跑完比中途放弃划算。
                # 代价是租约可能被别人回收 —— 那种情况下本进程后续所有写入
                # 都会因 locked_by 不匹配而失败，因此不会污染状态。
                logger.warning("心跳失败（任务继续，但租约可能过期）", exc_info=True)
                continue
            if cancel_requested is None:
                logger.error("任务 %s 的租约已失效（可能已被回收），通知执行方停止", task_id)
                ctx.abort()
                return
            if cancel_requested:
                ctx.request_cancel()

    # ------------------------------------------------------------ 执行
    @staticmethod
    def _discard_source_file(task: dict) -> None:
        """任务到达终态、不会再重跑时，删掉落盘的源文件。

        为什么判断在任务层、删除在 rag 层：文件该不该留，取决于
        「这个任务还会不会重跑」，而那是任务层的知识（见 rag.py 的对应注释）。
        取消与失败都是终态 → 删；停机重排 → 留。
        实际的文件操作统一走 rag.discard_stored_file —— 排队中取消那条路径
        不经过 worker，必须和这里共用同一份实现，否则迟早漏一条（实测漏过）。
        """
        rag.discard_stored_file((task.get("payload") or {}).get("stored_path"))

    def _execute(self, task: dict) -> None:
        task_id = task["task_id"]
        ctx = _RunContext(self.store, task_id, self.worker_id)
        with self._lock:
            self._current_ctx = ctx
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(task, ctx), name="task-heartbeat", daemon=True
        )
        heartbeat.start()
        logger.info(
            "开始执行任务 %s（类型 %s，第 %d 次尝试）",
            task_id,
            task["task_type"],
            task["attempts"],
        )
        try:
            executor = EXECUTORS.get(task["task_type"])
            if executor is None:
                raise RuntimeError(f"未知任务类型 {task['task_type']}")
            result = executor(task, ctx)
        except TaskCancelled:
            if ctx.aborted:
                # 租约已被别人拿走（本进程可能刚从卡死中恢复）。此时写任何终态
                # 都会覆盖真正持有者的结果，所以只记录、不落库。
                logger.warning("任务 %s 的租约已不属于本进程，停止执行且不写终态", task_id)
            elif ctx.shutting_down:
                if self.store.release(task_id, self.worker_id, "worker 重启，等待重新执行"):
                    logger.info("任务 %s 已放回队列（worker 停机，源文件保留）", task_id)
            else:
                if self.store.mark_cancelled(task_id, self.worker_id):
                    logger.info("任务 %s 已按用户请求取消", task_id)
                self._discard_source_file(task)
        except Exception as exc:
            logger.exception("任务 %s 执行失败", task_id)
            if not ctx.aborted:
                self.store.fail(task_id, self.worker_id, f"{type(exc).__name__}: {exc}")
                self._discard_source_file(task)
        else:
            if ctx.aborted:
                logger.warning("任务 %s 已完成但租约已被回收，不写终态", task_id)
            elif self.store.complete(task_id, self.worker_id, result):
                logger.info("任务 %s 完成：%s", task_id, result)
            else:
                logger.warning("任务 %s 完成但终态写入未命中（租约已变更）", task_id)
        finally:
            ctx.finish()
            heartbeat.join(timeout=2)
            with self._lock:
                self._current_ctx = None

    # ------------------------------------------------------------ 主循环
    def run_once(self) -> bool:
        """处理一个任务；没有可领取的任务时返回 False。测试直接驱动这个函数。"""
        now = time.monotonic()
        if now >= self._next_sweep_at:
            # 把「每次一启动就崩」的任务判死，避免形成 worker 反复重启的死循环。
            # 每 30 秒扫一次足够 —— 这是异常路径，不需要实时。
            # 这条路径同样绕过了 _execute，所以源文件得在这里收拾：任务已判死，
            # 不可能再重跑，留着就是孤儿。
            for dead in self.store.fail_exhausted(self.settings.task_max_attempts):
                self._discard_source_file(dead)
            self._next_sweep_at = now + 30.0

        task = self.store.claim_next(
            self.worker_id,
            self.settings.task_lease_seconds,
            self.settings.task_max_attempts,
        )
        if task is None:
            return False
        self._execute(task)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.store.worker_heartbeat(self.worker_id)
        self._touch_heartbeat_file()
        logger.info(
            "后台任务执行器已启动 id=%s 轮询=%.1fs 租约=%ds 最大尝试=%d",
            self.worker_id,
            self.settings.task_worker_poll_seconds,
            self.settings.task_lease_seconds,
            self.settings.task_max_attempts,
        )
        while not self._stop.is_set():
            try:
                if self.run_once():
                    # 还有活要干：立刻继续，不睡
                    continue
            except Exception:
                logger.exception("worker 主循环异常，5 秒后重试")
                self._stop.wait(5)
                continue
            # 空闲：报一次心跳再睡。这一步同时是「worker 还活着」的证据来源。
            try:
                self.store.worker_heartbeat(self.worker_id)
            except Exception:
                logger.warning("空闲心跳失败", exc_info=True)
            self._touch_heartbeat_file()
            self._stop.wait(self.settings.task_worker_poll_seconds)
        logger.info("后台任务执行器已退出")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        worker = Worker()
    except Exception:
        # 队列不可用就等于这个进程没有任何意义：让它退出（restart 策略会重试），
        # 而不是留一个「活着但什么也不做」的容器骗过健康检查。
        logger.exception("后台任务执行器启动失败（任务队列不可用）")
        raise SystemExit(1)
    try:
        worker.run_forever()
    finally:
        close_task_store()


if __name__ == "__main__":
    main()
