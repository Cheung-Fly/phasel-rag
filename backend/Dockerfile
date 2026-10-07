FROM python:3.12-slim

# AMD EPYC 7763 (2 vCPU / 8 GiB)：不装编译工具链，依赖全部用 wheels
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖，利用 Docker 层缓存：改代码不必重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 非 root 运行
RUN useradd -m -u 10001 appuser && \
    mkdir -p /app/data/uploads && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 单 worker：瓶颈是 embedding API 的 IO，不是 CPU。
# 注意：Chroma 已外迁为独立服务，「多 worker 会各自加载一份索引」这个旧约束
# 已经消失，理论上可以多开；但 2 vCPU 上多 worker 只会互相抢 CPU 与内存，
# 所以仍保持 1 个。等有真实并发压测数据再调整。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
