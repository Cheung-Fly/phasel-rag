"""主动建议 —— 基于长期记忆推断「现在该做什么」（阶段 D ②）。

【它解决什么问题】
    阶段 D ① 让助手「记住」了用户是谁（user_facts）。但记住的
    东西要能「用出去」才有价值 —— 本模块就是把 facts 变成一句
    主动、可执行、可确认的建议：比如「你在准备考研，要不要我帮你
    梳理一下本周复习计划？」。

【三个关键设计决定】
  1. 触发是「入口触发」，不是后台定时。
     本机是「网页界面 + 单人 + 打开即用」的形态，没有手机推送通道。
     「后台生成建议」若不解决「怎么送到用户眼前」，就是一堆没人读的
     笔记。所以触发点放在用户下一次打开页面 / 发消息时：后端检查
     「有没有该提醒的事」，有就返回建议卡片。这保证建议一定在用户
     「在」的时候送达。

  2. 生成是「LLM 推断」，不引入独立的待办登记。
     user_facts 已经存了偏好/习惯/话题（「在准备考研」「主修计算机」），
     这些足够 LLM 推断出「该复习了」「该投实习了」这类建议。
     「用户显式登记带截止日期的事项」是另一个功能（偏待办/提醒），
     不在本模块范围，避免阶段 D ② 失控。

  3. 建议是「候选」，确认才转任务；忽略要「记住」不再重复。
     建议不是命令，不能自动执行。用户「采纳」→ 复用阶段 B 的
     agent_tasks 队列生成一个真实任务（如「生成复习计划」）；
     「忽略」→ 记为 dismissed，同类建议短期内不再推送（否则每次
     打开都弹一遍，变成骚扰）。
"""
import json
import logging
import uuid
from typing import Any

from .config import get_settings
from .llm import get_chat_model

logger = logging.getLogger(__name__)

# 建议状态。终态是 accepted / dismissed / expired；pending 是「待用户决定」。
SUGGESTION_STATUSES = ("pending", "accepted", "dismissed", "expired")

# 忽略后多久不再推送同类建议（秒）。设短了会骚扰，设长了会显得「记性差」。
DISMISS_COOLDOWN_SECONDS = 7 * 24 * 3600  # 7 天


# 生成 prompt：要求模型只输出 JSON 数组，每条是一个可执行、可确认的建议。
# ★ 与 fact_store 同理：本串用 .format 填充，字面量花括号必须 {{ }} 转义。
_SUGGEST_PROMPT = """你是个人助手，基于已知的用户长期事实，推断「现在最值得主动提醒用户做的事」。

已知的用户事实（每条是用户过去透露的稳定信息）：
{facts_text}

请从这些事实出发，推断 1~3 条「现在该做」的主动建议。每条必须：
- 具体、可执行，不是空泛的关心（「注意休息」太弱，「我可以帮你整理本周复习计划」才对）
- 直接服务于用户的现实目标（备考、求职、学业、健康等）
- 是「建议去做的事」，不是「提问」，也不是「复述事实」

不要输出：
- 与已知事实无关的凭空猜测
- 需要用户授权才能做的敏感动作（发邮件、下单、登录账号）
- 一次性事件提醒（除非事实里明确有截止日期）

输出一个 JSON 数组，每条形如：
{{"suggestion": "我可以帮你把本周的复习计划按科目拆解成每天的任务", "reason": "你在准备研究生考试，正处在需要持续复习的阶段", "kind": "study"}}
kind 从 study/job/health/life 里选一个最贴切的。
若事实不足以给出有价值的建议，输出 []。

只输出 JSON 数组，不要任何解释文字。
"""


