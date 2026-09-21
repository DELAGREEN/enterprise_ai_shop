import logging
import uuid
import importlib.metadata
from datetime import datetime, timedelta
from langflow_client import LangflowClient, invalidate_flow_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text, delete
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    ADMIN_USERNAME,
    SYSTEM_ADMIN_ENABLED,
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
from models import (
    AgentFavorite,
    AgentGroup,         
    Chat,
    ChatMessage,
    FlowPublication,
    Group,              
    LLMRequestLog,
    User,
    UserGroup,
)
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

    # Загружаем все группы и их привязки к агентам
    g_res = await db.execute(select(Group))
    all_groups = {g.id: g.name for g in g_res.scalars().all()}

    ag_res = await db.execute(select(AgentGroup.flow_id, AgentGroup.group_id))
    flow_groups: dict[str, list[str]] = {}
    for fid, gid in ag_res.all():
        flow_groups.setdefault(fid, []).append(gid)

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
            "all_groups": all_groups,       
            "flow_groups": flow_groups,     
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
    invalidate_flow_cache() # сбрасываем кэш
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
    invalidate_flow_cache() # сбрасываем кэш
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


_PACKAGES_CACHE: list[dict] | None = None

# Пакеты, которые обычно «шумят» в runtime: инструменты сборки,
# линтеры, тестовые фреймворки, транзитивные утилиты.
_NOISY_EXACT = {
    # build / packaging
    "pip", "setuptools", "wheel", "build", "hatchling",
    "flit", "flit-core", "poetry", "poetry-core", "twine",
    "pyproject-hooks",
    # dev / test
    "pytest", "coverage", "mypy", "ruff", "black", "isort",
    "flake8", "tox", "nox", "pre-commit", "virtualenv",
    "distlib", "platformdirs", "filelock", "identify",
    "nodeenv", "cfgv",
}
_NOISY_PREFIXES = ("pytest-", "mypy-", "flake8-", "black-", "isort-", "types-")


def _is_noisy_package(name: str) -> bool:
    low = name.lower().replace("_", "-")
    if low in _NOISY_EXACT:
        return True
    return any(low.startswith(p) for p in _NOISY_PREFIXES)


def _collect_packages() -> list[dict]:
    """Список всех установленных пакетов: [{name, version, noisy}, ...]."""
    global _PACKAGES_CACHE
    if _PACKAGES_CACHE is not None:
        return _PACKAGES_CACHE

    seen: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        try:
            name = dist.metadata["Name"]
            if not name:
                continue
            seen[name] = dist.version or "—"
        except Exception:
            continue

    packages = [
        {"name": n, "version": v, "noisy": _is_noisy_package(n)}
        for n, v in sorted(seen.items(), key=lambda x: x[0].lower())
    ]
    _PACKAGES_CACHE = packages
    logger.info("Собрано пакетов окружения: %d", len(packages))
    return packages


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

    packages = _collect_packages()
    app_count = sum(1 for p in packages if not p["noisy"])
    noisy_count = len(packages) - app_count

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
                "system_admin_username": ADMIN_USERNAME,
                "system_admin_enabled": SYSTEM_ADMIN_ENABLED,
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
                "packages": packages, 
                "app_count": app_count,
                "noisy_count": noisy_count,
            },
        },
    )


# ---------- GROUPS ----------

@router.get("/groups", response_class=HTMLResponse)
async def admin_groups(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)

    res = await db.execute(select(Group).order_by(Group.is_system.desc(), Group.name))
    groups = list(res.scalars().all())

    mem_res = await db.execute(
        select(UserGroup.group_id, func.count()).group_by(UserGroup.group_id)
    )
    members = dict(mem_res.all())

    ag_res = await db.execute(
        select(AgentGroup.group_id, func.count()).group_by(AgentGroup.group_id)
    )
    agents = dict(ag_res.all())

    items = [{
        "id": g.id, "name": g.name, "description": g.description,
        "is_system": g.is_system,
        "members": members.get(g.id, 0),
        "agents": agents.get(g.id, 0),
    } for g in groups]

    return templates.TemplateResponse(
        "admin_groups.html",
        {"request": request, "user": user, "active_menu": "groups", "groups": items},
    )


class GroupCreatePayload(BaseModel):
    name: str
    description: str | None = None


@router.post("/groups/create")
async def groups_create(
    payload: GroupCreatePayload, request: Request, db: AsyncSession = Depends(get_db)
):
    _require_admin(request)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Пустое название")

    res = await db.execute(select(Group).where(Group.name == name))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Группа с таким именем уже существует")

    gid = uuid.uuid4().hex
    db.add(Group(id=gid, name=name, description=(payload.description or "").strip() or None))
    await db.commit()
    return {"status": "ok", "id": gid}


class GroupEditPayload(BaseModel):
    id: str
    name: str
    description: str | None = None


@router.post("/groups/edit")
async def groups_edit(
    payload: GroupEditPayload, request: Request, db: AsyncSession = Depends(get_db)
):
    _require_admin(request)
    g = await db.get(Group, payload.id)
    if not g:
        raise HTTPException(status_code=404, detail="Группа не найдена")

    # системные группы редактировать нельзя
    if g.is_system:
        raise HTTPException(status_code=400, detail="Системную группу нельзя редактировать")

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Пустое название")

    res = await db.execute(select(Group).where(Group.name == name, Group.id != payload.id))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Имя уже занято")

    g.name = name
    g.description = (payload.description or "").strip() or None
    await db.commit()
    return {"status": "ok"}


class GroupDeletePayload(BaseModel):
    id: str


