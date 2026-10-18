"""Encrypted chat-model profiles that can be switched without redeploying."""
import logging
import threading
import time
import uuid

from cryptography.fernet import Fernet, InvalidToken
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_profiles (
    profile_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    model TEXT NOT NULL,
    base_url TEXT NOT NULL,
    api_key_ciphertext TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(owner_user_id, name)
)
"""
_ACTIVE_TTL_SECONDS = 5


class ModelStore:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip() or not settings.credential_encryption_key.strip():
            raise RuntimeError("POSTGRES_DSN and CREDENTIAL_ENCRYPTION_KEY are required")
        self._fernet = Fernet(settings.credential_encryption_key.strip().encode())
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 4)),
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
        self._cache: tuple[float, dict | None] | None = None

    def close(self) -> None:
        self._pool.close()

    @staticmethod
    def _public(row: dict) -> dict:
        return {
            "profile_id": row["profile_id"],
            "name": row["name"],
            "model": row["model"],
            "base_url": row["base_url"],
            "active": row["active"],
        }

    def list(self, owner_user_id: str) -> list[dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_profiles WHERE owner_user_id = %s ORDER BY name",
                (owner_user_id,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def save(self, owner_user_id: str, name: str, model: str, base_url: str, api_key: str) -> dict:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                INSERT INTO model_profiles(profile_id, owner_user_id, name, model, base_url, api_key_ciphertext)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(owner_user_id, name) DO UPDATE SET
                    model = EXCLUDED.model,
                    base_url = EXCLUDED.base_url,
                    api_key_ciphertext = EXCLUDED.api_key_ciphertext,
                    updated_at = now()
                RETURNING *
                """,
                (
                    uuid.uuid4().hex, owner_user_id, name, model, base_url,
                    self._fernet.encrypt(api_key.encode()).decode(),
                ),
            ).fetchone()
        self._cache = None
        return self._public(row)

    def activate(self, owner_user_id: str, profile_id: str | None) -> bool:
        """profile_id=None 清除启用项，回退到环境变量默认模型。"""
        with self._pool.connection() as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE model_profiles SET active = FALSE WHERE owner_user_id = %s",
                    (owner_user_id,),
                )
                if profile_id is None:
                    self._cache = None
                    return True
                row = connection.execute(
                    """
                    UPDATE model_profiles SET active = TRUE, updated_at = now()
                    WHERE profile_id = %s AND owner_user_id = %s RETURNING profile_id
                    """,
                    (profile_id, owner_user_id),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
        self._cache = None
        return True

    def delete(self, owner_user_id: str, profile_id: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                "DELETE FROM model_profiles WHERE profile_id = %s AND owner_user_id = %s RETURNING profile_id",
                (profile_id, owner_user_id),
            ).fetchone()
        self._cache = None
        return row is not None

    def get(self, owner_user_id: str, profile_id: str) -> dict | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_profiles WHERE profile_id = %s AND owner_user_id = %s",
                (profile_id, owner_user_id),
            ).fetchone()
        return self._with_key(row) if row else None

    def _with_key(self, row: dict) -> dict:
        try:
            key = self._fernet.decrypt(row["api_key_ciphertext"].encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("模型密钥无法解密") from exc
        return {**self._public(row), "api_key": key}

    def active(self, owner_user_id: str) -> dict | None:
        now = time.monotonic()
        if self._cache and now - self._cache[0] < _ACTIVE_TTL_SECONDS:
            return self._cache[1]
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM model_profiles WHERE owner_user_id = %s AND active",
                (owner_user_id,),
            ).fetchone()
        value = self._with_key(row) if row else None
        self._cache = (now, value)
        return value


_store: ModelStore | None = None
_lock = threading.Lock()
_default_owner: str | None = None


def get_model_store() -> ModelStore:
    global _store
    with _lock:
        if _store is None:
            _store = ModelStore()
        return _store


def set_default_model_owner(owner_user_id: str | None) -> None:
    """单用户本地模式下，后台任务和 Agent 都使用该主人的启用模型。"""
    global _default_owner
    _default_owner = owner_user_id


def active_profile() -> dict | None:
    if not _default_owner:
        return None
    try:
        return get_model_store().active(_default_owner)
    except Exception as exc:
        logger.error("Active model lookup failed: %s", type(exc).__name__)
        return None


def close_model_store() -> None:
    global _store
    if _store is not None:
        _store.close()
        _store = None
