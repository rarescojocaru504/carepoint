import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.models.enums import MachineStatus
from app.schemas.common import InputModel

FirmwareVersion = Annotated[str, Field(pattern=r"^[A-Za-z0-9._+-]{1,32}$")]
UptimeSeconds = Annotated[int, Field(strict=True, ge=0, le=10 * 365 * 24 * 3600)]


class MachineHeartbeat(InputModel):
    firmware_version: FirmwareVersion
    uptime_seconds: UptimeSeconds


class MachineHeartbeatAck(BaseModel):
    machine_id: uuid.UUID
    serial_number: str
    status: MachineStatus
    server_time: datetime