@router.post("/groups/delete")
async def groups_delete(
    payload: GroupDeletePayload, request: Request, db: AsyncSession = Depends(get_db)
):
    _require_admin(request)
    g = await db.get(Group, payload.id)
    if not g:
        raise HTTPException(status_code=404, detail="Группа не найдена")
    if g.is_system:
        raise HTTPException(status_code=400, detail="Системную группу удалить нельзя")

    await db.execute(delete(UserGroup).where(UserGroup.group_id == g.id))
    await db.execute(delete(AgentGroup).where(AgentGroup.group_id == g.id))
    await db.delete(g)
    await db.commit()
    return {"status": "ok"}


# ---------- USERS ----------

@router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        return RedirectResponse(url="/", status_code=302)

    res = await db.execute(select(User).order_by(User.username))
    users = list(res.scalars().all())

    ug_res = await db.execute(select(UserGroup))
    memberships: dict[str, set[str]] = {}
    for ug in ug_res.scalars().all():
        memberships.setdefault(ug.username, set()).add(ug.group_id)

    g_res = await db.execute(select(Group).order_by(Group.is_system.desc(), Group.name))
    all_groups = list(g_res.scalars().all())

    other_admins = await _count_admins(db)

    items = [{
        "username": u.username,
        "created_at": u.created_at,
        "last_login": u.last_login,
        "group_ids": memberships.get(u.username, set()),
        "is_system_admin": (u.username == ADMIN_USERNAME),
        "is_disabled": u.is_disabled,
    } for u in users]

    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request, "user": user, "active_menu": "users",
            "users": items,
            "groups": [{"id": g.id, "name": g.name, "is_system": g.is_system}
                       for g in all_groups],
            "system_admin_username": ADMIN_USERNAME,
            "other_admins_count": other_admins,
            "system_admin_enabled": SYSTEM_ADMIN_ENABLED,
        },
    )


class UserGroupsPayload(BaseModel):
    username: str
    group_ids: list[str]


@router.post("/user/groups")
async def set_user_groups(
    payload: UserGroupsPayload, request: Request, db: AsyncSession = Depends(get_db)
):
    _require_admin(request)

    # Системному администратору группы назначать нельзя
    if payload.username == ADMIN_USERNAME:
        raise HTTPException(
            status_code=400,
            detail="Системному администратору нельзя назначать группы",
        )

    u = await db.get(User, payload.username)
    if not u:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Нельзя снять последнего администратора
    was_res = await db.execute(
        select(UserGroup).where(
            UserGroup.username == payload.username,
            UserGroup.group_id == "admins",
        )
    )
    was_admin = was_res.scalar_one_or_none() is not None
    will_be_admin = "admins" in payload.group_ids

    if was_admin and not will_be_admin:
        remaining = await _count_admins(db, exclude=payload.username)
        if remaining == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Нельзя снять с последнего администратора. "
                    "Сначала назначьте другого администратора — иначе вы "
                    "потеряете доступ к админке."
                ),
            )

    # Проверяем, что все запрошенные группы существуют
    if payload.group_ids:
        res = await db.execute(select(Group.id).where(Group.id.in_(payload.group_ids)))
        existing = {gid for (gid,) in res.all()}
        missing = set(payload.group_ids) - existing
        if missing:
            raise HTTPException(status_code=400, detail=f"Группы не найдены: {missing}")

    await db.execute(delete(UserGroup).where(UserGroup.username == payload.username))
    for gid in payload.group_ids:
        db.add(UserGroup(username=payload.username, group_id=gid))
    await db.commit()
    return {"status": "ok"}


class UserDisablePayload(BaseModel):
    username: str
    disabled: bool


async def _count_admins(db: AsyncSession, exclude: str | None = None) -> int:
    """
    Сколько пользователей состоит в группе admins.
    Если задан exclude — исключает этого пользователя из подсчёта.
    """
    q = select(func.count()).select_from(UserGroup).where(UserGroup.group_id == "admins")
    if exclude:
        q = q.where(UserGroup.username != exclude)
    return (await db.execute(q)).scalar() or 0


@router.post("/user/disable")
async def user_disable(request: Request):
    _require_admin(request)
    raise HTTPException(
        status_code=400,
        detail="Управление системным администратором выполняется через переменные окружения (.env)",
    )

# ---------- AGENT GROUPS ----------

@router.get("/agent/{flow_id}/groups")
async def agent_groups_get(
    flow_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    _require_admin(request)
    res = await db.execute(
        select(AgentGroup.group_id).where(AgentGroup.flow_id == flow_id)
    )
    return {"flow_id": flow_id, "group_ids": [gid for (gid,) in res.all()]}


class AgentGroupsPayload(BaseModel):
    flow_id: str
    group_ids: list[str]


@router.post("/agent/groups")
async def agent_groups_set(
    payload: AgentGroupsPayload, request: Request, db: AsyncSession = Depends(get_db)
):
    _require_admin(request)

    if payload.group_ids:
        res = await db.execute(select(Group.id).where(Group.id.in_(payload.group_ids)))
        existing = {gid for (gid,) in res.all()}
        missing = set(payload.group_ids) - existing
        if missing:
            raise HTTPException(status_code=400, detail=f"Группы не найдены: {missing}")

    await db.execute(delete(AgentGroup).where(AgentGroup.flow_id == payload.flow_id))
    for gid in payload.group_ids:
        db.add(AgentGroup(flow_id=payload.flow_id, group_id=gid))
    await db.commit()
    invalidate_flow_cache() # сбросить кэш
    return {"status": "ok"}