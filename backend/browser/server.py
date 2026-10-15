import threading

import uvicorn
from fastapi import FastAPI

from mcp_server import mcp

app = FastAPI(title="agent-browser bridge")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def run_mcp_server():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    threading.Thread(target=run_mcp_server, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8765)
