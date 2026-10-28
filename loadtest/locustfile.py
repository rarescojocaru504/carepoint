# ruff: noqa: S311  (random only picks simulated user actions; nothing security-related)
"""Sustained, realistic traffic for throughput numbers.

Customers mostly browse (map, machine pages, catalogue) and sometimes reserve
and then cancel, so stock is not exhausted during the run. Reads hit the seeded
Bucharest machines; reservations hit the load-test slots from loadtest_fixtures.

    LOADTEST_FIXTURES=fixtures.json locust -f loadtest/locustfile.py --headless \
        -u 200 -r 50 -t 60s --host http://localhost:8000
"""

import itertools
import json
import os
import random
from pathlib import Path
from typing import Any

from locust import HttpUser, between, task

_FIXTURES: dict[str, Any] = json.loads(
    Path(os.environ.get("LOADTEST_FIXTURES", "fixtures.json")).read_text()
)
_TOKENS = itertools.cycle(_FIXTURES["tokens"])
CAMPUS = [(44.4355, 26.1013), (44.4386, 26.0496), (44.4430, 26.0640), (44.4475, 26.0973)]


class Customer(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.auth = {"Authorization": f"Bearer {next(_TOKENS)}"}
        self.machine_ids: list[str] = []

    @task(6)
    def browse_map(self) -> None:
        lat, lng = random.choice(CAMPUS)
        with self.client.get(
            "/api/v1/machines/nearby",
            params={"lat": lat, "lng": lng, "radius_m": 3000},
            name="/machines/nearby",
            catch_response=True,
        ) as response:
            if response.ok:
                self.machine_ids = [m["id"] for m in response.json()]

    @task(3)
    def open_machine(self) -> None:
        if self.machine_ids:
            self.client.get(
                f"/api/v1/machines/{random.choice(self.machine_ids)}", name="/machines/{id}"
            )

    @task(1)
    def catalogue(self) -> None:
        self.client.get("/api/v1/products", name="/products")

    @task(1)
    def reserve_then_cancel(self) -> None:
        slot = random.choice(_FIXTURES["slots"])["slot_id"]
        with self.client.post(
            "/api/v1/reservations",
            json={"slot_id": slot},
            headers=self.auth,
            name="/reservations [create]",
            catch_response=True,
        ) as response:
            if response.status_code == 409:
                response.success()  # sold out is a correct answer, not an error
                return
        if response.status_code == 201:
            self.client.delete(
                f"/api/v1/reservations/{response.json()['id']}",
                headers=self.auth,
                name="/reservations/{id} [cancel]",
            )
