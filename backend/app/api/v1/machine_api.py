"""Endpoints called by machines themselves, authenticated by Ed25519 signatures."""

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import AuthenticatedMachine, DbSession
from app.schemas.machine_api import MachineHeartbeat, MachineHeartbeatAck

router = APIRouter(
    prefix="/machine",
    tags=["machine"],
    responses={401: {"description": "Invalid machine signature"}},
)


@router.post("/heartbeat", response_model=MachineHeartbeatAck)
async def heartbeat(
    data: MachineHeartbeat, machine: AuthenticatedMachine, session: DbSession
) -> MachineHeartbeatAck:
    now = datetime.now(UTC)
    machine.last_seen_at = now
    machine.firmware_version = data.firmware_version
    await session.commit()
    return MachineHeartbeatAck(
        machine_id=machine.id,
        serial_number=machine.serial_number,
        status=machine.status,
        server_time=now,
    )
