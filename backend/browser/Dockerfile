FROM node:24-bookworm

ENV NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    CHROME_PATH=/usr/bin/chromium \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium python3 python3-pip ca-certificates fonts-noto-cjk fontconfig && \
    npm install -g agent-browser && \
    fc-cache -f && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /browser
COPY browser/requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY browser/mcp_server.py browser/server.py ./
RUN mkdir -p /var/cache/agent-browser && useradd --create-home browser && chown -R browser:browser /browser /var/cache/agent-browser
USER browser
EXPOSE 8766
CMD ["python3", "server.py"]
