FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir \
      "mcp-server-fetch==2026.10.19" \
      "uvicorn>=0.31,<1" && \
    useradd --system --create-home --uid 10002 fetch

WORKDIR /app
COPY fetch_server.py .
USER fetch

EXPOSE 3000
CMD ["python", "fetch_server.py"]
