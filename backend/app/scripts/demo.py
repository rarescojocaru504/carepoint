"""Keep the demo busy: simulated customers for a presentation (ADR-032).

    docker compose exec backend python -m app.scripts.demo
    docker compose exec backend python -m app.scripts.demo --shoppers 6 --every 5 --minutes 20

A few shoppers keep using the system the way the app does, through the real
API: they find a machine near the demo centre with stock, reserve, pay, and
then most of them type their code at that machine's kiosk (so the simulated
machine really dispenses), some cancel, and some never come, so their hold
expires and the payment is voided. An operator account restocks slots that
run low, so a long presentation doesn't empty the machines. The map, the
dashboard's live feed and its KPI tiles stay in motion.

Stop it with Ctrl+C. It refuses to run with ENVIRONMENT=production.
"""

import argparse
import asyncio
import random
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import get_settings

SHOPPER_PASSWORD = "carepoint-shopper-demo"  # noqa: S105  (demo accounts only; refused in production)
NAMES = ("Ana", "Mihai", "Ioana", "Andrei", "Elena", "Radu", "Maria", "Vlad", "Cristina", "Alex")

# What a customer does after paying, and how often.
SCENARIOS: tuple[tuple[str, float], ...] = (("pickup", 0.65), ("abandon", 0.20), ("cancel", 0.15))

# A slot at or below this many units is refilled to capacity.
RESTOCK_AT = 2


@dataclass
class Options:
    api_url: str = "http://127.0.0.1:8000/api/v1"
    kiosk_url: str = "http://simulator:8080"
    lat: float = 44.44
    lng: float = 26.08
    shoppers: int = 5
    every: float = 6.0  # seconds between customers, on average
    minutes: float = 0.0  # 0: until stopped
    seed: int | None = None
    # The seeded demo operator (app.scripts.seed); shoppers are created as needed.
    operator_email: str = "operator@carepoint.demo"
    operator_password: str = "carepoint-operator-demo"  # noqa: S105  (demo account)
    shopper_domain: str = "carepoint.demo"


@dataclass
class Shopper:
    name: str
    email: str
    token: str = ""
    busy_until: float = 0.0  # monotonic time; a shopper holds one reservation at a time


@dataclass
class Visit:
    """One customer's visit, for the log and the tests."""

    shopper: str
    machine: str
    product: str
    scenario: str
    reservation_id: str
    code: str | None = None
    steps: list[str] = field(default_factory=list)


