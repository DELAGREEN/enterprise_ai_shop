import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, text

from database import AsyncSessionLocal, Base, engine
from models import Group, User

from session import get_current_user
from routers import admin, auth, chat, public, embed

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


MIGRATIONS = [
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS override_name VARCHAR(200)",
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS override_description TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE",
]

DEFAULT_GROUPS = [
    ("users",  "Пользователь",  "Базовая группа: доступна всем зарегистрированным"),
    ("admins", "Администратор", "Полный доступ к админке"),
]

async def _seed_defaults():
    """Идемпотентно создаёт системные группы users/admins, если их нет."""
    async with AsyncSessionLocal() as db:
        for gid, name, desc in DEFAULT_GROUPS:
            res = await db.execute(select(Group).where(Group.id == gid))
            if res.scalar_one_or_none() is None:
                db.add(Group(id=gid, name=name, description=desc, is_system=True))
                logger.info("Создана системная группа: %s", gid)
        await db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in MIGRATIONS:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                logger.warning("Миграция пропущена (%s): %s", stmt, e)

    # Засев системных групп ПОСЛЕ создания таблиц
    await _seed_defaults()

    logger.info("Схема БД инициализирована, системные группы проверены")
    yield
    await engine.dispose()
    logger.info("Приложение остановлено")


app = FastAPI(title="Langflow Agent Manager", lifespan=lifespan)

PUBLIC_PATHS = {"/auth/login", "/auth/logout", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static") or path.startswith("/embed/"):
        return await call_next(request)

    user = get_current_user(request)
    if not user:
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(url=f"/auth/login?next={path}", status_code=302)
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    # Проверяем, не отключён ли пользователь в БД прямо сейчас
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.username == user["username"]))
        row = res.scalar_one_or_none()
        if row and row.is_disabled:
            resp = RedirectResponse(url="/auth/login", status_code=302)
            resp.delete_cookie("session")
            return resp

    return await call_next(request)


app.include_router(public.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(embed.router, tags=["embed"])
app.mount("/static", StaticFiles(directory="static"), name="static")
