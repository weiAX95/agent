"""Stable chat checkpoint identities and PostgreSQL connection settings."""

import hashlib
import json

from sqlalchemy.engine import make_url

CHECKPOINT_SETUP_LOCK_ID = int.from_bytes(
    hashlib.sha256(b"agent:checkpoint:schema").digest()[:8], "big", signed=True
)


def _identity_digest(user_id: str, session_id: str) -> bytes:
    identity = json.dumps(
        [user_id, session_id], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode("utf-8")).digest()


def make_thread_id(user_id: str, session_id: str) -> str:
    """Keep each user's session separate, even when session IDs are reused."""
    return "chat:v1:" + _identity_digest(user_id, session_id).hex()


def checkpoint_lock_id(user_id: str, session_id: str) -> int:
    """Return a signed PostgreSQL advisory-lock key for this conversation."""
    return int.from_bytes(_identity_digest(user_id, session_id)[:8], "big", signed=True)


def checkpoint_dsn(database_url: str) -> str:
    """Convert SQLAlchemy's asyncpg URL to a psycopg-compatible PostgreSQL URL."""
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("checkpoint 需要 PostgreSQL DATABASE_URL")
    return url.set(drivername="postgresql").render_as_string(hide_password=False)