class Demo:
    def __init__(
        self,
        api: httpx.AsyncClient,
        kiosk: httpx.AsyncClient,
        options: Options,
        *,
        rng: random.Random | None = None,
        log: Callable[[str], None] = print,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.api, self.kiosk, self.options = api, kiosk, options
        # Varied, repeatable demo behaviour; nothing here is a secret.
        self.rng = rng or random.Random(options.seed)  # noqa: S311
        self.log, self.sleep = log, sleep
        self.shoppers: list[Shopper] = []
        self.operator_token = ""
        self.pending: set[asyncio.Task[None]] = set()

    # --- accounts -----------------------------------------------------------------------

    async def _login(self, email: str, password: str) -> str:
        response = await self.api.post("/auth/login", json={"email": email, "password": password})
        response.raise_for_status()
        token: str = response.json()["access_token"]
        return token

    async def _operator_login(self) -> str:
        return await self._login(self.options.operator_email, self.options.operator_password)

    async def setup(self) -> None:
        """Sign in the operator and the shoppers, creating shoppers the first time."""
        self.operator_token = await self._operator_login()
        self.shoppers = []
        for i in range(self.options.shoppers):
            name = NAMES[i % len(NAMES)]
            shopper = Shopper(name, f"shopper-{i + 1}@{self.options.shopper_domain}")
            created = await self.api.post(
                "/auth/register",
                json={"email": shopper.email, "password": SHOPPER_PASSWORD, "full_name": name},
            )
            if created.status_code not in (201, 409):  # 409: already exists from an earlier run
                created.raise_for_status()
            shopper.token = await self._login(shopper.email, SHOPPER_PASSWORD)
            self.shoppers.append(shopper)

    async def _call(
        self, who: Shopper | None, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """An API call as a shopper (or the operator); signs in again once when
        the 15-minute access token has run out."""

        def headers() -> dict[str, str]:
            token = who.token if who is not None else self.operator_token
            return {"Authorization": f"Bearer {token}"}

        response = await self.api.request(method, path, headers=headers(), **kwargs)
        if response.status_code == 401:
            if who is None:
                self.operator_token = await self._operator_login()
            else:
                who.token = await self._login(who.email, SHOPPER_PASSWORD)
            response = await self.api.request(method, path, headers=headers(), **kwargs)
        return response

    # --- one customer -------------------------------------------------------------------

    def choose_scenario(self) -> str:
        names = [name for name, _ in SCENARIOS]
        weights = [weight for _, weight in SCENARIOS]
        scenario: str = self.rng.choices(names, weights=weights)[0]
        return scenario

    async def _machines(self) -> list[dict[str, Any]]:
        """Open machines near the demo centre that the simulator can serve."""
        nearby = await self.api.get(
            "/machines/nearby",
            params={"lat": self.options.lat, "lng": self.options.lng, "radius_m": 20000,
                    "limit": 50},
        )  # fmt: skip
        nearby.raise_for_status()
        sims = {m["machine_id"]: m for m in (await self.kiosk.get("/api/machines")).json()}
        usable = []
        for machine in nearby.json():
            sim = sims.get(machine["id"])
            if machine["status"] != "active" or not machine["online"] or sim is None:
                continue
            faults = sim.get("faults", {})
            if faults.get("offline") or faults.get("jam", "off") != "off":
                continue
            if machine["units_available"] > 0:
                usable.append({**machine, "serial": sim["serial"]})
        return usable

    async def visit(self, scenario: str | None = None) -> Visit | None:
        """One customer: reserve and pay at a machine, then pick up, cancel or
        abandon. A pickup or cancel happens a little later, in the background."""
        now = time.monotonic()
        free = [s for s in self.shoppers if s.busy_until <= now]
        machines = await self._machines()
        if not free or not machines:
            return None
        shopper, machine = self.rng.choice(free), self.rng.choice(machines)
        detail = (await self.api.get(f"/machines/{machine['id']}")).json()
        slots = [s for s in detail["slots"] if s["available"] > 0]
        if not slots:
            return None
        slot = self.rng.choice(slots)
        scenario = scenario or self.choose_scenario()

        reserved = await self._call(
            shopper, "POST", "/reservations",
            json={"slot_id": slot["slot_id"], "quantity": 1,
                  "idempotency_key": f"demo-{uuid.uuid4().hex}"},
        )  # fmt: skip
        if reserved.status_code not in (200, 201):  # someone got the last one; fine
            return None
        reservation = reserved.json()
        paid = await self._call(
            shopper, "POST", f"/reservations/{reservation['id']}/checkout",
            json={"payment_method": "pm_card_ok"},
        )  # fmt: skip
        if paid.status_code not in (200, 201):
            return None
        pickup = paid.json().get("pickup") or {}
        record = Visit(
            shopper.name, machine["name"], slot["product"]["name"], scenario,
            reservation["id"], pickup.get("code"), ["reserved", "paid"],
        )  # fmt: skip
        expires = datetime.fromisoformat(reservation["expires_at"])
        shopper.busy_until = now + max((expires - datetime.now(UTC)).total_seconds(), 0) + 5
        self.log(f"{shopper.name:<9}reserved and paid: {record.product} at {record.machine}")

        if scenario == "pickup" and record.code is not None:
            self._later(self._pick_up(shopper, machine["serial"], record), 4, 15)
        elif scenario == "cancel":
            self._later(self._cancel(shopper, record), 3, 10)
        else:
            self.log(f"{shopper.name:<9}won't make it: the hold will expire and nobody is charged")
        return record

    def _later(self, action: Awaitable[None], low: float, high: float) -> None:
        delay = self.rng.uniform(low, high)

        async def run() -> None:
            await self.sleep(delay)
            await action

        task = asyncio.create_task(run())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def _pick_up(self, shopper: Shopper, serial: str, record: Visit) -> None:
        result = await self.kiosk.post(
            f"/api/machines/{serial}/verify", json={"credential": record.code}
        )
        accepted = result.status_code == 200 and result.json().get("ok") is True
        record.steps.append("picked up" if accepted else "pickup refused")
        self.log(f"{shopper.name:<9}" + (
            f"typed the code at {record.machine}: the item comes out" if accepted
            else f"code refused at {record.machine} ({result.text[:80]})"
        ))  # fmt: skip
        if accepted:
            shopper.busy_until = time.monotonic() + 5

    async def _cancel(self, shopper: Shopper, record: Visit) -> None:
        response = await self._call(shopper, "DELETE", f"/reservations/{record.reservation_id}")
        record.steps.append("cancelled" if response.status_code == 204 else "cancel refused")
        self.log(f"{shopper.name:<9}changed their mind and cancelled: nobody is charged")
        shopper.busy_until = time.monotonic() + 5

    # --- the operator -------------------------------------------------------------------

    async def restock_if_low(self) -> list[str]:
        """Refill, to capacity, every slot at or below RESTOCK_AT units."""
        refilled = []
        for machine in await self._machines_for_operator():
            detail = (await self._call(None, "GET", f"/admin/machines/{machine['id']}")).json()
            for slot in detail["slots"]:
                missing = slot["capacity"] - slot["quantity"]
                if slot["product"] is None or slot["quantity"] > RESTOCK_AT or missing <= 0:
                    continue
                response = await self._call(
                    None, "POST", "/admin/stock-movements",
                    json={"slot_id": slot["id"], "reason": "restock", "delta": min(missing, 100),
                          "note": "demo script: refill"},
                )  # fmt: skip
                if response.status_code == 201:
                    refilled.append(f"{detail['name']} {slot['slot_code']}")
                    self.log(f"{'operator':<9}restocked {slot['slot_code']} at {detail['name']}"
                             f" (+{min(missing, 100)})")  # fmt: skip
        return refilled

    async def _machines_for_operator(self) -> list[dict[str, Any]]:
        response = await self._call(None, "GET", "/admin/machines")
        response.raise_for_status()
        return [m for m in response.json() if m["status"] == "active" and m["online"]]

    # --- the loop -----------------------------------------------------------------------

    async def run(self) -> None:
        await self.setup()
        self.log(f"Demo running with {len(self.shoppers)} shoppers; Ctrl+C to stop.")
        deadline = time.monotonic() + self.options.minutes * 60 if self.options.minutes else None
        visits = 0
        try:
            while deadline is None or time.monotonic() < deadline:
                try:
                    await self.visit()
                    visits += 1
                    if visits % 5 == 0:
                        await self.restock_if_low()
                except httpx.HTTPError as error:  # the stack restarting, say: keep going
                    self.log(f"(skipped a step: {error})")
                await self.sleep(self.rng.uniform(0.5, 1.5) * self.options.every)
        finally:
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)


def parse(argv: list[str]) -> Options:
    defaults = Options()
    parser = argparse.ArgumentParser(description="Simulated customers for a CarePoint demo.")
    parser.add_argument("--shoppers", type=int, default=defaults.shoppers, choices=range(1, 11),
                        metavar="1-10")  # fmt: skip
    parser.add_argument("--every", type=float, default=defaults.every,
                        help="seconds between customers, on average (default 6)")  # fmt: skip
    parser.add_argument("--minutes", type=float, default=defaults.minutes,
                        help="stop after this long (default: run until Ctrl+C)")  # fmt: skip
    parser.add_argument("--api-url", default=defaults.api_url)
    parser.add_argument("--kiosk-url", default=defaults.kiosk_url,
                        help="the simulator (default: http://simulator:8080)")  # fmt: skip
    parser.add_argument("--seed", type=int, default=None, help="repeat the same run")
    args = parser.parse_args(argv)
    if args.every <= 0:
        parser.error("--every must be positive")
    return Options(
        api_url=args.api_url, kiosk_url=args.kiosk_url, shoppers=args.shoppers,
        every=args.every, minutes=args.minutes, seed=args.seed,
    )  # fmt: skip


async def _main(options: Options) -> None:
    async with (
        httpx.AsyncClient(base_url=options.api_url, timeout=15) as api,
        httpx.AsyncClient(base_url=options.kiosk_url, timeout=15) as kiosk,
    ):
        await Demo(api, kiosk, options, log=lambda line: print(
            f"{datetime.now().strftime('%H:%M:%S')}  {line}", flush=True)).run()  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    if get_settings().environment == "production":
        print("Refusing to run the demo with ENVIRONMENT=production.", file=sys.stderr)
        return 1
    try:
        asyncio.run(_main(parse(sys.argv[1:] if argv is None else argv)))
    except KeyboardInterrupt:
        print("Demo stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
