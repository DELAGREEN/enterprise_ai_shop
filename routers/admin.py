import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient
from models import FlowPublication
from session import get_current_user
from templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _require_admin(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Только администратор")
    return user


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/auth/login", status_code=302)
    if not user.get("is_admin"):
        # Обычный пользователь → на главную
        return RedirectResponse(url="/", status_code=302)

    client = LangflowClient()
    flows = await client.get_all_flows()

    result = await db.execute(select(FlowPublication))
    pubs = {p.flow_id: p for p in result.scalars().all()}

    items = []
    for flow in flows:
        fid = flow.get("id")
        p = pubs.get(fid)
        items.append(
            {
                "id": fid,
                "name": flow.get("name") or "Без имени",
                "description": flow.get("description") or "",
                "is_published": bool(p and p.is_published),
            }
        )

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "flows": items, "user": user},
    )


class PublishPayload(BaseModel):
    flow_id: str
    publish: bool


@router.post("/publish")
async def publish(
    payload: PublishPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _require_admin(request)  # 403 для не-админов

    result = await db.execute(
        select(FlowPublication).where(FlowPublication.flow_id == payload.flow_id)
    )
    row = result.scalar_one_or_none()
    now = datetime.utcnow()

    if row is None:
        row = FlowPublication(
            flow_id=payload.flow_id,
            is_published=payload.publish,
            published_at=now if payload.publish else None,
            updated_at=now,
        )
        db.add(row)
    else:
        row.is_published = payload.publish
        if payload.publish and not row.published_at:
            row.published_at = now
        row.updated_at = now

    await db.commit()
    logger.info(
        "Админ %s: flow %s => %s",
        user.get("username"),
        payload.flow_id,
        "published" if payload.publish else "unpublished",
    )
    return {"status": "ok"}