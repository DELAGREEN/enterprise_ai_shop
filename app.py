import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
import config
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import and_, or_, select, text

import chat_purge
import branding
from database import AsyncSessionLocal, Base, engine
from langflow_client import LangflowClient
from models import (
    AppAsset,
    AppSetting,
    AgentFavorite,
    AgentGroup,
    Chat,
    ChatMessage,
    EmbedNonce,
    FlowPublication,
    Group,
    IconLibrary,
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
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_stage VARCHAR",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_done INTEGER",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_total INTEGER",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_message TEXT",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_eta_seconds INTEGER",
    "ALTER TABLE processing_jobs ADD COLUMN IF NOT EXISTS progress_updated_at TIMESTAMP",
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS supports_files BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS show_thinking BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS deleted_by VARCHAR",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS langflow_purged_at TIMESTAMP",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS purge_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chats ADD COLUMN IF NOT EXISTS purge_error TEXT",
    "CREATE INDEX IF NOT EXISTS ix_chats_deleted_at ON chats (deleted_at)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS theme VARCHAR(10)",
    "CREATE TABLE IF NOT EXISTS app_settings ("
    " key VARCHAR(64) PRIMARY KEY,"
    " value TEXT,"
    " updated_at TIMESTAMP"
    ")",
    "CREATE TABLE IF NOT EXISTS app_assets ("
    " key VARCHAR(64) PRIMARY KEY,"
    " mime VARCHAR(100) NOT NULL,"
    " size INTEGER NOT NULL DEFAULT 0,"
    " data BYTEA NOT NULL,"
    " updated_at TIMESTAMP"
    ")",
    "CREATE TABLE IF NOT EXISTS icon_library ("
    " id VARCHAR(32) PRIMARY KEY,"
    " name VARCHAR(200) NOT NULL,"
    " mime VARCHAR(100) NOT NULL,"
    " size INTEGER NOT NULL DEFAULT 0,"
    " data BYTEA NOT NULL,"
    " uploaded_by VARCHAR,"
    " created_at TIMESTAMP NOT NULL DEFAULT NOW()"
    ")",
    "ALTER TABLE flow_publications ADD COLUMN IF NOT EXISTS icon_id VARCHAR",
    "CREATE INDEX IF NOT EXISTS ix_flow_publications_icon_id"
    " ON flow_publications (icon_id)",
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


# --- Фоновые поллеры: один ведущий на всё приложение ---
#
# Приложение запускается несколькими воркерами uvicorn, и каждый процесс
# выполняет lifespan — то есть поднимает свой поллер. Четыре воркера давали
# четырёхкратный опрос Langflow и БД на каждую активную задачу.
#
# Ведущего выбираем через advisory-лок Postgres: он привязан к соединению,
# поэтому падение процесса или обрыв связи снимают лок автоматически, и
# ведущим становится следующий освободившийся воркер.
POLLER_LOCK_JOBS = 8_150_001
POLLER_LOCK_PURGES = 8_150_002

# Как часто не-ведущий проверяет, освободился ли лок. Это единственный его
# запрос: пока лок занят, ни Langflow, ни таблицы задач он не трогает.
LOCK_RETRY_SECONDS = 20


class _LeaderLock:
    """Держит advisory-лок Postgres, пока живёт соединение.

    Лок сессионный: он снимается сам, когда соединение закрывается. Поэтому
    «ведущий» — это не флаг в памяти, который может остаться после падения,
    а состояние, которое БД гарантирует.
    """

    def __init__(self, key: int, name: str, engine=None):
        self.key = key
        self.name = name
        self._conn = None
        # engine передаётся явно только в тестах; в приложении берём общий.
        self._engine = engine

    @property
    def _db(self):
        if self._engine is not None:
            return self._engine
        return engine

    @property
    def held(self) -> bool:
        return self._conn is not None

    async def try_acquire(self) -> bool:
        """Пытается стать ведущим. Не блокирует и не бросает исключений."""
        if self._conn is not None:
            return True

        try:
            conn = await self._db.connect()
        except Exception as exc:
            logger.warning("Поллер %s: нет соединения с БД: %s", self.name, exc)
            return False

        try:
            res = await conn.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}
            )
            acquired = bool(res.scalar())
        except Exception as exc:
            logger.warning("Поллер %s: лок не взят: %s", self.name, exc)
            await _quiet_close(conn)
            return False

        if not acquired:
            # Лок у другого воркера — соединение больше не нужно.
            await _quiet_close(conn)
            return False

        self._conn = conn
        logger.info("Поллер %s: этот процесс ведущий", self.name)
        return True

    async def ensure(self) -> bool:
        """Гарантирует, что мы ведущий. Проверяет живость соединения."""
        if self._conn is not None:
            try:
                # Соединение держится нами, поэтому если оно отвечает —
                # лок всё ещё наш. Обрыв связи освободил бы его в Postgres.
                await self._conn.execute(text("SELECT 1"))
                return True
            except Exception as exc:
                logger.warning("Поллер %s: связь с БД потеряна: %s", self.name, exc)
                await self.release()
        return await self.try_acquire()

    async def release(self) -> None:
        """Отдаёт лок явно, чтобы при штатном останове не ждать таймаута."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self.key}
            )
        except Exception:
            pass  # сессия всё равно закроется и лок снимется
        await _quiet_close(conn)


async def _quiet_close(conn) -> None:
    try:
        await conn.close()
    except Exception:
        pass


async def _pause(seconds: float) -> None:
    """Пауза между итерациями поллера.

    Отдельная функция, потому что тесту нужно прервать бесконечный цикл:
    подменять ради этого `asyncio.sleep` глобально — значит ломать
    планировщик самого asyncio.
    """
    await asyncio.sleep(seconds)


async def _poll_jobs_once() -> None:
    """Один проход: свежие статусы из Langflow + недописанная история."""
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


async def _poll_jobs():
    """Опрашивает Langflow — но только в одном процессе из всех воркеров."""
    lock = _LeaderLock(POLLER_LOCK_JOBS, "задачи")
    try:
        while True:
            try:
                if not await lock.ensure():
                    # Ведущий — другой воркер. Ничего не опрашиваем,
                    # просто ждём, пока лок освободится.
                    await _pause(LOCK_RETRY_SECONDS)
                    continue
                await _poll_jobs_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Ошибка поллера задач: %s", exc)
                # Не знаем, жива ли сессия с локом после ошибки — начинаем
                # выборы заново, это дешевле, чем потерять ведущего.
                await lock.release()
                await _pause(LOCK_RETRY_SECONDS)
                continue
            await _pause(10)
    finally:
        await lock.release()


async def _poll_chat_purges():
    """Добирает чистку памяти удалённых чатов в Langflow.

    Пользователь видит чат удалённым сразу, а память в Langflow вычищается
    следом — здесь же подхватываются неудавшиеся попытки (Langflow лежал,
    сеть отвалилась, флоу принадлежит другому пользователю Langflow).
    """
    lock = _LeaderLock(POLLER_LOCK_PURGES, "чистка чатов")
    try:
        while True:
            try:
                if not await lock.ensure():
                    await _pause(LOCK_RETRY_SECONDS)
                    continue
                count = await chat_purge.purge_pending()
                if count:
                    logger.info("Память удалённых чатов вычищена: %s", count)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Ошибка поллера чистки чатов: %s", exc)
                await lock.release()
                await _pause(LOCK_RETRY_SECONDS)
                continue
            await _pause(60)
    finally:
        await lock.release()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _apply_migrations()

    # Засев системных групп ПОСЛЕ создания таблиц
    await _seed_defaults()
    await _seed_demo_mode_publication()

    # Брендинг из админки (важнее .env) — кэшируется в памяти
    async with AsyncSessionLocal() as db:
        await branding.load(db)

    logger.info("Схема БД инициализирована, системные группы проверены")
    poll_task = asyncio.create_task(_poll_jobs())
    purge_task = asyncio.create_task(_poll_chat_purges())
    try:
        yield
    finally:
        purge_task.cancel()
        poll_task.cancel()
        for task in (poll_task, purge_task):
            try:
                await task
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
        or path.startswith("/branding/")
        or path.startswith("/icons/")
        or path.startswith("/api/v1/")
        # Прогресс присылает компонент Langflow: у него нет cookie
        # пользователя, он авторизуется своим токеном в самом эндпоинте.
        or (path.startswith("/api/jobs/") and path.endswith("/progress"))):
        
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