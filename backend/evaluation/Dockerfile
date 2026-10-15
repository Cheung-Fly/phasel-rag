FROM phase1-rag-api:0.1.0

USER root
COPY evaluation/requirements-ragas.txt /tmp/requirements-ragas.txt
RUN pip install --no-cache-dir -r /tmp/requirements-ragas.txt

# ★ 兼容垫片：ragas 0.4.3 仍在 ragas/llms/base.py 顶部从
#   langchain_community.chat_models.vertexai 导入 ChatVertexAI，
#   而 langchain-community >= 0.4 已删除该模块，使任何 `import ragas` 直接失败，
#   RAGAS 评测因此从未跑通。ragas 只在 MULTIPLE_COMPLETION_SUPPORTED 能力清单里
#   引用这个类（isinstance 判断），本部署也用不到 Vertex AI，故补一个惰性占位即可。
#   只作用于本评测镜像；运行时 api 镜像不受影响。
COPY evaluation/compat/vertexai.py /tmp/vertexai_shim.py
RUN python -c "import os, shutil, langchain_community; d = os.path.dirname(langchain_community.__file__); p = os.path.join(d, 'chat_models', 'vertexai.py'); shutil.copy('/tmp/vertexai_shim.py', p); print('installed vertexai shim ->', p)"

WORKDIR /app
COPY evaluation ./evaluation
USER appuser

ENTRYPOINT ["python", "-m", "evaluation.evaluate_ragas"]
