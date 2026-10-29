"""Smoke test of the demo stack (CI, and by hand), standard library only:

    python3 hardware_sim/scripts/pickup_smoke.py [API_URL] [KIOSK_API_URL]

A customer pays through the backend API, then enters the code at simulated
machine CP-BUC-0001's kiosk API. The dispense must complete the order, capture
the payment and take one unit off the app's availability.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/api/v1"
KIOSK = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8080/api"
MACHINE = "CP-BUC-0001"
CUSTOMER = {"email": "customer@carepoint.demo", "password": "carepoint-customer-demo"}


def call(method: str, url: str, body: Any = None, token: str | None = None) -> Any:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an HTTP URL: {url}")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return json.load(response) if response.status != 204 else None
    except urllib.error.HTTPError as exc:
        return json.load(exc)


def a1_available(machine_id: str) -> tuple[str, int]:
    slots = call("GET", f"{API}/machines/{machine_id}")["slots"]
    slot = next(s for s in slots if s["slot_code"] == "A1")
    return slot["slot_id"], slot["available"]


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAILED: {message}")


def main() -> None:
    token = call("POST", f"{API}/auth/login", CUSTOMER)["access_token"]
    kiosk = call("GET", f"{KIOSK}/machines/{MACHINE}")
    check(kiosk["online"], f"{MACHINE} is not online")
    slot_id, before = a1_available(kiosk["machine_id"])
    print(f"1. {MACHINE} online; A1 available in the app: {before}")

    reservation = call("POST", f"{API}/reservations", {"slot_id": slot_id}, token)
    paid = call("POST", f"{API}/reservations/{reservation['id']}/checkout",
                {"payment_method": "pm_card_ok"}, token)  # fmt: skip
    code = paid["pickup"]["code"]
    print(f"2. reserved and paid; pickup code {code}")

    wrong = "000000" if code != "000000" else "111111"
    refused = call("POST", f"{KIOSK}/machines/{MACHINE}/verify", {"credential": wrong})
    check(refused == {"ok": False, "refusal": "invalid_code", "retry_after_seconds": None,
                      "command_id": None}, f"wrong code: {refused}")  # fmt: skip
    print("3. wrong code at the kiosk -> refused")
    accepted = call("POST", f"{KIOSK}/machines/{MACHINE}/verify", {"credential": code})
    check(accepted["ok"], f"right code refused: {accepted}")
    print("4. right code at the kiosk -> accepted, dispensing")

    order: dict[str, Any] = {}
    for _ in range(150):
        orders = call("GET", f"{API}/orders", token=token)
        order = next(o for o in orders if o["reservation_id"] == reservation["id"])
        # Capture follows the dispense commit (ADR-015), so wait for both.
        if (order["reservation_status"], order["payment_status"]) == ("dispensed", "captured"):
            break
        time.sleep(0.1)
    _, after = a1_available(kiosk["machine_id"])
    print(f"5. order {order['reservation_status']} / {order['payment_status']}; "
          f"A1 available in the app: {after}")  # fmt: skip
    check(order["reservation_status"] == "dispensed", "order not dispensed")
    check(order["payment_status"] == "captured", "payment not captured")
    check(after == before - 1, f"availability {before} -> {after}, expected -1")
    print("OK")


if __name__ == "__main__":
    main()
