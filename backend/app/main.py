from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.db import engine
from app.core.logging import configure_logging
from app.core.redis import redis_client
from app.core.request_context import RequestContextMiddleware
from app.services.background import start_background_workers, stop_background_workers


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Here rather than at import: uvicorn sets up its own logging first, and
    # this routes it through ours (ADR-029).
    configure_logging(settings.log_format, settings.log_level)
    workers = start_background_workers()
    yield
    await stop_background_workers(workers)
    await redis_client.aclose()
    await engine.dispose()


settings = get_settings()
app = FastAPI(title=settings.project_name, lifespan=lifespan)
app.add_middleware(RequestContextMiddleware)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(api_router, prefix=settings.api_v1_prefix)
