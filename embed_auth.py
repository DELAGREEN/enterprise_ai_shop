import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from models import EmbedIntegration, EmbedNonce

logger = logging.getLogger(__name__)

NONCE_TTL = 600  # сколько храним nonce (секунды)


def generate_secret() -> str:
    """64-символьный hex-секрет."""
    return secrets.token_hex(32)


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def make_signature(secret: str, user: str, expires_at: int, nonce: str) -> str:
    """
    Подпись для встраивания.
    Сообщение: "<user>|<expires_at>|<nonce>"
    """
    return _sign(secret, f"{user}|{expires_at}|{nonce}")


def verify_signature(secret: str, user: str, expires_at: int, nonce: str, signature: str) -> bool:
    expected = make_signature(secret, user, expires_at, nonce)
    return hmac.compare_digest(expected, signature)


def check_origin(request: Request, allowed_origins: str | None) -> bool:
    """Проверка Referer/Origin против белого списка. Если список пуст — пропускаем всё."""
    if not allowed_origins:
        return True

    allowed = [o.strip().lower() for o in allowed_origins.split(",") if o.strip()]
    if not allowed:
        return True

    referer = (request.headers.get("referer") or request.headers.get("origin") or "").lower()
    if not referer:
        return False

    return any(referer.startswith(a) for a in allowed)


async def validate_embed_token(
    db: AsyncSession,
    request: Request,
    integration_id: str,
    user: str,
    expires_at: int,
    nonce: str,
    signature: str,
    *,
    consume_nonce: bool = True,
) -> EmbedIntegration:
    """
    Полная валидация: подпись + срок + nonce + origin + интеграция активна.

    consume_nonce=True  — «сжигает» nonce (для GET iframe / GET SSO).
                          Второй запрос с тем же nonce → 403.
    consume_nonce=False — только проверяет подпись и срок (для POST /send).
                          Nonce не сохраняется и не сверяется с БД.
    """
    # 1. Интеграция существует и активна
    res = await db.execute(
        select(EmbedIntegration).where(EmbedIntegration.id == integration_id)
    )
    integ = res.scalar_one_or_none()
    if not integ or not integ.is_active:
        logger.warning("Embed: интеграция %s не найдена или отключена", integration_id)
        raise HTTPException(status_code=403, detail="Embed integration не активна")

    # 2. Срок годности
    now_ts = int(time.time())
    if expires_at < now_ts:
        logger.warning("Embed: токен истёк (exp=%s, now=%s)", expires_at, now_ts)
        raise HTTPException(status_code=403, detail="Embed token истёк")

    # 3. Подпись
    if not verify_signature(integ.secret, user, expires_at, nonce, signature):
        logger.warning("Embed: неверная подпись для интеграции %s", integration_id)
        raise HTTPException(status_code=403, detail="Неверная подпись")

    # 4. Origin / Referer (всегда)
    if not check_origin(request, integ.allowed_origins):
        logger.warning("Embed: origin не разрешён для интеграции %s", integration_id)
        raise HTTPException(status_code=403, detail="Origin не разрешён")

    # 5. Nonce — только если consume_nonce=True
    if consume_nonce:
        res = await db.execute(
            select(EmbedNonce).where(EmbedNonce.nonce == nonce)
        )
        if res.scalar_one_or_none() is not None:
            logger.warning("Embed: nonce %s уже использован", nonce)
            raise HTTPException(status_code=403, detail="Nonce уже использован (replay)")

        db.add(EmbedNonce(nonce=nonce, integration_id=integration_id))
        cutoff = datetime.utcnow() - timedelta(seconds=NONCE_TTL)
        await db.execute(delete(EmbedNonce).where(EmbedNonce.created_at < cutoff))

    # 6. Обновляем last_used_at
    integ.last_used_at = datetime.utcnow()

    # Важно: без commit — транзакция закрывается в роуте
    await db.flush()
    return integ


def build_signed_url(
    base_url: str,
    integration_id: str,
    secret: str,
    flow_id: str,
    user: str,
    ttl: int | None = None,
) -> str:
    """
    Хелпер для бэкенда портала.
    Возвращает готовый URL для iframe.
    """
    ttl = ttl or config.EMBED_TOKEN_TTL
    expires_at = int(time.time()) + ttl
    nonce = secrets.token_hex(16)
    sig = make_signature(secret, user, expires_at, nonce)
    return (
        f"{base_url}/embed/chat/{flow_id}"
        f"?i={integration_id}&u={user}&e={expires_at}&n={nonce}&s={sig}"
    )