# Architecture Decisions

## ADR-001: Backend foundation (2026-10-27)

- **Orchestration:** `docker-compose.yml` runs `db` (`postgis/postgis:16-3.4`), `redis` (`redis:7-alpine`, AOF persistence) and `backend`. The backend waits on both services' healthchecks (`depends_on: condition: service_healthy`).
- **Configuration:** All runtime settings come from environment variables through `pydantic-settings` (`app/core/config.py`). The cached `get_settings()` is the single access point. Defaults target a local `docker compose` setup; Compose overrides hostnames to the service names (`db`, `redis`).
- **Database access:** One process-wide `AsyncEngine` (asyncpg driver, `pool_pre_ping=True`) and an `async_sessionmaker` with `expire_on_commit=False`, `autoflush=False`. Route handlers get a request-scoped `AsyncSession` through the `get_db()` dependency. The engine connects lazily and is disposed of in the FastAPI lifespan shutdown.
- **Models:** All SQLAlchemy models inherit from `app.core.db.Base`, whose metadata is Alembic's `target_metadata` (ADR-002).
- **Health:** `/health` is a liveness probe only and does not touch Postgres or Redis (see ADR-003 for readiness).

## ADR-002: Database migrations with Alembic (2026-10-27)

- **Async env:** `backend/alembic/env.py` is based on Alembic's `async` template. It takes the DSN from `get_settings().database_url`, never from `alembic.ini`, so the app and migrations always target the same database.
- **Metadata:** `env.py` imports `app.models` and targets `Base.metadata`. Every new model module **must** be imported in `app/models/__init__.py`, or autogenerate will not see it.
- **PostGIS:** Revision `0001` runs `CREATE EXTENSION IF NOT EXISTS postgis`. An `include_object` filter hides extension-owned tables (`spatial_ref_sys`, `tiger*`, `topology`) so autogenerate never tries to drop them.
- **Naming:** Revision files are named `YYYY_MM_DD_<rev>_<slug>.py` and use short sequential revision IDs (`0001`, `0002`, …) so their order is easy to read.
- **Startup:** In Compose, the backend runs `alembic upgrade head` before starting Uvicorn. With multiple replicas, this should move to a one-off migration job.
- **Drift guard:** `tests/test_migrations.py` runs upgrade → downgrade → upgrade, then `alembic check`. It fails if a model changes without a matching revision.

## ADR-003: Readiness probe (2026-10-27)

- `GET /ready` checks PostgreSQL (`SELECT 1` through a request-scoped `AsyncSession`) and Redis (`PING`) concurrently. Each check has a 2s timeout. It returns 200 when both pass, otherwise 503 with a per-dependency `ok`/`unavailable` map.
- Failure details are **logged, never returned**. The probe is public, so it must not leak hostnames or driver errors.
- The check logic lives in `app/services/health.py`, and the route in `app/api/health.py` stays thin. Redis uses a single process-wide `redis.asyncio.Redis` client (`app/core/redis.py`), injected through `get_redis` and closed in the app lifespan.
- The Compose `backend` healthcheck calls `/ready`.

## ADR-004: Test layout (2026-10-27)

- Unit tests replace `get_db`/`get_redis` through `app.dependency_overrides` and need no running services.
- Tests marked `@pytest.mark.integration` need real PostgreSQL/PostGIS and Redis. They **skip automatically** when those aren't reachable. Run only these with `pytest -m integration`, or skip them with `-m "not integration"`.
- Migration tests use a disposable `<POSTGRES_DB>_test` database (`TEST_DATABASE_URL`). The Postgres container creates it on first start (`infra/postgres/init/`).

## ADR-005: CI pipeline and auto-merge (2026-10-27)

- `.github/workflows/ci.yml` runs on every PR and on pushes to `main`. It has three jobs:
  - **lint**: `ruff check`, `ruff format --check` and `mypy --strict` (configured in `backend/pyproject.toml`).
  - **test**: pytest against PostGIS and Redis service containers, with migrations applied first.
  - **docker**: `docker compose up --build --wait`, which blocks until every healthcheck passes, including the backend's `/ready`. It then curls the probes and runs pytest inside the backend container.
- CI sets `REQUIRE_SERVICES=1`, which makes integration tests **fail** instead of skip when a service is unreachable. A broken service container can't produce a green run.
- **Auto-merge:** When all three jobs pass, the `automerge` job merges the PR with a merge commit and deletes the head branch, using the workflow's `GITHUB_TOKEN`. Safeguards:
  - Pull requests from forks, draft PRs and PRs labelled `do-not-merge` are never merged.
  - `--match-head-commit` stops a merge if new commits arrived after the run started.
  - A newer push cancels the older run (concurrency group).
  - Removing the `do-not-merge` label or marking a draft ready reruns CI, which can then merge.
- Known limitation: a merge made with `GITHUB_TOKEN` does not trigger new workflow runs, so CI does not run again on `main` after an auto-merge. The merged code already passed CI on the PR.
- Branches are named `<type>/<short-kebab-name>` (see `CLAUDE.md`).
