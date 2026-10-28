import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, EmailStr, Field

from app.models.enums import UserRole
from app.schemas.common import InputModel, ReadModel

# Lowercased so the unique constraint is effectively case-insensitive (the
# database also enforces `email = lower(email)`).
NormalizedEmail = Annotated[EmailStr, AfterValidator(lambda email: email.lower())]
Password = Annotated[str, Field(min_length=12, max_length=128)]
FullName = Annotated[str, Field(min_length=1, max_length=100)]


class UserCreate(InputModel):
    """Public sign-up. There is deliberately no `role` field: every self-registered
    account is a customer, and `extra="forbid"` rejects attempts to send one."""

    email: NormalizedEmail
    password: Password
    full_name: FullName


class UserRead(ReadModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
