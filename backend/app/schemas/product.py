import uuid
from typing import Annotated

from pydantic import AnyHttpUrl, Field, field_validator

from app.models.enums import ProductCategory
from app.schemas.common import CurrencyCode, InputModel, PriceCents, ReadModel

Sku = Annotated[str, Field(pattern=r"^[A-Z0-9-]{3,32}$")]
ProductName = Annotated[str, Field(min_length=1, max_length=120)]
Description = Annotated[str, Field(max_length=2000)]


def _require_https(url: AnyHttpUrl | None) -> AnyHttpUrl | None:
    if url is not None and url.scheme != "https":
        raise ValueError("image_url must use https")
    return url


class ProductCreate(InputModel):
    sku: Sku
    name: ProductName
    description: Description | None = None
    category: ProductCategory
    price_cents: PriceCents
    currency: CurrencyCode
    image_url: AnyHttpUrl | None = None

    _https_only = field_validator("image_url")(_require_https)


class ProductUpdate(InputModel):
    name: ProductName | None = None
    description: Description | None = None
    category: ProductCategory | None = None
    price_cents: PriceCents | None = None
    image_url: AnyHttpUrl | None = None
    is_active: Annotated[bool, Field(strict=True)] | None = None

    _https_only = field_validator("image_url")(_require_https)


class ProductRead(ReadModel):
    id: uuid.UUID
    sku: str
    name: str
    description: str | None
    category: ProductCategory
    price_cents: int
    currency: str
    image_url: str | None
    is_active: bool
