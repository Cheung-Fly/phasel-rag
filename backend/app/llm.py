"""LLM / Embedding 工厂。

这是整个阶段一最关键的解耦点：
业务代码只调用 get_chat_model() / get_embeddings()，
把 LLM_PROVIDER 从 cloud 改成 local，就能切到 Llama.cpp，其它代码零改动。

2026-10-15 供应商解耦：这两个工厂不再共用一个 key / base_url，而是各读各的
配置（chat → LLM_*，向量化 → EMBEDDING_*）。因此换供应商只需改 .env。
本文件只消费 Settings 上【已经解析好】的字段，不做任何回退判断 ——
回退链（含旧 DASHSCOPE_* 兼容）统一在 config.Settings 里完成，见该文件的
_resolve_provider_credentials()。把回退留在这里，就会出现「谁才是真相源」
的分裂，是这类重构最容易留下的半迁移。

两者都走 OpenAI 兼容协议，所以只用 langchain-openai 一个依赖包。
"""
import logging
from urllib.parse import urlsplit

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

from .config import get_settings

logger = logging.getLogger(__name__)


def _safe_endpoint(url: str) -> str:
    """只回显 scheme://host[:port]，丢掉可能内嵌的 user:pass@ 凭据。

    日志里带上端点主机是排障刚需 —— 「我到底打到了哪家供应商」是换供应商时
    第一个要回答的问题。但 base_url 理论上允许内嵌账号密码，直接整串打印
    等于把凭据写进日志，所以这里显式裁掉。
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


def get_chat_model(temperature: float = 0.2) -> BaseChatModel:
    s = get_settings()

    from langchain_openai import ChatOpenAI

    if s.llm_provider == "local":
        # 阶段三：llama.cpp server 暴露 OpenAI 兼容接口
        logger.info("LLM = local llama.cpp @ %s", s.local_llm_base_url)
        return ChatOpenAI(
            model=s.local_llm_model,
            base_url=s.local_llm_base_url,
            api_key="sk-no-key-required",  # llama.cpp 不校验
            temperature=temperature,
            max_tokens=1024,
        )

    # ★ 报错要指名道姓，并同时列出旧变量名 —— 迁移期的用户可能只认得旧名，
    #   只说「LLM_API_KEY 未配置」会让人以为自己的 DASHSCOPE_API_KEY 丢了。
    if not s.llm_api_key:
        raise RuntimeError(
            "LLM_PROVIDER=cloud 但未配置 chat 凭证：请设置 LLM_API_KEY"
            "（旧名 DASHSCOPE_API_KEY 仍兼容）"
        )
    if not s.llm_base_url:
        raise RuntimeError(
            "未配置 chat 端点：请设置 LLM_BASE_URL"
            "（旧名 DASHSCOPE_BASE_URL 仍兼容）"
        )

    logger.info("LLM = %s @ %s", s.llm_model, _safe_endpoint(s.llm_base_url))
    return ChatOpenAI(
        model=s.llm_model,
        base_url=s.llm_base_url,
        api_key=s.llm_api_key,
        temperature=temperature,
        max_tokens=1024,
        max_retries=2,
        timeout=60,
    )


def get_embeddings() -> Embeddings:
    s = get_settings()
    # 这两个字段在 Settings 里已解析完毕：未显式配置时会自动沿用 chat 的
    # key / base_url。所以这里为空 = 真的什么都没配，报错必须给出全部可选变量名。
    if not s.embedding_api_key:
        raise RuntimeError(
            "未配置向量化凭证：请设置 EMBEDDING_API_KEY"
            "（留空时会自动沿用 LLM_API_KEY / 旧名 DASHSCOPE_API_KEY）"
        )
    if not s.embedding_base_url:
        raise RuntimeError(
            "未配置向量化端点：请设置 EMBEDDING_BASE_URL"
            "（留空时会自动沿用 LLM_BASE_URL / 旧名 DASHSCOPE_BASE_URL）"
        )

    from langchain_openai import OpenAIEmbeddings

    logger.info(
        "Embeddings = %s @ %s (dim=%d, batch=%d)",
        s.embedding_model,
        _safe_endpoint(s.embedding_base_url),
        s.embedding_dim,
        s.embedding_batch_size,
    )
    return OpenAIEmbeddings(
        model=s.embedding_model,
        base_url=s.embedding_base_url,
        api_key=s.embedding_api_key,
        dimensions=s.embedding_dim,
        # ★ 单次行数上限是【供应商能力】，各家不同：百炼 text-embedding-v4 = 10，
        #   智谱 embedding-3 = 64。默认取 10（对所有已知供应商都安全）；要在
        #   智谱上提速请在 .env 里显式设 EMBEDDING_BATCH_SIZE=64。
        #   langchain 默认是 1000，不覆盖就一定会在上传较大文档时报错。
        chunk_size=s.embedding_batch_size,
        # 关闭 tiktoken 预分词：这些端点是中文模型，用 OpenAI 的 tokenizer
        # 估算长度既不准也没必要，关掉可省一次 CPU 开销。
        check_embedding_ctx_length=False,
        # 超时/重试改为配置项：入库是批量串行调用，超时值直接决定
        # 「一份文档最多可能挂多久」，不该写死在代码里。
        max_retries=s.embedding_max_retries,
        timeout=s.embedding_timeout_seconds,
    )
