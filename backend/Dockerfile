FROM python:3.12-slim

# B2ts_v2 (2 vCPU / 1 GiB)：不装编译工具链，依赖全部用 wheels
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
    mkdir -p /app/data/chroma /app/data/uploads && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 单 worker：瓶颈是内存(1GiB)和 embedding API 的 IO，不是 CPU。
# 多开 worker 会按倍数放大内存占用，而吞吐几乎不变 —— 不划算。
# 若日后确认内存有余量，可改为 2（2 vCPU），但必须先压测。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "30"]
