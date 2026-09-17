from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import AgentGroup, FlowPublication, UserGroup


async def user_group_ids(db: AsyncSession, username: str) -> set[str]:
    """Возвращает множество id групп, в которых состоит пользователь."""
    res = await db.execute(
        select(UserGroup.group_id).where(UserGroup.username == username)
    )
    return {gid for (gid,) in res.all()}


async def flow_group_ids(db: AsyncSession, flow_id: str) -> set[str]:
    """Возвращает множество id групп, к которым привязан агент."""
    res = await db.execute(
        select(AgentGroup.group_id).where(AgentGroup.flow_id == flow_id)
    )
    return {gid for (gid,) in res.all()}


async def is_flow_published(db: AsyncSession, flow_id: str) -> bool:
    """Агент опубликован?"""
    res = await db.execute(
        select(FlowPublication).where(
            FlowPublication.flow_id == flow_id,
            FlowPublication.is_published.is_(True),
        )
    )
    return res.scalar_one_or_none() is not None


async def can_user_access_flow(db: AsyncSession, user: dict, flow_id: str) -> bool:
    """
    Может ли пользователь открыть агента:
      1. Агент должен быть опубликован.
      2. Админ — всегда да.
      3. Агент без групповых привязок — всем.
      4. Иначе — пересечение групп агента и групп пользователя.
    """
    if not await is_flow_published(db, flow_id):
        return False

    if user.get("is_admin"):
        return True

    flow_groups = await flow_group_ids(db, flow_id)
    # нет привязок → доступ только админам
    if not flow_groups:
        return False

    user_groups = await user_group_ids(db, user["username"])
    return bool(flow_groups & user_groups)


async def visible_flow_ids(db: AsyncSession, user: dict) -> set[str]:
    """
    Множество flow_id, доступных пользователю (только опубликованные).
    Используется на дашборде.
    """
    pub_res = await db.execute(
        select(FlowPublication.flow_id).where(FlowPublication.is_published.is_(True))
    )
    published = {fid for (fid,) in pub_res.all()}

    if user.get("is_admin"):
        return published

    user_groups = await user_group_ids(db, user["username"])

    ag_res = await db.execute(select(AgentGroup.flow_id, AgentGroup.group_id))
    flow_groups: dict[str, set[str]] = {}
    for fid, gid in ag_res.all():
        flow_groups.setdefault(fid, set()).add(gid)

    visible: set[str] = set()
    for fid in published:
        groups = flow_groups.get(fid, set())
        if not groups:
            continue                    # без привязок — только для админов
        if groups & user_groups:
            visible.add(fid)
    return visible