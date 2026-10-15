"""长期记忆 —— 跨会话的用户事实（阶段 D ①）。

【它解决什么问题】
    会话历史（messages）是「按时间顺序的流水账」，只服务单个 session 的上下文。
    但「用户上次说喜欢简洁回答」「用户是计算机专业、大三」这类事实跨会话都有用，
    如果每轮都把整段历史塞进 Prompt，既烧 token 又稀释重点。
    所以单独抽一张 user_facts 表：只存「值得长期记住的一句话」，并保留它的
    出处（哪次会话哪条消息），方便「忘掉某条」与「这是从哪来的」两种追问。

【三个关键设计决定】
  1. 抽取用 LLM，放 worker 异步做（不是请求线程里做）。
     理由：每轮对话后调一次轻量 LLM 是「质量」的代价，但绝不能让它拖慢
     用户眼前这轮的回答。所以 API 只「登记一个抽取任务」到阶段 B 的
     agent_tasks 队列，worker 领取后调 LLM 抽 facts、算 embedding、落库。
     队列不可用时降级为「跳过抽取」—— 记忆是增强，不是主链路的硬依赖。

  2. 检索用 pgvector，不新引向量库。
     user_facts 表就几十上百条，PG 的 HNSW 索引（或直接暴力余弦）完全够用，
     且 facts 的 embedding 与知识库共用同一个 EMBEDDING_* 配置（同一个向量空间）。
     为这点数据再挂一个 Chroma collection 是过度工程。

  3. 「忘掉」是软删除（forgotten=true），不是物理删除。
     审计与「为什么这条不见了」都需要留痕；软删除同时让「恢复」成为可能。

【pgvector 依赖】
     建表用 vector(1024) 类型，需要 PG 镜像内置 pgvector 扩展
     （docker-compose 里 postgres 镜像已从 postgres:16-alpine 换成
     pgvector/pgvector:pg16）。CREATE EXTENSION IF NOT EXISTS vector 幂等。
     若 PG 没装 pgvector，本模块所有写/查都会在 _init 时降级为「不可用」，
     不阻断主链路（见 _require 的降级策略）。
"""
import json
import logging
import time
from typing import Any

from .config import get_settings
from .llm import get_chat_model, get_embeddings

logger = logging.getLogger(__name__)

# facts 的类型。CHECK 约束住取值，避免脏数据混入；抽取时 LLM 只能选这三类。
FACT_TYPES = ("preference", "habit", "topic")

# 抽取 prompt：要求模型只输出 JSON 数组，每条是一个可独立引用的短事实。
# ★ 注意：本串用 .format(user_msg=..., ai_msg=...) 填充，因此 prompt 里
#   所有「字面量花括号」（如 JSON 示例）都必须写成 {{ }} 转义，否则
#   .format() 会把 {"fact": ...} 误当占位符，抛 KeyError: 'fact'。
_EXTRACT_PROMPT = """你是个人助手，负责从一轮对话里抽取「值得长期记住的用户事实」。

只抽取三类、且每条必须能脱离上下文独立成立（第三人称、现在时、具体）：
- preference：用户的稳定偏好（"喜欢/不喜欢/更倾向于…"）
- habit：用户的习惯或常规（"经常/通常/每周…"）
- topic：用户常问或关注的话题（"在准备…/关心…/主修…"）

不要抽取：
- 一次性事件（"今天问了X"）、寒暄、临时情绪
- 助手自己的回答内容
- 需要结合上下文才能理解的半句话

输出一个 JSON 数组，每条形如：
{{"fact": "用户偏好简洁的回答", "type": "preference", "confidence": 0.9}}
confidence 是 0~1 之间你对「这确实是长期事实」的把握；低于 0.6 的不要输出。
若没有值得记的，输出 []。

对话：
用户：{user_msg}
助手：{ai_msg}
"""


def _pg_vector() -> bool:
    """判断当前 PG 是否已启用 pgvector。"""
    from .memory import get_memory

    pool = get_memory()._pg_pool
    if pool is None:
        return False
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
            return bool(row and row[0])
    except Exception:
        return False