class SuggestionStore:
    """suggestions 的读写 + LLM 推断生成。PG 不可用时安全降级为空。"""

    def __init__(self) -> None:
        self.s = get_settings()
        self._available = False
        self._init()

    def _init(self) -> None:
        from .memory import get_memory

        pool = get_memory()._pg_pool
        if pool is None:
            logger.info("SuggestionStore：PostgreSQL 不可用，主动建议关闭")
            return
        try:
            with pool.connection() as conn:
                conn.execute(_SCHEMA)
            self._available = True
            logger.info("SuggestionStore：主动建议可用（PG）")
        except Exception as exc:
            logger.warning("SuggestionStore 初始化失败，主动建议关闭: %s", exc)
            self._available = False

    def _require(self):
        if not self._available:
            return None
        from .memory import get_memory

        return get_memory()._pg_pool

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------ 生成
    def generate(self, owner_user_id: str) -> list[dict[str, Any]]:
        """基于用户未遗忘的 facts 推断建议，落库为 pending。返回新建建议列表。

        失败（LLM 挂、无 facts、PG 不可用）都返回 []，不阻断调用方。
        """
        from .fact_store import get_fact_store

        pool = self._require()
        if pool is None:
            return []
        # 拿事实：直接列出全部未遗忘 facts（数量小），喂给 LLM 一起推断。
        facts = [f for f in get_fact_store().list_facts(owner_user_id) if not f["forgotten"]]
        if not facts:
            return []
        facts_text = "\n".join(
            f"- [{f['fact_type']}] {f['fact']}" for f in facts
        )
        try:
            model = get_chat_model(temperature=0.0)
            raw = model.invoke(_SUGGEST_PROMPT.format(facts_text=facts_text)).content
        except Exception as exc:
            logger.warning("建议生成 LLM 调用失败（已忽略）: %s", exc)
            return []
        suggestions = _parse_suggestions(raw)
        if not suggestions:
            return []
        return self._store_pending(owner_user_id, suggestions)

    def _store_pending(self, owner_user_id: str, suggestions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把推断出的建议落库为 pending，跳过近期已 dismiss 的同类建议。"""
        pool = self._require()
        if pool is None:
            return []
        created: list[dict[str, Any]] = []
        for s in suggestions:
            text = (s.get("suggestion") or "").strip()
            if not text:
                continue
            if self._recently_dismissed(pool, owner_user_id, text):
                continue
            sid = uuid.uuid4().hex
            with pool.connection() as conn:
                row = conn.execute(
                    """
                    INSERT INTO suggestions
                        (id, owner_user_id, suggestion, reason, kind)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, suggestion, reason, kind, status, created_at
                    """,
                    (
                        sid,
                        owner_user_id,
                        text,
                        (s.get("reason") or "").strip(),
                        (s.get("kind") or "life").strip()[:16],
                    ),
                ).fetchone()
            created.append(self._serialize(row))
        return created

    @staticmethod
    def _recently_dismissed(pool, owner_user_id: str, text: str) -> bool:
        """最近冷却期内是否 dismiss 过「高度相似」的建议。

        用文本相似度判断去重太贵（要调 embedding），这里用一个务实近似：
        冷却期内 dismiss 过的建议里，若存在与当前建议「共词比例高」的，
        视为同类，跳过。目标只是「别每次都弹同一条」，不必精确。
        """
        try:
            with pool.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT suggestion FROM suggestions
                    WHERE owner_user_id = %s AND status = 'dismissed'
                      AND created_at > now() - interval '7 days'
                    """,
                    (owner_user_id,),
                ).fetchall()
        except Exception:
            return False
        for (old,) in rows:
            if _overlap_ratio(old, text) >= 0.5:
                return True
        return False

    # ------------------------------------------------------------ 查询
    def list_pending(self, owner_user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """列出待用户决定的建议（pending），旧的在前。"""
        pool = self._require()
        if pool is None:
            return []
        with pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, suggestion, reason, kind, status, created_at
                FROM suggestions
                WHERE owner_user_id = %s AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (owner_user_id, limit),
            ).fetchall()
        return [self._serialize(r) for r in rows]

    def get(self, owner_user_id: str, suggestion_id: str) -> dict[str, Any] | None:
        pool = self._require()
        if pool is None:
            return None
        with pool.connection() as conn:
            row = conn.execute(
                """
                SELECT id, suggestion, reason, kind, status, created_at
                FROM suggestions
                WHERE id = %s AND owner_user_id = %s
                """,
                (suggestion_id, owner_user_id),
            ).fetchone()
        return self._serialize(row) if row else None

    # ------------------------------------------------------------ 决定
    def accept(self, owner_user_id: str, suggestion_id: str) -> dict[str, Any] | None:
        """采纳：pending → accepted。返回更新后的建议，不存在/非 pending 返回 None。"""
        return self._transition(owner_user_id, suggestion_id, "accepted")

    def dismiss(self, owner_user_id: str, suggestion_id: str) -> dict[str, Any] | None:
        """忽略：pending → dismissed。返回更新后的建议，不存在/非 pending 返回 None。"""
        return self._transition(owner_user_id, suggestion_id, "dismissed")

    def _transition(self, owner_user_id: str, suggestion_id: str, target: str) -> dict[str, Any] | None:
        pool = self._require()
        if pool is None:
            return None
        with pool.connection() as conn:
            row = conn.execute(
                """
                UPDATE suggestions
                SET status = %s
                WHERE id = %s AND owner_user_id = %s AND status = 'pending'
                RETURNING id, suggestion, reason, kind, status, created_at
                """,
                (target, suggestion_id, owner_user_id),
            ).fetchone()
        return self._serialize(row) if row else None

    @staticmethod
    def _serialize(row) -> dict[str, Any]:
        return {
            "id": row[0],
            "suggestion": row[1],
            "reason": row[2],
            "kind": row[3],
            "status": row[4],
            "created_at": row[5].isoformat() if row[5] else None,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id          TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    suggestion  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'life',
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'dismissed', 'expired')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _parse_suggestions(raw: Any) -> list[dict[str, Any]]:
    """把 LLM 输出解析成建议列表。对非 JSON / 包裹格式 / 空结果都安全。"""
    if not raw:
        return []
    text = raw if isinstance(raw, str) else str(raw)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    lb, rb = text.find("["), text.rfind("]")
    if lb == -1 or rb == -1 or rb <= lb:
        return []
    try:
        data = json.loads(text[lb : rb + 1])
    except json.JSONDecodeError:
        logger.info("建议生成输出不是合法 JSON，已忽略")
        return []
    if not isinstance(data, list):
        return []
    return [
        d for d in data
        if isinstance(d, dict) and (d.get("suggestion") or "").strip()
    ]


def _overlap_ratio(a: str, b: str) -> float:
    """两个中文短句的共词比例（简单字符 bigram 交集 / 并集）。"""
    if not a or not b:
        return 0.0
    def bigrams(s: str) -> set[str]:
        s = "".join(s.split())
        return {s[i : i + 2] for i in range(len(s) - 1)} or {s}
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    inter = ga & gb
    union = ga | gb
    return len(inter) / len(union)


_suggestion_store: SuggestionStore | None = None


def get_suggestion_store() -> SuggestionStore:
    global _suggestion_store
    if _suggestion_store is None:
        _suggestion_store = SuggestionStore()
    return _suggestion_store
