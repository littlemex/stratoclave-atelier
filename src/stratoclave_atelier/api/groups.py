"""``/api/groups`` REST router.

Stage L extends the original CRUD surface: groups now carry a colour
that the Fork DAG renders, so the router exposes PATCH / DELETE on
top of the create / list / get verbs. Sessions can be moved into or
out of a group via ``PUT /api/sessions/{id}/group`` (handled in
:mod:`stratoclave_atelier.api.sessions`); the constraint that only
root sessions can be assigned to a group is enforced at the store
layer.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from stratoclave_atelier.api.deps import StoreDep, http_conflict, http_not_found
from stratoclave_atelier.api.schemas import GroupCreate, GroupRead, GroupUpdate
from stratoclave_atelier.core import ConflictError, NotFoundError

router = APIRouter(prefix="/api/groups", tags=["groups"])


@router.post(
    "",
    response_model=GroupRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_group(payload: GroupCreate, store: StoreDep) -> GroupRead:
    group = await store.create_group(
        name=payload.name,
        description=payload.description,
        color=payload.color,
    )
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


@router.patch("/{group_id}", response_model=GroupRead)
async def update_group(
    group_id: UUID,
    payload: GroupUpdate,
    store: StoreDep,
) -> GroupRead:
    """Rename / recolour an existing group.

    The handler rejects an empty body (all fields ``None``) so a
    misclick on the rename modal cannot silently no-op. Each field
    independently overrides the persisted value.
    """

    try:
        group = await store.update_group(
            group_id,
            name=payload.name,
            description=payload.description,
            color=payload.color,
        )
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    except ConflictError as exc:
        raise http_conflict(exc) from exc
    return GroupRead.from_domain(group)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(group_id: UUID, store: StoreDep) -> None:
    """Delete a group; existing sessions are detached (group_id -> NULL).

    This mirrors the asyncpg ``ON DELETE SET NULL`` foreign key on
    ``sessions.group_id`` so the in-memory backend behaves the same.
    """

    try:
        await store.delete_group(group_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
