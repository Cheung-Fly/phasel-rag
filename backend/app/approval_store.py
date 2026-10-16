"""操作审批闸门（阶段 C ②）：任何会改变外部状态的动作，先落审批再执行。

【要解决什么】
改造前，页面控件的「确认」是一段进程内内存态（browser 容器
`_sessions[owner]["pending_click"]`，TTL 90 秒），用户在聊天里手打
「确认点击：X」就真的点了。它有三个问题：
  1. 内存态 ⇒ 容器重启即丢，且**没有任何地方能回答「助手批准过什么」**
  2. 没有超时记录 ⇒ 「超时自动放弃」发生了但看不出来
  3. 没有一次性消费保证 ⇒ 同一句话重发两次就可能点两次

路线图阶段 C 的验收口径是：**一次需审批的操作在无批准时不产生任何外部副作用**。
要满足它，执行入口必须唯一且可验证 —— 所以真相源搬到 PostgreSQL，
由 api 侧把关，browser 侧的 `pending_click` 降级为第二道防线（纵深防御）。

【状态机】
    pending ──approve──▶ approved ──begin_execution──▶ executing
       │                    │                              │
       │                    │                    ┌─────────┴─────────┐
       │                    │                 成功                 失败/不确定
       │                    │                    │                   │
       │                    │                 executed        expired / unknown
       │                    │
       ├──reject──▶ rejected│
       └──超时────▶ expired ◀┘（approved 也可能在超时后才被点执行）

终态（rejected / expired / executed / unknown）一经写入不再变更。

【为什么用「原子 UPDATE + 影响行数」而不是「先查后改」】
approve 与 begin_execution 都是「只能成功一次」的动作。先 SELECT 判断
再 UPDATE 会留下竞态窗口：双击按钮 / 用户重发请求就会消费两次。
这里全部写成 `UPDATE ... WHERE status = '期望值' RETURNING *`，
拿不到行即视为冲突，由调用方回 409。

【为什么 executing 卡住不自动重放】
execute 是三步：PG 消费 → 调 browser 真点击 → 回写终态。中间崩溃会停在
`executing`。此时**不能**自动重放 —— 重放等于重复点击，而网页点击的副作用
（选课、提交、缴费）不可逆。这与阶段 B「普通异常即终态、不自动重试」
是同一种取舍：宁可要一次人工介入，也不要静默做两次。
卡住超过 `STUCK_EXECUTION_SECONDS` 的会被标记为 `unknown`，
在时间线上以「结果未知」呈现，提示用户自行到网站上核对。
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

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_EXECUTING = "executing"
STATUS_EXECUTED = "executed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"

# 与 browser 侧 PENDING_CLICK_TTL_SECONDS 保持一致：两处 TTL 不一致会出现
# 「界面显示还能点，实际浏览器已判超时」这种自相矛盾的表现。
APPROVAL_TTL_SECONDS = 90

# 停在 executing 超过这个时长即视为「结果未知」（见模块头说明）。
STUCK_EXECUTION_SECONDS = 300

MAX_TARGET_CHARS = 200
MAX_PENDING_PER_OWNER = 20

# 审计事件的动作名。刻意不加 CHECK 约束 —— 与 credential_audit 不同，
# 这里的取值注定会随功能演进增加，而 CHECK 约束一旦落地就得靠迁移维护
# （第十章记录过漏迁移导致接口 503 的真实事故）。status 那类闭环取值才值得 CHECK。
ACTION_REQUESTED = "approval_requested"
ACTION_GRANTED = "approval_granted"
ACTION_REJECTED = "approval_rejected"
ACTION_EXPIRED = "approval_expired"
ACTION_EXECUTED = "approval_executed"
ACTION_EXECUTION_FAILED = "approval_execution_failed"
ACTION_EXECUTION_UNKNOWN = "approval_execution_unknown"


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS operation_approvals (
        approval_id   TEXT PRIMARY KEY,
        owner_user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        site_key      TEXT NOT NULL DEFAULT '',
        session_id    TEXT NOT NULL DEFAULT '',
        action        TEXT NOT NULL DEFAULT 'click_element',
        target        TEXT NOT NULL,
        fingerprint   TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'pending',
        detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at    TIMESTAMPTZ NOT NULL,
        approved_at   TIMESTAMPTZ,
        executed_at   TIMESTAMPTZ,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_operation_approvals_status
            CHECK (status IN ('pending', 'approved', 'rejected', 'expired',
                              'executing', 'executed', 'failed', 'unknown'))
    )
    """,
    # 面板按「最近创建」倒序列出某人的待审批项
    """
    CREATE INDEX IF NOT EXISTS idx_operation_approvals_owner_created
    ON operation_approvals(owner_user_id, created_at DESC)
    """,
    # 惰性过期与「卡在 executing」的扫描都按 (status, expires_at/updated_at) 过滤
    """
    CREATE INDEX IF NOT EXISTS idx_operation_approvals_status_expiry
    ON operation_approvals(status, expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS operation_audit (
        audit_id      BIGSERIAL PRIMARY KEY,
        owner_user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        kind          TEXT NOT NULL,
        action        TEXT NOT NULL,
        detail        JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_operation_audit_owner_created
    ON operation_audit(owner_user_id, created_at DESC)
    """,
)


