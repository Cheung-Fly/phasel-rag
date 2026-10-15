"""后台任务队列（阶段 B）：PostgreSQL 作为唯一真相源。

链路：接口入队 → worker 轮询领取 → 执行并汇报进度 → 终结（成功/失败/取消）

【为什么队列建在 PostgreSQL，而不是 Redis】
    Redis 在本项目里的定位是「热缓存」：配置是 allkeys-lru + 384MB 上限，
    键会被主动淘汰。把任务放进一个会主动丢数据的存储里，等于接受
    「任务凭空消失」。而阶段 B 的验收口径明确要求
    「worker 被杀掉重启后任务不丢」—— 所以队列必须建在真相源上。
    代价是 worker 需要轮询（默认 2 秒一次 SELECT），在 2 vCPU 上可以忽略；
    换来的是一条硬保证：任务不会静默消失。

【为什么不引入 Celery / RQ / RabbitMQ】
    本机是 2 vCPU、单 worker、每个任务 1~3 分钟。为这种负载引入 broker
    容器与一整套新的运维面，换到的并发能力恰好是这台机器用不上的
    （见 rag._ingest_gate 的说明：并行只会互相抢 CPU 与云端接口配额）。
    需要更多并发时，正确做法是加「名额」，不是加中间件。

【崩溃恢复：租约 + 心跳，而不是「启动即重置」】
    一个看起来更简单、实际有害的做法是：worker 启动时把所有 running
    任务重置为 queued。若误开了两个 worker，后启动的那个会把前一个
    正在跑的任务抢走 —— 同一份文档被入库两次，而重复的向量片段会
    污染检索结果且很难被发现。
    这里改用租约（lease）：领取任务时写入 locked_by 与 lease_expires_at，
    执行期间由心跳线程续租。进程被 SIGKILL 后租约自然过期，任务被重新领取；
    优雅退出（SIGTERM）则由 worker 主动 release 回队列，重启即刻恢复、不必等租约。

【取消是协作式的，不是抢占式的】
    运行中的任务无法被外部线程安全地打断（它正卡在某次 embedding 调用里）。
    因此 cancel 只置 cancel_requested，由 worker 在「检查点」（每批 embedding
    之间）自行停止。若取消到达时任务其实已经跑完，则结果被保留并记为成功 ——
    已经烧掉的算力与已写入的向量不会被回滚，因为回滚本身也要花代价、
    且用户真正想要的是「别再继续」，而不是「把已完成的工作销毁」。
"""
import logging
import threading
import uuid
from datetime import datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger(__name__)

# 任务状态取值。终态（succeeded / failed / cancelled）一经写入不再变更。
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

# 错误信息落库前的截断长度：任务失败原因给用户看，不需要完整堆栈
_MAX_ERROR_CHARS = 2000


