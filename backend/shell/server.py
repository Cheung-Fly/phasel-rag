import threading

import uvicorn
from fastapi import FastAPI

from mcp_server import mcp

app = FastAPI(title="shell-executor bridge")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def run_mcp_server():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    threading.Thread(target=run_mcp_server, daemon=True).start()
    # ★ uvicorn 用 8768，MCP 用 8767 —— 与 browser 容器同构（MCP 与 health
    #   服务端口分开）。若都绑 8767，端口冲突会让 MCP 的 /mcp 返回 404。
    uvicorn.run(app, host="0.0.0.0", port=8768)
