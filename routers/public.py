from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient
from models import Chat, FlowPublication
from session import get_current_user
from templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)

    client = LangflowClient()
    flows = await client.get_all_flows()

    pub_res = await db.execute(
        select(FlowPublication).where(FlowPublication.is_published.is_(True))
    )
    published_ids = {p.flow_id for p in pub_res.scalars().all()}

    # Сколько чатов у пользователя по каждому flow
    cnt_res = await db.execute(
        select(Chat.flow_id, func.count(Chat.id))
        .where(Chat.user_id == user["username"])
        .group_by(Chat.flow_id)
    )
    chat_counts = {fid: n for fid, n in cnt_res.all()}

    agents = []
    for flow in flows:
        fid = flow.get("id")
        if fid and fid in published_ids:
            agents.append(
                {
                    "id": fid,
                    "name": flow.get("name") or "Без имени",
                    "description": flow.get("description") or "",
                    "icon": "🤖",
                    "chat_count": chat_counts.get(fid, 0),
                }
            )

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "agents": agents, "user": user},
    )