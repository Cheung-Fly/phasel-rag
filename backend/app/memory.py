"""对话记忆：优先 Upstash Redis，未配置则降级为进程内字典。

降级设计让你在还没注册 Upstash 时也能先把阶段一跑通，
拿到凭据后只需在 .env 填 REDIS_URL 并重启，代码无需改动。
"""
import json
import logging
import time
from typing import Any

from .config import get_settings

logger = logging.getLogger(__name__)

# ---- 进程内降级实现（单 worker 场景够用；重启即丢） ----
_inproc: dict[str, list[dict[str, Any]]] = {}


class MemoryStore:
    def __init__(self) -> None:
        self.s = get_settings()
        self._redis = None
        self.backend = self.s.memory_backend()

        if self.backend == "redis":
            try:
                import redis as redis_lib

                # Upstash 强制 TLS：rediss:// 开头
                # decode_responses=True 让返回值直接是 str
                self._redis = redis_lib.from_url(
                    self.s.redis_url,
                    decode_responses=True,
                    socket_timeout=5,
                    socket_connect_timeout=5,
                    retry_on_timeout=True,
                )
                self._redis.ping()
                logger.info("Memory backend = Upstash Redis")
            except Exception as exc:  # 连不上就降级，不让服务起不来
                logger.warning("Redis 连接失败，降级为进程内记忆: %s", exc)
                self._redis = None
                self.backend = "inproc"
        else:
            logger.info("Memory backend = in-process (未配置 REDIS_URL)")

    def _key(self, session_id: str) -> str:
        return f"rag:chat:{session_id}"

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        if self._redis is not None:
            raw = self._redis.get(self._key(session_id))
            return json.loads(raw) if raw else []
        return _inproc.get(session_id, [])

    def append(self, session_id: str, user_msg: str, ai_msg: str) -> None:
        history = self.get_history(session_id)
        history.append({"user": user_msg, "ai": ai_msg, "ts": int(time.time())})
        history = history[-20:]  # 只保留最近 20 轮，控制上下文长度和费用

        if self._redis is not None:
            self._redis.set(self._key(session_id), json.dumps(history, ensure_ascii=False),
                            ex=self.s.memory_ttl_seconds)
        else:
            _inproc[session_id] = history

    def clear(self, session_id: str) -> None:
        if self._redis is not None:
            self._redis.delete(self._key(session_id))
        else:
            _inproc.pop(session_id, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有有历史的会话（用于前端的会话列表）。

        返回按最后活动时间倒序的摘要，每条含：
            session_id / turns / last_ts / first_question

        为什么需要 first_question：
            前端要用它当会话标题。用第一句提问做标题是最自然的做法 ——
            用户自己一看就知道那段对话在聊什么。
        """
        sessions: list[dict[str, Any]] = []

        if self._redis is not None:
            # 用 scan_iter 而不是 keys()：keys() 会阻塞 Redis，
            # 在有一定数据量时是危险操作。scan_iter 是游标式的，安全。
            try:
                for raw_key in self._redis.scan_iter(match=self._key("*"), count=100):
                    sid = raw_key.split(":", 2)[-1]
                    raw = self._redis.get(raw_key)
                    if raw:
                        sessions.append(self._summarize(sid, json.loads(raw)))
            except Exception as exc:
                logger.warning("列出 Redis 会话失败: %s", exc)
        else:
            for sid, history in list(_inproc.items()):
                sessions.append(self._summarize(sid, history))

        sessions.sort(key=lambda s: s["last_ts"], reverse=True)
        return sessions

    @staticmethod
    def _summarize(session_id: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        first_q = history[0]["user"] if history else ""
        return {
            "session_id": session_id,
            "turns": len(history),
            "last_ts": history[-1]["ts"] if history else 0,
            "first_question": first_q[:60],
        }

    def clear_all(self) -> int:
        """清空所有会话，返回清掉的会话数。"""
        n = 0
        if self._redis is not None:
            try:
                for raw_key in list(self._redis.scan_iter(match=self._key("*"), count=100)):
                    self._redis.delete(raw_key)
                    n += 1
            except Exception as exc:
                logger.warning("清空 Redis 会话失败: %s", exc)
        else:
            n = len(_inproc)
            _inproc.clear()
        return n


_store: MemoryStore | None = None


def get_memory() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
