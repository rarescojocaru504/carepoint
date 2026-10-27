# Architecture Decisions

## ADR-001: Backend foundation (2026-10-27)

- **Orchestration:** `docker-compose.yml` runs `db` (`postgis/postgis:16-3.4`), `redis` (`redis:7-alpine`, AOF persistence) and `backend`. The backend waits on both services' healthchecks (`depends_on: condition: service_healthy`).
- **Configuration:** All runtime settings come from environment variables through `pydantic-settings` (`app/core/config.py`). The cached `get_settings()` is the single access point. Defaults target a local `docker compose` setup; Compose overrides hostnames to the service names (`db`, `redis`).
- **Database access:** One process-wide `AsyncEngine` (asyncpg driver, `pool_pre_ping=True`) and an `async_sessionmaker` with `expire_on_commit=False`, `autoflush=False`. Route handlers get a request-scoped `AsyncSession` through the `get_db()` dependency. The engine connects lazily and is disposed of in the FastAPI lifespan shutdown.
- **Models:** All SQLAlchemy models inherit from `app.core.db.Base`. Its metadata will be Alembic's `target_metadata` once migrations are initialised.
- **Health:** `/health` is a liveness probe only and does not touch Postgres or Redis. A separate readiness probe that checks dependencies is planned.
