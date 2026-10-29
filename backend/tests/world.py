"""Committed test data for concurrency tests.

Races need one database connection per request (like production), which a
rolled-back shared session cannot provide. `World` commits real rows and the
`world` fixture (conftest.py) deletes them afterwards.

Machines are placed in the Southern Ocean, far from every other test's
coordinates, so committed rows can never appear in another test's geo queries.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Machine,
    MachineEvent,
    MachineSlot,
    Order,
    Product,
    ProductCategory,
    RefreshToken,
    Reservation,
    StockMovement,
    User,
)
from app.services.machine_auth import public_key_bytes

WORLD_LOCATION = "SRID=4326;POINT(150 -60)"


@dataclass
class World:
    sessions: async_sessionmaker[AsyncSession]
    created: list[Any] = field(default_factory=list)

    async def add(self, *objects: Any) -> None:
        async with self.sessions() as session:
            session.add_all(objects)
            await session.commit()
        self.created.extend(objects)

    async def slot(self, quantity: int, capacity: int = 10) -> MachineSlot:
        tag = uuid.uuid4().hex[:8].upper()
        product = Product(
            sku=f"RACE-{tag}",
            name="Race item",
            category=ProductCategory.HYGIENE,
            price_cents=500,
            currency="RON",
        )
        machine = Machine(
            serial_number=f"CP-RACE-{tag}", name="Race", address="x", location=WORLD_LOCATION
        )
        await self.add(product, machine)
        slot = MachineSlot(
            machine_id=machine.id,
            product_id=product.id,
            slot_code="A1",
            quantity=quantity,
            capacity=capacity,
        )
        await self.add(slot)
        return slot

    async def machine(
        self, slots: dict[str, int | None], capacity: int = 10
    ) -> tuple[Machine, Ed25519PrivateKey, dict[str, MachineSlot]]:
        """A machine with a registered key and one slot per entry: slot code ->
        quantity, or None for a slot with no product assigned."""
        tag = uuid.uuid4().hex[:8].upper()
        key = Ed25519PrivateKey.generate()
        product = Product(
            sku=f"GW-{tag}",
            name="Gateway item",
            category=ProductCategory.HYGIENE,
            price_cents=500,
            currency="RON",
        )
        machine = Machine(
            serial_number=f"CP-GW-{tag}",
            name="Gateway",
            address="x",
            location=WORLD_LOCATION,
            auth_public_key=public_key_bytes(key),
        )
        await self.add(product, machine)
        made = {
            code: MachineSlot(
                machine_id=machine.id,
                product_id=None if quantity is None else product.id,
                slot_code=code,
                quantity=quantity or 0,
                capacity=capacity,
            )
            for code, quantity in slots.items()
        }
        await self.add(*made.values())
        return machine, key, made

    async def users(self, n: int) -> list[User]:
        users = [
            User(email=f"race-{uuid.uuid4().hex[:12]}@example.com", password_hash="!",
                 full_name="Racer")
            for _ in range(n)
        ]  # fmt: skip
        await self.add(*users)
        return users

    def ids(self, model: type[Any]) -> list[uuid.UUID]:
        return [o.id for o in self.created if isinstance(o, model)]

    async def cleanup(self) -> None:
        slot_ids, user_ids = self.ids(MachineSlot), self.ids(User)
        async with self.sessions() as session:
            # TEST-ONLY: stock_movements is append-only by trigger (ADR-006).
            # Suspend triggers for this one transaction to delete the ledger
            # rows these tests created; nothing else is touched.
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            await session.execute(delete(StockMovement).where(StockMovement.slot_id.in_(slot_ids)))
            await session.execute(
                delete(Order).where(
                    Order.reservation_id.in_(
                        select(Reservation.id).where(
                            Reservation.slot_id.in_(slot_ids) | Reservation.user_id.in_(user_ids)
                        )
                    )
                )
            )
            await session.execute(
                delete(Reservation).where(
                    Reservation.slot_id.in_(slot_ids) | Reservation.user_id.in_(user_ids)
                )
            )
            await session.execute(delete(RefreshToken).where(RefreshToken.user_id.in_(user_ids)))
            await session.execute(
                delete(MachineEvent).where(MachineEvent.machine_id.in_(self.ids(Machine)))
            )
            for model in (MachineSlot, Machine, Product, User):
                await session.execute(delete(model).where(model.id.in_(self.ids(model))))
            await session.commit()
