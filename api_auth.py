"""
Аутентификация для /api/v1/*.
Определяет пользователя по одному из способов:
  1. Authorization: Bearer <api_key>
  2. Session cookie (внутренний пользователь)
  3. X-Remote-User от nginx (Kerberos)
  4. Authorization: Basic (LDAP) — опционально
"""
import hashlib
import logging
import uuid
from datetime import datetime

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal
from kerberos_auth import get_kerberos_user
from ldap_auth import authenticate_ldap
from models import Integration, User, UserGroup
from session import get_current_user

logger = logging.getLogger(__name__)


def _hash_api_key(key: str) -> str:
    """SHA-256 от ключа — хранится в БД."""
    return hashlib.sha256(key.encode()).hexdigest()


def _parse_basic_auth(header: str) -> tuple[str, str] | None:
    """Authorization: Basic base64(user:pass) → (user, pass)"""
    import base64
    try:
        scheme, b64 = header.split(" ", 1)
        if scheme.lower() != "basic":
            return None
        decoded = base64.b64decode(b64).decode()
        user, _, password = decoded.partition(":")
        return user, password
    except Exception:
        return None


async def _find_integration_by_key(db: AsyncSession, key: str) -> Integration | None:
    h = _hash_api_key(key)
    res = await db.execute(
        select(Integration).where(
            Integration.api_key_hash == h,
            Integration.is_active.is_(True),
        )
    )
    return res.scalar_one_or_none()


async def _sync_user(db: AsyncSession, username: str) -> User:
    """Автосоздание пользователя для Kerberos/LDAP входа."""
    res = await db.execute(select(User).where(User.username == username))
    user = res.scalar_one_or_none()
    if user is None:
        user = User(username=username)
        db.add(user)
        db.add(UserGroup(username=username, group_id="users"))
        await db.flush()
    return user


async def authenticate_api(request: Request, db: AsyncSession) -> dict:
    """
    Определяет контекст вызова для /api/v1/*.
    Возвращает dict: {username, auth_method, integration_id?}
    Бросает HTTPException(401) если не удалось.
    """
    auth_header = request.headers.get("authorization", "")

    # ── 1. Bearer API-ключ ────────────────────────────────────
    if auth_header.lower().startswith("bearer "):
        key = auth_header[7:].strip()
        if not key:
            raise HTTPException(401, "Пустой Bearer-токен")

        integ = await _find_integration_by_key(db, key)
        if not integ:
            raise HTTPException(401, "Неверный API-ключ")

        integ.last_used_at = datetime.utcnow()
        await db.flush()

        return {
            "username": f"apikey:{integ.id}",
            "auth_method": "api_key",
            "integration_id": integ.id,
            "is_service": True,          # это сервисный вызов, не от человека
        }

    # ── 2. Session cookie (внутренний пользователь) ───────────
    user = get_current_user(request)
    if user:
        # проверка is_disabled
        res = await db.execute(select(User).where(User.username == user["username"]))
        row = res.scalar_one_or_none()
        if row and row.is_disabled:
            raise HTTPException(403, "Учётная запись отключена")
        return {
            "username": user["username"],
            "is_admin": user.get("is_admin", False),
            "auth_method": "internal",
        }

    # ── 3. Kerberos (X-Remote-User от nginx) ──────────────────
    krb_user = get_kerberos_user(request)
    if krb_user:
        row = await _sync_user(db, krb_user)
        if row.is_disabled:
            raise HTTPException(403, "Учётная запись отключена")
        await db.commit()
        return {
            "username": krb_user,
            "auth_method": "kerberos",
        }

    # ── 4. Basic Auth → LDAP ──────────────────────────────────
    if auth_header.lower().startswith("basic "):
        parsed = _parse_basic_auth(auth_header)
        if parsed:
            username, password = parsed
            result = authenticate_ldap(username, password)
            if result:
                row = await _sync_user(db, username)
                if row.is_disabled:
                    raise HTTPException(403, "Учётная запись отключена")
                await db.commit()
                return {
                    "username": username,
                    "auth_method": "ldap",
                }

    raise HTTPException(
        401,
        "Требуется аутентификация. Используйте Bearer-токен, Basic Auth или сессию.",
        headers={"WWW-Authenticate": 'Bearer realm="api"'},
    )