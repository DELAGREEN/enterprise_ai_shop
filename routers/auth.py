import logging
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_PASSWORD, ADMIN_USERNAME, SYSTEM_ADMIN_ENABLED
from database import get_db
from ldap_auth import authenticate_ldap
from models import User, UserGroup
from session import MAX_AGE, SESSION_COOKIE, create_session, get_current_user
from templating import templates

from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_next(next_url: str | None) -> str:
    """Не позволяем редиректить на внешние домены или protocol-relative ссылки."""
    if not next_url:
        return "/"

    candidate = next_url.strip()
    if not candidate or "\n" in candidate or "\r" in candidate:
        return "/"

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or candidate.startswith("//"):
        return "/"
    if not candidate.startswith("/"):
        return "/"
    return candidate


async def _sync_user(db: AsyncSession, username: str) -> User:
    """
    Авторегистрация: при первом входе создаёт User и добавляет в группу «users».
    При повторном — обновляет last_login.
    """
    res = await db.execute(select(User).where(User.username == username))
    user = res.scalar_one_or_none()

    if user is None:
        user = User(username=username)
        db.add(user)
        if username != ADMIN_USERNAME:
            db.add(UserGroup(username=username, group_id="users"))
        logger.info("Зарегистрирован новый пользователь: %s", username)
    else:
        user.last_login = datetime.utcnow()
        logger.info("Повторный вход: %s", username)

    await db.flush()
    return user


async def _user_is_local_admin(db: AsyncSession, username: str) -> bool:
    """Проверяет, что пользователь состоит в локальной группе admins."""
    res = await db.execute(
        select(UserGroup).where(
            UserGroup.username == username,
            UserGroup.group_id == "admins",
        )
    )
    return res.scalar_one_or_none() is not None


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(_safe_next(request.query_params.get("next")), 302)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "next": request.query_params.get("next", ""),
        },
    )


@router.post("/auth/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if username == ADMIN_USERNAME:
        if not SYSTEM_ADMIN_ENABLED:
            logger.warning("Попытка входа, но учётка отключена через .env")
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "error": "Учётная запись отключена",
                    "next": next,
                },
                status_code=403,
            )

        if password != ADMIN_PASSWORD:
            return templates.TemplateResponse(
                "login.html",
                {"request": request, "error": "Неверный пароль", "next": next},
                status_code=401,
            )

        user_row = await _sync_user(db, username)

        if user_row.is_disabled:
            user_row.is_disabled = False
            logger.info("Сброшен is_disabled в БД для системного администратора (включён через .env)")
        await db.commit()

        token = create_session({
            "username": username,
            "user_dn": "cn=break-glass",
            "is_admin": True,
            "is_system_admin": True,
        })
        logger.info("Break-glass вход системного администратора: %s", username)

        resp = RedirectResponse(url=_safe_next(next), status_code=302)
        resp.set_cookie(
            SESSION_COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax",
        )
        return resp

    result = await run_in_threadpool(authenticate_ldap, username, password)

    if not result:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль", "next": next},
            status_code=401,
        )

    user_row = await _sync_user(db, username)

    if user_row.is_disabled:
        await db.rollback()
        logger.warning("Отклонён вход отключённого пользователя: %s", username)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Учётная запись отключена", "next": next},
            status_code=403,
        )

    in_local_admins = await _user_is_local_admin(db, username)
    is_admin = bool(result["is_ldap_admin"] or in_local_admins)
    await db.commit()

    token = create_session({
        "username": username,
        "user_dn": result["user_dn"],
        "is_admin": is_admin,
        "is_system_admin": False,
    })
    logger.info("Вход %s (is_admin=%s)", username, is_admin)

    resp = RedirectResponse(url=_safe_next(next), status_code=302)
    resp.set_cookie(
        SESSION_COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax",
    )
    return resp


@router.get("/auth/logout")
async def logout():
    resp = RedirectResponse(url="/auth/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp
