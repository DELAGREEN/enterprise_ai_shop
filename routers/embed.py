import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from database import get_db
from embed_auth import validate_embed_token
from langflow_client import LangflowClient, extract_output_text, parse_thinking
from models import Chat, ChatMessage, FlowPublication, LLMRequestLog, User, UserGroup
from session import MAX_AGE, SESSION_COOKIE, create_session
from templating import templates

from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter()


async def _sync_embed_user(db: AsyncSession, username: str) -> None:
    """Автосоздание пользователя в users и добавление в группу users."""
    res = await db.execute(select(User).where(User.username == username))
    if res.scalar_one_or_none() is None:
        db.add(User(username=username))
        db.add(UserGroup(username=username, group_id="users"))
        await db.flush()


#async def _get_or_create_chat(
#    db: AsyncSession, flow_id: str, user_id: str
#) -> Chat:
#    """Один «активный» чат на пользователя+агента. Если уже есть — переиспользуем последний."""
#    res = await db.execute(
#        select(Chat)
#        .where(Chat.flow_id == flow_id, Chat.user_id == user_id)
#        .order_by(Chat.updated_at.desc())
#        .limit(1)
#    )
#    chat = res.scalar_one_or_none()
#    if chat:
#        return chat
#
#    chat = Chat(
#        id=uuid.uuid4().hex,
#        flow_id=flow_id,
#        user_id=user_id,
#        title="Встроенный чат",
#    )
#    db.add(chat)
##    await db.commit()
#    return chat

async def _get_or_create_chat(db, flow_id, user_id):
    # Всегда создаём новый
    chat = Chat(
        id=uuid.uuid4().hex,
        flow_id=flow_id,
        user_id=user_id,
        title="Встроенный чат",
    )
    db.add(chat)
    await db.flush()
    return chat


@router.get("/embed/chat/{flow_id}", response_class=HTMLResponse)
async def embed_chat(
    flow_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    i: str = Query(...),
    u: str = Query(...),
    e: int = Query(...),
    n: str = Query(...),
    s: str = Query(...),
):
    integ = await validate_embed_token(
                                        db, request, i, u, e, n, s, 
                                        consume_nonce=False,                # 👈 разрешаем F5 / повторный рендер 
                                    )

    if integ.flow_id != flow_id:
        raise HTTPException(status_code=403, detail="Токен выдан для другого flow")

    # Агент должен быть опубликован
    pub_res = await db.execute(
        select(FlowPublication).where(
            FlowPublication.flow_id == flow_id,
            FlowPublication.is_published.is_(True),
        )
    )
    pub = pub_res.scalar_one_or_none()
    if not pub:
        raise HTTPException(status_code=404, detail="Агент не опубликован")

    await _sync_embed_user(db, u)
    chat = await _get_or_create_chat(db, flow_id, u)

    # Название агента
    client = LangflowClient()
    flows = await client.get_all_flows()
    flow = next((f for f in flows if f.get("id") == flow_id), None)
    agent_name = (pub.override_name or (flow.get("name") if flow else None) or "Агент")

    # История
    msgs_res = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.chat_id == chat.id)
        .order_by(ChatMessage.id.asc())
    )
    messages = list(msgs_res.scalars().all())

    # Ссылка на полную версию — отдельный короткоживущий подписанный токен
    from embed_auth import make_signature
    import secrets as _secrets, time as _time
    sso_expires = int(_time.time()) + 60
    sso_nonce = _secrets.token_hex(16)
    sso_sig = make_signature(integ.secret, u, sso_expires, sso_nonce)

    full_url = (
        f"{config.PUBLIC_BASE_URL}/embed/sso/{flow_id}/{chat.id}"
        f"?i={i}&u={u}&e={sso_expires}&n={sso_nonce}&s={sso_sig}"
    )

    await db.commit()

    return templates.TemplateResponse(
        "embed_chat.html",
        {
            "request": request,
            "flow_id": flow_id,
            "chat_id": chat.id,
            "agent_name": agent_name,
            "messages": messages,
            "full_url": full_url,
            "embed_user": u,
        },
    )


class EmbedMessagePayload(BaseModel):
    message: str
    i: str
    u: str
    e: int
    n: str
    s: str


@router.post("/embed/chat/{flow_id}/{chat_id}/send")
async def embed_chat_send(
    flow_id: str,
    chat_id: str,
    payload: EmbedMessagePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    integ = await validate_embed_token(
        db, request, payload.i, payload.u, payload.e, payload.n, payload.s,
        consume_nonce=False,   # 👈 ключевой момент
    )
    if integ.flow_id != flow_id:
        raise HTTPException(status_code=403, detail="Токен выдан для другого flow")

    # Проверим публикацию (на случай, если админ снял агент после открытия iframe)
    pub_res = await db.execute(
        select(FlowPublication).where(
            FlowPublication.flow_id == flow_id,
            FlowPublication.is_published.is_(True),
        )
    )
    if pub_res.scalar_one_or_none() is None:
        raise HTTPException(status_code=403, detail="Агент больше не опубликован")

    chat = await db.get(Chat, chat_id)
    if not chat or chat.flow_id != flow_id or chat.user_id != payload.u:
        raise HTTPException(status_code=404, detail="Чат не найден")

    text_in = payload.message.strip()
    if not text_in:
        raise HTTPException(status_code=400, detail="Пустое сообщение")

    if chat.title in ("Новый чат", "Встроенный чат"):
        chat.title = (text_in.split("\n")[0].strip()[:60] or chat.title)

    db.add(ChatMessage(chat_id=chat.id, role="user", content=text_in))

    client = LangflowClient()
    data = await client.run_flow(flow_id, text_in, session_id=chat.id)
    raw = extract_output_text(data)
    answer, thinking = parse_thinking(raw)

    db.add(ChatMessage(
        chat_id=chat.id, role="assistant",
        content=answer or "", thinking=thinking or None,
    ))
    db.add(LLMRequestLog(
        user_id=payload.u, flow_id=flow_id, chat_id=chat.id,
        chat_title=chat.title, question=text_in,
        answer=answer or "", thinking=thinking or None,
    ))
    chat.updated_at = datetime.utcnow()

    await db.commit()

    return {"text": answer, "thinking": thinking}

@router.get("/embed/sso/{flow_id}/{chat_id}")
async def embed_sso(
    flow_id: str,
    chat_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    i: str = Query(...),
    u: str = Query(...),
    e: int = Query(...),
    n: str = Query(...),
    s: str = Query(...),
):
    """Хендофф из iframe в полную версию приложения: ставим сессию и редиректим."""
    await validate_embed_token(db, request, i, u, e, n, s)

    chat = await db.get(Chat, chat_id)
    if not chat or chat.flow_id != flow_id or chat.user_id != u:
        raise HTTPException(status_code=404, detail="Чат не найден")

    await _sync_embed_user(db, u)

    # Проверим, что пользователь не отключён
    res = await db.execute(select(User).where(User.username == u))
    row = res.scalar_one_or_none()
    if row and row.is_disabled:
        raise HTTPException(status_code=403, detail="Учётная запись отключена")
    await db.commit()

    # Полноценная сессия (только на текущую вкладку — 7 дней, но is_admin=False)
    token = create_session({
        "username": u,
        "user_dn": f"cn=embed,dc=external",
        "is_admin": False,
        "is_system_admin": False,
    })

    resp = RedirectResponse(url=f"/chat/{flow_id}/{chat_id}", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax",
    )
    logger.info("Embed SSO: %s → /chat/%s/%s", u, flow_id[:8], chat_id[:8])
    return resp