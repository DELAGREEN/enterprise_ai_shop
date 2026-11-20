"""Удаление памяти разговоров в Langflow.

Чат «удаляется» у пользователя мягко (``chats.deleted_at``), но память
разговора живёт в Langflow — там ``session_id`` совпадает с ``chats.id``.
Этот модуль вычищает сессию и добирает неудавшиеся попытки.

Важно: Langflow отвечает ``204`` и тогда, когда удалять нечего **или** когда
флоу принадлежит другому пользователю Langflow. Поэтому успехом считается
только пустая сессия после удаления, а не код ответа.
"""

import logging
from datetime import datetime

from sqlalchemy import or_, select

from database import AsyncSessionLocal
from langflow_client import LangflowClient
from models import Chat

logger = logging.getLogger(__name__)

# После стольких неудач перестаём дёргать Langflow: причина уже записана
# в purge_error и видна в админке.
MAX_ATTEMPTS = 5


async def purge_chat_session(chat_id: str) -> tuple[bool, str]:
    """Удаляет память сессии в Langflow и проверяет результат.

    Возвращает ``(успех, причина_ошибки)``.
    """
    client = LangflowClient()
    try:
        await client.delete_session(chat_id)
        left = await client.session_message_count(chat_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось вычистить сессию %s в Langflow: %s", chat_id, exc)
        return False, str(exc)

    if left:
        reason = f"в Langflow осталось сообщений: {left}"
        logger.warning("Сессия %s не вычищена: %s", chat_id, reason)
        return False, reason

    logger.info("Память сессии %s в Langflow вычищена", chat_id)
    return True, ""


async def purge_deleted_chat(db, chat: Chat) -> bool:
    """Вычищает память чата и обновляет поля в переданной сессии.

    ``db`` — активная сессия SQLAlchemy; commit остаётся за вызывающим.
    """
    ok, error = await purge_chat_session(chat.id)

    if ok:
        chat.langflow_purged_at = datetime.utcnow()
        chat.purge_error = None
    else:
        chat.purge_attempts = (chat.purge_attempts or 0) + 1
        chat.purge_error = error[:1000]

    return ok


async def purge_pending(limit: int = 50) -> int:
    """Добирает чаты, у которых память в Langflow ещё не вычищена."""
    purged = 0
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Chat)
            .where(
                Chat.deleted_at.isnot(None),
                Chat.langflow_purged_at.is_(None),
                or_(
                    Chat.purge_attempts.is_(None),
                    Chat.purge_attempts < MAX_ATTEMPTS,
                ),
            )
            .order_by(Chat.deleted_at.asc())
            .limit(limit)
        )
        for chat in res.scalars().all():
            if await purge_deleted_chat(db, chat):
                purged += 1
            await db.commit()
    return purged
