from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient
from models import FlowPublication
from session import get_current_user
from templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)  # middleware уже проверил, что он есть
    client = LangflowClient()
    flows = await client.get_all_flows()

    result = await db.execute(
        select(FlowPublication).where(FlowPublication.is_published.is_(True))
    )
    published_ids = {p.flow_id for p in result.scalars().all()}

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
                }
            )

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "agents": agents, "user": user},
    )