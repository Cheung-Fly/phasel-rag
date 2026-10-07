"""集中配置：所有可变项都从环境变量来，代码里不写死任何供应商。

当前供应商：阿里云百炼（DashScope 的 OpenAI 兼容模式）
阶段三可切到本地 llama.cpp（同样是 OpenAI 兼容协议），只改环境变量。

2026-10-07 架构调整：存储层由「嵌入式 Chroma + 可选 Upstash」改为
「独立 Chroma 服务 + 本机 Redis（热缓存）+ 本机 PostgreSQL（真相源）」。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---------- LLM 供应商 ----------
    # cloud = 阿里百炼云端；local = 阶段三的 llama.cpp 本地模型
    llm_provider: str = "cloud"

    # ---------- 阿里百炼（DashScope）----------
    # 一个 key 同时用于 chat 和 embedding
    dashscope_api_key: str = ""
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # chat 模型：qwen-plus 质量与成本平衡；省钱可换 qwen-turbo
    llm_model: str = "qwen-plus"

    # embedding 模型与维度
    # text-embedding-v4 支持 2048/1536/1024/768/512/256/128/64
    embedding_model: str = "text-embedding-v4"
    embedding_dim: int = 1024

    # ★ 百炼 OpenAI 兼容接口限制：embedding 单次最多 10 行输入。
    #   设大了会直接报错，绝不要改成 1000（langchain 的默认值）。
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

    # ---------- 存储：会话（PG 真相源 + Redis 热缓存）----------
    # Redis  = 热缓存，可丢，丢了自动从 PG 回填
    # Postgres = 真相源，不可丢
    redis_url: str = ""
    memory_ttl_seconds: int = 60 * 60 * 24 * 7  # 热缓存 7 天（不影响 PG 永久存档）

    postgres_dsn: str = ""
    pg_pool_min: int = 1
    pg_pool_max: int = 4

    # ---------- 检索参数 ----------
    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieval_top_k: int = 4

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

    # ---------- HTTP 层护栏 ----------
    # CORS 白名单：逗号分隔的完整源，例如 https://rag.example.com
    # 留空 = 不允许任何跨域请求。前端由本服务自带（/ui，与 API 同源），
    # 因此留空不影响正常使用；只有要用 Vue dev server 跨域直连时才需要填。
    # 原实现是 allow_origins=["*"]，等于任意网站都能借访客浏览器调本 API。
    cors_origins: str = ""

    # 单文件上传上限（MB）。这只是第一道粗筛，真正的闸门是上面的 ingest_max_chunks。
    max_upload_mb: int = 50

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
