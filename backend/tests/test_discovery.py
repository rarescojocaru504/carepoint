"""Discovery & catalogue API against real PostGIS geometry."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.redis import get_redis
from app.main import app
from app.models import Machine, MachineStatus, ProductCategory, ReservationStatus
from app.schemas.common import GeoPoint
from app.services.discovery import _point
from tests.factories import make_machine, make_product, make_reservation, make_slot, make_user

pytestmark = pytest.mark.integration

# Mid-Atlantic origin: nothing else in any database lives here.
ORIGIN = {"lat": 10.0, "lng": -30.0}
METRES_PER_DEGREE_LAT = 110_574  # at ~10 degrees latitude on the WGS84 spheroid


def _north(metres: float) -> dict[str, float]:
    return {"lat": ORIGIN["lat"] + metres / METRES_PER_DEGREE_LAT, "lng": ORIGIN["lng"]}


async def _machine_at(session: AsyncSession, metres_north: float, **overrides: Any) -> Machine:
    return await make_machine(session, **_north(metres_north), **overrides)


async def _nearby(api: httpx.AsyncClient, **params: Any) -> httpx.Response:
    return await api.get("/api/v1/machines/nearby", params=ORIGIN | params)


# --- /machines/nearby --------------------------------------------------------


async def test_nearby_orders_by_distance_and_reports_metres(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    far = await _machine_at(db_session, 3000, name="Far")
    near = await _machine_at(db_session, 500, name="Near")
    mid = await _machine_at(db_session, 1500, name="Mid")

    response = await _nearby(api, radius_m=5000)
    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body] == [str(near.id), str(mid.id), str(far.id)]
    for machine, expected in zip(body, (500, 1500, 3000), strict=True):
        assert machine["distance_m"] == pytest.approx(expected, rel=0.01)
        assert machine["location"]["lng"] == pytest.approx(ORIGIN["lng"])


async def test_nearby_radius_cut_off(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    await _machine_at(db_session, 500)
    await _machine_at(db_session, 1500)
    await _machine_at(db_session, 3000)
    assert len((await _nearby(api, radius_m=1000)).json()) == 1
    assert len((await _nearby(api, radius_m=2000)).json()) == 2
    assert len((await _nearby(api, radius_m=4000)).json()) == 3


async def test_nearby_radius_boundary_is_exact(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Use the database's own distance: radius 1 m short excludes, 1 m over includes."""
    machine = await _machine_at(db_session, 1234)
    exact = await db_session.scalar(
        select(func.ST_Distance(Machine.location, _point(GeoPoint(**ORIGIN)))).where(
            Machine.id == machine.id
        )
    )
    assert exact is not None
    assert (await _nearby(api, radius_m=int(exact) - 1)).json() == []
    assert len((await _nearby(api, radius_m=int(exact) + 1)).json()) == 1


async def test_nearby_limit(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    for metres in (100, 200, 300, 400):
        await _machine_at(db_session, metres)
    body = (await _nearby(api, radius_m=1000, limit=2)).json()
    assert [round(m["distance_m"], -2) for m in body] == [100, 200]


async def test_nearby_hides_decommissioned_and_flags_maintenance(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    product = await make_product(db_session)
    gone = await _machine_at(db_session, 100, status=MachineStatus.DECOMMISSIONED)
    paused = await _machine_at(db_session, 200, status=MachineStatus.MAINTENANCE)
    await make_slot(db_session, machine=paused, product=product, quantity=5)

    body = (await _nearby(api, radius_m=1000)).json()
    assert [m["id"] for m in body] == [str(paused.id)]
    assert body[0]["status"] == "maintenance"
    # Stock exists but cannot be bought while the machine is in maintenance.
    assert body[0]["units_available"] == 0
    assert str(gone.id) not in {m["id"] for m in body}


async def test_online_flag_from_last_seen(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    fresh = await _machine_at(db_session, 100, last_seen_at=now - timedelta(seconds=10))
    stale = await _machine_at(db_session, 200, last_seen_at=now - timedelta(minutes=10))
    never = await _machine_at(db_session, 300)
    online = {m["id"]: m["online"] for m in (await _nearby(api, radius_m=1000)).json()}
    assert online == {str(fresh.id): True, str(stale.id): False, str(never.id): False}


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91},
        {"lat": -91},
        {"lng": 181},
        {"lat": "nan"},
        {"lat": "inf"},
        {"lat": "abc"},
        {"radius_m": 0},
        {"radius_m": 20_001},
        {"limit": 0},
        {"limit": 51},
    ],
)
async def test_nearby_validates_parameters(api: httpx.AsyncClient, params: dict[str, Any]) -> None:
    assert (await _nearby(api, **params)).status_code == 422


