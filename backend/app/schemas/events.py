"""Live notices for the customer app (ADR-026), as streamed by GET /api/v1/events.

Like the operators' notices (ADR-022) they carry no data, only what changed;
the app re-reads it through the normal API. Declared as a model so the shape
is part of the OpenAPI schema and the app's generated types.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CustomerTopic = Literal["hello", "stock", "machine", "reservation"]


class CustomerNotice(BaseModel):
    """One server-sent event's `data`.

    - `hello` / `subscribed`: the stream is open; re-read everything once.
    - `stock` / `availability`: what can be reserved at `machine_id` changed
      (`null`: possibly at several machines).
    - `machine` / `online`, `offline`, `status`: the machine connected,
      disconnected, or was put into or out of maintenance.
    - `reservation` / `created`, `paid`, `dispensing`, `dispensed`, `partial`,
      `cancelled`, `expired`: one of **your own** reservations changed.
    """

    topic: CustomerTopic
    machine_id: uuid.UUID | None = None
    reservation_id: uuid.UUID | None = Field(
        default=None, description="Set only on your own reservation notices"
    )
    change: str
    at: datetime
