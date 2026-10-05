"""向量库：Chroma 以嵌入式 PersistentClient 方式跑在 API 进程内。

为什么不用独立 Chroma 容器：1 GiB 内存下多一个容器就多 200MB+ 常驻，
嵌入式模式省掉一整个容器的开销，且单机单进程不存在并发写入竞争。
"""
import logging
from functools import lru_cache

from langchain_chroma import Chroma

from .config import get_settings
from .llm import get_embeddings

logger = logging.getLogger(__name__)


@lru_cache
def get_vectorstore() -> Chroma:
    s = get_settings()
    logger.info("Chroma persist dir = %s", s.chroma_dir)
    return Chroma(
        collection_name=s.collection_name,
        embedding_function=get_embeddings(),
        persist_directory=s.chroma_dir,
        collection_metadata={"hnsw:space": "cosine"},
    )
