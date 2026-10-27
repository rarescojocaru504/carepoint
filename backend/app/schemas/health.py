from enum import StrEnum

from pydantic import BaseModel


class ProbeStatus(StrEnum):
    OK = "ok"
    UNAVAILABLE = "unavailable"


class HealthResponse(BaseModel):
    status: ProbeStatus


class ReadinessChecks(BaseModel):
    database: ProbeStatus
    redis: ProbeStatus


class ReadinessResponse(BaseModel):
    status: ProbeStatus
    checks: ReadinessChecks
