from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.common import InputModel
from app.schemas.user import NormalizedEmail

# Login accepts any length up to the max so a policy change never locks out
# existing users; the max bounds argon2 work per request.
LoginPassword = Annotated[str, Field(min_length=1, max_length=128)]
OpaqueToken = Annotated[str, Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class LoginRequest(InputModel):
    email: NormalizedEmail
    password: LoginPassword


class RefreshRequest(InputModel):
    refresh_token: OpaqueToken


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth2 token type)
    expires_in: int
