import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from database import Base, engine
from session import get_current_user
from routers import admin, auth, chat, public

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


MIGRATIONS = [
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS override_name VARCHAR(200)",
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS override_description TEXT",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for stmt in MIGRATIONS:
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                logger.warning("Миграция пропущена (%s): %s", stmt, e)
    logger.info("Схема БД инициализирована")
    yield
    await engine.dispose()
    logger.info("Приложение остановлено")


app = FastAPI(title="Langflow Agent Manager", lifespan=lifespan)

PUBLIC_PATHS = {"/auth/login", "/auth/logout", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static"):
        return await call_next(request)

    user = get_current_user(request)
    if not user:
        if request.method in ("GET", "HEAD"):
            return RedirectResponse(url=f"/auth/login?next={path}", status_code=302)
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)

    return await call_next(request)


app.include_router(public.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chat.router)