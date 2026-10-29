"""Simulator configuration (a JSON file, validated strictly)."""

import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Serial = Annotated[str, Field(pattern=r"^[A-Z0-9-]{4,32}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Faults(_Strict):
    """Faults to inject; all can also be changed at runtime from the kiosk API."""

    offline: bool = False  # drop the connection and stay away until cleared
    jam: Literal["off", "next", "always"] = "off"  # the mechanism jams: nothing comes out
    partial_next: bool = False  # next multi-unit dispense stops after one unit
    seconds_per_unit: float = Field(default=1.5, ge=0, le=30)  # dispensing speed
    drop_next_result: bool = False  # lose the connection right after the next dispense


class MachineConfig(_Strict):
    serial: Serial
    firmware_version: Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,32}$")] = "sim-1.0.0"
    faults: Faults = Faults()


class SimConfig(_Strict):
    backend_url: str = "ws://localhost:8000"
    keys_dir: Path
    state_dir: Path
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8080, ge=1, le=65535)
    kiosk_dir: Path | None = None  # built kiosk SPA to serve at /
    reconnect_initial_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_seconds: float = Field(default=60.0, gt=0)
    machines: Annotated[list[MachineConfig], Field(min_length=1)]


def load_config(path: Path) -> SimConfig:
    """Read the config file; SIM_BACKEND_URL / SIM_HTTP_HOST override it."""
    raw = json.loads(path.read_text())
    for env, field in (("SIM_BACKEND_URL", "backend_url"), ("SIM_HTTP_HOST", "http_host")):
        if value := os.environ.get(env):
            raw[field] = value
    return SimConfig.model_validate(raw)
