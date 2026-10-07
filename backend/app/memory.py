"""对话记忆：PostgreSQL 存档 + Redis 热缓存（2026-10-07 重构）。

【分层职责 —— 这是本模块最重要的设计决定】

    PostgreSQL = 真相源（source of truth）
        每条消息逐条落库，只追加不修改，永久保留。
        Redis 丢了、容器重建了、整机迁走了，都能从这里完整重建。

    Redis      = 热缓存（cache-aside）
        按 session 聚合的最近 HOT_TURNS 轮，供拼 Prompt 时 O(1) 读取，带 TTL。
        它随时可以丢 —— 丢了会自动从 PostgreSQL 回填。

【为什么不是"近期写 Redis、长期写 PG"（两个独立数据源）】
    那样本质是双写：两边都可能失败，且没有明确的权威副本，
    一旦不一致根本不知道该信谁。正确做法是先定真相源，再让缓存从真相源派生。
    因此写入顺序固定为【先落 PG，再刷 Redis】——
    PG 成功而 Redis 失败只损失一点性能，反过来则直接丢数据。

【降级链】任何一层挂掉都不影响服务可用：
    PG + Redis  →  PG only（无缓存）  →  Redis only（无持久化）  →  进程内

【为什么不用 Upstash 了】
    原实现假设用 Upstash 托管 Redis（公网 + TLS，有额度与延迟成本）。
    现在 Redis 与 PG 都作为本机容器跑在同一台 VM 上：
    延迟从毫秒级公网往返降到容器内网微秒级，且完全免费。
    memory.py 依旧兼容 redis:// 与 rediss:// 两种 URL。
"""
import json
import logging
import threading
import time
from typing import Any

from .config import get_settings

logger = logging.getLogger(__name__)

# 进程内最终降级（PG 与 Redis 都不可用时才用；重启即丢）
_inproc: dict[str, list[dict[str, Any]]] = {}
_inproc_lock = threading.Lock()

# 建表语句。逐条执行，因为 psycopg3 的 execute() 一次只接受一条语句。
# role 用 CHECK 约束住取值，避免脏数据混入；
# messages 对 sessions 外键级联删除，保证"删会话 = 删干净"。
_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id     TEXT PRIMARY KEY,
        first_question TEXT NOT NULL DEFAULT '',
        created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id         BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content    TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(last_active_at DESC)",
]


def _mask(url: str) -> str:
    """日志里不打明文口令。"""
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1) if "://" in url else ("", url)
    _, host = rest.rsplit("@", 1)
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


