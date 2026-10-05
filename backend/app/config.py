"""集中配置：所有可变项都从环境变量来，代码里不写死任何供应商。

当前供应商：阿里云百炼（DashScope 的 OpenAI 兼容模式）
阶段三可切到本地 llama.cpp（同样是 OpenAI 兼容协议），只改环境变量。
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

    # ---------- 本地（阶段三才用）----------
    local_llm_base_url: str = "http://llama:8080/v1"
    local_llm_model: str = "qwen2.5-1.5b-instruct"

    # ---------- 存储 ----------
    chroma_dir: str = "/app/data/chroma"
    upload_dir: str = "/app/data/uploads"
    collection_name: str = "phase1_docs"

    # Upstash Redis（未配置时自动降级为进程内记忆）
    redis_url: str = ""
    memory_ttl_seconds: int = 60 * 60 * 24 * 7

    # ---------- 检索参数 ----------
    chunk_size: int = 800
    chunk_overlap: int = 120
    retrieval_top_k: int = 4

    def memory_backend(self) -> str:
        return "redis" if self.redis_url.strip() else "inproc"


@lru_cache
def get_settings() -> Settings:
    return Settings()
