from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from ..config import settings

ALGO = "HS256"


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode()


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode())
    except ValueError:
        return False


def create_token(user_id: int) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.token_expire_minutes)
    return jwt.encode({"sub": str(user_id), "exp": exp}, settings.secret_key, algorithm=ALGO)


def decode_token(token: str) -> int | None:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=[ALGO])
        return int(data["sub"])
    except (JWTError, KeyError, ValueError):
        return None


def new_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, 前缀, 哈希)。明文只在创建时展示一次。"""
    raw = "psk_" + secrets.token_urlsafe(32)
    return raw, raw[:10], hash_api_key(raw)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def new_portal_token() -> str:
    return secrets.token_urlsafe(24)
