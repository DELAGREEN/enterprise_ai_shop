import logging
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ldap_auth import authenticate_full
from session import MAX_AGE, SESSION_COOKIE, create_session, get_current_user
from templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_next(next_url: str | None) -> str:
    """Не позволяем редиректить на внешние домены."""
    if not next_url or not next_url.startswith("/"):
        return "/"
    return next_url


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
):
    user_data = authenticate_full(username, password)

    if user_data:
        token = create_session(user_data)
        resp = RedirectResponse(url=_safe_next(next), status_code=302)
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return resp

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Неверный логин или пароль",
            "next": next,
        },
        status_code=401,
    )


@router.get("/auth/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/auth/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp