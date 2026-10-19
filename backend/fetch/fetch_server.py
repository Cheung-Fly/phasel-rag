import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from mcp_server_fetch.server import create_server


def main() -> None:
    server = create_server()
    uvicorn.run(
        server.streamable_http_app(
            transport_security=TransportSecuritySettings(
                allowed_hosts=["mcp-fetch:3000"],
            ),
        ),
        host="0.0.0.0",
        port=3000,
        access_log=False,
    )


if __name__ == "__main__":
    main()
