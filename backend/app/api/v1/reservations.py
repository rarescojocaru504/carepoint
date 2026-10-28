import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import CurrentUser, DbSession, RedisClient, enforce_rate_limit
from app.core.config import get_settings
from app.schemas.reservation import ReservationCreate, ReservationRead
from app.services import reservations as service

router = APIRouter(
    prefix="/reservations",
    tags=["reservations"],
    responses={401: {"description": "Not authenticated"}},
)


def _out(view: service.ReservationView) -> ReservationRead:
    r = view.reservation
    return ReservationRead(
        id=r.id,
        slot_id=r.slot_id,
        machine_id=view.machine_id,
        product_sku=view.product_sku,
        product_name=view.product_name,
        quantity=r.quantity,
        unit_price_cents=r.unit_price_cents,
        total_cents=r.unit_price_cents * r.quantity,
        currency=view.currency,
        status=service.effective_status(r),
        expires_at=r.expires_at,
        created_at=r.created_at,
    )


async def _limit_per_user(user: CurrentUser, redis: RedisClient) -> None:
    await enforce_rate_limit(
        redis, "reservation-user", str(user.id), get_settings().rate_limit_reservation_user
    )


@router.post(
    "",
    response_model=ReservationRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_limit_per_user)],
    responses={
        200: {"description": "Idempotent retry: the existing reservation"},
        404: {"description": "Slot not found"},
        409: {"description": "Not enough stock, not sellable, too many active, or key reuse"},
        429: {"description": "Rate limited"},
    },
)
async def create(
    data: ReservationCreate,
    user: CurrentUser,
    session: DbSession,
    redis: RedisClient,
    response: Response,
) -> ReservationRead:
    """Hold stock for `RESERVATION_HOLD_SECONDS` (15 minutes by default)."""
    try:
        view, created = await service.create_reservation(session, redis, user, data)
    except service.SlotNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slot not found") from None
    except service.InsufficientStockError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "insufficient_stock",
                "message": "Not enough stock",
                "available": exc.available,
            },
        ) from None
    except service.SlotNotSellableError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "not_sellable", "message": str(exc)}
        ) from None
    except service.TooManyActiveReservationsError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "too_many_active", "message": str(exc), "limit": exc.limit},
        ) from None
    except service.IdempotencyConflictError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "idempotency_conflict", "message": str(exc)}
        ) from None
    if not created:
        response.status_code = status.HTTP_200_OK
    return _out(view)


@router.get("", response_model=list[ReservationRead])
async def list_mine(
    user: CurrentUser, session: DbSession, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[ReservationRead]:
    """Your reservations, newest first."""
    return [_out(v) for v in await service.list_reservations(session, user, limit=limit)]


@router.get(
    "/{reservation_id}",
    response_model=ReservationRead,
    responses={404: {"description": "Not found (or not yours)"}},
)
async def get_one(
    reservation_id: uuid.UUID, user: CurrentUser, session: DbSession
) -> ReservationRead:
    try:
        return _out(await service.get_reservation(session, user, reservation_id))
    except service.ReservationNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found") from None


@router.delete(
    "/{reservation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "Not found (or not yours)"},
        409: {"description": "Already expired or dispensed"},
    },
)
async def cancel(
    reservation_id: uuid.UUID, user: CurrentUser, session: DbSession, redis: RedisClient
) -> None:
    """Cancel a held reservation and release its stock. Idempotent."""
    try:
        await service.cancel_reservation(session, redis, user, reservation_id)
    except service.ReservationNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found") from None
    except service.ReservationNotCancellableError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "not_cancellable", "message": str(exc), "status": exc.status.value},
        ) from None
