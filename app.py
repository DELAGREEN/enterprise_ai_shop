import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from database import Base, engine
from session import get_current_user
from routers import admin, auth, chat, public

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Схема БД инициализирована")
    yield
    await engine.dispose()
    logger.info("Приложение остановлено")


app = FastAPI(title="Langflow Agent Manager", lifespan=lifespan)


@app.middleware("http")
async def protect_admin(request: Request, call_next):
    path = request.url.path
    if path.startswith("/admin"):
        if not get_current_user(request):
            return RedirectResponse(url="/auth/login", status_code=302)
    return await call_next(request)


app.include_router(public.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chat.router)