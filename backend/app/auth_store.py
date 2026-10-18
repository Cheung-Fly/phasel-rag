"""Persistent user sessions and an owner-scoped encrypted credential vault."""
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import uuid
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from psycopg_pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger(__name__)

_SESSION_COOKIE = "campus_assistant_session"
_PASSWORD_SCRYPT_N = 2**14
_PASSWORD_SCRYPT_R = 8
_PASSWORD_SCRYPT_P = 1
_MAX_LOGIN_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 900
_DUMMY_SALT = bytes.fromhex("95d81557c2c93461ec2037f03ec3ab9a")
_DUMMY_HASH = hashlib.scrypt(
    b"not-a-user-password",
    salt=_DUMMY_SALT,
    n=_PASSWORD_SCRYPT_N,
    r=_PASSWORD_SCRYPT_R,
    p=_PASSWORD_SCRYPT_P,
    dklen=64,
)

# ★ 口令脱敏占位符（2026-10-14 落地）。
#   凭证详情接口默认回传它而不是明文口令；取明文必须由调用点显式声明。
#   刻意使用固定长度：若回传与真口令等长的掩码，等于额外泄露「口令有多长」。
MASKED_PASSWORD = "********"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS app_users (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_auth_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_app_auth_sessions_expiry
    ON app_auth_sessions(expires_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS app_login_attempts (
        attempt_key TEXT PRIMARY KEY,
        failures INTEGER NOT NULL DEFAULT 0,
        window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        blocked_until TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS encrypted_credentials (
        credential_id TEXT PRIMARY KEY,
        owner_user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        service_key TEXT NOT NULL,
        label TEXT NOT NULL,
        full_name_ciphertext TEXT NOT NULL,
        student_id_ciphertext TEXT NOT NULL,
        account_ciphertext TEXT NOT NULL,
        password_ciphertext TEXT NOT NULL,
        site_url_ciphertext TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(owner_user_id, service_key, label)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_credentials_owner_service
    ON encrypted_credentials(owner_user_id, service_key)
    """,
    """
    ALTER TABLE encrypted_credentials
    ADD COLUMN IF NOT EXISTS site_url_ciphertext TEXT NOT NULL DEFAULT ''
    """,
    """
    CREATE TABLE IF NOT EXISTS credential_audit (
        audit_id BIGSERIAL PRIMARY KEY,
        owner_user_id TEXT NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
        credential_id TEXT NOT NULL,
        action TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        CONSTRAINT ck_credential_audit_action
            CHECK (action IN ('create', 'read', 'update', 'delete', 'reveal'))
    )
    """,
    # ★ 迁移：为审计新增 'reveal'（明文读取）动作。
    #   CREATE TABLE IF NOT EXISTS 对已存在的表完全不起作用，所以旧库里的
    #   CHECK 约束仍然是旧版本 —— 不迁移的话，插入 'reveal' 会被数据库拒绝，
    #   表现为「取明文接口直接 503」，而不是一个容易看懂的报错。
    #   这里把两种可能的名字都 DROP 掉再重建同一条显式命名的约束：
    #   旧库建的是匿名约束（PostgreSQL 自动命名 credential_audit_action_check），
    #   新库与迁移后的库用的是 ck_credential_audit_action。先删后加，
    #   重复执行不会因「约束已存在」而失败。
    """
    ALTER TABLE credential_audit
    DROP CONSTRAINT IF EXISTS credential_audit_action_check
    """,
    """
    ALTER TABLE credential_audit
    DROP CONSTRAINT IF EXISTS ck_credential_audit_action
    """,
    """
    ALTER TABLE credential_audit
    ADD CONSTRAINT ck_credential_audit_action
    CHECK (action IN ('create', 'read', 'update', 'delete', 'reveal'))
    """,
)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    username: str


def _normalize_username(username: str) -> str:
    return username.strip().lower()


def _hash_password(password: str) -> str:
    salt = os.urandom(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_PASSWORD_SCRYPT_N,
        r=_PASSWORD_SCRYPT_R,
        p=_PASSWORD_SCRYPT_P,
        dklen=64,
    )
    return (
        f"scrypt${_PASSWORD_SCRYPT_N}${_PASSWORD_SCRYPT_R}${_PASSWORD_SCRYPT_P}"
        f"${salt.hex()}${password_hash.hex()}"
    )


def _verify_password(password: str, encoded: str | None) -> bool:
    if encoded is None:
        candidate_salt = _DUMMY_SALT
        expected_hash = _DUMMY_HASH
    else:
        try:
            algorithm, n, r, p, salt_hex, hash_hex = encoded.split("$")
            if algorithm != "scrypt":
                raise ValueError("unsupported password hash")
            parameters = {"n": int(n), "r": int(r), "p": int(p)}
            if parameters != {
                "n": _PASSWORD_SCRYPT_N,
                "r": _PASSWORD_SCRYPT_R,
                "p": _PASSWORD_SCRYPT_P,
            }:
                raise ValueError("unsupported scrypt parameters")
            candidate_salt = bytes.fromhex(salt_hex)
            expected_hash = bytes.fromhex(hash_hex)
            if len(candidate_salt) != 16 or len(expected_hash) != 64:
                raise ValueError("invalid password hash length")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("stored application password hash is invalid") from exc
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=candidate_salt,
            dklen=len(expected_hash),
            **parameters,
        )
        return hmac.compare_digest(actual, expected_hash)

    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=candidate_salt,
        n=_PASSWORD_SCRYPT_N,
        r=_PASSWORD_SCRYPT_R,
        p=_PASSWORD_SCRYPT_P,
        dklen=64,
    )
    hmac.compare_digest(actual, expected_hash)
    return False


class AuthStore:
    """Fail-closed authentication and encrypted credential persistence."""

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.postgres_dsn.strip():
            raise RuntimeError("POSTGRES_DSN is required for authentication")
        self.session_ttl_seconds = settings.auth_session_ttl_seconds
        self._encryption_key = settings.credential_encryption_key.strip()
        self._pool = ConnectionPool(
            settings.postgres_dsn,
            min_size=1,
            max_size=max(2, min(settings.pg_pool_max, 8)),
            open=True,
            timeout=5,
        )
        try:
            with self._pool.connection() as connection:
                for statement in _SCHEMA:
                    connection.execute(statement)
        except Exception:
            self._pool.close()
            raise
        logger.info("Credential vault database schema is ready")

    def close(self) -> None:
        self._pool.close()

    def create_user(self, username: str, password: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise ValueError("username format is invalid")
        if not password_is_valid(password):
            raise ValueError("application password length is invalid")
        user_id = str(uuid.uuid4())
        normalized = _normalize_username(username)
        password_hash = _hash_password(password)
        with self._pool.connection() as connection:
            connection.execute(
                "INSERT INTO app_users(user_id, username, password_hash) VALUES (%s, %s, %s)",
                (user_id, normalized, password_hash),
            )
        logger.info("Created application user %s", normalized)

    def ensure_local_owner(self, username: str) -> AuthenticatedUser:
        """返回（不存在时创建）单用户本地模式使用的固定身份。

        为什么本地模式也要在 app_users 里留一条真实记录，而不是把 user_id
        写死成常量：全部业务数据（向量片段、会话、凭证）都通过外键
        owner_user_id 关联到 app_users(user_id)。没有这一行，要么外键插入失败，
        要么得把整套 owner 隔离逻辑拆掉 —— 而后者正是「看起来数据全丢」的成因。

        幂等性：用 INSERT ... ON CONFLICT DO NOTHING 再回查，而不是先 SELECT
        再 INSERT。这样多次启动、并发启动（api 容器重启时可能有两个进程短暂重叠）
        都只会得到同一条记录，不会撞唯一索引。
        """
        normalized = _normalize_username(username)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", normalized):
            raise ValueError("local owner username format is invalid")
        with self._pool.connection() as connection:
            # 本地模式不走口令登录，但 password_hash 是 NOT NULL，仍写入一份
            # 随机口令的合法哈希。附带好处：即便日后把 AUTH_MODE 切回 session，
            # 这个身份也无法用「空口令」蒙进来（随机 32 字节口令无人知晓）。
            connection.execute(
                """
                INSERT INTO app_users(user_id, username, password_hash)
                VALUES (%s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    normalized,
                    _hash_password(secrets.token_urlsafe(32)),
                ),
            )
            row = connection.execute(
                "SELECT user_id, username FROM app_users WHERE username = %s",
                (normalized,),
            ).fetchone()
        if row is None:
            raise RuntimeError("local owner row is missing after upsert")
        return AuthenticatedUser(*row)

    def sync_access_password(self, username: str, password: str) -> bool:
        """让该用户的口令哈希与 ACCESS_PASSWORD 一致；口令变了就撤销旧会话。

        返回口令是否发生了变化。口令没变时不重写哈希，避免每次启动都踢掉会话。
        """
        normalized = _normalize_username(username)
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT user_id, password_hash FROM app_users WHERE username = %s",
                (normalized,),
            ).fetchone()
            if row is None:
                raise RuntimeError("access owner row is missing")
            if _verify_password(password, row[1]):
                return False
            connection.execute(
                "UPDATE app_users SET password_hash = %s WHERE user_id = %s",
                (_hash_password(password), row[0]),
            )
            connection.execute("DELETE FROM app_auth_sessions WHERE user_id = %s", (row[0],))
        return True

    def authenticate(self, username: str, password: str, client_key: str) -> str | None:
        normalized = _normalize_username(username)
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT user_id, password_hash FROM app_users WHERE username = %s",
                (normalized,),
            ).fetchone()
        encoded = row[1] if row else None
        if not _verify_password(password, encoded):
            self._record_login_failure(client_key)
            return None
        self._clear_login_failures(client_key)
        return row[0]

    def is_rate_limited(self, client_key: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT blocked_until > now()
                FROM app_login_attempts
                WHERE attempt_key = %s
                """,
                (client_key,),
            ).fetchone()
        return bool(row and row[0])

    def _record_login_failure(self, client_key: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                DELETE FROM app_login_attempts
                WHERE window_started_at < now() - (%s * interval '2 seconds')
                """,
                (_LOGIN_WINDOW_SECONDS,),
            )
            connection.execute(
                """
                INSERT INTO app_login_attempts(attempt_key, failures, window_started_at, blocked_until)
                VALUES (%s, 1, now(), NULL)
                ON CONFLICT (attempt_key) DO UPDATE SET
                    failures = CASE
                        WHEN app_login_attempts.window_started_at
                             < now() - (%s * interval '1 second') THEN 1
                        ELSE app_login_attempts.failures + 1
                    END,
                    window_started_at = CASE
                        WHEN app_login_attempts.window_started_at
                             < now() - (%s * interval '1 second') THEN now()
                        ELSE app_login_attempts.window_started_at
                    END,
                    blocked_until = CASE
                        WHEN app_login_attempts.window_started_at
                             < now() - (%s * interval '1 second') THEN NULL
                        WHEN app_login_attempts.failures + 1 >= %s
                             THEN now() + (%s * interval '1 second')
                        ELSE app_login_attempts.blocked_until
                    END
                """,
                (
                    client_key,
                    _LOGIN_WINDOW_SECONDS,
                    _LOGIN_WINDOW_SECONDS,
                    _LOGIN_WINDOW_SECONDS,
                    _MAX_LOGIN_FAILURES,
                    _LOGIN_WINDOW_SECONDS,
                ),
            )

    def _clear_login_failures(self, client_key: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM app_login_attempts WHERE attempt_key = %s",
                (client_key,),
            )

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._pool.connection() as connection:
            connection.execute("DELETE FROM app_auth_sessions WHERE expires_at <= now()")
            connection.execute(
                """
                INSERT INTO app_auth_sessions(token_hash, user_id, expires_at)
                VALUES (%s, %s, now() + (%s * interval '1 second'))
                """,
                (token_hash, user_id, self.session_ttl_seconds),
            )
        return token

    def resolve_session(self, token: str | None) -> AuthenticatedUser | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT users.user_id, users.username
                FROM app_auth_sessions AS sessions
                JOIN app_users AS users ON users.user_id = sessions.user_id
                WHERE sessions.token_hash = %s AND sessions.expires_at > now()
                """,
                (token_hash,),
            ).fetchone()
        return AuthenticatedUser(*row) if row else None

    def revoke_session(self, token: str | None) -> None:
        if not token:
            return
        token_hash = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        with self._pool.connection() as connection:
            connection.execute(
                "DELETE FROM app_auth_sessions WHERE token_hash = %s",
                (token_hash,),
            )

    def _fernet(self) -> Fernet:
        if not self._encryption_key:
            raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is not configured")
        try:
            return Fernet(self._encryption_key.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is invalid") from exc

    def save_credential(
        self,
        owner_user_id: str,
        service_key: str,
        label: str,
        site_url: str,
        full_name: str,
        student_id: str,
        account: str,
        password: str,
        credential_id: str | None = None,
    ) -> str:
        cipher = self._fernet()
        encrypted = [
            cipher.encrypt(value.encode("utf-8")).decode("ascii")
            for value in (site_url, full_name, student_id, account, password)
        ]
        creating = credential_id is None
        credential_id = credential_id or str(uuid.uuid4())
        with self._pool.connection() as connection:
            if creating:
                existing = connection.execute(
                    """
                    SELECT credential_id FROM encrypted_credentials
                    WHERE owner_user_id = %s AND service_key = %s AND label = %s
                    """,
                    (owner_user_id, service_key.strip(), label.strip()),
                ).fetchone()
                if existing:
                    raise ValueError("该系统和标签的凭证已存在")
            else:
                existing = connection.execute(
                    """
                    SELECT credential_id FROM encrypted_credentials
                    WHERE owner_user_id = %s AND credential_id = %s
                    """,
                    (owner_user_id, credential_id),
                ).fetchone()
                if existing is None:
                    raise LookupError("校园账号不存在")
                duplicate = connection.execute(
                    """
                    SELECT credential_id FROM encrypted_credentials
                    WHERE owner_user_id = %s AND service_key = %s AND label = %s
                          AND credential_id <> %s
                    """,
                    (
                        owner_user_id,
                        service_key.strip(),
                        label.strip(),
                        credential_id,
                    ),
                ).fetchone()
                if duplicate:
                    raise ValueError("该系统和标签的凭证已存在")
                connection.execute(
                    """
                    UPDATE encrypted_credentials SET
                        service_key = %s,
                        label = %s,
                        site_url_ciphertext = %s,
                        full_name_ciphertext = %s,
                        student_id_ciphertext = %s,
                        account_ciphertext = %s,
                        password_ciphertext = %s,
                        updated_at = now()
                    WHERE owner_user_id = %s AND credential_id = %s
                    """,
                    (
                        service_key.strip(),
                        label.strip(),
                        *encrypted,
                        owner_user_id,
                        credential_id,
                    ),
                )
                action = "update"
            if creating:
                connection.execute(
                    """
                    INSERT INTO encrypted_credentials(
                        credential_id, owner_user_id, service_key, label,
                        site_url_ciphertext,
                        full_name_ciphertext, student_id_ciphertext,
                        account_ciphertext, password_ciphertext
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (credential_id, owner_user_id, service_key.strip(), label.strip(), *encrypted),
                )
                action = "create"
            self._audit(connection, owner_user_id, credential_id, action)
        return credential_id

    def list_credentials(self, owner_user_id: str) -> list[dict]:
        with self._pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT credential_id, service_key, label, updated_at
                FROM encrypted_credentials
                WHERE owner_user_id = %s
                ORDER BY service_key, label
                """,
                (owner_user_id,),
            ).fetchall()
        return [
            {
                "credential_id": row[0],
                "service_key": row[1],
                "label": row[2],
                "updated_at": row[3].isoformat(),
            }
            for row in rows
        ]

    def get_credential(
        self,
        owner_user_id: str,
        credential_id: str,
        *,
        reveal_password: bool = False,
    ) -> dict | None:
        """读取单项凭证。**默认脱敏**：口令字段返回占位符。

        取明文必须由调用点显式传 reveal_password=True。这个方向是刻意的 ——
        如果默认返回明文、再用一个 mask 参数去遮蔽，那么「新调用点忘了传参数」
        就会静默泄露口令；反过来，忘传参数的后果是功能不可用（能被测出来），
        属于安全的失败方向。

        脱敏路径连密文都不解密：既然不会用到明文，就没有必要把它解出来放进内存。
        """
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT credential_id, service_key, label, site_url_ciphertext,
                       full_name_ciphertext,
                       student_id_ciphertext, account_ciphertext, password_ciphertext
                FROM encrypted_credentials
                WHERE owner_user_id = %s AND credential_id = %s
                """,
                (owner_user_id, credential_id),
            ).fetchone()
            if row is None:
                return None
            # 明文读取单独记一种动作：审计时间线要能区分「看了脱敏元数据」
            # 与「把口令取了出来」。后者才是需要被追问的操作。
            self._audit(
                connection,
                owner_user_id,
                credential_id,
                "reveal" if reveal_password else "read",
            )
        cipher = self._fernet()
        try:
            values = [
                cipher.decrypt(value.encode("ascii")).decode("utf-8")
                for value in row[3:7]
            ]
            password = (
                cipher.decrypt(row[7].encode("ascii")).decode("utf-8")
                if reveal_password
                else MASKED_PASSWORD
            )
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("Credential decryption failed; check the encryption key") from exc
        return {
            "credential_id": row[0],
            "service_key": row[1],
            "label": row[2],
            "site_url": values[0],
            "full_name": values[1],
            "student_id": values[2],
            "account": values[3],
            "password": password,
            # 让「当前是不是明文」在数据里可见：调用方可以据此断言，
            # 而不是靠猜测那个 "********" 到底是真的口令还是掩码。
            "password_revealed": reveal_password,
        }

    def delete_credential(self, owner_user_id: str, credential_id: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                DELETE FROM encrypted_credentials
                WHERE owner_user_id = %s AND credential_id = %s
                RETURNING credential_id
                """,
                (owner_user_id, credential_id),
            ).fetchone()
            if row:
                self._audit(connection, owner_user_id, credential_id, "delete")
        return row is not None

    @staticmethod
    def _audit(connection, owner_user_id: str, credential_id: str, action: str) -> None:
        connection.execute(
            """
            INSERT INTO credential_audit(owner_user_id, credential_id, action)
            VALUES (%s, %s, %s)
            """,
            (owner_user_id, credential_id, action),
        )


_store: AuthStore | None = None
_store_lock = threading.Lock()


def get_auth_store() -> AuthStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = AuthStore()
    return _store


def close_auth_store() -> None:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None


def login_attempt_key(username: str, client_host: str) -> str:
    normalized = _normalize_username(username)
    material = f"{normalized}\0{client_host}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def session_cookie_settings() -> dict:
    settings = get_settings()
    return {
        "key": _SESSION_COOKIE,
        "httponly": True,
        "secure": settings.auth_cookie_secure,
        "samesite": "strict",
        "path": "/",
        "max_age": settings.auth_session_ttl_seconds,
    }


def session_cookie_name() -> str:
    return _SESSION_COOKIE


def rate_limit_retry_after() -> int:
    return _LOGIN_WINDOW_SECONDS


def password_is_valid(password: str) -> bool:
    return 12 <= len(password) <= 1024
