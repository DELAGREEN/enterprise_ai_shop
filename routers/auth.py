import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADMIN_PASSWORD, ADMIN_USERNAME
from database import get_db
from ldap_auth import authenticate_ldap
from models import User, UserGroup
from session import MAX_AGE, SESSION_COOKIE, create_session, get_current_user
from templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_next(next_url: str | None) -> str:
    """Не позволяем редиректить на внешние домены."""
    if not next_url or not next_url.startswith("/"):
        return "/"
    return next_url


async def _sync_user(db: AsyncSession, username: str) -> None:
    """
    Авторегистрация: при первом входе создаёт User и добавляет в группу «users».
    При повторном — обновляет last_login.
    """
    res = await db.execute(select(User).where(User.username == username))
    user = res.scalar_one_or_none()

    if user is None:
        db.add(User(username=username))
        db.add(UserGroup(username=username, group_id="users"))
        logger.info("Зарегистрирован новый пользователь: %s", username)
    else:
        user.last_login = datetime.utcnow()
        logger.info("Повторный вход: %s", username)

    # flush, чтобы FK-связи были видны в этой же транзакции;
    # commit делаем в вызывающем коде после всех операций.
    await db.flush()


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
    # Уже залогинен — можно сразу отправлять дальше
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
    # 1. Пробуем LDAP
    result = authenticate_ldap(username, password)

    # 2. Fallback-админ из .env
    if not result and username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        logger.info("Fallback-вход администратора: %s", username)
        result = {
            "username": username,
            "user_dn": "cn=fallback",
            "is_ldap_admin": True,
        }

    if not result:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Неверный логин или пароль", "next": next},
            status_code=401,
        )

    # 3. Синхронизация с БД — пользователь появляется в users + группа users
    await _sync_user(db, username)

    # 4. Если пользователь в локальной группе admins — получает админку
    in_local_admins = await _user_is_local_admin(db, username)

    # 5. Коммит — без него ничего не сохранится!
    await db.commit()

    is_admin = bool(result["is_ldap_admin"] or in_local_admins)

    token = create_session({
        "username": username,
        "user_dn": result["user_dn"],
        "is_admin": is_admin,
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