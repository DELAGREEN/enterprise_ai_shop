"""
Публичный API v1 для доступа к агентам.

Аутентификация: Bearer API-ключ / сессия / Kerberos / Basic-LDAP.
Формат: JSON. Никаких HTML.
"""
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from access import can_user_access_flow
from api_auth import authenticate_api
from database import get_db
from langflow_client import LangflowClient, extract_output_text, parse_thinking
from models import Chat, ChatMessage, Integration, LLMRequestLog
from session import get_current_user
from templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["api"])


# ═══════════════════════════════════════════════════════════
#                     ЗАВИСИМОСТИ
# ═══════════════════════════════════════════════════════════

async def current_api_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Определяет пользователя для API-запроса."""
    return await authenticate_api(request, db)


def _check_integration_access(user: dict, integration: Integration | None, flow_id: str) -> None:
    """
    Проверка доступа:
      - API-ключ привязан к конкретному flow_id → проверяем совпадение
      - Обычный пользователь → проверяем через RBAC
    """
    if integration and integration.flow_id != flow_id:
        raise HTTPException(403, "API-ключ не даёт доступ к этому агенту")


# ═══════════════════════════════════════════════════════════
#                     МОДЕЛИ ЗАПРОСОВ
# ═══════════════════════════════════════════════════════════

class ChatSendRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)
    chat_id: str | None = None       # если не указан — создать новый или взять последний
    session_id: str | None = None    # для Langflow (если нужен отдельный контекст)


class ChatSendResponse(BaseModel):
    chat_id: str
    message_id: int
    text: str
    thinking: str = ""
    created_at: datetime


class ChatHistoryItem(BaseModel):
    id: int
    role: str
    content: str
    thinking: str = ""
    created_at: datetime


class ChatHistoryResponse(BaseModel):
    chat_id: str
    title: str
    created_at: datetime
    messages: list[ChatHistoryItem]


class FlowInfo(BaseModel):
    id: str
    name: str
    description: str = ""


# ═══════════════════════════════════════════════════════════
#                     ЭНДПОИНТЫ
# ═══════════════════════════════════════════════════════════

@router.get("/me")
async def whoami(
    user: dict = Depends(current_api_user),
):
    """Кто я? Возвращает способ аутентификации и логин."""
    return {
        "username": user["username"],
        "auth_method": user["auth_method"],
        "is_admin": user.get("is_admin", False),
        "integration_id": user.get("integration_id"),
    }


@router.get("/flows", response_model=list[FlowInfo])
async def list_flows(
    user: dict = Depends(current_api_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Список доступных агентов.
    - API-ключ: возвращает только тот flow, к которому привязан ключ.
    - Пользователь: возвращает все, к которым есть доступ.
    """
    # API-ключ
    if user.get("integration_id"):
        integ = await db.get(Integration, user["integration_id"])
        if not integ or not integ.is_active:
            raise HTTPException(403, "Интеграция отключена")

        client = LangflowClient()
        flows = await client.get_all_flows()
        flow = next((f for f in flows if f.get("id") == integ.flow_id), None)
        if not flow:
            return []
        return [FlowInfo(
            id=integ.flow_id,
            name=flow.get("name") or "Без имени",
            description=flow.get("description") or "",
        )]

    # Обычный пользователь — все доступные flow
    from access import visible_flow_ids
    allowed = await visible_flow_ids(db, user)

    client = LangflowClient()
    flows = await client.get_all_flows()
    return [
        FlowInfo(
            id=f.get("id"),
            name=f.get("name") or "Без имени",
            description=f.get("description") or "",
        )
        for f in flows
        if f.get("id") in allowed
    ]


