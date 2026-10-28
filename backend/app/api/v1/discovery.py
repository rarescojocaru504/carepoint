"""Public discovery & catalogue API: no account needed to browse the map."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from app.api.deps import DbSession, rate_limit_by_ip
from app.core.config import get_settings
from app.models import ProductCategory
from app.schemas.common import GeoPoint
from app.schemas.discovery import MachineDetail, MachineSummary, ProductNearby
from app.schemas.product import ProductRead
from app.services import discovery

router = APIRouter(
    tags=["discovery"],
    dependencies=[
        Depends(
            rate_limit_by_ip("discovery-ip", lambda s: s.rate_limit_discovery_ip, fail_open=True)
        )
    ],
)

Lat = Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False, description="WGS84 latitude")]
Lng = Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False, description="WGS84 longitude")]
RadiusM = Annotated[int, Query(ge=1, le=20_000, description="Search radius in metres")]
Limit = Annotated[int, Query(ge=1, le=50)]
SkuPath = Annotated[str, Path(pattern=r"^[A-Z0-9-]{3,32}$")]


@router.get("/machines/nearby", response_model=list[MachineSummary])
async def nearby_machines(
    session: DbSession, lat: Lat, lng: Lng, radius_m: RadiusM = 2000, limit: Limit = 20
) -> list[MachineSummary]:
    """Machines within `radius_m` of the point, nearest first."""
    return await discovery.find_nearby_machines(
        session,
        GeoPoint(lat=lat, lng=lng),
        radius_m=radius_m,
        limit=limit,
        online_window_seconds=get_settings().machine_online_window_seconds,
    )


@router.get(
    "/machines/{machine_id}",
    response_model=MachineDetail,
    responses={404: {"description": "Machine not found"}},
)
async def machine_detail(session: DbSession, machine_id: uuid.UUID) -> MachineDetail:
    """A machine's stocked slots with the quantity available to reserve now."""
    try:
        return await discovery.get_machine_detail(
            session, machine_id, online_window_seconds=get_settings().machine_online_window_seconds
        )
    except discovery.MachineNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Machine not found"
        ) from None


@router.get("/products", response_model=list[ProductRead])
async def products(
    session: DbSession, category: ProductCategory | None = None
) -> list[ProductRead]:
    """The active catalogue, optionally filtered by category."""
    return await discovery.list_products(session, category)


@router.get(
    "/products/{sku}/nearby",
    response_model=ProductNearby,
    responses={404: {"description": "Product not found"}},
)
async def product_nearby(
    session: DbSession,
    sku: SkuPath,
    lat: Lat,
    lng: Lng,
    radius_m: RadiusM = 2000,
    limit: Limit = 20,
) -> ProductNearby:
    """Where is this item available nearest to me? Machines without stock are omitted."""
    try:
        return await discovery.find_product_nearby(
            session,
            sku,
            GeoPoint(lat=lat, lng=lng),
            radius_m=radius_m,
            limit=limit,
            online_window_seconds=get_settings().machine_online_window_seconds,
        )
    except discovery.ProductNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        ) from None