class MemoryStore:
    """PG（真相源）+ Redis（热缓存）双层会话存储。"""

    # 热缓存保留的轮数：供拼 Prompt 使用，再多也是浪费 token
    HOT_TURNS = 20
    # Redis 里缓存的键前缀
    _PREFIX = "rag:chat:"

    def __init__(self) -> None:
        self.s = get_settings()
        self._redis = None
        self._pg_pool = None
        self._pg_failed_logged = False
        self._init_pg()
        self._init_redis()

    # ------------------------------------------------------------ 初始化
    def _init_pg(self) -> None:
        if not self.s.postgres_dsn.strip():
            logger.info("PostgreSQL 未配置（POSTGRES_DSN 为空），持久化存档不可用")
            return
        try:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                self.s.postgres_dsn,
                min_size=self.s.pg_pool_min,
                max_size=self.s.pg_pool_max,
                open=True,
                timeout=5,
            )
            with pool.connection() as conn:
                for stmt in _SCHEMA:
                    conn.execute(stmt)
            self._pg_pool = pool
            logger.info("Memory 真相源 = PostgreSQL（存档）")
        except Exception as exc:
            logger.warning("PostgreSQL 连接失败，存档不可用: %s", exc)
            self._pg_pool = None

    def _init_redis(self) -> None:
        if not self.s.redis_url.strip():
            logger.info("Redis 未配置（REDIS_URL 为空），热缓存不可用")
            return
        try:
            import redis as redis_lib

            self._redis = redis_lib.from_url(
                self.s.redis_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            self._redis.ping()
            logger.info("Memory 热缓存 = Redis（%s）", _mask(self.s.redis_url))
        except Exception as exc:
            logger.warning("Redis 连接失败，热缓存不可用: %s", exc)
            self._redis = None

    # ------------------------------------------------------------ 对外状态
    def backend_summary(self) -> str:
        """给 /health 用的一句话描述，例如 'postgres+redis'。"""
        parts = []
        if self._pg_pool is not None:
            parts.append("postgres")
        if self._redis is not None:
            parts.append("redis")
        return "+".join(parts) if parts else "inproc"

    def _key(self, session_id: str) -> str:
        return f"{self._PREFIX}{session_id}"

    # ------------------------------------------------------------ 热缓存
    def _hot_read(self, session_id: str) -> list[dict[str, Any]] | None:
        """返回 None 表示缓存未命中（区别于"命中但是空列表"）。"""
        if self._redis is None:
            return None
        try:
            raw = self._redis.get(self._key(session_id))
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("Redis 读取失败: %s", exc)
            return None

    def _hot_write(self, session_id: str, history: list[dict[str, Any]]) -> bool:
        if self._redis is None:
            return False
        try:
            self._redis.set(
                self._key(session_id),
                json.dumps(history, ensure_ascii=False),
                ex=self.s.memory_ttl_seconds,
            )
            return True
        except Exception as exc:
            logger.warning("Redis 写入失败: %s", exc)
            return False

    # ------------------------------------------------------------ 真相源
    def _pg_append(self, session_id: str, user_msg: str, ai_msg: str) -> bool:
        if self._pg_pool is None:
            return False
        try:
            with self._pg_pool.connection() as conn:
                # 会话表：首次插入时记下首问（供前端当标题），之后只更新活跃时间
                conn.execute(
                    "INSERT INTO sessions (session_id, first_question) VALUES (%s, %s) "
                    "ON CONFLICT (session_id) DO UPDATE SET last_active_at = now()",
                    (session_id, user_msg[:60]),
                )
                # 逐条落库：一轮问答 = user 一条 + assistant 一条
                conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES (%s, 'user', %s)",
                    (session_id, user_msg),
                )
                conn.execute(
                    "INSERT INTO messages (session_id, role, content) VALUES (%s, 'assistant', %s)",
                    (session_id, ai_msg),
                )
            return True
        except Exception as exc:
            if not self._pg_failed_logged:
                logger.warning("PostgreSQL 写入失败（后续相同错误不再重复记录）: %s", exc)
                self._pg_failed_logged = True
            return False

    def _pg_load(self, session_id: str) -> list[dict[str, Any]]:
        """从真相源重建完整历史，并按 user/assistant 配成轮次。"""
        if self._pg_pool is None:
            return []
        try:
            with self._pg_pool.connection() as conn:
                rows = conn.execute(
                    "SELECT role, content, EXTRACT(EPOCH FROM created_at)::bigint "
                    "FROM messages WHERE session_id = %s ORDER BY id",
                    (session_id,),
                ).fetchall()
        except Exception as exc:
            logger.warning("PostgreSQL 读取失败: %s", exc)
            return []

        pairs: list[dict[str, Any]] = []
        pending: dict[str, Any] | None = None
        for role, content, ts in rows:
            if role == "user":
                if pending is not None:
                    pairs.append(pending)  # 上一轮没等到回答（生成失败），照样保留
                pending = {"user": content, "ai": "", "ts": int(ts)}
            else:
                if pending is None:
                    pending = {"user": "", "ai": content, "ts": int(ts)}
                else:
                    pending["ai"] = content
                    pairs.append(pending)
                    pending = None
        if pending is not None:
            pairs.append(pending)
        return pairs

    # ------------------------------------------------------------ 公开接口
    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """读历史：缓存优先，未命中则从真相源回填。"""
        cached = self._hot_read(session_id)
        if cached is not None:
            return cached

        if self._pg_pool is not None:
            rows = self._pg_load(session_id)
            if rows:
                self._hot_write(session_id, rows)  # cache-aside 回填
            return rows

        with _inproc_lock:
            return list(_inproc.get(session_id, []))

    def append(self, session_id: str, user_msg: str, ai_msg: str) -> None:
        """写一轮问答：先落真相源，再刷热缓存。"""
        ts = int(time.time())
        entry = {"user": user_msg, "ai": ai_msg, "ts": ts}

        if self._pg_append(session_id, user_msg, ai_msg):
            history = self._hot_read(session_id)
            if history is None:
                # 缓存冷启动：从真相源全量回填（此时已包含刚写入的这一轮）
                history = self._pg_load(session_id)
            else:
                history = history + [entry]
            self._hot_write(session_id, history[-self.HOT_TURNS:])
            return

        # ---- 降级：真相源不可用，退化为"仅缓存"（不再尝试 PG，避免每次拖 5 秒超时）----
        history = self._hot_read(session_id)
        if history is None:
            with _inproc_lock:
                history = list(_inproc.get(session_id, []))
        history = (history + [entry])[-self.HOT_TURNS:]
        if not self._hot_write(session_id, history):
            with _inproc_lock:
                _inproc[session_id] = history

    def clear(self, session_id: str) -> None:
        if self._pg_pool is not None:
            try:
                with self._pg_pool.connection() as conn:
                    # messages 走外键级联，删会话即可连带删干净
                    conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
            except Exception as exc:
                logger.warning("PostgreSQL 删除会话失败: %s", exc)
        if self._redis is not None:
            try:
                self._redis.delete(self._key(session_id))
            except Exception as exc:
                logger.warning("Redis 删除会话失败: %s", exc)
        with _inproc_lock:
            _inproc.pop(session_id, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话（真相源优先，带缓存兜底）。

        每条含 session_id / turns / last_ts / first_question。
        first_question 供前端当会话标题 —— 用户一眼就知道那段对话在聊什么。
        """
        if self._pg_pool is not None:
            try:
                with self._pg_pool.connection() as conn:
                    rows = conn.execute(
                        "SELECT s.session_id, s.first_question, "
                        "       COALESCE(m.turns, 0), "
                        "       EXTRACT(EPOCH FROM s.last_active_at)::bigint "
                        "FROM sessions s "
                        "LEFT JOIN ("
                        "    SELECT session_id, COUNT(*) FILTER (WHERE role = 'user') AS turns "
                        "    FROM messages GROUP BY session_id"
                        ") m ON m.session_id = s.session_id "
                        "ORDER BY s.last_active_at DESC LIMIT 200"
                    ).fetchall()
                return [
                    {
                        "session_id": r[0],
                        "first_question": r[1],
                        "turns": int(r[2]),
                        "last_ts": int(r[3]),
                    }
                    for r in rows
                ]
            except Exception as exc:
                logger.warning("PostgreSQL 列出会话失败，回退缓存: %s", exc)

        # ---- 回退：Redis / 进程内 ----
        sessions: list[dict[str, Any]] = []
        if self._redis is not None:
            try:
                # scan_iter 是游标式的；keys() 会阻塞 Redis，数据量上来后是危险操作
                for raw_key in self._redis.scan_iter(match=f"{self._PREFIX}*", count=100):
                    sid = raw_key.split(":", 2)[-1]
                    raw = self._redis.get(raw_key)
                    if raw:
                        sessions.append(self._summarize(sid, json.loads(raw)))
            except Exception as exc:
                logger.warning("列出 Redis 会话失败: %s", exc)
        else:
            with _inproc_lock:
                for sid, history in list(_inproc.items()):
                    sessions.append(self._summarize(sid, history))

        sessions.sort(key=lambda s: s["last_ts"], reverse=True)
        return sessions

    def clear_all(self) -> int:
        """清空所有会话，返回清掉的会话数。"""
        n = 0
        if self._pg_pool is not None:
            try:
                with self._pg_pool.connection() as conn:
                    n = conn.execute("DELETE FROM sessions").rowcount
            except Exception as exc:
                logger.warning("PostgreSQL 清空会话失败: %s", exc)
        if self._redis is not None:
            try:
                for raw_key in list(self._redis.scan_iter(match=f"{self._PREFIX}*", count=100)):
                    self._redis.delete(raw_key)
            except Exception as exc:
                logger.warning("清空 Redis 会话失败: %s", exc)
        with _inproc_lock:
            if self._pg_pool is None and self._redis is None:
                n = len(_inproc)
            _inproc.clear()
        return n

    @staticmethod
    def _summarize(session_id: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        first_q = history[0]["user"] if history else ""
        return {
            "session_id": session_id,
            "turns": len(history),
            "last_ts": history[-1]["ts"] if history else 0,
            "first_question": first_q[:60],
        }


_store: MemoryStore | None = None


def get_memory() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
