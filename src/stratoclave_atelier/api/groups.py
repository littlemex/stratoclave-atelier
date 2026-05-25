"""``/api/groups`` REST router."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from stratoclave_atelier.api.deps import StoreDep, http_not_found
from stratoclave_atelier.api.schemas import GroupCreate, GroupRead
from stratoclave_atelier.core import NotFoundError

router = APIRouter(prefix="/api/groups", tags=["groups"])


@router.post(
    "",
    response_model=GroupRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_group(payload: GroupCreate, store: StoreDep) -> GroupRead:
    group = await store.create_group(name=payload.name, description=payload.description)
    return GroupRead.from_domain(group)


@router.get("", response_model=list[GroupRead])
async def list_groups(store: StoreDep) -> list[GroupRead]:
    groups = await store.list_groups()
    return [GroupRead.from_domain(g) for g in groups]


@router.get("/{group_id}", response_model=GroupRead)
async def get_group(group_id: UUID, store: StoreDep) -> GroupRead:
    try:
        group = await store.get_group(group_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    return GroupRead.from_domain(group)
