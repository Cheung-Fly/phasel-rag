"""Owner-scoped encrypted MCP server configuration."""
import json
import logging
import uuid

from cryptography.fernet import Fernet, InvalidToken
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

from .config import get_settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    server_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    transport TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config_ciphertext TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_user_id, name)
)
"""


class McpStore:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip():
            raise RuntimeError("POSTGRES_DSN is required for MCP configuration")
        if not settings.credential_encryption_key.strip():
            raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is required for MCP configuration")
        self._fernet = Fernet(settings.credential_encryption_key.strip().encode())
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 8)),
            open=True,
            timeout=5,
            kwargs={"row_factory": dict_row},
        )
        try:
            with self._pool.connection() as connection:
                connection.execute(_SCHEMA)
        except Exception:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    def _decrypt(self, ciphertext: str) -> dict:
        try:
            value = json.loads(self._fernet.decrypt(ciphertext.encode()).decode())
        except (InvalidToken, ValueError, TypeError) as exc:
            raise RuntimeError("MCP 配置无法解密") from exc
        if not isinstance(value, dict):
            raise RuntimeError("MCP 配置格式无效")
        return value

    def _serialize(self, row: dict, *, include_config: bool = False) -> dict:
        result = {
            "server_id": row["server_id"],
            "name": row["name"],
            "transport": row["transport"],
            "enabled": row["enabled"],
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
        }
        if include_config:
            result["config"] = self._decrypt(row["config_ciphertext"])
        return result

    def list(self, owner_user_id: str) -> list[dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM mcp_servers WHERE owner_user_id = %s ORDER BY name",
                (owner_user_id,),
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def configs(self, owner_user_id: str) -> dict[str, dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM mcp_servers WHERE owner_user_id = %s AND enabled",
                (owner_user_id,),
            ).fetchall()
        return {row["name"]: self._decrypt(row["config_ciphertext"]) for row in rows}

    def save(self, owner_user_id: str, name: str, config: dict) -> dict:
        clean_name = " ".join(name.split())
        if not clean_name or len(clean_name) > 80:
            raise ValueError("MCP 服务名称无效")
        if config.get("transport", "streamable_http") not in {"streamable_http", "stdio"}:
            raise ValueError("不支持的 MCP 传输类型")
        payload = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
        server_id = uuid.uuid4().hex
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO mcp_servers(server_id, owner_user_id, name, transport, config_ciphertext)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT(owner_user_id, name) DO UPDATE SET
                    transport = EXCLUDED.transport,
                    config_ciphertext = EXCLUDED.config_ciphertext,
                    enabled = TRUE,
                    updated_at = now()
                RETURNING *
                """,
                (
                    server_id,
                    owner_user_id,
                    clean_name,
                    config.get("transport", "streamable_http"),
                    self._fernet.encrypt(payload.encode()).decode(),
                ),
            ).fetchone()
        return self._serialize(row)

    def set_enabled(self, owner_user_id: str, server_id: str, enabled: bool) -> dict | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE mcp_servers SET enabled = %s, updated_at = now()
                WHERE server_id = %s AND owner_user_id = %s
                RETURNING *
                """,
                (enabled, server_id, owner_user_id),
            ).fetchone()
        return self._serialize(row) if row else None

    def delete(self, owner_user_id: str, server_id: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                "DELETE FROM mcp_servers WHERE server_id = %s AND owner_user_id = %s RETURNING server_id",
                (server_id, owner_user_id),
            ).fetchone()
        return row is not None

    def get_config(self, owner_user_id: str, server_id: str) -> tuple[dict, dict] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE server_id = %s AND owner_user_id = %s",
                (server_id, owner_user_id),
            ).fetchone()
        return (self._serialize(row), self._decrypt(row["config_ciphertext"])) if row else None


_store: McpStore | None = None


def get_mcp_store() -> McpStore:
    global _store
    if _store is None:
        _store = McpStore()
    return _store


def get_mcp_store_or_none() -> McpStore | None:
    try:
        return get_mcp_store()
    except Exception as exc:
        logger.error("MCP store unavailable: %s", type(exc).__name__)
        return None


def close_mcp_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
