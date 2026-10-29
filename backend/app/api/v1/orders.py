import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.reservation import OrderRead
from app.services import orders as service

router = APIRouter(
    prefix="/orders", tags=["orders"], responses={401: {"description": "Not authenticated"}}
)


@router.get("", response_model=list[OrderRead])
async def list_mine(
    user: CurrentUser, session: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[OrderRead]:
    """Your orders, newest first, with payment and pickup status."""
    return await service.list_orders(session, user, limit=limit)


@router.get("/{order_id}", response_model=OrderRead, responses={404: {"description": "Not found"}})
async def get_one(order_id: uuid.UUID, user: CurrentUser, session: DbSession) -> OrderRead:
    try:
        return await service.get_order(session, user, order_id)
    except service.OrderNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found") from None
