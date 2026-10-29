"""Whether a machine is online (ADR-011, ADR-021).

Online means heard from within MACHINE_ONLINE_WINDOW_SECONDS, unless its live
connection closed after that: a machine that disconnects cleanly (or whose
simulator is stopped) shows as offline at once, not when the window runs out.
A crash without a close is caught by the window.
"""

from datetime import UTC, datetime, timedelta

from app.models import Machine


def is_online(machine: Machine, window_seconds: int, *, now: datetime | None = None) -> bool:
    if machine.last_seen_at is None:
        return False
    current = datetime.now(UTC) if now is None else now
    if machine.last_seen_at < current - timedelta(seconds=window_seconds):
        return False
    return machine.disconnected_at is None or machine.disconnected_at < machine.last_seen_at
