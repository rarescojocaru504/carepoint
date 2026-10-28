"""Operator/admin API. Every route here requires the operator or admin role;
individual routes may narrow that further."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.deps import DbSession, require_role
from app.models import User, UserRole
from app.schemas.user import UserRead

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_role(UserRole.OPERATOR, UserRole.ADMIN))],
    responses={401: {"description": "Not authenticated"}, 403: {"description": "Forbidden"}},
)


@router.get(
    "/users",
    response_model=list[UserRead],
    # Personal data: admins only, not operators.
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)
async def list_users(
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[UserRead]:
    users = await session.scalars(
        select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
    )
    return [UserRead.model_validate(user) for user in users]
