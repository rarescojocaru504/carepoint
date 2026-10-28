"""Zero-trust request validation (no database needed)."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.models import ProductCategory, StockMovementReason
from app.schemas.common import GeoPoint
from app.schemas.inventory import StockAdjustmentCreate
from app.schemas.machine import MachineCreate, SlotCreate
from app.schemas.product import ProductCreate
from app.schemas.reservation import ReservationCreate
from app.schemas.user import UserCreate

VALID_PRODUCT: dict[str, Any] = {
    "sku": "PAD-REG-10",
    "name": "Pads, regular (10)",
    "category": "menstrual_care",
    "price_cents": 1299,
    "currency": "RON",
}


@pytest.mark.parametrize(
    "point",
    [
        {"lat": 91, "lng": 0},
        {"lat": -90.0001, "lng": 0},
        {"lat": 0, "lng": 180.5},
        {"lat": float("nan"), "lng": 0},
        {"lat": float("inf"), "lng": 0},
        {"lat": "44.4", "lng": 26.1},  # strings are not coordinates
        {"lat": 44.4},
        {"lat": 44.4, "lng": 26.1, "alt": 80},  # unknown field
    ],
)
def test_geopoint_rejects_invalid(point: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GeoPoint.model_validate(point)


def test_geopoint_accepts_bounds_and_ints() -> None:
    assert GeoPoint.model_validate({"lat": -90, "lng": 180}).lat == -90
    assert GeoPoint.model_validate({"lat": 44.4355, "lng": 26.1025}).lng == 26.1025


def test_product_valid_and_enum_from_string() -> None:
    product = ProductCreate.model_validate(VALID_PRODUCT)
    assert product.category is ProductCategory.MENSTRUAL_CARE


@pytest.mark.parametrize(
    "override",
    [
        {"price_cents": 0},
        {"price_cents": -1},
        {"price_cents": 12.99},  # money is integer cents
        {"price_cents": "1299"},  # no string coercion
        {"price_cents": True},
        {"currency": "ron"},
        {"sku": "bad sku"},
        {"category": "weapons"},
        {"image_url": "http://insecure.example.com/a.png"},
        {"image_url": "javascript:alert(1)"},
        {"name": ""},
        {"is_active": False},  # not settable on create
    ],
)
def test_product_rejects_invalid(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(VALID_PRODUCT | override)


def test_product_accepts_https_image() -> None:
    product = ProductCreate.model_validate(
        VALID_PRODUCT | {"image_url": "https://cdn.example.com/pad.png"}
    )
    assert product.image_url is not None


@pytest.mark.parametrize(
    "override",
    [
        {"quantity": 11},
        {"quantity": -1},
        {"capacity": 0},
        {"capacity": 101},
        {"low_stock_threshold": 11},
        {"slot_code": "a1"},
        {"slot_code": "A100"},
        {"product_id": None, "quantity": 3},
        {"quantity": "3"},
    ],
)
def test_slot_rejects_invalid(override: dict[str, Any]) -> None:
    base: dict[str, Any] = {"slot_code": "A1", "product_id": str(uuid.uuid4()), "capacity": 10}
    with pytest.raises(ValidationError):
        SlotCreate.model_validate(base | override)


def test_machine_create_validates_nested_location() -> None:
    base: dict[str, Any] = {
        "serial_number": "CP-0001",
        "name": "Library",
        "address": "1 Main St",
        "location": {"lat": 44.4, "lng": 26.1},
    }
    assert MachineCreate.model_validate(base).status == "active"
    with pytest.raises(ValidationError):
        MachineCreate.model_validate(base | {"location": {"lat": 200, "lng": 0}})
    with pytest.raises(ValidationError):
        MachineCreate.model_validate(base | {"serial_number": "cp 1"})


def test_user_create_normalises_email_and_has_no_role() -> None:
    user = UserCreate.model_validate(
        {"email": "Ana@Example.COM", "password": "correct horse battery", "full_name": "Ana"}
    )
    assert user.email == "ana@example.com"
    with pytest.raises(ValidationError):  # privilege escalation attempt
        UserCreate.model_validate(
            {
                "email": "a@example.com",
                "password": "correct horse battery",
                "full_name": "Ana",
                "role": "admin",
            }
        )
    with pytest.raises(ValidationError):
        UserCreate.model_validate({"email": "a@example.com", "password": "short", "full_name": "A"})
    with pytest.raises(ValidationError):
        UserCreate.model_validate(
            {"email": "not-an-email", "password": "correct horse battery", "full_name": "A"}
        )


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"delta": 5, "reason": "restock"}, True),
        ({"delta": -2, "reason": "adjustment"}, True),
        ({"delta": 0, "reason": "restock"}, False),
        ({"delta": 5, "reason": "dispense"}, False),  # system-only reasons
        ({"delta": 5, "reason": "initial"}, False),
        ({"delta": 5, "reason": "telemetry_correction"}, False),
        ({"delta": 5000, "reason": "restock"}, False),
    ],
)
def test_stock_adjustment(payload: dict[str, Any], ok: bool) -> None:
    data = {"slot_id": str(uuid.uuid4())} | payload
    if ok:
        adjustment = StockAdjustmentCreate.model_validate(data)
        assert adjustment.reason in (StockMovementReason.RESTOCK, StockMovementReason.ADJUSTMENT)
    else:
        with pytest.raises(ValidationError):
            StockAdjustmentCreate.model_validate(data)


@pytest.mark.parametrize(
    "override",
    [{"quantity": 0}, {"quantity": 11}, {"idempotency_key": "short"}, {"idempotency_key": "x y"}],
)
def test_reservation_create_rejects_invalid(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReservationCreate.model_validate({"slot_id": str(uuid.uuid4())} | override)
