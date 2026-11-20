from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import branding
import icons
from access import can_user_access_flow, visible_flow_ids   # 👈 RBAC
from config import THEMES, THEME_COOKIE
from database import get_db
from langflow_client import LangflowClient
from models import AgentFavorite, AppAsset, Chat, FlowPublication, IconLibrary, User
from session import get_current_user
from templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    if not user or "username" not in user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = user["username"]

    # 👇 Какие flow доступны пользователю по группам
    allowed = await visible_flow_ids(db, user)

    client = LangflowClient()
    flows = await client.get_all_flows()

    # Опубликованные
    pub_res = await db.execute(
        select(FlowPublication).where(FlowPublication.is_published.is_(True))
    )
    pubs = {p.flow_id: p for p in pub_res.scalars().all()}

    # Избранные
    fav_res = await db.execute(
        select(AgentFavorite).where(AgentFavorite.user_id == username)
    )
    favs = {f.flow_id: f for f in fav_res.scalars().all()}

    # Чаты
    cnt_res = await db.execute(
        select(Chat.flow_id, func.count(Chat.id))
        .where(Chat.user_id == username, Chat.deleted_at.is_(None))
        .group_by(Chat.flow_id)
    )
    chat_counts = {fid: n for fid, n in cnt_res.all()}

    # Какие иконки из библиотеки реально существуют (битые ссылки не отдаём)
    icon_ids = {p.icon_id for p in pubs.values() if p.icon_id}
    existing_icons: set[str] = set()
    if icon_ids:
        icon_res = await db.execute(
            select(IconLibrary.id).where(IconLibrary.id.in_(icon_ids))
        )
        existing_icons = {i for (i,) in icon_res.all()}

    agents = []
    for flow in flows:
        fid = flow.get("id")

        # 👇 Три условия: есть id, опубликован, доступен по RBAC
        if not fid or fid not in pubs or fid not in allowed:
            continue

        p = pubs[fid]
        name = p.override_name or flow.get("name") or "Без имени"
        desc = p.override_description or flow.get("description") or ""

        agents.append({
            "id": fid,
            "name": name,
            "description": desc,
            # Пустая строка — иконки нет (это значение по умолчанию)
            "icon": icons.url(p.icon_id) if p.icon_id in existing_icons else "",
            "chat_count": chat_counts.get(fid, 0),
            "is_favorite": fid in favs,
            "favorited_at": favs[fid].created_at if fid in favs else None,
        })

    # Сортировка: избранные сверху (по дате добавления desc), остальные — в исходном порядке
    favorites = sorted(
        [a for a in agents if a["is_favorite"]],
        key=lambda a: a["favorited_at"],
        reverse=True,
    )
    others = [a for a in agents if not a["is_favorite"]]

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "agents": favorites + others,
            "user": user,
            "favorites_count": len(favorites),
        },
    )


@router.get("/icons/{icon_id}")
async def icon_file(icon_id: str, db: AsyncSession = Depends(get_db)):
    """Отдаёт иконку агента из библиотеки."""
    icon = await db.get(IconLibrary, icon_id)
    if icon is None:
        raise HTTPException(status_code=404, detail="Иконка не найдена")
    return Response(
        content=icon.data,
        media_type=icon.mime or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/branding/icon")
async def branding_icon(db: AsyncSession = Depends(get_db)):
    """Отдаёт загруженную из админки эмблему (или 404, если её нет)."""
    asset = await db.get(AppAsset, branding.ASSET_LOGO)
    if asset is None:
        raise HTTPException(status_code=404, detail="Эмблема не загружена")
    return Response(
        content=asset.data,
        media_type=asset.mime or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=86400"},
    )


class ThemePayload(BaseModel):
    theme: str


@router.post("/api/theme")
async def save_theme(
    payload: ThemePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Запоминает выбранную тему: cookie (для рендера) и users.theme."""
    theme = (payload.theme or "").strip().lower()
    if theme not in THEMES:
        raise HTTPException(status_code=400, detail="Неизвестная тема")

    user = get_current_user(request)
    if user and user.get("username"):
        row = await db.get(User, user["username"])
        if row is not None:
            row.theme = theme
            await db.commit()

    resp = JSONResponse({"status": "ok", "theme": theme})
    resp.set_cookie(
        THEME_COOKIE, theme, max_age=60 * 60 * 24 * 365,
        httponly=False, samesite="lax",
    )
    return resp


class ToggleFavoritePayload(BaseModel):
    flow_id: str


@router.post("/api/favorites/toggle")
async def toggle_favorite(
    payload: ToggleFavoritePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = get_current_user(request)
    if not user or "username" not in user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = user["username"]

    # 👇 RBAC-проверка вместо ручной проверки публикации
    if not await can_user_access_flow(db, user, payload.flow_id):
        raise HTTPException(status_code=404, detail="Агент недоступен")

    res = await db.execute(
        select(AgentFavorite).where(
            AgentFavorite.user_id == username,
            AgentFavorite.flow_id == payload.flow_id,
        )
    )
    existing = res.scalar_one_or_none()

    if existing:
        await db.delete(existing)
        await db.commit()
        return JSONResponse({"status": "ok", "is_favorite": False})

    db.add(AgentFavorite(user_id=username, flow_id=payload.flow_id))
    await db.commit()
    return JSONResponse({"status": "ok", "is_favorite": True})