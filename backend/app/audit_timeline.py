"""审计时间线（阶段 C ④ / W4）：把「助手做过什么」合成一条可读的流水。

【为什么是独立的读模型，而不是一张新表】
路线图的表述是「把 `credential_audit` + 操作记录合并成一条可读的时间线」。
有三种实现：
  A. 建一张统一大表，把 credential_audit 迁移进去
  B. 保留两张表，查询时 UNION
  C. 应用层各查各的，再在 Python 里合并排序
选 B。理由：
  · 反选 A：要迁移既有审计数据并改 CHECK 约束，而这个约束历史上已经出过
    一次事故（漏迁移表现为「接口直接 503」，见 SERVER-CHANGES 第十次变更 §6）。
    凭证审计与操作审计的关注点也确实不同，强行合并会丢掉各自的特化字段。
  · 反选 C：应用层合并要先各自取 N 条再截断，分页会错 ——
    「第 2 页」可能比「第 1 页」还新。UNION 在 SQL 里做，排序与 LIMIT 才是全局正确的。

【翻页为什么用游标而不是 OFFSET】
上面反对「应用层合并」的那条理由，对 OFFSET 同样成立：时间线是**持续增长**的，
而且新事件插在最前面。用 `OFFSET 30` 取第 2 页，只要这期间新增了一条，
第 1 页的最后一条就会滑到第 2 页的开头 —— 用户看到重复；反过来漏删（清理旧审计）
则会整条跳过。所以游标里带的是**排序键本身** `(created_at, source, seq)`，
SQL 里用行值比较 `(created_at, source, seq) < (…)` 定位，新增/删除都不影响已翻过的位置。
排序键必须**全局唯一**才能当游标：两张表的 `audit_id` 各自取号会撞号，
所以第三段用 `source`，第四段才是来源表内的自增号 `seq`。

【为什么可读文案在后端生成】
「助手做过什么」是一句人话。若把 action → 中文的映射放在前端，同一份语义
会在多端各写一遍（将来做 CLI 或通知时还要再写一遍），且凭证 label 需要
join 才能拿到 —— 前端拿不到。所以摘要在这里生成，前端只负责渲染。

【连接池为什么自带一份】
这是它的生命周期，与 SiteStore / ApprovalStore 各自独立：任何一个模块的
初始化失败都不该把另外两个拖下水。代价是每个池常驻 1 条空闲连接（min_size=1），
在 2 vCPU 上可以忽略。
"""
import logging
import threading
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 游标的三段分隔符。用 '|' 是因为它不会出现在 isoformat 的时间戳、
# 也不会出现在两个 source 取值里 —— 不必额外做转义。
CURSOR_SEPARATOR = "|"

_SOURCES = ("credential", "operation")

_CREDENTIAL_ACTIONS = {
    "create": "登记了校园账号凭证「{label}」",
    "read": "查看了凭证「{label}」",
    "update": "更新了凭证「{label}」",
    "delete": "删除了凭证「{label}」",
    "reveal": "取出了凭证「{label}」的明文口令（用于自动登录）",
}

_OPERATION_ACTIONS = {
    "approval_requested": "请求你批准：{target}",
    "approval_granted": "你批准了：{target}",
    "approval_rejected": "你拒绝了：{target}",
    "approval_expired": "操作超时已自动放弃：{target}",
    "approval_executed": "已执行：{target}",
    "approval_execution_failed": "执行未成功：{target}",
    "approval_execution_unknown": "执行结果未知，请自行核对：{target}",
    # 站点变更（阶段 C ④ 补写）。它们会出现在这里而不是各自散落的原因：
    # 「助手能碰哪些站点」是这条信任链的起点，起点改了却不留痕，
    # 后面的审批闸门就没有可核对的依据。
    "site_registered": "登记了可操作站点「{target}」",
    "site_updated": "修改了可操作站点「{target}」",
    "site_removed": "移除了可操作站点「{target}」",
}

