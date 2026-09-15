import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    ADMIN_USERNAME,
    DATABASE_URL,
    LANGFLOW_API_KEY,
    LANGFLOW_URL,
    LDAP_ADMIN_GROUP,
    LDAP_BASE_DN,
    LDAP_SERVER,
    LDAP_USE_SSL,
    LDAP_USER_ATTR,
    LOCAL_TZ_OFFSET_HOURS,
)
from database import get_db
from langflow_client import LangflowClient
from models import Chat, ChatMessage, FlowPublication, LLMRequestLog
from session import get_current_user
from templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


# ---------- общие проверки ----------

def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Только администратор")
    return user


async def _get_or_create_pub(db: AsyncSession, flow_id: str) -> FlowPublication:
    res = await db.execute(
        select(FlowPublication).where(FlowPublication.flow_id == flow_id)
    )
    row = res.scalar_one_or_none()
    if row is None:
        row = FlowPublication(flow_id=flow_id, is_published=False)
        db.add(row)
        await db.flush()
    return row


def _parse_local_date(s: str, end: bool = False) -> datetime | None:
    """'YYYY-MM-DD' (локальная) → naive-UTC datetime."""
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    if end:
        d = d + timedelta(days=1)
    return d - timedelta(hours=LOCAL_TZ_OFFSET_HOURS)


# ---------- редирект с корня админки ----------

@router.get("", response_class=HTMLResponse)
async def admin_root(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)
    return RedirectResponse(url="/admin/publications", status_code=302)


# ---------- 1. Публикации ----------

@router.get("/publications", response_class=HTMLResponse)
async def admin_publications(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)

    client = LangflowClient()
    flows = await client.get_all_flows()

    res = await db.execute(select(FlowPublication))
    pubs = {p.flow_id: p for p in res.scalars().all()}

    items = []
    for flow in flows:
        fid = flow.get("id")
        p = pubs.get(fid)

        lf_name = flow.get("name") or "Без имени"
        lf_desc = flow.get("description") or ""

        items.append(
            {
                "id": fid,
                "langflow_name": lf_name,
                "langflow_description": lf_desc,
                "name": p.override_name if p and p.override_name else lf_name,
                "description": p.override_description if p and p.override_description else lf_desc,
                "name_overridden": bool(p and p.override_name),
                "description_overridden": bool(p and p.override_description),
                "is_published": bool(p and p.is_published),
            }
        )

    return templates.TemplateResponse(
        "admin_publications.html",
        {
            "request": request,
            "flows": items,
            "user": user,
            "active_menu": "publications",
        },
    )


# ---------- publish / unpublish ----------

class PublishPayload(BaseModel):
    flow_id: str
    publish: bool