class TaskCancelled(Exception):
    """任务被要求停止（用户取消 / worker 停机 / 失去租约）。

    为什么这个异常定义在任务域而不是 rag 里：它是「执行方」与「取消方」之间
    的契约。rag 只是按调用方给的 should_cancel 回调行事，语义的所有权
    属于任务域。全项目只保留这一个取消异常，避免每加一种任务就多一个类型。
    """


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS agent_tasks (
        task_id          TEXT PRIMARY KEY,
        task_type        TEXT NOT NULL,
        owner_user_id    TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        status           TEXT NOT NULL DEFAULT 'queued',
        stage            TEXT NOT NULL DEFAULT '',
        progress_current INTEGER NOT NULL DEFAULT 0,
        progress_total   INTEGER NOT NULL DEFAULT 0,
        payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
        result           JSONB,
        error            TEXT NOT NULL DEFAULT '',
        attempts         INTEGER NOT NULL DEFAULT 0,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        locked_by        TEXT NOT NULL DEFAULT '',
        lease_expires_at TIMESTAMPTZ,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at       TIMESTAMPTZ,
        finished_at      TIMESTAMPTZ,
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_agent_tasks_status
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
    )
    """,
    # 面板按「最近创建」倒序列出某人的任务
    """
    CREATE INDEX IF NOT EXISTS idx_agent_tasks_owner_created
    ON agent_tasks(owner_user_id, created_at DESC)
    """,
    # 领取查询的过滤条件是 (status, lease_expires_at)：status='queued' 的分支
    # 与 status='running' 且租约过期的分支都要能走索引。
    """
    CREATE INDEX IF NOT EXISTS idx_agent_tasks_claimable
    ON agent_tasks(status, lease_expires_at)
    """,
    # worker 心跳：既用于 /health 报告「后台执行器是否在跑」，也是排查
    # 「任务一直排队不动」的第一手证据（任务全在排队 = worker 没起来）。
    """
    CREATE TABLE IF NOT EXISTS task_worker_heartbeats (
        worker_id    TEXT PRIMARY KEY,
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


def _serialize(row: dict) -> dict:
    """把数据库行转成可直接 JSON 化的字典（datetime → ISO 字符串）。"""
    return {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in row.items()
    }


class TaskStore:
    """任务队列的读写入口。所有方法都是同步的（worker 与 FastAPI 线程池均适用）。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip():
            raise RuntimeError("POSTGRES_DSN is required for the task queue")
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 8)),
            open=True,
            timeout=5,
            # ★ row_factory 必须建在【连接】上，而不是在每次查询前临时设置。
            #   连接池会把连接还回来复用，而 row_factory 是连接自身的属性 ——
            #   临时设置等于把「这一行怎么取值」泄漏给下一个借到它的调用者。
            #   真实事故（本次）：enqueue 里设过 dict_row 之后，没设过的
            #   heartbeat 拿到的是字典，`row[0]` 直接 KeyError: 0
            #   （试图用整数 0 去索引字典）。统一在池这一层定死，全部返回 dict。
            kwargs={"row_factory": dict_row},
        )
        try:
            with self._pool.connection() as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
        except Exception:
            self._pool.close()
            raise
        logger.info("后台任务队列表结构就绪")

    def close(self) -> None:
        self._pool.close()

    # ------------------------------------------------------------ 入队 / 查询
    def enqueue(
        self,
        task_type: str,
        owner_user_id: str,
        payload: dict,
        task_id: str | None = None,
    ) -> dict:
        resolved_id = task_id or uuid.uuid4().hex
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO agent_tasks(task_id, task_type, owner_user_id, payload)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (resolved_id, task_type, owner_user_id, Jsonb(payload)),
            ).fetchone()
        logger.info("已入队任务 %s（类型 %s）", resolved_id, task_type)
        return _serialize(row)

    def get(self, task_id: str, owner_user_id: str) -> dict | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = %s AND owner_user_id = %s",
                (task_id, owner_user_id),
            ).fetchone()
        return _serialize(row) if row else None

    def list_for_owner(self, owner_user_id: str, limit: int = 20) -> list[dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_tasks
                WHERE owner_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (owner_user_id, limit),
            ).fetchall()
        return [_serialize(row) for row in rows]

    # ------------------------------------------------------------ 取消
    def cancel(self, task_id: str, owner_user_id: str) -> dict | None:
        """请求取消。排队中直接终结；运行中只置标记，由 worker 在检查点停下。

        已经是终态的任务返回原样（幂等），不报错 —— 用户点两次「取消」
        不该得到第二种结果。
        """
        current = self.get(task_id, owner_user_id)
        if current is None:
            return None
        if current["status"] not in ACTIVE_STATUSES:
            return current
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE agent_tasks
                   SET cancel_requested = TRUE,
                       status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END,
                       stage = CASE WHEN status = 'queued' THEN '已取消' ELSE stage END,
                       finished_at = CASE WHEN status = 'queued' THEN now() ELSE finished_at END,
                       updated_at = now()
                 WHERE task_id = %s AND owner_user_id = %s
                 RETURNING *
                """,
                (task_id, owner_user_id),
            ).fetchone()
        # 注意：SET 右侧引用的 status 是【更新前】的值（PostgreSQL 语义），
        # 所以两个 CASE 判断的都是旧状态 —— 正是这里需要的语义。
        return _serialize(row) if row else None

    # ------------------------------------------------------------ 领取 / 心跳
    def claim_next(
        self, worker_id: str, lease_seconds: int, max_attempts: int
    ) -> dict | None:
        """领取下一个可执行任务：排队中的，或租约已过期的（上一次执行者已消失）。"""
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE agent_tasks
                   SET status = 'running',
                       locked_by = %s,
                       attempts = attempts + 1,
                       stage = '准备中',
                       progress_current = 0,
                       progress_total = 0,
                       started_at = COALESCE(started_at, now()),
                       lease_expires_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE task_id = (
                       SELECT task_id FROM agent_tasks
                        WHERE (status = 'queued'
                               OR (status = 'running'
                                   AND (lease_expires_at IS NULL OR lease_expires_at < now())))
                          AND attempts < %s
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1)
                 RETURNING *
                """,
                (worker_id, lease_seconds, max_attempts),
            ).fetchone()
        return _serialize(row) if row else None

    def heartbeat(self, task_id: str, worker_id: str, lease_seconds: int) -> bool | None:
        """续租并顺带取回取消请求。

        返回 True/False 表示「我仍持有这个任务，取消请求为 是/否」；
        返回 None 表示租约已经不在自己手上（被回收给了别的 worker）——
        调用方必须立刻停止执行，且【不要】写任何终态：那个任务已经不归它了，
        写下去会覆盖真正持有者的结果。
        """
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE agent_tasks
                   SET lease_expires_at = now() + make_interval(secs => %s),
                       updated_at = now()
                 WHERE task_id = %s AND locked_by = %s AND status = 'running'
                 RETURNING cancel_requested
                """,
                (lease_seconds, task_id, worker_id),
            ).fetchone()
        return None if row is None else bool(row["cancel_requested"])

    def update_progress(
        self, task_id: str, worker_id: str, stage: str, current: int, total: int
    ) -> None:
        """汇报进度。带 locked_by 条件：失去租约的 worker 不该再往这一行写东西。"""
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE agent_tasks
                   SET stage = %s, progress_current = %s, progress_total = %s,
                       updated_at = now()
                 WHERE task_id = %s AND locked_by = %s AND status = 'running'
                """,
                (stage, max(0, current), max(0, total), task_id, worker_id),
            )

    # ------------------------------------------------------------ 终结
    def complete(self, task_id: str, worker_id: str, result: dict) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_tasks
                   SET status = 'succeeded',
                       stage = '完成',
                       result = %s,
                       error = '',
                       progress_current = CASE WHEN progress_total > 0
                                               THEN progress_total ELSE progress_current END,
                       finished_at = now(),
                       lease_expires_at = NULL,
                       updated_at = now()
                 WHERE task_id = %s AND locked_by = %s AND status = 'running'
                """,
                (Jsonb(result or {}), task_id, worker_id),
            )
            return cursor.rowcount > 0

    def fail(self, task_id: str, worker_id: str, error: str) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_tasks
                   SET status = 'failed',
                       stage = '失败',
                       error = %s,
                       finished_at = now(),
                       lease_expires_at = NULL,
                       updated_at = now()
                 WHERE task_id = %s AND locked_by = %s AND status = 'running'
                """,
                ((error or "")[:_MAX_ERROR_CHARS], task_id, worker_id),
            )
            return cursor.rowcount > 0

    def mark_cancelled(self, task_id: str, worker_id: str) -> bool:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_tasks
                   SET status = 'cancelled',
                       stage = '已取消',
                       error = '',
                       finished_at = now(),
                       lease_expires_at = NULL,
                       updated_at = now()
                 WHERE task_id = %s AND locked_by = %s AND status = 'running'
                """,
                (task_id, worker_id),
            )
            return cursor.rowcount > 0

    def release(self, task_id: str, worker_id: str, reason: str = "等待重试") -> bool:
        """把任务放回队列（worker 优雅停机时用），并回退本次 attempt。

        为什么 attempts 要减一：优雅停机不是任务的错。不减的话，反复
        restart 会把重试预算耗光，任务会以「反复被中断」为由失败 ——
        那是把运维操作的代价记在任务头上。
        """
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_tasks
                   SET status = 'queued',
                       locked_by = '',
                       lease_expires_at = NULL,
                       attempts = GREATEST(attempts - 1, 0),
                       stage = %s,
                       progress_current = 0,
                       progress_total = 0,
                       updated_at = now()
                 WHERE task_id = %s AND locked_by = %s AND status = 'running'
                """,
                (reason, task_id, worker_id),
            )
            return cursor.rowcount > 0

    def fail_exhausted(self, max_attempts: int) -> list[dict]:
        """把反复被中断（worker 反复崩溃）且租约已过期的任务判为失败。

        没有这一步，一个每次都在同一位置打崩 worker 的任务会被无限重领，
        表现是「worker 一启动就死」的循环。

        返回被终结的任务（而不是个数），因为调用方还要清理它们的源文件 ——
        这条路径同样绕过了 worker 的执行流程，不处理就是又一处孤儿
        （与其让每个调用方各写一份，不如把「谁被终结了」如实交出去）。
        """
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                UPDATE agent_tasks
                   SET status = 'failed',
                       stage = '失败',
                       error = '任务在多次重试中被反复中断（执行进程多次退出），已停止重试',
                       finished_at = now(),
                       lease_expires_at = NULL,
                       updated_at = now()
                 WHERE status = 'running'
                   AND attempts >= %s
                   AND (lease_expires_at IS NULL OR lease_expires_at < now())
                 RETURNING *
                """,
                (max_attempts,),
            ).fetchall()
        killed = [_serialize(row) for row in rows]
        if killed:
            logger.warning("已将 %d 个反复中断的任务判为失败", len(killed))
        return killed

    # ------------------------------------------------------------ worker 存活
    def worker_heartbeat(self, worker_id: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO task_worker_heartbeats(worker_id, last_seen_at)
                VALUES (%s, now())
                ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = now()
                """,
                (worker_id,),
            )

    def active_workers(self, within_seconds: int) -> list[dict]:
        """返回最近仍在心跳的 worker。

        判活窗口直接复用租约时长（默认 60s）：心跳间隔是租约的 1/4，
        所以活着的 worker 必然落在窗口内，而死掉的会同期被两个机制
        （心跳报告 / 租约回收）一起识别出来，不用维护两套阈值。
        """
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT worker_id,
                       last_seen_at,
                       EXTRACT(EPOCH FROM (now() - last_seen_at))::int AS seconds_ago
                  FROM task_worker_heartbeats
                 WHERE last_seen_at > now() - make_interval(secs => %s)
                 ORDER BY last_seen_at DESC
                """,
                (within_seconds,),
            ).fetchall()
        return [_serialize(row) for row in rows]


_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_task_store() -> TaskStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = TaskStore()
    return _store


def get_task_store_or_none() -> TaskStore | None:
    """容错版：队列不可用时返回 None，让调用方退回同步路径。

    上传接口用它做降级判断 —— 队列是「更好的方式」，不是「唯一的方式」，
    不该因为 PG 抖动就让入库能力整个消失。
    """
    try:
        return get_task_store()
    except Exception as exc:
        logger.warning("任务队列不可用: %s", type(exc).__name__)
        return None


def close_task_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None


def new_worker_id(hostname: str, pid: int) -> str:
    """worker 身份：主机名 + 进程号 + 随机后缀。

    随机后缀不可省：同一个容器里重启进程时 pid 可能复用（更常见的是
    容器重建后主机名与 pid 都相同），复用身份会让「旧进程的租约」
    看起来像是自己的，崩溃恢复判断随即失效。
    """
    return f"{hostname}-{pid}-{uuid.uuid4().hex[:6]}"
