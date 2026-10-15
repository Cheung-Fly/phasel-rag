"""集中配置：所有可变项都从环境变量来，代码里不写死任何供应商。

2026-10-15 供应商解耦：chat 与 embedding 不再共用同一把 key / base_url。
两者各自独立配置，可指向同一家（最常见），也可指向两家。旧的
DASHSCOPE_* 变量仍被兼容读取，但只是回退，不再是一等公民。

阶段三可切到本地 llama.cpp（同样是 OpenAI 兼容协议），只改环境变量。

2026-10-07 架构调整：存储层由「嵌入式 Chroma + 可选 Upstash」改为
「独立 Chroma 服务 + 本机 Redis（热缓存）+ 本机 PostgreSQL（真相源）」。
"""
import logging
from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# 旧版 DASHSCOPE_BASE_URL 的内置默认值。2026-10-15 之前它写在字段默认值上，
# 于是「只填了 DASHSCOPE_API_KEY、没填 base_url」的旧 .env 依赖它生效。
# 现在字段默认值改为空串（避免把新供应商静默指向百炼），改由下面的
# _resolve_provider_credentials() 在「检测到旧变量」时显式重建同一行为。
_LEGACY_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------- LLM 供应商 ----------
    # cloud = 任意 OpenAI 兼容云端端点（百炼 / 智谱 / 硅基流动 / DeepSeek…）；
    # local = 阶段三的 llama.cpp 本地模型。
    llm_provider: str = "cloud"

    # ---------- 供应商凭证（2026-10-15 解耦：chat 与 embedding 各配各的）----------
    # ★ 为什么必须拆开：原实现里 chat 与 embedding 共用同一个 key 与 base_url
    #   （旧 llm.py:52-62），后果是「供应商必须同时提供 chat 与 embedding 接口」。
    #   换任何一家只有 chat 的服务（如 DeepSeek），整条 RAG 链路直接不可用 ‑‑
    #   而报错点在检索/入库，离配置很远，排查成本极高。
    #   拆开后两者可以指向同一家（最常见），也可以指向两家。
    #
    # 回退链（新变量优先；旧变量保留兼容；实现见 _resolve_provider_credentials()）：
    #   llm_api_key        ← LLM_API_KEY        ‖ DASHSCOPE_API_KEY
    #   llm_base_url       ← LLM_BASE_URL       ‖ DASHSCOPE_BASE_URL
    #   embedding_api_key  ← EMBEDDING_API_KEY  ‖ llm_api_key
    #   embedding_base_url ← EMBEDDING_BASE_URL ‖ llm_base_url
    #
    # 语义：embedding 的 key / base_url「默认跟随 chat」，这样只配一家供应商时
    # 不必重复填写；一旦显式给了 EMBEDDING_*，就以它为准。
    #
    # ★ 失败方向是刻意的：全部留空时两个 key 都为空串，get_chat_model() /
    #   get_embeddings() 会抛出指名道姓的 RuntimeError，而不是拿一个「看起来
    #   能用」的地址去发请求、最后在对话时报一个看不懂的 401。
    llm_api_key: str = ""
    llm_base_url: str = ""
    embedding_api_key: str = ""
    embedding_base_url: str = ""

    # ---------- 旧变量（deprecated，仅作回退，请勿在新部署中使用）----------
    # 保留的目的只有一个：让已经写好的 .env 不改也能照常运行（幂等迁移）。
    # 注意 dashscope_base_url 的默认值已由「写死的百炼地址」改为空串，
    # 旧行为在 _resolve_provider_credentials() 里重建 —— 见文件顶部常量注释。
    dashscope_api_key: str = ""
    dashscope_base_url: str = ""

    # chat 模型：qwen-plus 质量与成本平衡；省钱可换 qwen-turbo
    # ★ 换供应商时必须同步改这里，否则会把 A 家的模型名发给 B 家端点。
    #   （首次运行向导会接管这件事，见 docs/Muse化路线图.md 阶段 E.3 #1）
    llm_model: str = "qwen-plus"

    # embedding 模型与维度
    # text-embedding-v4 支持 2048/1536/1024/768/512/256/128/64
    # ★ 换 embedding 模型 = 已入库向量全部作废（向量空间不同，不是维度不同）。
    #   即便新旧维度都是 1024 也必须重新入库，否则检索结果看似正常实则错乱。
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024

    # embedding 单次请求的最大输入行数。这是【供应商能力】，各家不同：
    #   百炼 text-embedding-v4 = 10；智谱 embedding-3 = 64。
    # 默认取 10 是刻意的偏保守值 —— 它对所有已知供应商都安全，只是比智谱的
    # 上限慢。要提速请在 .env 里显式设大（EMBEDDING_BATCH_SIZE=64）。
    # 绝不要改成 1000（langchain 的默认值），那会直接 400。
    embedding_batch_size: int = 10

    # embedding 调用的超时与重试（2026-10-07 由写死的 60/2 提到配置里）
    embedding_timeout_seconds: int = 60
    embedding_max_retries: int = 2

    # ---------- 本地（阶段三才用）----------
    local_llm_base_url: str = "http://llama:8080/v1"
    local_llm_model: str = "qwen2.5-1.5b-instruct"

    # ---------- 存储：向量库（独立 Chroma 服务）----------
    # 2026-10-07 起 Chroma 作为 compose 里的独立容器运行，API 通过 HTTP 访问，
    # 不再在进程内打开 sqlite。
    # 为什么删掉原来的 chroma_dir：嵌入式模式已取消，留一个没人读的路径
    # 只会让后来者以为还有一份本地向量数据。
    chroma_host: str = "chroma"
    chroma_port: int = 8000
    chroma_ssl: bool = False
    collection_name: str = "phase1_docs"

    # 上传文件落盘目录（向量数据已不在 API 这一侧）
    upload_dir: str = "/app/data/uploads"

    # ---------- 前端 ----------
    # 2026-10-07 起本服务**不再托管任何前端**。
    # 原本这里是 frontend_dir，指向 compose 挂进来的 ./frontend，
    # 由本进程通过 StaticFiles 挂到 /ui 发出去。前端迁到 Vue3 + Vite 后，
    # 产物由独立的 nginx 容器托管，API 只提供接口 —— 职责切开，
    # 改前端不必碰 API 镜像，API 出问题也不影响静态页面能否打开。
    # 保留一个没人读的路径只会让后来者以为「这里还有一条前端通路」，
    # 所以连同 StaticFiles 挂载一起去掉了。

    # ---------- 存储：会话（PG 真相源 + Redis 热缓存）----------
    # Redis  = 热缓存，可丢，丢了自动从 PG 回填
    # Postgres = 真相源，不可丢
    redis_url: str = ""
    memory_ttl_seconds: int = 60 * 60 * 24 * 7  # 热缓存 7 天（不影响 PG 永久存档）

    postgres_dsn: str = ""
    pg_pool_min: int = 1
    pg_pool_max: int = 4
    credential_encryption_key: str = ""
    auth_cookie_secure: bool = False
    auth_session_ttl_seconds: int = 43_200

    # ---------- 认证模式（2026-10-14 新增：单用户本地模式）----------
    # session = 需要账号密码登录，多用户互相隔离（原行为）
    # local   = 免登录：任何请求都直接归属到 local_owner_username 这一个身份
    #
    # ★ 为什么默认仍是 session（安全默认）：
    #   默认值会在「忘了配 .env」这种最常见的部署事故里生效。若默认 local，
    #   一次疏忽就得到一个无鉴权服务；默认 session 则最坏只是「要登录」。
    #   免登录必须是一次显式选择 —— 在 .env 里写 AUTH_MODE=local。
    auth_mode: str = "session"

    # 本地模式下自动使用的身份（用户名，非 user_id）。
    # ★ 这台机器上已存在数据时，这个值必须与既有用户名一致（本机为 admin），
    #   否则凭证库、会话会「看起来全空了」—— 数据一条没丢，只是换了 owner，
    #   而 owner 隔离是设计如此，不是 bug。
    #   该身份不存在时会被自动创建（口令为随机值，本地模式不使用口令登录）。
    local_owner_username: str = "admin"

    @field_validator("auth_mode")
    @classmethod
    def _validate_auth_mode(cls, value: str) -> str:
        # 不做静默回退：写错值时若悄悄按 session 处理，现象是「明明配了 local
        # 却还在要登录」，排查成本远高于启动即报错。
        normalized = value.strip().lower()
        if normalized not in {"session", "local"}:
            raise ValueError("AUTH_MODE 只能是 session 或 local")
        return normalized

    @model_validator(mode="after")
    def _resolve_provider_credentials(self) -> "Settings":
        """把旧变量名回填到中性变量，使 s.llm_api_key 等**恒等于实际生效值**。

        为什么不写成 @property：日志、/health、llm.py、evaluation 若各自处理
        回退链，就会出现「同一份配置在不同位置解析出不同结果」的隐患 ——
        典型事故是 health 报 A 供应商、实际请求却打到 B 供应商。
        回填后只有一个真相源，任何位置读到的都一致。
        """
        # 顺手去掉两端空白：.env 里多打一个空格，是最常见的「key 明明对却 401」。
        self.llm_api_key = self.llm_api_key.strip()
        self.llm_base_url = self.llm_base_url.strip()
        self.embedding_api_key = self.embedding_api_key.strip()
        self.embedding_base_url = self.embedding_base_url.strip()
        self.dashscope_api_key = self.dashscope_api_key.strip()
        self.dashscope_base_url = self.dashscope_base_url.strip()

        legacy_used = False
        if not self.llm_api_key:
            self.llm_api_key = self.dashscope_api_key
            legacy_used = self.llm_api_key != ""
        if not self.llm_base_url:
            if self.dashscope_base_url:
                self.llm_base_url = self.dashscope_base_url
            elif self.dashscope_api_key:
                # 旧 .env 只填了 key：重建「字段默认值曾内置百炼地址」的历史行为。
                # 这条只在检测到旧变量时才走，所以新部署不会被静默指向百炼。
                self.llm_base_url = _LEGACY_DASHSCOPE_BASE_URL
            legacy_used = legacy_used or bool(self.llm_base_url)

        if legacy_used:
            logger.warning(
                "检测到旧变量 DASHSCOPE_*，已作为回退生效；建议迁移到 "
                "LLM_API_KEY / LLM_BASE_URL（chat）与 EMBEDDING_*（向量化）"
            )

        # embedding 默认跟随 chat：只配一家供应商时不必重复填写。
        if not self.embedding_base_url:
            self.embedding_base_url = self.llm_base_url
        if not self.embedding_api_key:
            if self.embedding_base_url != self.llm_base_url:
                # 端点不同却沿用 chat 的 key —— 大概率是漏填，但不阻断：
                # 「同一把 key 走两个端点」确实存在，属合法用法（放行 + 标记）。
                logger.warning(
                    "EMBEDDING_BASE_URL(%s) 与 chat 端点(%s) 不同，但未配置 "
                    "EMBEDDING_API_KEY：将沿用 chat 的 key，若两家不通用会 401",
                    self.embedding_base_url,
                    self.llm_base_url,
                )
            self.embedding_api_key = self.llm_api_key
        return self

    # ---------- 检索参数 ----------
    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieval_top_k: int = 4
    # Initial calibration against backend/evaluation/retrieval_cases.json; re-evaluate
    # these thresholds when the knowledge base gains new document categories.
    retrieval_candidate_k: int = Field(default=12, ge=1, le=100)
    retrieval_min_relevance: float = Field(default=0.42, ge=0, le=1)
    retrieval_min_lexical_coverage: float = Field(default=0.30, ge=0, le=1)
    retrieval_max_lexical_chunks: int = Field(default=10_000, ge=1)

    # ---------- 入库护栏（2026-10-07 按服务器规格重定）----------
    # 背景：2026-10-07 测试中，22MB 纯文本被切成 33002 个片段并疯狂调用 embedding
    # 接口，直接把容器健康检查打挂。根因是 MAX_UPLOAD_MB 只拦「文件体积」，
    # 完全没拦「切片数量」—— 而后者才是真正的成本与负载单位。
    # 下面三个值把闸门从「体积」挪到「工作量」，依据是 2 vCPU / 8 GiB + 百炼单次 10 行：
    #
    #   ingest_max_chars = 2,000,000
    #       解析后字符数上限。约等价于 3000 个切片（chunk_size=800），
    #       在切片之前就拦下明显过大的文件，省掉一次全量切片。
    #   ingest_max_chunks = 1200
    #       切片数硬上限。1200 片 = 120 次 embedding 调用（每批 10 行）；
    #       按单次 0.5~1.5 秒估算耗时约 1~3 分钟，已是交互能忍受的上限。
    #       再大就该走后台任务队列（阶段二），而不是让 HTTP 请求一直挂着。
    #   ingest_max_concurrent = 1
    #       同时进行的入库任务数。2 vCPU 上并行入库只会互相抢 CPU 并触发云端
    #       接口限流，收益为负；串行反而更快更稳。
    ingest_max_chars: int = 2_000_000
    ingest_max_chunks: int = 1200
    ingest_max_concurrent: int = 1

    # ---------- 后台任务队列（2026-10-14 阶段 B 新增）----------
    # 文档入库从「同步等待」改为「入队 + 后台执行」后新增的三个参数。
    #
    #   task_worker_poll_seconds
    #       worker 空闲时的轮询间隔。队列建在 PG（而不是消息中间件）上，
    #       所以只能轮询。2 秒的代价是一次走索引的 SELECT，在 2 vCPU 上可忽略；
    #       调大只会让「上传后多久开始跑」变慢，调小则是白烧 CPU credit。
    #   task_lease_seconds
    #       任务租约时长，同时是「worker 崩溃后任务多久被重新领取」的上界。
    #       心跳间隔取其 1/4 且不超过 5 秒，因此活着的 worker 不会被误判为死亡。
    #   task_max_attempts
    #       单个任务最多被领取几次。用于兜住「每次都在同一处打崩 worker」的
    #       任务 —— 没有这个上限，它就变成 worker 反复重启的死循环。
    task_worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    task_lease_seconds: int = Field(default=60, ge=10, le=3600)
    task_max_attempts: int = Field(default=3, ge=1, le=10)

    # ---------- HTTP 层护栏 ----------
    # CORS 白名单：逗号分隔的完整源，例如 http://localhost:5173
    # 留空 = 不允许任何跨域请求。
    # ★ 正常情况下这里就该是空的：开发期由 Vite 的 dev server 代理
    #   /api 转发过来（浏览器视角同源），生产期由 nginx 反向代理
    #   （同样同源）。两边的跨域问题都在代理层解决了，不需要浏览器
    #   层面放开。只有在「前端直连后端、中间没有代理」的特殊场景
    #   （例如把前端部署到另一个域名）才需要在这里填白名单。
    # 原实现是 allow_origins=["*"]，等于任意网站都能借访客浏览器调本 API。
    cors_origins: str = ""

    # 单文件上传上限（MB）。这只是第一道粗筛，真正的闸门是上面的 ingest_max_chunks。
    max_upload_mb: int = 50

    # HTTP runtime safeguards. These are process-local by design; use an API
    # gateway or Redis-backed limiter when scaling to multiple API replicas.
    api_rate_limit: int = Field(default=120, ge=1, le=10_000)
    api_rate_window_seconds: int = Field(default=60, ge=1, le=3600)
    browser_circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    browser_circuit_recovery_seconds: int = Field(default=30, ge=1, le=3600)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_local_auth(self) -> bool:
        """是否处于单用户本地模式（免登录）。"""
        return self.auth_mode == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
