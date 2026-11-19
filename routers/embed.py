import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from database import AsyncSessionLocal, get_db
from embed_auth import validate_embed_token
from langflow_client import LangflowClient, extract_output_text, parse_thinking
from models import Chat, ChatMessage, FlowPublication, LLMRequestLog, User, UserGroup
from session import MAX_AGE, SESSION_COOKIE, create_session
from streaming import iter_flow_events, sse_pack
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
            "show_thinking": bool(chat.show_thinking),
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


async def _save_embed_stream_result(
    chat_id: str,
    flow_id: str,
    username: str,
    title: str,
    question: str,
    answer: str | None,
    thinking: str,
) -> None:
    """Сохраняет вопрос и ответ embed-чата после завершения потока.

    ``answer is None`` — поток не дал ответа: сохраняем только вопрос.
    """
    async with AsyncSessionLocal() as session:
        chat = await session.get(Chat, chat_id)
        if chat is None:
            logger.warning("Embed-чат %s исчез до сохранения ответа", chat_id)
            return

        chat.title = title
        chat.updated_at = datetime.utcnow()

        session.add(ChatMessage(chat_id=chat_id, role="user", content=question))

        if answer is not None:
            session.add(
                ChatMessage(
                    chat_id=chat_id,
                    role="assistant",
                    content=answer,
                    thinking=thinking or None,
                )
            )
            session.add(
                LLMRequestLog(
                    user_id=username,
                    flow_id=flow_id,
                    chat_id=chat_id,
                    chat_title=title,
                    question=question,
                    answer=answer,
                    thinking=thinking or None,
                )
            )

        await session.commit()


@router.post("/embed/chat/{flow_id}/{chat_id}/send-stream")
async def embed_chat_send_stream(
    flow_id: str,
    chat_id: str,
    payload: EmbedMessagePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Потоковый ответ embed-чата (SSE)."""
    integ = await validate_embed_token(
        db, request, payload.i, payload.u, payload.e, payload.n, payload.s,
        consume_nonce=False,
    )
    if integ.flow_id != flow_id:
        raise HTTPException(status_code=403, detail="Токен выдан для другого flow")

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

    username = payload.u
    title = chat.title
    if title in ("Новый чат", "Встроенный чат"):
        title = text_in.split("\n")[0].strip()[:60] or title

    async def event_stream():
        client = LangflowClient()
        thinking_parts: list[str] = []
        answer_parts: list[str] = []
        final_text = ""
        failed = ""

        try:
            async for kind, chunk in iter_flow_events(
                client, flow_id, text_in, chat_id
            ):
                if kind == "thinking":
                    thinking_parts.append(chunk)
                elif kind == "answer":
                    answer_parts.append(chunk)
                elif kind == "reset":
                    thinking_parts.clear()
                    answer_parts = [chunk]
                elif kind == "final":
                    final_text = chunk
                    continue
                elif kind == "error":
                    failed = chunk
                    continue

                yield sse_pack(kind, {"text": chunk})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка стрима embed-чата %s/%s: %s", flow_id, chat_id, exc)
            failed = failed or "Ошибка flow. Попробуйте ещё раз."

        answer = "".join(answer_parts).strip()
        thinking = "".join(thinking_parts).strip()

        # Событие output — источник истины (см. chat_send_stream)
        if final_text:
            final_answer, final_thinking = parse_thinking(final_text)
            if final_answer and final_answer.strip():
                answer = final_answer.strip()
            if final_thinking and not thinking:
                thinking = final_thinking.strip()

        if not answer:
            logger.warning(
                "Стрим embed-чата %s/%s не дал ответа: %s", flow_id, chat_id, failed
            )
            try:
                await _save_embed_stream_result(
                    chat_id, flow_id, username, title, text_in, None, ""
                )
            except Exception:  # noqa: BLE001
                logger.exception("Не удалось сохранить вопрос embed-чата %s", chat_id)
            yield sse_pack(
                "error",
                {"error": failed or "Ошибка flow. Попробуйте ещё раз.", "retryable": True},
            )
            return

        try:
            await _save_embed_stream_result(
                chat_id, flow_id, username, title, text_in, answer, thinking
            )
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось сохранить ответ embed-чата %s", chat_id)

        yield sse_pack(
            "done", {"text": answer, "thinking": thinking, "title": title}
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class EmbedThinkingPayload(BaseModel):
    enabled: bool
    i: str
    u: str
    e: int
    n: str
    s: str


@router.post("/embed/chat/{flow_id}/{chat_id}/thinking")
async def embed_chat_thinking(
    flow_id: str,
    chat_id: str,
    payload: EmbedThinkingPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Сохраняет настройку «показывать мышление» для embed-чата."""
    integ = await validate_embed_token(
        db, request, payload.i, payload.u, payload.e, payload.n, payload.s,
        consume_nonce=False,
    )
    if integ.flow_id != flow_id:
        raise HTTPException(status_code=403, detail="Токен выдан для другого flow")

    chat = await db.get(Chat, chat_id)
    if not chat or chat.flow_id != flow_id or chat.user_id != payload.u:
        raise HTTPException(status_code=404, detail="Чат не найден")

    chat.show_thinking = bool(payload.enabled)
    await db.commit()
    return {"status": "ok", "show_thinking": chat.show_thinking}

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