async def test_nearby_requires_coordinates(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/machines/nearby")).status_code == 422


# --- availability arithmetic -------------------------------------------------


async def test_available_subtracts_only_active_holds(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    product = await make_product(db_session)
    machine = await _machine_at(db_session, 100)
    slot = await make_slot(db_session, machine=machine, product=product, quantity=10)

    counted = [
        (ReservationStatus.HELD, timedelta(minutes=10), 2),
        (ReservationStatus.PAID, timedelta(minutes=5), 1),
    ]
    ignored = [
        (ReservationStatus.HELD, timedelta(seconds=-1), 4),  # expired, not yet swept
        (ReservationStatus.EXPIRED, timedelta(minutes=-5), 3),
        (ReservationStatus.CANCELLED, timedelta(minutes=10), 2),
        (ReservationStatus.DISPENSED, timedelta(minutes=10), 1),
    ]
    for status, expires_in, qty in counted + ignored:
        await make_reservation(
            db_session, user=user, slot=slot, quantity=qty, status=status, expires_in=expires_in
        )

    detail = (await api.get(f"/api/v1/machines/{machine.id}")).json()
    assert detail["slots"][0]["available"] == 10 - 2 - 1
    summary = (await _nearby(api, radius_m=1000)).json()[0]
    assert summary["units_available"] == 7
    assert summary["products_available"] == 1


async def test_available_never_negative(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    user = await make_user(db_session)
    product = await make_product(db_session)
    machine = await _machine_at(db_session, 100)
    await make_slot(db_session, machine=machine, product=product, quantity=1)
    slot = await make_slot(db_session, machine=machine, product=product, quantity=2)
    await make_reservation(db_session, user=user, slot=slot, quantity=5)  # inconsistent on purpose

    detail = (await api.get(f"/api/v1/machines/{machine.id}")).json()
    assert sorted(s["available"] for s in detail["slots"]) == [0, 1]


async def test_machine_aggregates_and_low_stock(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    pads = await make_product(db_session, sku="AGG-PADS", name="Pads")
    wipes = await make_product(db_session, sku="AGG-WIPES", name="Wipes")
    retired = await make_product(db_session, sku="AGG-OLD", is_active=False)
    machine = await _machine_at(db_session, 100)
    await make_slot(db_session, machine=machine, product=pads, slot_code="A1", quantity=8)
    await make_slot(db_session, machine=machine, product=pads, slot_code="A2", quantity=1)
    await make_slot(db_session, machine=machine, product=wipes, slot_code="A3", quantity=0)
    await make_slot(db_session, machine=machine, product=retired, slot_code="A4", quantity=9)
    await make_slot(db_session, machine=machine, product=None, slot_code="A5")

    summary = (await _nearby(api, radius_m=1000)).json()[0]
    assert summary["units_available"] == 9  # 8 + 1; sold-out wipes and retired product add 0
    assert summary["products_available"] == 1  # only pads (two slots, one product)

    detail = (await api.get(f"/api/v1/machines/{machine.id}")).json()
    slots = {s["slot_code"]: s for s in detail["slots"]}
    assert set(slots) == {"A1", "A2", "A3"}  # empty slot and retired product hidden
    assert (slots["A1"]["low_stock"], slots["A2"]["low_stock"], slots["A3"]["low_stock"]) == (
        False,
        True,
        True,
    )
    assert slots["A1"]["product"]["sku"] == "AGG-PADS"


# --- /machines/{id} ----------------------------------------------------------


async def test_machine_detail_not_found(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    gone = await _machine_at(db_session, 100, status=MachineStatus.DECOMMISSIONED)
    assert (await api.get(f"/api/v1/machines/{gone.id}")).status_code == 404
    unknown = "00000000-0000-0000-0000-000000000000"
    assert (await api.get(f"/api/v1/machines/{unknown}")).status_code == 404
    assert (await api.get("/api/v1/machines/not-a-uuid")).status_code == 422


async def test_machine_detail_does_not_leak_internal_fields(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    machine = await _machine_at(db_session, 100)
    machine.auth_public_key = b"k" * 32
    await db_session.flush()
    text = (await api.get(f"/api/v1/machines/{machine.id}")).text
    for secretish in ("auth_public_key", "serial_number", "firmware_version", "capacity"):
        assert secretish not in text


# --- /products ---------------------------------------------------------------


async def test_products_lists_active_and_filters(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_product(db_session, sku="CAT-AID", category=ProductCategory.FIRST_AID)
    await make_product(db_session, sku="CAT-HYG", category=ProductCategory.HYGIENE)
    await make_product(db_session, sku="CAT-OFF", category=ProductCategory.HYGIENE, is_active=False)

    all_skus = {p["sku"] for p in (await api.get("/api/v1/products")).json()}
    assert {"CAT-AID", "CAT-HYG"} <= all_skus
    assert "CAT-OFF" not in all_skus
    first_aid = (await api.get("/api/v1/products", params={"category": "first_aid"})).json()
    assert {p["sku"] for p in first_aid} >= {"CAT-AID"}
    assert all(p["category"] == "first_aid" for p in first_aid)
    assert (await api.get("/api/v1/products", params={"category": "nope"})).status_code == 422


# --- /products/{sku}/nearby --------------------------------------------------


async def test_product_nearby_finds_stock_nearest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    product = await make_product(db_session, sku="FIND-ME")
    decoy = await make_product(db_session, sku="DECOY-1")
    empty = await _machine_at(db_session, 100, name="Sold out")
    near = await _machine_at(db_session, 400, name="Near")
    far = await _machine_at(db_session, 900, name="Far")
    other = await _machine_at(db_session, 200, name="Other product only")
    await make_slot(db_session, machine=empty, product=product, quantity=0)
    await make_slot(db_session, machine=near, product=product, slot_code="A1", quantity=1)
    best = await make_slot(db_session, machine=near, product=product, slot_code="A2", quantity=4)
    await make_slot(db_session, machine=far, product=product, quantity=2)
    await make_slot(db_session, machine=other, product=decoy, quantity=9)

    response = await api.get("/api/v1/products/FIND-ME/nearby", params=ORIGIN | {"radius_m": 2000})
    assert response.status_code == 200
    body = response.json()
    assert body["product"]["sku"] == "FIND-ME"
    locations = body["locations"]
    assert [loc["machine_id"] for loc in locations] == [str(near.id), str(far.id)]
    assert locations[0]["available"] == 5  # summed across both slots
    assert locations[0]["slot_id"] == str(best.id)  # reserve from the fullest slot
    assert locations[0]["distance_m"] == pytest.approx(400, rel=0.01)


async def test_product_nearby_respects_holds_and_radius(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session)
    product = await make_product(db_session, sku="HELD-1")
    machine = await _machine_at(db_session, 300)
    slot = await make_slot(db_session, machine=machine, product=product, quantity=2)
    await make_reservation(db_session, user=user, slot=slot, quantity=2)

    held = await api.get("/api/v1/products/HELD-1/nearby", params=ORIGIN)
    assert held.json()["locations"] == []  # all units are held by someone

    await make_slot(db_session, machine=machine, product=product, slot_code="B9", quantity=1)
    assert (
        len((await api.get("/api/v1/products/HELD-1/nearby", params=ORIGIN)).json()["locations"])
        == 1
    )
    out_of_range = await api.get(
        "/api/v1/products/HELD-1/nearby", params=ORIGIN | {"radius_m": 100}
    )
    assert out_of_range.json()["locations"] == []


async def test_product_nearby_unknown_or_inactive(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    await make_product(db_session, sku="GONE-1", is_active=False)
    assert (await api.get("/api/v1/products/GONE-1/nearby", params=ORIGIN)).status_code == 404
    assert (await api.get("/api/v1/products/NOPE-0/nearby", params=ORIGIN)).status_code == 404
    bad_sku = await api.get("/api/v1/products/bad sku/nearby", params=ORIGIN)
    assert bad_sku.status_code == 422


# --- operational -------------------------------------------------------------


async def test_discovery_is_public_and_rate_limited(
    api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_discovery_ip", "2/60")
    statuses = [(await api.get("/api/v1/products")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


async def test_discovery_fails_open_without_redis(db_session: AsyncSession) -> None:
    """Read-only browsing keeps working if the rate limiter's Redis is down."""
    dead: Redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)

    async def _db() -> Any:
        yield db_session

    async def _redis() -> Redis:
        return dead

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = _redis
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/v1/products")).status_code == 200
    finally:
        app.dependency_overrides.clear()
        await dead.aclose()
