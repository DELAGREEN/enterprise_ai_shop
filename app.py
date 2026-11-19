import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import config
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, or_, select, text

from database import AsyncSessionLocal, Base, engine
from langflow_client import LangflowClient
from models import (
    AgentFavorite,
    AgentGroup,
    Chat,
    ChatMessage,
    EmbedNonce,
    FlowPublication,
    Group,
    Integration,
    LLMRequestLog,
    ProcessingJob,
    User,
    UserGroup,
)

from session import get_current_user
from routers import admin, auth, chat, public, embed, api, jobs

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


MIGRATIONS = [
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS override_name VARCHAR(200)",
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS override_description TEXT",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_disabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE embed_integrations RENAME TO integrations",
    """
    ALTER TABLE integrations
        ADD COLUMN IF NOT EXISTS api_key_hash VARCHAR UNIQUE,
        ADD COLUMN IF NOT EXISTS api_key_prefix VARCHAR(16),
        ADD COLUMN IF NOT EXISTS auth_methods TEXT DEFAULT '["api_key"]'
    """,
    """
    UPDATE integrations
    SET auth_methods = '["embed_hmac"]'
    WHERE auth_methods IS NULL
    """,
    "CREATE TABLE IF NOT EXISTS processing_jobs ("
    " id VARCHAR PRIMARY KEY,"
    " flow_id VARCHAR NOT NULL,"
    " user_id VARCHAR NOT NULL,"
    " chat_id VARCHAR,"
    " job_type VARCHAR NOT NULL,"
    " status VARCHAR NOT NULL DEFAULT 'queued',"
    " langflow_job_id VARCHAR,"
    " input_files TEXT,"
    " input_text TEXT,"
    " result TEXT,"
    " error TEXT,"
    " history_saved BOOLEAN NOT NULL DEFAULT FALSE,"
    " created_at TIMESTAMP NOT NULL DEFAULT NOW(),"
    " updated_at TIMESTAMP,"
    " finished_at TIMESTAMP"
    ")",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS chat_id VARCHAR",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS history_saved BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS supports_files BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS show_thinking BOOLEAN NOT NULL DEFAULT FALSE",
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


async def _seed_demo_mode_publication():
    """Создаёт один демо-flow для локального UI-проверки без Langflow."""
    if not config.ENABLE_DEMO_MODE:
        return

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(FlowPublication).where(FlowPublication.flow_id == config.DEMO_FLOW_ID)
        )
        row = existing.scalar_one_or_none()
        if row is None:
            row = FlowPublication(
                flow_id=config.DEMO_FLOW_ID,
                is_published=True,
                override_name=config.DEMO_FLOW_NAME,
                override_description=config.DEMO_FLOW_DESCRIPTION,
            )
            db.add(row)
        else:
            row.is_published = True
            row.override_name = row.override_name or config.DEMO_FLOW_NAME
            row.override_description = row.override_description or config.DEMO_FLOW_DESCRIPTION

        await db.commit()


async def _apply_migrations() -> None:
    """Запускает каждую миграцию в отдельной транзакции.

    Это важно: если одна миграция падает, последующая не должна находиться
    в уже "забитой" транзакции. Новый connection per statement устраняет
    состояние "transaction aborted".
    """
    for stmt in MIGRATIONS:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(stmt))
        except Exception as e:
            logger.warning("Миграция пропущена (%s): %s", stmt, e)

async def _poll_jobs():
    while True:
        try:
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(ProcessingJob).where(
                        or_(
                            # Активные задачи — обновляем статус из Langflow
                            and_(
                                ProcessingJob.status.in_(["queued", "running"]),
                                ProcessingJob.langflow_job_id.isnot(None),
                            ),
                            # Завершённые, но ещё не попавшие в историю чата.
                            # Такой job мог закрыть другой процесс/воркер,
                            # поэтому дозаписываем историю здесь.
                            and_(
                                ProcessingJob.status.in_(
                                    ["completed", "failed", "timeout"]
                                ),
                                ProcessingJob.history_saved.is_(False),
                                ProcessingJob.chat_id.isnot(None),
                            ),
                        )
                    )
                )
                job_rows = list(res.scalars().all())
                client = LangflowClient()
                for job in job_rows:
                    try:
                        changed = False
                        if job.status in ("queued", "running") and job.langflow_job_id:
                            lf = await client.get_job_status(job.langflow_job_id)
                            changed = jobs.apply_job_status(job, lf)
                        if await jobs.save_job_history(db, job):
                            changed = True
                        if changed:
                            await db.commit()
                    except Exception as exc:
                        logger.warning("Не удалось обновить job %s: %s", job.id, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Ошибка поллера задач: %s", exc)
        await asyncio.sleep(10)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _apply_migrations()

    # Засев системных групп ПОСЛЕ создания таблиц
    await _seed_defaults()
    await _seed_demo_mode_publication()

    logger.info("Схема БД инициализирована, системные группы проверены")
    poll_task = asyncio.create_task(_poll_jobs())
    try:
        yield
    finally:
        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await engine.dispose()
        logger.info("Приложение остановлено")

app = FastAPI(title="Langflow Agent Manager", lifespan=lifespan)

PUBLIC_PATHS = {"/auth/login", "/auth/logout", "/favicon.ico", "/health"}


@app.get("/health")
async def health_check():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "up"}
    except Exception as exc:
        logger.warning("Health check failed: %s", exc)
        return JSONResponse(
            {"status": "down", "database": "down", "error": str(exc)},
            status_code=503,
        )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if (path in PUBLIC_PATHS 
        or path.startswith("/static") 
        or path.startswith("/embed/")
        or path.startswith("/api/v1/")):
        
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

#app.mount("/static", StaticFiles(directory="/app/static"), name="static")
app.mount("/static", StaticFiles(directory="./static"), name="static")
app.include_router(public.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(embed.router, tags=["embed"])
app.include_router(api.router)  
app.include_router(jobs.router)