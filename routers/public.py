from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient
from models import AgentFavorite, Chat, FlowPublication
from session import get_current_user
from templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    username = user["username"]

    client = LangflowClient()
    flows = await client.get_all_flows()

    # Опубликованные
    pub_res = await db.execute(
        select(FlowPublication).where(FlowPublication.is_published.is_(True))
    )
    pubs = {p.flow_id: p for p in pub_res.scalars().all()}

    # Избранные текущего пользователя
    fav_res = await db.execute(
        select(AgentFavorite).where(AgentFavorite.user_id == username)
    )
    favs = {f.flow_id: f for f in fav_res.scalars().all()}

    # Кол-во чатов пользователя по каждому flow
    cnt_res = await db.execute(
        select(Chat.flow_id, func.count(Chat.id))
        .where(Chat.user_id == username)
        .group_by(Chat.flow_id)
    )
    chat_counts = {fid: n for fid, n in cnt_res.all()}

    agents = []
    for flow in flows:
        fid = flow.get("id")
        if not fid or fid not in pubs:
            continue

        p = pubs[fid]
        name = p.override_name or flow.get("name") or "Без имени"
        desc = p.override_description or flow.get("description") or ""

        agents.append(
            {
                "id": fid,
                "name": name,
                "description": desc,
                "icon": "🤖",
                "chat_count": chat_counts.get(fid, 0),
                "is_favorite": fid in favs,
                "favorited_at": favs[fid].created_at if fid in favs else None,
            }
        )

    # Сортировка: избранные сверху (по дате добавления desc), остальные — в исходном порядке
    favorites = sorted(
        [a for a in agents if a["is_favorite"]],
        key=lambda a: a["favorited_at"],
        reverse=True,
    )
    others = [a for a in agents if not a["is_favorite"]]
    ordered = favorites + others

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "agents": ordered,
            "user": user,
            "favorites_count": len(favorites),
        },
    )


class ToggleFavoritePayload(BaseModel):
    flow_id: str


@router.post("/api/favorites/toggle")
async def toggle_favorite(
    payload: ToggleFavoritePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = get_current_user(request)
    username = user["username"]

    # Проверим, что агент вообще опубликован — иначе непонятно, что фаворитим
    pub_res = await db.execute(
        select(FlowPublication).where(
            FlowPublication.flow_id == payload.flow_id,
            FlowPublication.is_published.is_(True),
        )
    )
    if pub_res.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Агент не найден или не опубликован")

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