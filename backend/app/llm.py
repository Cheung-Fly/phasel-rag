"""LLM / Embedding 工厂。

这是整个阶段一最关键的解耦点：
业务代码只调用 get_chat_model() / get_embeddings()，
阶段三把 LLM_PROVIDER 从 cloud 改成 local，就能切到 Llama.cpp，其它代码零改动。

两者都走 OpenAI 兼容协议，所以只用 langchain-openai 一个依赖包。
"""
import logging

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel

from .config import get_settings

logger = logging.getLogger(__name__)


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

    if not s.dashscope_api_key:
        raise RuntimeError("LLM_PROVIDER=cloud 但 DASHSCOPE_API_KEY 未配置")

    logger.info("LLM = dashscope:%s", s.llm_model)
    return ChatOpenAI(
        model=s.llm_model,
        base_url=s.dashscope_base_url,
        api_key=s.dashscope_api_key,
        temperature=temperature,
        max_tokens=1024,
        max_retries=2,
        timeout=60,
    )


def get_embeddings() -> Embeddings:
    s = get_settings()
    if not s.dashscope_api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置（向量化必需）")

    from langchain_openai import OpenAIEmbeddings

    logger.info("Embeddings = dashscope:%s (dim=%d, batch=%d)",
                s.embedding_model, s.embedding_dim, s.embedding_batch_size)
    return OpenAIEmbeddings(
        model=s.embedding_model,
        base_url=s.dashscope_base_url,
        api_key=s.dashscope_api_key,
        dimensions=s.embedding_dim,
        # ★ 百炼单次上限 10 行。langchain 默认 1000，不改就一定会在
        #   上传较大文档时报错。
        chunk_size=s.embedding_batch_size,
        # 关闭 tiktoken 预分词：百炼是中文模型，用 OpenAI 的 tokenizer
        # 估算长度既不准也没必要，关掉可省一次 CPU 开销。
        check_embedding_ctx_length=False,
        max_retries=2,
        timeout=60,
    )