# 前端据此决定图标与配色；也免得前端去猜 action 的语义。
_LEVELS = {
    "credential": {
        "create": "info",
        "read": "muted",
        "update": "info",
        "delete": "warn",
        "reveal": "warn",
    },
    "operation": {
        "approval_requested": "info",
        "approval_granted": "info",
        "approval_rejected": "muted",
        "approval_expired": "warn",
        "approval_executed": "ok",
        "approval_execution_failed": "warn",
        "approval_execution_unknown": "warn",
        # 扩充可操作范围是「放宽约束」，值得用 warn 让人一眼看见；
        # 移除也是 warn —— 它会让原本能用的凭证变成孤儿。
        "site_registered": "warn",
        "site_updated": "info",
        "site_removed": "warn",
    },
}

# 事件来源与来源表之间的映射只在 SQL 里出现一次，避免两处各写一份。
#
# 游标那三行值得单独说明：无游标时传入 NULL，COALESCE 把它们顶成「比任何行都大」
# 的哨兵值（'infinity' / 'zzzz' / bigint 最大值），于是行值比较恒真 ——
# 同一个 SQL 既能取第一页也能取后续页，不必写两条语句、也不必拼字符串。
# 三个参数都显式 ::cast，否则全传 NULL 时 PostgreSQL 推断不出参数类型。
_TIMELINE_SQL = """
WITH events AS (
    SELECT 'credential' AS source,
           ca.audit_id   AS seq,
           ca.action     AS action,
           'credential'  AS kind,
           ca.created_at AS created_at,
           jsonb_build_object(
               'credential_id', ca.credential_id,
               'label',         COALESCE(ec.label, ''),
               'service_key',   COALESCE(ec.service_key, '')
           ) AS detail
    FROM credential_audit ca
    LEFT JOIN encrypted_credentials ec
           ON ec.credential_id = ca.credential_id
          AND ec.owner_user_id = ca.owner_user_id
    WHERE ca.owner_user_id = %(owner)s
    UNION ALL
    SELECT 'operation',
           oa.audit_id,
           oa.action,
           oa.kind,
           oa.created_at,
           oa.detail
    FROM operation_audit oa
    WHERE oa.owner_user_id = %(owner)s
)
SELECT source, seq, action, kind, created_at, detail
FROM events
WHERE (created_at, source, seq) < (
        COALESCE(%(cursor_at)s::timestamptz, 'infinity'::timestamptz),
        COALESCE(%(cursor_source)s::text, 'zzzz'),
        COALESCE(%(cursor_seq)s::bigint, 9223372036854775807)
      )
ORDER BY created_at DESC, source DESC, seq DESC
LIMIT %(limit)s
"""


def _label_of(detail: dict, credential_id: str) -> str:
    label = str((detail or {}).get("label") or "").strip()
    if label:
        return label
    # 凭证被删掉后 join 不到 label。退回一个可辨认的短标识，
    # 而不是显示空字符串 —— 时间线上「删除了凭证『』」是没法读的。
    return f"凭证 {str(credential_id or '')[:8]}"


def summarize(event: dict) -> str:
    """把一条时间线事件写成一句中文。未知 action 原样返回，不猜。"""
    source = event.get("source")
    action = str(event.get("action") or "")
    detail = event.get("detail") or {}
    if source == "credential":
        template = _CREDENTIAL_ACTIONS.get(action)
        if template:
            return template.format(
                label=_label_of(detail, detail.get("credential_id", ""))
            )
        return f"凭证操作：{action}"
    template = _OPERATION_ACTIONS.get(action)
    if template:
        return template.format(target=str(detail.get("target") or "（未记录目标）"))
    return f"助手操作：{action}（{event.get('kind') or 'unknown'}）"


def _serialize(event: dict) -> dict:
    created_at = event.get("created_at")
    source = event.get("source")
    seq = event.get("seq")
    enriched = dict(event)
    # 对外的 id 必须带来源前缀：两张表的 audit_id 各自取号，会撞号。
    enriched["id"] = f"{source}-{seq}"
    # seq 是来源表内的自增号，只用于拼翻页游标（见 make_cursor）。
    # 保留它是因为 _serialize 之后的 id 已经变成字符串，拿不回数字了。
    enriched["seq"] = seq
    enriched["created_at"] = (
        created_at.isoformat() if isinstance(created_at, datetime) else created_at
    )
    enriched["summary"] = summarize(event)
    enriched["level"] = (_LEVELS.get(source) or {}).get(
        str(event.get("action") or ""), "muted"
    )
    return enriched