@router.post("/chat/{flow_id}/send", response_model=ChatSendResponse)
async def chat_send(
    flow_id: str,
    payload: ChatSendRequest,
    user: dict = Depends(current_api_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Отправить сообщение агенту.
    Если chat_id не указан — создаёт новый чат (или переиспользует последний за 24 часа).
    """
    # Проверка доступа
    integration = None
    if user.get("integration_id"):
        integration = await db.get(Integration, user["integration_id"])
        _check_integration_access(user, integration, flow_id)
    else:
        # RBAC для пользователя
        if not await can_user_access_flow(db, user, flow_id):
            raise HTTPException(403, "Агент недоступен")

    # Найти или создать чат
    chat = None
    if payload.chat_id:
        chat = await db.get(Chat, payload.chat_id)
        if not chat or chat.flow_id != flow_id or chat.user_id != user["username"]:
            raise HTTPException(404, "Чат не найден")
    else:
        # Ищем последний чат (переиспользуем за последние 24 часа)
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(hours=24)
        res = await db.execute(
            select(Chat)
            .where(
                Chat.flow_id == flow_id,
                Chat.user_id == user["username"],
                Chat.updated_at >= cutoff,
            )
            .order_by(Chat.updated_at.desc())
            .limit(1)
        )
        chat = res.scalar_one_or_none()

        if chat is None:
            chat = Chat(
                id=uuid.uuid4().hex,
                flow_id=flow_id,
                user_id=user["username"],
                title="API-чат",
            )
            db.add(chat)
            await db.flush()

    # Сохранить вопрос пользователя
    if chat.title in ("Новый чат", "API-чат"):
        chat.title = payload.message.split("\n")[0].strip()[:60] or chat.title

    user_msg = ChatMessage(chat_id=chat.id, role="user", content=payload.message)
    db.add(user_msg)
    chat.updated_at = datetime.utcnow()

    # Вызов Langflow
    client = LangflowClient()
    data = await client.run_flow(
        flow_id,
        payload.message,
        session_id=payload.session_id or chat.id,
    )
    raw = extract_output_text(data)
    answer, thinking = parse_thinking(raw)

    # Сохранить ответ
    bot_msg = ChatMessage(
        chat_id=chat.id,
        role="assistant",
        content=answer or "",
        thinking=thinking or None,
    )
    db.add(bot_msg)

    # Логировать
    db.add(LLMRequestLog(
        user_id=user["username"],
        flow_id=flow_id,
        chat_id=chat.id,
        chat_title=chat.title,
        question=payload.message,
        answer=answer or "",
        thinking=thinking or None,
    ))

    chat.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(user_msg)
    await db.refresh(bot_msg)

    return ChatSendResponse(
        chat_id=chat.id,
        message_id=bot_msg.id,
        text=answer,
        thinking=thinking,
        created_at=bot_msg.created_at,
    )


@router.get("/chat/{flow_id}/{chat_id}", response_model=ChatHistoryResponse)
async def chat_history(
    flow_id: str,
    chat_id: str,
    user: dict = Depends(current_api_user),
    db: AsyncSession = Depends(get_db),
):
    """История чата."""
    if user.get("integration_id"):
        integration = await db.get(Integration, user["integration_id"])
        _check_integration_access(user, integration, flow_id)
    else:
        if not await can_user_access_flow(db, user, flow_id):
            raise HTTPException(403, "Агент недоступен")

    chat = await db.get(Chat, chat_id)
    if not chat or chat.flow_id != flow_id or chat.user_id != user["username"]:
        raise HTTPException(404, "Чат не найден")

    msgs_res = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.chat_id == chat_id)
        .order_by(ChatMessage.id.asc())
    )
    messages = list(msgs_res.scalars().all())

    return ChatHistoryResponse(
        chat_id=chat.id,
        title=chat.title,
        created_at=chat.created_at,
        messages=[
            ChatHistoryItem(
                id=m.id,
                role=m.role,
                content=m.content,
                thinking=m.thinking or "",
                created_at=m.created_at,
            )
            for m in messages
        ],
    )


@router.get("/chat/{flow_id}")
async def list_chats(
    flow_id: str,
    user: dict = Depends(current_api_user),
    db: AsyncSession = Depends(get_db),
):
    """Список чатов пользователя с агентом."""
    if user.get("integration_id"):
        integration = await db.get(Integration, user["integration_id"])
        _check_integration_access(user, integration, flow_id)
    else:
        if not await can_user_access_flow(db, user, flow_id):
            raise HTTPException(403, "Агент недоступен")

    res = await db.execute(
        select(Chat)
        .where(Chat.flow_id == flow_id, Chat.user_id == user["username"])
        .order_by(Chat.updated_at.desc())
    )
    chats = list(res.scalars().all())

    return [
        {
            "id": c.id,
            "title": c.title,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
        }
        for c in chats
    ]


@router.post("/chat/{flow_id}/new")
async def new_chat(
    flow_id: str,
    user: dict = Depends(current_api_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать новый чат."""
    if user.get("integration_id"):
        integration = await db.get(Integration, user["integration_id"])
        _check_integration_access(user, integration, flow_id)
    else:
        if not await can_user_access_flow(db, user, flow_id):
            raise HTTPException(403, "Агент недоступен")

    chat = Chat(
        id=uuid.uuid4().hex,
        flow_id=flow_id,
        user_id=user["username"],
        title="API-чат",
    )
    db.add(chat)
    await db.commit()

    return {"chat_id": chat.id, "title": chat.title}