"""What a machine must remember across disconnects and reboots
(docs/machine-protocol.md §6): its physical counts, which commands it has
executed and with what result, and which results the server has not yet
acknowledged. Written atomically after every change, like flash storage.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Old results are only needed to answer a resent command; keep a bounded tail.
_MAX_EXECUTED = 500


@dataclass
class InFlight:
    """A dispense under way: survives a crash, so it is never restarted."""

    command_id: str
    slot: str
    quantity: int
    units_done: int = 0


@dataclass
class MachineState:
    counts: dict[str, int] = field(default_factory=dict)  # physical units per slot
    executed: dict[str, dict[str, Any]] = field(default_factory=dict)  # command -> result
    outbox: list[str] = field(default_factory=list)  # results not yet acknowledged
    in_flight: InFlight | None = None

    def record_result(self, command_id: str, result: dict[str, Any]) -> None:
        self.executed[command_id] = result
        if command_id not in self.outbox:
            self.outbox.append(command_id)
        while len(self.executed) > _MAX_EXECUTED:
            oldest = next(iter(self.executed))
            if oldest in self.outbox:
                break  # never forget an unacknowledged result
            del self.executed[oldest]


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> MachineState:
        """Read the state; a dispense interrupted by a crash is turned into a
        `failed` result with the units that did come out (§6 rule 1)."""
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return MachineState()
        in_flight = raw.get("in_flight")
        state = MachineState(
            counts=raw.get("counts", {}),
            executed=raw.get("executed", {}),
            outbox=raw.get("outbox", []),
            in_flight=InFlight(**in_flight) if in_flight else None,
        )
        if state.in_flight is not None:
            interrupted = state.in_flight
            state.in_flight = None
            complete = interrupted.units_done == interrupted.quantity
            state.record_result(
                interrupted.command_id,
                {
                    "outcome": "dispensed" if complete else "failed",
                    "units_dispensed": interrupted.units_done,
                    "error_code": None if complete else "power_loss",
                },
            )
            self.save(state)
        return state

    def save(self, state: MachineState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w") as handle:
            json.dump(asdict(state), handle)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)
