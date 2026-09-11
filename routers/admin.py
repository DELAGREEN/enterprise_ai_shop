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


@router.get("", response_class=HTMLResponse)
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
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

        eff_name = (p.override_name if p and p.override_name else lf_name)
        eff_desc = (p.override_description if p and p.override_description else lf_desc)

        items.append(
            {
                "id": fid,
                "langflow_name": lf_name,
                "langflow_description": lf_desc,
                "name": eff_name,
                "description": eff_desc,
                "name_overridden": bool(p and p.override_name),
                "description_overridden": bool(p and p.override_description),
                "is_published": bool(p and p.is_published),
            }
        )

    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "flows": items, "user": user},
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
        user.get("username"),
        payload.flow_id,
        "published" if payload.publish else "unpublished",
    )
    return {"status": "ok"}


# ---------- edit name / description ----------

class EditPayload(BaseModel):
    flow_id: str
    name: str | None = None          # None → не менять; "" → сбросить override
    description: str | None = None   # None → не менять; "" → сбросить override


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

    logger.info(
        "Админ %s: flow %s отредактирован (name=%r, desc=%r)",
        user.get("username"),
        payload.flow_id,
        row.override_name,
        row.override_description,
    )
    return {
        "status": "ok",
        "override_name": row.override_name,
        "override_description": row.override_description,
    }


# ---------- reset to Langflow ----------

class ResetPayload(BaseModel):
    flow_id: str


@router.post("/flow/reset")
async def flow_reset(
    payload: ResetPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = _require_admin(request)
    row = await _get_or_create_pub(db, payload.flow_id)

    row.override_name = None
    row.override_description = None
    row.updated_at = datetime.utcnow()
    await db.commit()

    logger.info("Админ %s: flow %s сброшен к версии Langflow", user.get("username"), payload.flow_id)
    return {"status": "ok"}