def _serialize(row: dict) -> dict:
    return {
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in row.items()
    }


class ApprovalStore:
    """审批状态机 + 操作审计的读写入口。所有方法同步。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip():
            raise RuntimeError("POSTGRES_DSN is required for the approval gate")
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 8)),
            open=True,
            timeout=5,
            # 与 TaskStore/SiteStore 同因：row_factory 是连接属性，建在池这一层。
            kwargs={"row_factory": dict_row},
        )
        try:
            with self._pool.connection() as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
        except Exception:
            self._pool.close()
            raise
        logger.info("操作审批闸门表结构就绪")

    def close(self) -> None:
        self._pool.close()

    # ------------------------------------------------------------ 审计
    def record_event(
        self,
        owner_user_id: str,
        *,
        kind: str,
        action: str,
        detail: dict | None = None,
    ) -> None:
        """写一条操作审计。

        ★ 刻意不抛出异常：审计是旁路，它写失败不该让「批准/执行」这类主流程
        变成 500。但必须留下 error 日志 —— 静默失败的审计等于没有审计。
        """
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO operation_audit(owner_user_id, kind, action, detail)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (owner_user_id, kind, action, Jsonb(detail or {})),
                )
        except Exception as exc:
            logger.error("操作审计写入失败: %s", type(exc).__name__)

    # ------------------------------------------------------------ 创建 / 查询
    def create(
        self,
        owner_user_id: str,
        *,
        target: str,
        site_key: str = "",
        session_id: str = "",
        action: str = "click_element",
        fingerprint: str = "",
        detail: dict | None = None,
        ttl_seconds: int = APPROVAL_TTL_SECONDS,
    ) -> dict:
        text = " ".join(str(target or "").split())
        if not text:
            raise ValueError("target 不能为空")
        if len(text) > MAX_TARGET_CHARS:
            raise ValueError(f"target 不能超过 {MAX_TARGET_CHARS} 字符")
        ttl = max(10, min(int(ttl_seconds), 600))
        approval_id = uuid.uuid4().hex
        with self._pool.connection() as connection:
            pending = connection.execute(
                """
                SELECT count(*) AS n FROM operation_approvals
                WHERE owner_user_id = %s AND status IN ('pending', 'approved')
                """,
                (owner_user_id,),
            ).fetchone()["n"]
            if pending >= MAX_PENDING_PER_OWNER:
                raise ValueError(f"待审批的操作过多（{MAX_PENDING_PER_OWNER} 条），请先处理")
            row = connection.execute(
                """
                INSERT INTO operation_approvals(
                    approval_id, owner_user_id, site_key, session_id, action,
                    target, fingerprint, detail, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + (%s || ' seconds')::interval)
                RETURNING *
                """,
                (
                    approval_id,
                    owner_user_id,
                    site_key,
                    session_id,
                    action,
                    text,
                    fingerprint,
                    Jsonb(detail or {}),
                    str(ttl),
                ),
            ).fetchone()
        self.record_event(
            owner_user_id,
            kind="approval",
            action=ACTION_REQUESTED,
            detail={"approval_id": approval_id, "target": text, "site_key": site_key},
        )
        logger.info("已登记待审批操作 %s（owner=%s）", approval_id, owner_user_id)
        return _serialize(row)

    def get(self, owner_user_id: str, approval_id: str) -> dict | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM operation_approvals
                WHERE approval_id = %s AND owner_user_id = %s
                """,
                (approval_id, owner_user_id),
            ).fetchone()
        return _serialize(row) if row else None

    def list_for_owner(self, owner_user_id: str, limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        # 先把到期的置成终态再列 —— 否则界面会显示一堆「已过期但仍是 pending」
        # 的条目，用户点了才发现是 409。
        self.expire_stale(owner_user_id)
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM operation_approvals
                WHERE owner_user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (owner_user_id, limit),
            ).fetchall()
        return [_serialize(row) for row in rows]

    # ------------------------------------------------------------ 状态迁移
    def approve(self, owner_user_id: str, approval_id: str) -> dict | None:
        """pending → approved。已有终态或已过期时返回 None（调用方回 409）。

        ★ `require_unexpired=True` 是这里的要害，不能省。少了它，一条早就过期的
        pending 记录仍能被打成 approved —— 界面上那张卡片上的按钮从此「永久有效」，
        而卡片描述的是**几十秒前的那个页面**。批准的本质是「我确认过眼前这个东西」，
        过期就意味着眼前的东西已经不是被确认的那个了：控件索引可能已经被新内容顶掉，
        点下去就是点错地方。所以批准的时效和执行的时效必须共用同一条时间线。
        """
        return self._transition(
            owner_user_id,
            approval_id,
            from_statuses=(STATUS_PENDING,),
            to_status=STATUS_APPROVED,
            extra_sql=", approved_at = now()",
            audit_action=ACTION_GRANTED,
            audit_key="approval_id",
            require_unexpired=True,
        )

    def reject(self, owner_user_id: str, approval_id: str) -> dict | None:
        """pending → rejected。同样要求未过期，理由和 approve 不同：

        拒绝本身没有任何外部副作用，放行也不会做错事。要紧的是**状态机的唯一性** ——
        `expired` 这个终态只允许由 `expire_stale()` 落库。若这里放行，同一条「其实已过期」
        的记录会因「用户有没有先打开过列表页」而变成 rejected（用户点过）或 expired（被惰性
        清扫过），审计上就出现了两种说法，其中 rejected 那种是假的：没有人拒绝过它，它只是超时了。
        """
        return self._transition(
            owner_user_id,
            approval_id,
            from_statuses=(STATUS_PENDING,),
            to_status=STATUS_REJECTED,
            extra_sql="",
            audit_action=ACTION_REJECTED,
            audit_key="approval_id",
            require_unexpired=True,
        )

    def begin_execution(self, owner_user_id: str, approval_id: str) -> dict | None:
        """approved → executing。**这是唯一的执行租约**：原子消费，重放必失败。

        同时要求 `expires_at > now()`：批准之后如果拖到超时才点执行，
        等同于没批准 —— 页面早就变了，那个控件索引指向的东西可能已经不是原来那个。
        """
        return self._transition(
            owner_user_id,
            approval_id,
            from_statuses=(STATUS_APPROVED,),
            to_status=STATUS_EXECUTING,
            extra_sql="",
            audit_action=None,
            require_unexpired=True,
        )

    def complete_execution(
        self,
        owner_user_id: str,
        approval_id: str,
        *,
        outcome: str,
        detail: dict | None = None,
        merge_detail: dict | None = None,
    ) -> dict | None:
        """executing → 终态。

        outcome 三选一，且**只有 browser 明确回报 clicked 时才能传 'executed'**：
          executed —— 网页确实被点了
          expired  —— 浏览器侧已无待执行项 / 已超时（控件没了，什么都没发生）
          failed   —— 控件变了或点击本身失败（发生了某种变化，但不确定是不是我们要的）
        api 侧的状态自认不算数：把「我们以为点了」写成「点了」是这里最危险的错误。

        detail 仍是审计摘要（写 operation_audit）；merge_detail 才是「写回审批记录
        本身 detail 列」的执行结果（如终端 stdout、分析图表文件名列表），供前端刷新
        后读回。两者分开，避免把大段 stdout 也塞进审计流水里。
        """
        if outcome not in (STATUS_EXECUTED, STATUS_EXPIRED, STATUS_FAILED):
            raise ValueError(f"不支持的 outcome: {outcome}")
        return self._transition(
            owner_user_id,
            approval_id,
            from_statuses=(STATUS_EXECUTING,),
            to_status=outcome,
            extra_sql=", executed_at = now()" if outcome == STATUS_EXECUTED else "",
            audit_action=(
                ACTION_EXECUTED if outcome == STATUS_EXECUTED else ACTION_EXECUTION_FAILED
            ),
            audit_detail=detail,
            merge_detail=merge_detail,
        )

    def _transition(
        self,
        owner_user_id: str,
        approval_id: str,
        *,
        from_statuses: tuple[str, ...],
        to_status: str,
        extra_sql: str,
        audit_action: str | None,
        audit_detail: dict | None = None,
        audit_key: str | None = None,
        require_unexpired: bool = False,
        merge_detail: dict | None = None,
    ) -> dict | None:
        expiry_clause = " AND expires_at > now()" if require_unexpired else ""
        placeholders = ", ".join(["%s"] * len(from_statuses))
        # merge_detail：把执行结果（stdout/artifacts 等）合并进 operation_approvals.detail 列，
        # 让「执行完的结果」持久化到审批记录本身，前端刷新后仍能读回。
        # 用 `detail = detail || %s::jsonb`（右值覆盖同名键），不传时保持原样不动。
        if merge_detail:
            extra_sql += ", detail = detail || %s::jsonb"
        with self._pool.connection() as connection:
            params: list = [to_status]
            if merge_detail:
                params.append(Jsonb(merge_detail))
            params += [approval_id, owner_user_id, *from_statuses]
            row = connection.execute(
                f"""
                UPDATE operation_approvals
                SET status = %s, updated_at = now(){extra_sql}
                WHERE approval_id = %s
                  AND owner_user_id = %s
                  AND status IN ({placeholders}){expiry_clause}
                RETURNING *
                """,
                tuple(params),
            ).fetchone()
        if row is None:
            return None
        if audit_action:
            detail = dict(audit_detail or {})
            detail.setdefault(audit_key or "approval_id", approval_id)
            detail.setdefault("status", to_status)
            self.record_event(
                owner_user_id, kind="approval", action=audit_action, detail=detail
            )
        return _serialize(row)

    # ------------------------------------------------------------ 惰性维护
    def expire_stale(self, owner_user_id: str | None = None) -> int:
        """把到期的 pending / approved 置为 expired。返回受影响行数。

        为什么不做成后台定时任务：本项目是单进程 + 2 vCPU，为一件「列表一读
        就能顺带算出」的事常驻一个线程不划算。惰性淘汰的代价只是
        「没人打开界面时，过期状态晚一点落库」—— 而那种情况下也没人看它。
        """
        with self._pool.connection() as connection:
            if owner_user_id is None:
                rows = connection.execute(
                    """
                    UPDATE operation_approvals
                    SET status = 'expired', updated_at = now()
                    WHERE status IN ('pending', 'approved') AND expires_at <= now()
                    RETURNING approval_id, owner_user_id, target
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    UPDATE operation_approvals
                    SET status = 'expired', updated_at = now()
                    WHERE owner_user_id = %s
                      AND status IN ('pending', 'approved') AND expires_at <= now()
                    RETURNING approval_id, owner_user_id, target
                    """,
                    (owner_user_id,),
                ).fetchall()
        for row in rows:
            self.record_event(
                row["owner_user_id"],
                kind="approval",
                action=ACTION_EXPIRED,
                detail={"approval_id": row["approval_id"], "target": row["target"]},
            )
        return len(rows)

    def mark_stuck_executions(self, timeout_seconds: int = STUCK_EXECUTION_SECONDS) -> int:
        """把长时间停在 executing 的记录标为 unknown（结果未知）。

        为什么不重放也不判失败：见模块头。unknown 是一个诚实的表达 ——
        我们确实不知道那次点击有没有生效，用户应当自行到网站上核对。
        """
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                UPDATE operation_approvals
                SET status = 'unknown', updated_at = now()
                WHERE status = 'executing'
                  AND updated_at <= now() - (%s || ' seconds')::interval
                RETURNING approval_id, owner_user_id, target
                """,
                (str(max(30, int(timeout_seconds))),),
            ).fetchall()
        for row in rows:
            self.record_event(
                row["owner_user_id"],
                kind="approval",
                action=ACTION_EXECUTION_UNKNOWN,
                detail={"approval_id": row["approval_id"], "target": row["target"]},
            )
        return len(rows)


_store: ApprovalStore | None = None
_store_lock = threading.Lock()


def get_approval_store() -> ApprovalStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ApprovalStore()
    return _store


def get_approval_store_or_none() -> ApprovalStore | None:
    try:
        return get_approval_store()
    except Exception as exc:
        logger.error("审批闸门不可用: %s", type(exc).__name__)
        return None


def close_approval_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None
