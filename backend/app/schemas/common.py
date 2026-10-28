"""Shared validation primitives.

Zero-trust input rules used by every request schema:
- unknown fields are rejected (`extra="forbid"`);
- numbers are strict: `"5"` or `true` is never silently accepted as an int;
- floats reject NaN/inf; coordinates are range-checked.

Models are not strict as a whole: FastAPI validates parsed JSON in Python mode,
where whole-model strictness would also reject UUID and enum strings.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Latitude = Annotated[float, Field(strict=True, ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Field(strict=True, ge=-180, le=180, allow_inf_nan=False)]

PriceCents = Annotated[int, Field(strict=True, gt=0, le=1_000_000)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
NonNegativeQty = Annotated[int, Field(strict=True, ge=0, le=1000)]
PositiveQty = Annotated[int, Field(strict=True, gt=0, le=1000)]


class InputModel(BaseModel):
    """Base for request bodies."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadModel(BaseModel):
    """Base for responses built from ORM objects."""

    model_config = ConfigDict(from_attributes=True)


class GeoPoint(InputModel):
    lat: Latitude
    lng: Longitude
