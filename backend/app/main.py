from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.redis import redis_client
from app.services.background import start_background_workers, stop_background_workers


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    workers = start_background_workers()
    yield
    await stop_background_workers(workers)
    await redis_client.aclose()
    await engine.dispose()


settings = get_settings()
app = FastAPI(title=settings.project_name, lifespan=lifespan)
app.include_router(health_router)
app.include_router(api_router, prefix=settings.api_v1_prefix)
