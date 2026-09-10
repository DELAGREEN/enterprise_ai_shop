import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from langflow_client import LangflowClient, extract_output_text, parse_thinking
from models import FlowPublication
from templating import templates

from session import get_current_user  # добавить импорт

logger = logging.getLogger(__name__)

router = APIRouter()


async def _is_published(db: AsyncSession, flow_id: str) -> bool:
    result = await db.execute(
        select(FlowPublication).where(
            FlowPublication.flow_id == flow_id,
            FlowPublication.is_published.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None



@router.get("/chat/{flow_id}", response_class=HTMLResponse)
async def chat_page(flow_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    if not await _is_published(db, flow_id):
        raise HTTPException(status_code=404, detail="Агент не найден или не опубликован")

    user = get_current_user(request)
    client = LangflowClient()
    flows = await client.get_all_flows()
    flow = next((f for f in flows if f.get("id") == flow_id), None)
    name = flow.get("name") if flow else flow_id

    return templates.TemplateResponse(
        "chat.html",
        {"request": request, "flow_id": flow_id, "name": name, "user": user},
    )


class MessagePayload(BaseModel):
    message: str
    session_id: str | None = None


@router.post("/chat/{flow_id}/send")
async def chat_send(
    flow_id: str,
    payload: MessagePayload,
    db: AsyncSession = Depends(get_db),
):
    if not await _is_published(db, flow_id):
        raise HTTPException(status_code=403, detail="Агент не опубликован")

    client = LangflowClient()
    data = await client.run_flow(flow_id, payload.message, payload.session_id)
    raw_text = extract_output_text(data)
    text, thinking = parse_thinking(raw_text)
    return {"text": text, "thinking": thinking}