class FactStore:
    """user_facts 的读写 + 抽取 + 检索。PG 不可用或未装 pgvector 时安全降级。"""

    def __init__(self) -> None:
        self.s = get_settings()
        self._available = False
        self._init()

    # ------------------------------------------------------------ 初始化
    def _init(self) -> None:
        from .memory import get_memory

        pool = get_memory()._pg_pool
        if pool is None:
            logger.info("FactStore：PostgreSQL 不可用，长期记忆关闭")
            return
        try:
            with pool.connection() as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                conn.execute(_SCHEMA)
            self._available = True
            logger.info("FactStore：长期记忆可用（PG + pgvector）")
        except Exception as exc:
            # pgvector 未装 / 版本不匹配等都会走到这里。记忆是增强不是硬依赖，
            # 降级为「不可用」即可，绝不让它拖垮整个服务启动。
            logger.warning("FactStore 初始化失败，长期记忆关闭: %s", exc)
            self._available = False

    def _require(self):
        if not self._available:
            return None
        from .memory import get_memory

        return get_memory()._pg_pool

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------ 抽取
    def extract(self, user_msg: str, ai_msg: str) -> list[dict[str, Any]]:
        """调 LLM 把一轮对话抽成 facts。失败返回 []（抽取是尽力而为）。"""
        model = get_chat_model(temperature=0.0)
        try:
            raw = model.invoke(
                _EXTRACT_PROMPT.format(user_msg=user_msg, ai_msg=ai_msg)
            ).content
        except Exception as exc:
            logger.warning("fact 抽取 LLM 调用失败（已忽略）: %s", exc)
            return []
        return _parse_facts(raw)

    # ------------------------------------------------------------ 写入
    def upsert_facts(
        self,
        owner_user_id: str,
        facts: list[dict[str, Any]],
        source_session_id: str,
        source_message_id: int | None = None,
    ) -> int:
        """把抽取出的 facts 落库。返回实际写入条数。"""
        pool = self._require()
        if pool is None or not facts:
            return 0
        emb = get_embeddings()
        written = 0
        for f in facts:
            fact_text = (f.get("fact") or "").strip()
            ftype = f.get("type")
            if not fact_text or ftype not in FACT_TYPES:
                continue
            try:
                vector = emb.embed_query(fact_text)
            except Exception as exc:
                logger.warning("fact embedding 失败（跳过该条）: %s", exc)
                continue
            confidence = float(f.get("confidence") or 0.0)
            confidence = max(0.0, min(1.0, confidence))
            with pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO user_facts
                        (owner_user_id, fact, fact_type, confidence,
                         source_session_id, source_message_id, embedding)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        owner_user_id,
                        fact_text,
                        ftype,
                        confidence,
                        source_session_id,
                        source_message_id,
                        json.dumps(vector),
                    ),
                )
            written += 1
        return written

    # ------------------------------------------------------------ 检索
    def search(self, owner_user_id: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """按语义检索与 query 相关的未遗忘 facts。返回 top_k 条（含来源）。"""
        pool = self._require()
        if pool is None:
            return []
        try:
            vector = get_embeddings().embed_query(query)
        except Exception:
            return []
        with pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, fact, fact_type, confidence,
                       source_session_id, source_message_id,
                       created_at
                FROM user_facts
                WHERE owner_user_id = %s AND forgotten = false
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (owner_user_id, json.dumps(vector), top_k),
            ).fetchall()
        return [
            {
                "id": r[0],
                "fact": r[1],
                "fact_type": r[2],
                "confidence": float(r[3]),
                "source_session_id": r[4],
                "source_message_id": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------ 查询 / 遗忘
    def list_facts(self, owner_user_id: str) -> list[dict[str, Any]]:
        pool = self._require()
        if pool is None:
            return []
        with pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, fact, fact_type, confidence,
                       source_session_id, source_message_id, forgotten, created_at
                FROM user_facts
                WHERE owner_user_id = %s
                ORDER BY created_at DESC
                LIMIT 200
                """,
                (owner_user_id,),
            ).fetchall()
        return [
            {
                "id": r[0],
                "fact": r[1],
                "fact_type": r[2],
                "confidence": float(r[3]),
                "source_session_id": r[4],
                "source_message_id": r[5],
                "forgotten": bool(r[6]),
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]

    def forget(self, owner_user_id: str, fact_id: int) -> bool:
        """软删除一条 fact。返回是否真的删到了（存在且属于该 owner）。"""
        pool = self._require()
        if pool is None:
            return False
        with pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE user_facts
                SET forgotten = true
                WHERE id = %s AND owner_user_id = %s AND forgotten = false
                RETURNING id
                """,
                (fact_id, owner_user_id),
            ).fetchone()
            return row is not None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_facts (
    id                BIGSERIAL PRIMARY KEY,
    owner_user_id     TEXT NOT NULL,
    fact              TEXT NOT NULL,
    fact_type         TEXT NOT NULL CHECK (fact_type IN ('preference', 'habit', 'topic')),
    confidence        REAL NOT NULL DEFAULT 0.0,
    source_session_id TEXT NOT NULL,
    source_message_id BIGINT,
    embedding         vector(1024),
    forgotten         BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _parse_facts(raw: Any) -> list[dict[str, Any]]:
    """把 LLM 输出解析成 facts 列表。对非 JSON / 包裹格式 / 空结果都安全。"""
    if not raw:
        return []
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    # 模型可能包一层 ```json ... ``` 或夹杂说明文字，先剥掉。
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    # 尽量截取到第一个 '[' 与最后一个 ']' 之间的内容。
    lb, rb = text.find("["), text.rfind("]")
    if lb == -1 or rb == -1 or rb <= lb:
        return []
    try:
        data = json.loads(text[lb : rb + 1])
    except json.JSONDecodeError:
        logger.info("fact 抽取输出不是合法 JSON，已忽略")
        return []
    if not isinstance(data, list):
        return []
    return [
        d for d in data
        if isinstance(d, dict) and (d.get("fact") or "").strip()
    ]


_fact_store: FactStore | None = None


def get_fact_store() -> FactStore:
    global _fact_store
    if _fact_store is None:
        _fact_store = FactStore()
    return _fact_store
