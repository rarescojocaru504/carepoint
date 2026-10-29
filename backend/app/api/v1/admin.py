"""Operator/admin API. Every route here requires the operator or admin role;
individual routes may narrow that further."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, RedisClient, require_role
from app.models import MachineStatus, User, UserRole
from app.schemas.admin import (
    AdminMachine,
    AdminMachineDetail,
    AdminReservation,
    KpiSummary,
    MachineStatusUpdate,
    OperatorStockChange,
    ReservationFilter,
    StockMovementResult,
    StockMovementWithActor,
)
from app.schemas.product import ProductCreate, ProductRead, ProductUpdate
from app.schemas.user import UserRead
from app.services import admin, reservations
from app.services.inventory import InventoryError
from app.services.kpis import kpi_summary
from app.services.payments import PaymentProvider, get_payment_provider

Provider = Annotated[PaymentProvider, Depends(get_payment_provider)]

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


# --- machines ------------------------------------------------------------------


@router.get("/machines", response_model=list[AdminMachine])
async def list_machines(session: DbSession) -> list[AdminMachine]:
    """Every machine in service, with live status and stock warnings."""
    return await admin.list_machines(session)


@router.get(
    "/machines/{machine_id}",
    response_model=AdminMachineDetail,
    responses={404: {"description": "No such machine"}},
)
async def get_machine(machine_id: uuid.UUID, session: DbSession) -> AdminMachineDetail:
    try:
        return await admin.get_machine(session, machine_id)
    except admin.NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Machine not found") from None


@router.patch(
    "/machines/{machine_id}",
    response_model=AdminMachineDetail,
    responses={404: {"description": "No such machine"}},
)
async def set_machine_status(
    machine_id: uuid.UUID, data: MachineStatusUpdate, user: CurrentUser, session: DbSession
) -> AdminMachineDetail:
    """Put a machine into maintenance or back into service. Decommissioning
    (which also cuts off its live connection) is for admins only."""
    if data.status is MachineStatus.DECOMMISSIONED and user.role is not UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can decommission machines")
    try:
        return await admin.set_machine_status(session, machine_id, data.status)
    except admin.NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Machine not found") from None


# --- stock ---------------------------------------------------------------------


@router.get(
    "/slots/{slot_id}/movements",
    response_model=list[StockMovementWithActor],
    responses={404: {"description": "No such slot"}},
)
async def stock_history(
    slot_id: uuid.UUID, session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[StockMovementWithActor]:
    """The slot's ledger, newest first: restocks, dispenses, corrections."""
    try:
        return await admin.stock_history(session, slot_id, limit=limit)
    except admin.NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slot not found") from None


@router.post(
    "/stock-movements",
    response_model=StockMovementResult,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "No such slot"},
        409: {"description": "Machine offline, or the change does not fit the slot"},
    },
)
async def record_stock_change(
    data: OperatorStockChange, user: CurrentUser, session: DbSession, redis: RedisClient
) -> StockMovementResult:
    """Record a restock or adjustment; the machine is told (adjust_stock)."""
    try:
        return await admin.record_operator_change(session, redis, user, data)
    except admin.NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Slot not found") from None
    except admin.MachineOfflineError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "machine_offline", "message": str(exc)}
        ) from None
    except InventoryError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "stock_conflict", "message": str(exc)}
        ) from None


# --- reservations --------------------------------------------------------------


@router.get("/reservations", response_model=list[AdminReservation])
async def list_reservations(
    session: DbSession,
    status_filter: Annotated[ReservationFilter | None, Query(alias="status")] = None,
    machine_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AdminReservation]:
    """Reservations of all customers, newest first. `status=active` means held
    or paid and not yet expired."""
    return await admin.list_reservations(
        session, status=status_filter, machine_id=machine_id, limit=limit
    )


@router.post(
    "/reservations/{reservation_id}/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "No such reservation"},
        409: {"description": "Already finished, or being dispensed right now"},
    },
)
async def cancel_reservation(
    reservation_id: uuid.UUID, session: DbSession, redis: RedisClient, provider: Provider
) -> None:
    """Cancel a customer's reservation: stock is released and any payment voided."""
    try:
        await reservations.cancel_any_reservation(session, redis, provider, reservation_id)
    except reservations.ReservationNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reservation not found") from None
    except reservations.ReservationNotCancellableError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"code": "not_cancellable", "message": str(exc), "status": exc.status.value},
        ) from None
    except reservations.ReservationBeingDispensedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": "pickup_in_progress", "message": str(exc)}
        ) from None


# --- products ------------------------------------------------------------------


@router.get("/products", response_model=list[ProductRead])
async def list_products(session: DbSession) -> list[ProductRead]:
    """The whole catalogue, inactive products included."""
    return [ProductRead.model_validate(p) for p in await admin.list_products(session)]


@router.post(
    "/products",
    response_model=ProductRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "SKU already exists"}},
)
async def create_product(data: ProductCreate, session: DbSession) -> ProductRead:
    try:
        return ProductRead.model_validate(await admin.create_product(session, data))
    except admin.ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


@router.patch(
    "/products/{product_id}",
    response_model=ProductRead,
    responses={404: {"description": "No such product"}},
)
async def update_product(
    product_id: uuid.UUID, data: ProductUpdate, session: DbSession
) -> ProductRead:
    """Change details or price (existing reservations keep their price), or
    deactivate (`is_active: false`) to stop selling it everywhere."""
    try:
        return ProductRead.model_validate(await admin.update_product(session, product_id, data))
    except admin.NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found") from None


@router.delete(
    "/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"description": "No such product"},
        409: {"description": "Assigned to a slot: deactivate it instead"},
    },
)
async def delete_product(product_id: uuid.UUID, session: DbSession) -> None:
    try:
        await admin.delete_product(session, product_id)
    except admin.NotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found") from None
    except admin.ConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None


# --- KPIs ----------------------------------------------------------------------


@router.get("/kpis", response_model=KpiSummary)
async def kpis(session: DbSession) -> KpiSummary:
    """Headline numbers for the dashboard."""
    return await kpi_summary(session)