def _cursor_time(value: datetime | str) -> str:
    """把时间戳写成游标里的形式：UTC、以 `Z` 结尾。

    ★ 为什么不用 `isoformat()` 的 `+00:00` 结尾：游标是放在 query string 里传的，
      而 `+` 在 query string 中会被解码成**空格** —— 于是「服务端刚打印出来的
      游标、原样回传」会变成 `2026-10-15T07:46:47.583487 00:00`，
      `fromisoformat` 解析失败，用户收到一个莫名其妙的「游标格式不正确」。
      这类 bug 只在翻页时才现形，而且看起来像用户自己乱填的。
      `Z` 是 ISO 8601 的 UTC 写法，在 URI 里无需转义；
      `datetime.fromisoformat` 自 Python 3.11 起可以解析它。
    """
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value or ""))
    if moment.tzinfo is None:
        # 自身产出永远带时区；真收到裸时间就当 UTC，不猜本地时区。
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_cursor(event: dict) -> str:
    """把一条事件编成「下一页从它之后取」的游标。

    编进去的是**排序键本身**，不是偏移量 —— 理由见模块头部。
    """
    try:
        stamp = _cursor_time(event.get("created_at"))
    except ValueError as exc:
        raise ValueError("游标格式不正确") from exc
    return CURSOR_SEPARATOR.join(
        (
            stamp,
            str(event.get("source") or ""),
            str(event.get("seq") if event.get("seq") is not None else ""),
        )
    )


def parse_cursor(raw: str) -> tuple[datetime, str, int] | None:
    """把游标字符串还原成排序键。空串 = 没有游标（取第一页）。

    格式不合法一律 ValueError，由路由层转成 422 —— **不能让一个手改过的
    query string 变成 500**，那会让人以为服务坏了。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    parts = text.split(CURSOR_SEPARATOR)
    if len(parts) != 3:
        raise ValueError("游标格式不正确")
    created_at, source, seq = (part.strip() for part in parts)
    if source not in _SOURCES:
        raise ValueError("游标格式不正确")
    try:
        return (datetime.fromisoformat(created_at), source, int(seq))
    except ValueError as exc:
        raise ValueError("游标格式不正确") from exc


class AuditTimeline:
    """只读读模型：不拥有任何表，只查别人的。"""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip():
            raise RuntimeError("POSTGRES_DSN is required for the audit timeline")
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 8)),
            open=True,
            timeout=5,
            kwargs={"row_factory": dict_row},
        )
        logger.info("审计时间线读模型就绪")

    def close(self) -> None:
        self._pool.close()

    def for_owner(
        self,
        owner_user_id: str,
        limit: int = DEFAULT_LIMIT,
        cursor: tuple[datetime, str, int] | None = None,
    ) -> list[dict]:
        """取这个人的时间线，最新在前。cursor 为 None 时取第一页。

        ⚠️ 本方法**不去重、不合并、不排序**，全部交给 SQL —— 这是本模块
        存在的理由（见头部），别在这里「顺手」再排一次。
        """
        limit = max(1, min(int(limit), MAX_LIMIT))
        params = {
            "owner": owner_user_id,
            "limit": limit,
            "cursor_at": cursor[0] if cursor else None,
            "cursor_source": cursor[1] if cursor else None,
            "cursor_seq": cursor[2] if cursor else None,
        }
        with self._pool.connection() as connection:
            rows = connection.execute(_TIMELINE_SQL, params).fetchall()
        return [_serialize(row) for row in rows]


_model: AuditTimeline | None = None
_model_lock = threading.Lock()


def get_audit_timeline() -> AuditTimeline:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = AuditTimeline()
    return _model


def get_audit_timeline_or_none() -> AuditTimeline | None:
    try:
        return get_audit_timeline()
    except Exception as exc:
        logger.error("审计时间线不可用: %s", type(exc).__name__)
        return None


def close_audit_timeline() -> None:
    global _model
    with _model_lock:
        if _model is not None:
            _model.close()
            _model = None