@router.post("/publish")
async def publish(
    payload: PublishPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _require_admin(request)
    row = await _get_or_create_pub(db, payload.flow_id)
    now = datetime.utcnow()

    row.is_published = payload.publish
    if payload.publish and not row.published_at:
        row.published_at = now
    row.updated_at = now

    await db.commit()
    logger.info(
        "Админ %s: flow %s => %s",
        user.get("username"), payload.flow_id,
        "published" if payload.publish else "unpublished",
    )
    return {"status": "ok"}


# ---------- edit name / description ----------

class EditPayload(BaseModel):
    flow_id: str
    name: str | None = None
    description: str | None = None


@router.post("/flow/edit")
async def flow_edit(
    payload: EditPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _require_admin(request)
    row = await _get_or_create_pub(db, payload.flow_id)

    if payload.name is not None:
        stripped = payload.name.strip()
        row.override_name = stripped[:200] if stripped else None

    if payload.description is not None:
        stripped = payload.description.strip()
        row.override_description = stripped or None

    row.updated_at = datetime.utcnow()
    await db.commit()
    logger.info("Админ %s: flow %s отредактирован", user.get("username"), payload.flow_id)
    return {"status": "ok"}


# ---------- 2. История чатов ----------

@router.get("/history", response_class=HTMLResponse)
async def admin_history(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_filter: str = Query("", alias="user"),
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    flow_filter: str = Query("", alias="flow"),
    sort: str = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=500),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)

    # Базовый запрос
    query = select(LLMRequestLog)

    if user_filter:
        query = query.where(LLMRequestLog.user_id == user_filter)
    if flow_filter:
        query = query.where(LLMRequestLog.flow_id == flow_filter)

    dt_from = _parse_local_date(date_from, end=False)
    dt_to = _parse_local_date(date_to, end=True)
    if dt_from:
        query = query.where(LLMRequestLog.created_at >= dt_from)
    if dt_to:
        query = query.where(LLMRequestLog.created_at < dt_to)

    # Сортировка
    order = LLMRequestLog.created_at.desc() if sort != "asc" else LLMRequestLog.created_at.asc()

    # Всего строк по фильтру
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    # Пагинация
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    offset = (page - 1) * per_page

    rows_res = await db.execute(
        query.order_by(order, LLMRequestLog.id.desc()).offset(offset).limit(per_page)
    )
    rows = list(rows_res.scalars().all())

    # Список пользователей для фильтра
    users_res = await db.execute(
        select(LLMRequestLog.user_id).distinct().order_by(LLMRequestLog.user_id)
    )
    users = [u for (u,) in users_res.all()]

    # Список flow для фильтра (id → отображаемое имя)
    flows_res = await db.execute(
        select(LLMRequestLog.flow_id).distinct().order_by(LLMRequestLog.flow_id)
    )
    flow_ids = [f for (f,) in flows_res.all()]

    # Статистика по всей таблице (не по фильтру)
    stats_res = await db.execute(
        select(
            func.min(LLMRequestLog.created_at),
            func.max(LLMRequestLog.created_at),
            func.count(LLMRequestLog.id),
        )
    )
    min_dt, max_dt, total_all = stats_res.one()

    # Размеры таблиц в БД
    size_res = await db.execute(text("""
        SELECT
            pg_total_relation_size('llm_request_log') AS log_size,
            pg_total_relation_size('chat_messages')   AS msg_size,
            pg_total_relation_size('chats')           AS chats_size,
            pg_total_relation_size('flow_publications') AS pub_size
    """))
    sizes = size_res.mappings().one()

    total_db = sum(v or 0 for v in sizes.values())

    return templates.TemplateResponse(
        "admin_history.html",
        {
            "request": request,
            "user": user,
            "active_menu": "history",
            "rows": rows,
            "users": users,
            "flow_ids": flow_ids,
            "filters": {
                "user": user_filter,
                "from": date_from,
                "to": date_to,
                "flow": flow_filter,
                "sort": sort,
            },
            "page": page,
            "per_page": per_page,
            "pages": pages,
            "total": total,
            "cfg_tz": LOCAL_TZ_OFFSET_HOURS,
            "stats": {
                "min_dt": min_dt,
                "max_dt": max_dt,
                "total_all": total_all or 0,
            },
            "sizes": {
                "log": sizes["log_size"] or 0,
                "messages": sizes["msg_size"] or 0,
                "chats": sizes["chats_size"] or 0,
                "publications": sizes["pub_size"] or 0,
                "total": total_db,
            },
        },
)


# ---------- 3. Настройки / диагностика ----------

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)

    # Счётчики
    def _scalar(q):
        return q

    chats_cnt = (await db.execute(select(func.count(Chat.id)))).scalar() or 0
    msgs_cnt = (await db.execute(select(func.count(ChatMessage.id)))).scalar() or 0
    logs_cnt = (await db.execute(select(func.count(LLMRequestLog.id)))).scalar() or 0
    pub_cnt = (
        await db.execute(
            select(func.count(FlowPublication.flow_id)).where(FlowPublication.is_published.is_(True))
        )
    ).scalar() or 0

    # Размеры
    size_res = await db.execute(text("""
        SELECT
            pg_total_relation_size('llm_request_log') AS log_size,
            pg_total_relation_size('chat_messages')   AS msg_size,
            pg_total_relation_size('chats')           AS chats_size,
            pg_total_relation_size('flow_publications') AS pub_size,
            pg_database_size(current_database())      AS db_size
    """))
    sizes = size_res.mappings().one()

    # Скрываем пароль в DATABASE_URL
    safe_db_url = DATABASE_URL
    try:
        from urllib.parse import urlparse, urlunparse
        p = urlparse(DATABASE_URL)
        if p.password:
            netloc = f"{p.username}:***@{p.hostname}"
            if p.port:
                netloc += f":{p.port}"
            safe_db_url = urlunparse(p._replace(netloc=netloc))
    except Exception:
        pass

    import sys
    import fastapi as _fastapi

    return templates.TemplateResponse(
        "admin_settings.html",
        {
            "request": request,
            "user": user,
            "active_menu": "settings",
            "cfg": {
                "langflow_url": LANGFLOW_URL,
                "langflow_api_key_set": bool(LANGFLOW_API_KEY),
                "ldap_server": LDAP_SERVER,
                "ldap_use_ssl": LDAP_USE_SSL,
                "ldap_base_dn": LDAP_BASE_DN,
                "ldap_user_attr": LDAP_USER_ATTR,
                "ldap_admin_group": LDAP_ADMIN_GROUP or "(не задано — LDAP-пользователи не админы)",
                "admin_username_fallback": ADMIN_USERNAME,
                "tz_offset": LOCAL_TZ_OFFSET_HOURS,
                "database_url": safe_db_url,
            },
            "counts": {
                "chats": chats_cnt,
                "messages": msgs_cnt,
                "logs": logs_cnt,
                "published": pub_cnt,
            },
            "sizes": {
                "log": sizes["log_size"] or 0,
                "messages": sizes["msg_size"] or 0,
                "chats": sizes["chats_size"] or 0,
                "publications": sizes["pub_size"] or 0,
                "db": sizes["db_size"] or 0,
            },
            "runtime": {
                "python": sys.version.split()[0],
                "fastapi": _fastapi.__version__,
            },
        },
    )