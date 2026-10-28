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

## ADR-006: Domain model (2026-10-28)

- **Tables** (migration `0002`):
  - `users`, with `role` of `customer`, `operator` or `admin`;
  - `products`;
  - `machines`, with a PostGIS `geography(Point, 4326)` location and a GiST index;
  - `machine_slots`;
  - `stock_movements`;
  - `reservations`;
  - `orders`.
- **Keys:** UUID primary keys, generated by the application with `gen_random_uuid()` as the server default. The exception is `stock_movements`, which uses a `BIGINT` identity because it's a high-volume ledger.
- **Enums:** stored as `VARCHAR` with a `CHECK` constraint, not native PostgreSQL enums. Adding a status later then only means swapping a constraint in a migration. The values are lowercase strings shared with the API.
- **Naming convention:** `Base.metadata` defines one (`pk_`, `fk_`, `uq_`, `ck_`, `ix_`), so every constraint has a deterministic name that future migrations can refer to.
- **Database-enforced invariants.** The database is the last line of defence, even against our own bugs:
  - prices and amounts are positive integer minor units, never floats;
  - currency is ISO-4217 format;
  - SKUs, serial numbers and slot codes match fixed patterns;
  - `0 <= quantity <= capacity`, and an empty slot can't hold stock;
  - emails are stored lowercase, which makes the unique constraint case-insensitive;
  - an Ed25519 public key is exactly 32 bytes.
- **No cascading deletes.** Every foreign key is `ON DELETE RESTRICT`. Machines and products are retired by status or `is_active`, never deleted, so history (`stock_movements`, reservations, orders) can't disappear as a side effect.
- **Append-only stock ledger.** Every change to `machine_slots.quantity` goes through `services.inventory.record_stock_movement()`. In one transaction it locks the slot row (`SELECT … FOR UPDATE`), checks the bounds, updates the quantity and appends a `stock_movements` row recording the delta, the resulting quantity, the reason, the actor and the reservation. A trigger rejects `UPDATE`, `DELETE` and `TRUNCATE` on `stock_movements`. Restock history, dispense audits and telemetry corrections can therefore be trusted, and the sum of the deltas always equals the current quantity (tested).
- **Price snapshot:** `reservations.unit_price_cents` records the price at reservation time, so a later catalogue change can't alter an open reservation.
- **Online status isn't stored.** `machines.status` is operational (`active`, `maintenance`, `decommissioned`). Online/offline is derived from `last_seen_at`.
- **Validation:** request schemas (`app/schemas/`) reject unknown fields and use strict numbers, so `"5"`, `true` and `12.99` are never accepted as quantities or cents. Coordinates are range-checked and NaN/inf are rejected. Image URLs must be HTTPS. Sign-up has no `role` field. Operators may only use the `restock` and `adjustment` reasons, since the others are produced by the system.
- **Tests:** they run in a transaction that's rolled back after each test (`db_session` fixture), so nothing persists. This matters because the append-only ledger can't be cleaned up any other way.

## ADR-007: Machines authenticate with Ed25519 keys, not shared secrets (2026-10-28)

- The roadmap first proposed "a per-machine secret, stored hashed, used for HMAC signatures". Those two requirements contradict each other: to verify an HMAC the server needs the **raw** secret, so it can't store only a hash.
- **Decision:** every machine has an Ed25519 key pair. The private key is generated on the machine (or at provisioning) and **never leaves it**. The server stores only the 32-byte public key (`machines.auth_public_key`).
- **Why:**
  - A leak of the database or a backup exposes no credentials, so an attacker still can't impersonate a machine.
  - Signatures are small (64 bytes) and verify quickly.
  - Ed25519 is supported on Raspberry Pi (Python `cryptography`/libsodium) and ESP32 (libsodium/Monocypher).
- The signing scheme (canonical request, timestamp window, nonce replay protection) is specified in Phase 2, `feature/machine-auth`.

## ADR-008: Demo data seeding (2026-10-28)

- Run it with `python -m app.scripts.seed`, or `docker compose exec backend python -m app.scripts.seed`.
- **What it creates:**
  - 6 machines around university campuses in Bucharest, one of them in `maintenance`, with 8 slots each;
  - 12 products in `RON`, with the currency taken from the `CURRENCY` setting;
  - an admin, an operator and a customer account.
- **Where the data lives:** `app/services/demo_data.py`. Edit `MACHINES` to demo somewhere else.
- **Idempotent by natural key:** email, SKU, serial number, and machine plus slot code. A second run creates only what's missing. It **never** changes existing rows, so live stock, operator edits and changed passwords survive a re-seed (tested).
- Initial stock goes through `record_stock_movement(reason=initial)`, so the ledger is complete from the first row.
- **Medicines are left out on purpose.** In most of the EU, over-the-counter drugs can't be sold from unattended machines without pharmacy oversight.
- **Safety:**
  - the command refuses to run with `ENVIRONMENT=production`;
  - passwords come from the `SEED_ADMIN_PASSWORD`, `SEED_OPERATOR_PASSWORD` and `SEED_CUSTOMER_PASSWORD` environment variables, falling back to documented development defaults;
  - passwords are hashed with argon2id (`app/core/security.py`).
