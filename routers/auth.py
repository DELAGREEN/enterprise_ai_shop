import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ldap_auth import authenticate
from session import MAX_AGE, SESSION_COOKIE, create_session
from templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": None}
    )


@router.post("/auth/login")
async def login_submit(
    request: Request, username: str = Form(...), password: str = Form(...)
):
    if authenticate(username, password):
        token = create_session({"user": username})
        resp = RedirectResponse(url="/admin", status_code=302)
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
        {"request": request, "error": "Неверный логин или пароль"},
        status_code=401,
    )


@router.get("/auth/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp