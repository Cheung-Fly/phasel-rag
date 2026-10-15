"""Compatibility shim for the evaluation image only.

背景
----
``ragas==0.4.3`` 在 ``ragas/llms/base.py`` 顶部硬性执行::

    from langchain_community.chat_models.vertexai import ChatVertexAI

但本项目 API 镜像使用的 ``langchain-community>=0.4`` 已经删除了
``chat_models.vertexai`` 模块（Vertex AI 相关实现迁往独立的
``langchain-google-vertexai`` 包）。于是任何 ``import ragas`` 都会失败，
RAGAS 评测因此无法运行。

为什么可以安全打桩
------------------
在整个 ragas 源码中，``ChatVertexAI`` 只出现在 ``MULTIPLE_COMPLETION_SUPPORTED``
这个用于 ``isinstance`` 判断的能力清单里；本部署使用 ChatOpenAI，永远不会走到
Vertex AI 分支。因此这里提供一个惰性占位类即可让导入通过，不改变任何评测行为。

该文件由 ``backend/evaluation/Dockerfile`` 复制进 langchain_community 包内，
只影响 ragas-eval 镜像；运行时 API 镜像完全不受影响。
"""


class ChatVertexAI:  # pragma: no cover - 惰性占位，不参与实际调用
    """Placeholder that keeps ``ragas.llms`` importable."""

    pass
