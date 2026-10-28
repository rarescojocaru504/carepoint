from fastapi import APIRouter

from app.api.deps import CurrentUser
from app.schemas.user import UserRead

router = APIRouter(tags=["users"])


@router.get("/me", response_model=UserRead, responses={401: {"description": "Not authenticated"}})
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
