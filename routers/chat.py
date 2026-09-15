import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient, extract_output_text, parse_thinking
from models import Chat, ChatMessage, FlowPublication, LLMRequestLog
from session import get_current_user
from templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------- helpers ----------

async def _is_published(db: AsyncSession, flow_id: str) -> bool:
    result = await db.execute(
        select(FlowPublication).where(
            FlowPublication.flow_id == flow_id,
            FlowPublication.is_published.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None


async def _flow_name(flow_id: str, db: AsyncSession) -> str:
    # 1. Сначала смотрим override
    res = await db.execute(
        select(FlowPublication).where(FlowPublication.flow_id == flow_id)
    )
    pub = res.scalar_one_or_none()
    if pub and pub.override_name:
        return pub.override_name

    # 2. Иначе — из Langflow
    client = LangflowClient()
    flows = await client.get_all_flows()
    flow = next((f for f in flows if f.get("id") == flow_id), None)
    return (flow.get("name") if flow else None) or flow_id


async def _get_user_chats(db: AsyncSession, flow_id: str, user_id: str):
    result = await db.execute(
        select(Chat)
        .where(Chat.flow_id == flow_id, Chat.user_id == user_id)
        .order_by(Chat.updated_at.desc())
    )
    return list(result.scalars().all())


async def _get_chat_or_404(
    db: AsyncSession, chat_id: str, flow_id: str, user_id: str
) -> Chat:
    result = await db.execute(
        select(Chat).where(
            Chat.id == chat_id,
            Chat.flow_id == flow_id,
            Chat.user_id == user_id,
        )
    )
    chat = result.scalar_one_or_none()
    if not chat:
        raise HTTPException(status_code=404, detail="Чат не найден")
    return chat


# ---------- routes ----------

@router.get("/chat/{flow_id}")
async def chat_root(
    flow_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Открывает последний чат пользователя с агентом или создаёт новый."""
    if not await _is_published(db, flow_id):
        raise HTTPException(status_code=404, detail="Агент не найден или не опубликован")

    user = get_current_user(request)
    chats = await _get_user_chats(db, flow_id, user["username"])

    if chats:
        return RedirectResponse(f"/chat/{flow_id}/{chats[0].id}", status_code=302)

    chat = Chat(
        id=uuid.uuid4().hex,
        flow_id=flow_id,
        user_id=user["username"],
        title="Новый чат",
    )
    db.add(chat)
    await db.commit()
    return RedirectResponse(f"/chat/{flow_id}/{chat.id}", status_code=302)


@router.post("/chat/{flow_id}/new")
async def chat_new(
    flow_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Создать новый чат с агентом."""
    if not await _is_published(db, flow_id):
        raise HTTPException(status_code=404, detail="Агент не найден")

    user = get_current_user(request)
    chat = Chat(
        id=uuid.uuid4().hex,
        flow_id=flow_id,
        user_id=user["username"],
        title="Новый чат",
    )
    db.add(chat)
    await db.commit()
    return RedirectResponse(f"/chat/{flow_id}/{chat.id}", status_code=302)


@router.get("/chat/{flow_id}/{chat_id}", response_class=HTMLResponse)
async def chat_page(
    flow_id: str,
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not await _is_published(db, flow_id):
        raise HTTPException(status_code=404, detail="Агент не найден или не опубликован")

    user = get_current_user(request)
    chat = await _get_chat_or_404(db, chat_id, flow_id, user["username"])
    chats = await _get_user_chats(db, flow_id, user["username"])

    msgs_res = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.chat_id == chat.id)
        .order_by(ChatMessage.id.asc())
    )
    messages = list(msgs_res.scalars().all())

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "flow_id": flow_id,
            "name": await _flow_name(flow_id, db),
            "user": user,
            "chat": chat,
            "chats": chats,
            "messages": messages,
        },
    )


class MessagePayload(BaseModel):
    message: str


@router.post("/chat/{flow_id}/{chat_id}/send")
async def chat_send(
    flow_id: str,
    chat_id: str,
    payload: MessagePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if not await _is_published(db, flow_id):
        raise HTTPException(status_code=403, detail="Агент не опубликован")

    user = get_current_user(request)
    chat = await _get_chat_or_404(db, chat_id, flow_id, user["username"])

    text_in = payload.message.strip()
    if not text_in:
        raise HTTPException(status_code=400, detail="Пустое сообщение")

    # Авто-название чата по первому сообщению
    if chat.title == "Новый чат":
        first_line = text_in.split("\n")[0].strip()
        chat.title = (first_line[:60] or "Новый чат")

    # Сохраняем сообщение пользователя
    db.add(ChatMessage(chat_id=chat.id, role="user", content=text_in))
    chat.updated_at = datetime.utcnow()

    # Вызываем Langflow. session_id = chat.id → контекст изолирован по чату.
    client = LangflowClient()
    data = await client.run_flow(flow_id, text_in, session_id=chat.id)
    raw_text = extract_output_text(data)
    answer, thinking = parse_thinking(raw_text)

    # Сохраняем ответ
    db.add(
        ChatMessage(
            chat_id=chat.id,
            role="assistant",
            content=answer or "",
            thinking=thinking or None,
        )
    )
    chat.updated_at = datetime.utcnow()

    # персистентный журнал — не удаляется вместе с чатом
    db.add(
        LLMRequestLog(
            user_id=user["username"],
            flow_id=flow_id,
            chat_id=chat.id,
            chat_title=chat.title,
            question=text_in,
            answer=answer or "",
            thinking=thinking or None,
        )
    )

    await db.commit()

    return {"text": answer, "thinking": thinking, "title": chat.title}


class RenamePayload(BaseModel):
    title: str


@router.post("/chat/{flow_id}/{chat_id}/rename")
async def chat_rename(
    flow_id: str,
    chat_id: str,
    payload: RenamePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = get_current_user(request)
    chat = await _get_chat_or_404(db, chat_id, flow_id, user["username"])

    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Пустое название")

    chat.title = title[:120]
    chat.updated_at = datetime.utcnow()
    await db.commit()
    return {"status": "ok", "title": chat.title}


@router.post("/chat/{flow_id}/{chat_id}/delete")
async def chat_delete(
    flow_id: str,
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = get_current_user(request)
    chat = await _get_chat_or_404(db, chat_id, flow_id, user["username"])

    # Явно чистим сообщения (не полагаемся на каскад БД)
    await db.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat.id))
    await db.delete(chat)
    await db.commit()
    logger.info("Пользователь %s удалил чат %s", user["username"], chat.id)
    return {"status": "ok"}