"""向量库：连接独立的 Chroma 服务（HTTP），不再嵌在 API 进程内。

【2026-10-07 改造：嵌入式 PersistentClient → 独立 Chroma 服务】

原方案（嵌入式）的理由是"1 GiB 机型下多一个容器就多 200MB+ 常驻"。
机型已升级为 Standard_B2as_v2（2 vCPU / 8 GiB），这个前提已经不成立，
而嵌入式带来的四个代价开始显现：

  1) 内存与 API 绑死 —— HNSW 索引、sqlite 连接、chunk 缓存全在 API 进程里，
     文档规模一大，API 内存就跟着涨，无法单独限额；独立后 API 只持 HTTP 连接。
  2) 并发被锁死在单 worker —— 嵌入式模式下 uvicorn 多开 worker 会各自打开
     同一份 sqlite，写冲突风险高，等于永久放弃水平扩展；独立服务天然支持多客户端。
  3) 备份/迁移/升级都不独立 —— 向量库此前是 API 的一个"副作用"，无法单独
     快照与升级；现在它是一个有明确生命周期和数据目录的服务。
  4) 阶段二 Agent 需要多进程/多工具共享同一向量库，嵌入式做不到。

代价：多一个容器，实际常驻约 200~300MB。8 GiB 机型下完全可接受。
"""
import logging
from functools import lru_cache

import chromadb
from langchain_chroma import Chroma

from .config import get_settings
from .llm import get_embeddings

logger = logging.getLogger(__name__)


@lru_cache
def get_vectorstore() -> Chroma:
    s = get_settings()
    logger.info(
        "Chroma server = %s:%s (collection=%s)",
        s.chroma_host, s.chroma_port, s.collection_name,
    )
    # HttpClient 是官方推荐的服务端接入方式。注意：langchain 的 Chroma 包装
    # 在 add_documents / similarity_search 时仍由【本进程】计算 embedding，
    # 再把向量发给 Chroma 存储 —— 百炼 embedding 的调用路径完全不变。
    client = chromadb.HttpClient(
        host=s.chroma_host,
        port=s.chroma_port,
        ssl=s.chroma_ssl,
    )
    return Chroma(
        client=client,
        collection_name=s.collection_name,
        embedding_function=get_embeddings(),
        # 与迁移前保持一致，否则检索打分口径会变
        collection_metadata={"hnsw:space": "cosine"},
    )
