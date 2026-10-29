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

## ADR-009: User authentication, roles and rate limiting (2026-10-28)

- **Passwords:** argon2id (`argon2-cffi`, RFC 9106 low-memory profile: 64 MiB, t=3, p=4). Hashing runs in a thread pool so it never blocks the event loop. Hashes are upgraded on login when the parameters change.
- **Login gives nothing away.** An unknown email, a wrong password and a disabled account all return the same 401 body. Unknown emails are checked against a dummy hash, so they take the same time as a real check. Emails are case-insensitive (lowercased everywhere).
- **Sign-up can't grant privileges.** `POST /auth/register` always creates a `customer`. The schema has no `role` field, and `extra="forbid"` rejects attempts to send one. Operator and admin accounts are created by seeding or, later, by admins.
- **Access token:** a JWT signed with HS256 that lasts 15 minutes and carries `sub`, `role`, `type=access`, `iss`, `aud`, `iat`, `nbf`, `exp` and `jti`.
  - Decoding **pins the algorithm** (blocking `alg: none` and algorithm-confusion attacks) and requires every claim.
  - The token's `role` claim is informational only. **Authorisation always reads the role and `is_active` from the database on each request**, so a demotion or deactivation takes effect immediately.
- **Refresh token:** 256 random bits, opaque, stored only as SHA-256 (a fast hash is fine for high-entropy values), valid for 30 days.
  - **Rotated on every use.** All tokens from one login share a `family_id`.
  - **Reuse means theft.** Presenting an already-rotated token revokes the whole family, logging out both the attacker and the victim's session for that login, while other logins stay valid.
  - The token row is locked with `SELECT … FOR UPDATE`, so two concurrent refreshes with the same token serialise and the second one counts as reuse (tested with real concurrent connections).
  - `POST /auth/logout` revokes the family. It succeeds silently for unknown tokens.
  - Token responses send `Cache-Control: no-store`.
- **Secrets:** `JWT_SECRET` has a development default. With `ENVIRONMENT=production` the app **refuses to start** if it's the default or shorter than 32 characters.
- **Roles:** `require_role(...)` is a dependency factory. The `/api/v1/admin` router requires `operator` or `admin`, and individual routes can narrow that further (for example, `GET /admin/users` is admin-only because it exposes personal data).
- **Rate limiting:** fixed-window counters in Redis.
  - INCR and EXPIRE run in one Lua script, so a counter can never be left without a TTL.
  - Keys contain a SHA-256 of the identifier, never raw emails or IPs.
  - Defaults, configurable through `RATE_LIMIT_*` settings:

    | Endpoint | Per IP | Per email |
    |---|---|---|
    | Login | 30/min | 5/min |
    | Register | 20/hour | — |
    | Refresh | 60/min | — |

  - Per-IP limits are generous on purpose, because a whole campus can share one NAT address. The per-email limit is what stops password guessing against one account.
  - A 429 response carries `Retry-After`.
  - **Fails closed:** if Redis is unreachable, auth endpoints return 503 rather than running unprotected.
  - Behind a reverse proxy, run Uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>`. Client-supplied `X-Forwarded-For` is never trusted directly.

## ADR-010: Machine request signing, CAREPOINT-ED25519-V1 (2026-10-28)

Builds on ADR-007: each machine holds an Ed25519 private key, and the server stores only the 32-byte public key.

**Headers sent with every machine request**

| Header | Value |
|---|---|
| `X-CarePoint-Machine` | the serial number, for example `CP-BUC-0001` |
| `X-CarePoint-Timestamp` | Unix time in whole seconds |
| `X-CarePoint-Nonce` | 22–64 base64url characters (at least 128 random bits), never reused |
| `X-CarePoint-Signature` | unpadded base64url of the 64-byte Ed25519 signature |

**What is signed.** UTF-8 text with lines joined by `\n` and no trailing newline:
```
CAREPOINT-ED25519-V1
<METHOD>
<raw path>[?<raw query string>]
<serial number>
<timestamp>
<nonce>
<lowercase hex SHA-256 of the raw body; of the empty string if there's no body>
```
The server rebuilds this from the **raw** request path and query bytes (`scope["raw_path"]`, `scope["query_string"]`). No URL normalisation happens on either side, so client and server can't disagree about what was signed.

**Server checks, in this order:**
1. **Header format.**
2. **Clock window:** the timestamp must be within ±`MACHINE_AUTH_MAX_SKEW_SECONDS` (60 by default).
3. **Machine:** it must exist, have a key and not be `decommissioned`. Machines in `maintenance` may still authenticate, so they can report their state.
4. **Signature.**
5. **Nonce:** `SET NX` in Redis with a TTL of twice the clock window. A nonce is accepted only once while its timestamp is still valid.

The nonce is consumed **only after** the signature verifies. Otherwise an attacker could fill the replay store with junk, or use up a nonce they had seen before the real request arrived. A test covers this.

- **Failure responses:** every failure returns the same `401 {"detail": "Invalid machine signature"}`, and the specific reason is logged on the server. If Redis (the replay store) is down, the server returns 503, failing closed.
- **Authentication comes first:** it runs before body validation, so unauthenticated callers can't probe the payload schema.
- **WebSocket (Phase 6):** the handshake uses the same scheme, with method `GET` and an empty body.

**Provisioning** with `python -m app.scripts.provision_machine <serial>`:
- `--public-key <b64url>`: register a key the **device generated itself**. This is preferred for real hardware, because the private key never leaves the device.
- `--out <file>`: generate a key pair on the server (for simulators and bench setups). The private key is written to that file with mode 0600, and the command never overwrites an existing file.
- `--rotate`: required to replace an existing key. The old key stops working immediately.

**Known-answer vector.** Use it to check a firmware implementation. Ed25519 is deterministic, so this is the only valid signature. It was cross-checked with `openssl pkeyutl -sign -rawin`.
- **Private key seed:** bytes `00 01 02 … 1f`.
- **Public key:** `A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg`.
- **Request:** `POST /api/v1/machine/heartbeat`, serial `CP-TEST-0001`, timestamp `1767225600`, nonce `AAAAAAAAAAAAAAAAAAAAAA`.
- **Body:** `{"firmware_version":"1.0.0","uptime_seconds":42}`, whose SHA-256 is `44e0349bb5ee07e9fd15d4437bfd4984b8375a6d255fa45ecaebd409dcd43017`.
- **Signature:** `3rLGXdo8ZelsHHTse4YjpmEwH21fWUHkSdCbEjHI33PrIhvHbpnU1OuCuQdnHHY25lFAcRF8wsJS2ZObgMTsCw`.

**First signed endpoint:** `POST /api/v1/machine/heartbeat` with body `{firmware_version, uptime_seconds}`. It updates `last_seen_at` and `firmware_version` (migration `0004`).

## ADR-011: Discovery API and the definition of "available" (2026-10-28)

- **Endpoints.** All are public, so people can browse the map without an account. They share a generous per-IP rate limit (`RATE_LIMIT_DISCOVERY_IP`, 300/min), which **fails open** because the endpoints are read-only. A Redis outage therefore doesn't take the map down. Auth endpoints still fail closed (ADR-009).
  - `GET /api/v1/machines/nearby?lat&lng&radius_m&limit`: machines within the radius, nearest first, with distance, online flag and availability totals.
  - `GET /api/v1/machines/{id}`: stocked slots with the available quantity and a low-stock flag. Empty slots and inactive products are hidden.
  - `GET /api/v1/products?category=`: the active catalogue.
  - `GET /api/v1/products/{sku}/nearby?lat&lng&radius_m&limit`: "where can I get this nearest to me?". It lists only machines that have units available, sums all slots holding that SKU, and returns the **fullest slot** to reserve from.
- **One definition of availability** (`app/services/availability.py`), used by every read path:
  `available = max(quantity − active holds, 0)`.
  - An active hold is a `held` or `paid` reservation whose `expires_at` is in the future. A hold stops counting **the moment it expires**, even before Phase 4's expiry worker marks it `expired`.
  - Machines that aren't `active` (for example in maintenance) and inactive products always show 0 available. They are still listed with their status, so the app can show them greyed out.
  - Phase 4's Redis hold logic must produce the same numbers.
- **Online:** a machine is online when `last_seen_at` is within `MACHINE_ONLINE_WINDOW_SECONDS` (90 s). Decommissioned machines never appear and return 404.
- **Geo:**
  - The query point is built with `ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography` from **bound float parameters**, never from formatted SQL.
  - `ST_DWithin` on `geography` filters by true spheroid distance in metres and uses the GiST index `ix_machines_location` (confirmed with `EXPLAIN`).
  - Results are ordered by `ST_Distance` with the machine id as a tie-breaker.
- **Input limits:** `lat` must be in [-90, 90] and `lng` in [-180, 180], with NaN and inf rejected; `radius_m` must be 1–20,000; `limit` must be 1–50.
- **Responses never include internal fields:** serial numbers, public keys, firmware versions and slot capacity are left out.

## ADR-012: Reservation holds in Redis, the anti-oversell gate (2026-10-28)

**Problem.** Many customers may tap "reserve" on the last unit at the same moment. Exactly one may win, and a unit must never stay locked after its hold lapses.

**Why not one key per hold with a TTL?** It looks natural: `SET hold:<id> EX 900`. But when Redis expires that key, nothing gives the unit back to the "available" count. The design would need keyspace notifications, which are fire-and-forget pub/sub messages lost on any disconnect. A single lost message leaks a unit until a human notices.

**Decision.** Each slot keeps its holds in a sorted set scored by expiry. All keys share one hash tag, so they stay on one shard if Redis Cluster is used later:

| Key | Type | Contents |
|---|---|---|
| `cp:slot:{<slot_id>}:qty` | string | physical units, mirrored from Postgres |
| `cp:slot:{<slot_id>}:holds` | sorted set | reservation id → expiry in epoch milliseconds |
| `cp:slot:{<slot_id>}:holdqty` | hash | reservation id → units held |

Every operation is **one Lua script**, so Redis runs it atomically. Each script first removes holds whose expiry is at or before **Redis's own clock** (`TIME`), so client clock skew doesn't matter. The reserve script then works out `available = qty − Σ held` and either writes the hold or refuses it.

**Consequences:**
- **Expiry is part of the arithmetic.** An expired hold can't keep a unit locked, even if every background job is dead. No TTL callbacks or keyspace notifications are needed for correctness.
- **Concurrency.** Requests for the same slot serialise inside Redis, and exactly one of the requests competing for the last unit succeeds. Different slots don't contend.
- **Idempotency in Redis.** Reserving the same reservation id twice returns the same hold.

**Source of truth.** Postgres holds the durable record (`reservations` rows); Redis is the fast, atomic gate.
- **First use, or Redis flushed or restarted without persistence:** the reserve script reports *needs init*. The service rebuilds the slot from Postgres (physical quantity plus unexpired `held`/`paid` reservations) using a script that initialises **only if the keys are still missing**, so a concurrent rebuild can't overwrite holds granted in the meantime. It then retries.
- **Committed stock changes** (restocks, adjustments) are mirrored with `apply_stock_delta`, which is a no-op when the slot isn't initialised yet, because the next rebuild reads the new quantity.

**Write order, chosen so every failure is safe:**
- **Reserve:** take the Redis hold first, then insert the Postgres row. If the insert fails, the hold is released. If the process crashes between the two steps, the orphan hold expires by itself, so the unit is at worst unavailable for one hold period. The reverse order could leave a Postgres hold that Redis doesn't know about, which could oversell.
- **Cancel (and, later, dispense and expiry):** update Postgres first, then release in Redis. If the release fails, the unit stays locked only until the hold's expiry.

**Per-user rules:**
- Each user's create requests are serialised with a Postgres advisory transaction lock, so the active-hold limit (`MAX_ACTIVE_RESERVATIONS_PER_USER`, 3) and idempotency are race-free.
- `idempotency_key` is unique per user. A retry with the same key and parameters returns the original reservation (`200`); the same key with different parameters returns `409 idempotency_conflict`.
- New reservations are rate-limited per user (`RATE_LIMIT_RESERVATION_USER`, 20/min) against hoarding bots.

**Settings and API:**
- **Hold length:** `RESERVATION_HOLD_SECONDS`, 900 by default. Set it to about 60 for live demos.
- **Price:** the unit price is copied onto the reservation when it's created, so a later price change doesn't affect it.
- **Status:** a hold past its expiry **reads** as `expired` in the API even before the sweeper (Phase 4, part 2) records it.
- **Error codes:** errors return a stable `code`: `insufficient_stock` (with `available`), `not_sellable`, `too_many_active` (with `limit`), `idempotency_conflict`, `not_cancellable` (with `status`).
- **Privacy:** someone else's reservation returns 404, not 403.

**Evidence:**
- Tests against real Redis and Postgres:
  - 20 rounds of 40 concurrent HTTP customers racing for the last unit, with exactly one winner each round;
  - 30 customers for 7 units;
  - a double-tapped button;
  - one user trying to exceed the limit concurrently;
  - cancels interleaved with new reservations;
  - 100 coroutines for 5 units, over 10 rounds.
- **Mutation check:** replacing the atomic script with a check-then-act version made 33 race tests fail, including one run with 19 holds for 3 units.

## ADR-013: Expiry sweeper, reconciliation and dispensing (2026-10-28)

**Background jobs.** These run in-process, started from the FastAPI lifespan in `app/services/background.py`. They're controlled by `BACKGROUND_WORKERS_ENABLED`; the test suite sets it to false and drives the jobs directly. Each job loops on its own sessions. A failing pass is logged and retried next interval, and never kills the loop. Every job is safe to run on several app instances at once, so no leader election is needed.

- **Expiry sweeper** (`EXPIRY_SWEEP_INTERVAL_SECONDS`, 15 s). It marks `held` and `paid` reservations past `expires_at` as `expired`, then tidies their Redis holds. It's bookkeeping only: availability already ignores lapsed holds (ADR-011, ADR-012). It is also where Phase 5 will void payments.
  - The batch is selected in a **`MATERIALIZED` CTE** with `FOR UPDATE SKIP LOCKED`, and the `UPDATE` re-checks the status itself.
  - *Lesson learned:* the first version, `UPDATE … WHERE id IN (SELECT … LIMIT n FOR UPDATE SKIP LOCKED)`, expired **7 rows with a limit of 5** in a test. Postgres had picked a nested-loop semi-join that re-runs the locking subquery for every outer row; it happened on a never-analyzed table and isn't reliably reproducible afterwards. A `MATERIALIZED` CTE is evaluated once whatever plan Postgres chooses, and the status re-check in the `UPDATE` stops a reservation that was just dispensed from ever being expired.
  - `SKIP LOCKED` means a reservation locked by another transaction (a dispense in flight, a cancel) is **skipped**. It is never waited on or overwritten, and whatever that transaction decides wins.
- **Reconciliation** runs at startup and then every `RECONCILE_INTERVAL_SECONDS` (300 s). For each slot it compares Redis with Postgres and applies **only the difference**, in one atomic script:
  - holds Postgres has (`held`/`paid`, unexpired) but Redis lacks are **added**;
  - Redis holds whose reservation is finished in Postgres are **removed**;
  - Redis holds with no Postgres row are **removed once they're older than `HOLD_ORPHAN_GRACE_SECONDS`** (60 s). Younger ones may be an insert still in flight, so they stay;
  - the quantity mirror is set with **compare-and-set**: if a dispense or restock lands between the read and the write, it's left alone and fixed on the next pass;
  - a slot that isn't initialised is built from scratch, as after a Redis restart without persistence.

  Because only a difference is applied, holds granted while reconciliation is running are never dropped.
- **Dispensing** (`services/dispensing.complete_dispense`, which Phase 6 will call):
  1. Lock the reservation row. That makes the sweeper skip it.
  2. Check that it's `held` or `paid` and unexpired. A dispense that **started** in time completes even if the clock passes `expires_at` while it runs. If the sweeper took the lock first, the dispense is refused as `expired`.
  3. In one transaction, write a `dispense` stock movement and set the status to `dispensed`. The ledger refuses to take stock below zero, which is the final guard.
  4. Commit, then remove the Redis hold and the physical units together in one atomic step, so availability never briefly dips or spikes.

  Calling it again for an already-dispensed reservation changes nothing.
- **Known small window:** if that last Redis step fails after the commit, availability stays correct only while the hold is still present. Once the hold lapses, Redis over-counts stock by the dispensed units until the next reconciliation (at most 5 minutes). The ledger still refuses a dispense that would go below zero, so no phantom items can be dispensed, but a reservation could be accepted that later can't be fulfilled. Phase 5's payment void covers that case.
- **Evidence:**
  - **Live server:**
    - the sweeper expired a backdated hold within about 2 s;
    - startup reconciliation corrected a corrupted Redis quantity from 99 back to 8.
  - **Tests, on real concurrent connections:**
    - a dispense in flight across its expiry, with the sweeper running in parallel and stepping aside;
    - the sweeper getting there first, so the dispense is refused;
    - 5 rounds of 8 reservations around the expiry boundary, dispensed and swept concurrently. Each one ends exactly once, the slot quantity matches the ledger, and Redis agrees;
    - a Redis wipe followed by a full rebuild.
  - **Mutation-tested:**

    | Deliberately broken | Tests that failed |
    |---|---|
    | Sweeper with no lock and no status re-check | 1 |
    | Reconciliation without compare-and-set | 1 |
    | No orphan grace period | 1 |
    | Dispense not updating Redis | 6 |

- **Test infrastructure:** concurrency tests use a committed "world" fixture (`tests/world.py`). Its machines are in the Southern Ocean, away from every other test's coordinates. Cleanup deletes the ledger rows it created by suspending triggers (`session_replication_role = replica`) for that one transaction. This is test-only; the application never bypasses the append-only rule.

## ADR-014: Load testing, and what it changed (2026-10-28)

**Tools.** They live in `loadtest/`; methodology and results are in `docs/benchmarks.md`.
- **`race.py`**, the anti-oversell experiment. *N* customers send `POST /reservations` for the same slot at one shared wall-clock instant. A round passes only if the number of winners is exactly `min(units, N)`, every loser gets a clean 409, nothing errors, and the API then reports 0 left. The script exits non-zero on any failure.
- **`locustfile.py`**, sustained mixed traffic: browse, open machines, reserve and cancel.
- **`python -m app.scripts.loadtest_fixtures create|cleanup`**, test data. It creates customers with pre-issued tokens and machines on "Null Island" (0, 0), away from the demo map, and refuses to run in production.

**Result:** 500 customers against 1 unit gave **one winner in each of 10/10 rounds and 0 errors in 5,000 requests**, with p95 0.87 s on 2 Uvicorn workers. Everything shared one 4-vCPU box.

**Changes made because of the measurements:**
1. **Keep-alive.** Uvicorn's default 5 s idle timeout caused sporadic `RemoteProtocolError: server disconnected`, because the server closed connections just as clients reused them. The Compose command now passes `--timeout-keep-alive 75`, longer than typical load-balancer idle timeouts of about 60 s, which is the standard rule behind a load balancer. Errors went to 0.
2. **Fast-fail pre-check.** In a rush almost every request loses, but each one used to take the per-user lock and run 3 queries before Redis refused it. `create_reservation` now reads the slot's Redis availability first and refuses straight away when the slot is clearly short.
   - **Not a correctness check:** the atomic Lua script still decides every request that gets past it.
   - **Ordered after the idempotency lookup,** so a retried request whose reservation exists still gets `200` with its original reservation even after the item sells out (tested and mutation-checked).
   - It's skipped for slots Redis doesn't know yet.
   - **Result:** p95 with 1 worker went from 2.1 s to 1.3 s.
3. **Worker processes** can now be set through `WEB_CONCURRENCY` (read by Uvicorn; Compose default 1). The background jobs are already safe across processes (ADR-013).
4. **Campus NAT.** The discovery limit per IP went from 300/min to **1,200/min**, because hundreds of students on campus Wi-Fi share one public IP and 300/min covers only about 60–100 app opens per minute for the whole network. The long-term answer is per-device or per-account limits once the mobile app sends a device identity (Phase 9).
5. **Measuring the tool, not the system.** A single httpx event loop maxed out one core while the server sat idle. `top` caught it, and that's why `race.py` spreads the burst over several aiohttp client processes. Lesson: check where the CPU goes before you believe a latency number.

## ADR-015: Checkout, mock payments and settlement (2026-10-29)

**Reservation states:**
```
held ──checkout──▶ paid ──dispense──▶ dispensed
  │                 │
  ├─cancel─▶ cancelled ◀─cancel─┤
  └─expiry─▶ expired  ◀─expiry─┘
```
- **Only `paid` reservations can be dispensed.** Every other combination is refused, and `test_state_machine` covers all 17 combinations of state and action.
- **Repeats are harmless:** paying again, dispensing again and cancelling again are all idempotent.

**Money for one reservation:**
- **Checkout** *authorises* the amount, so it's reserved but not taken (`POST /reservations/{id}/checkout`).
- **Dispense** *captures* it, so the money is taken.
- **Expiry or cancellation** *voids* it, so the money is released.

This is the standard "authorise now, capture on fulfilment" flow. A customer whose hold lapses is never charged.

**Provider interface.** `PaymentProvider` (`app/services/payments.py`) has three methods: `authorize`, `capture` and `void`. The MVP's `MockPaymentProvider` moves no money and behaves according to test tokens, as real providers' test cards do:

| Token | Behaviour |
|---|---|
| `pm_card_ok` | succeeds |
| `pm_card_declined` | returns `402 payment_declined` |
| `pm_card_provider_error` | returns `503 payment_unavailable` (simulated outage) |

A real provider, such as Stripe PaymentIntents with manual capture, slots in behind the same interface.
- **Only tokens are accepted.** Checkout takes a provider payment-method token matching `^pm_[a-z0-9_]{2,60}$`, never a raw card number; a card number fails validation with 422.
- **Retries are safe.** The provider idempotency key is `checkout:<reservation id>`. If the process dies between authorising and committing, a retry gets the **same** authorisation instead of a second one.

**Checkout is race-free.** The reservation row is locked while checkout runs:
- a double-tapped "pay" button creates exactly one order: one request gets 201 and the others get 200 with the same order;
- the expiry sweeper skips the locked row;
- validity is judged once the lock is held.

A decline or outage happens **before anything is written**. The reservation stays `held` and the customer can retry until it expires.

**Settlement: payment follows the reservation.** The physical world doesn't wait for a payment provider. Once a machine reports "dispensed", the item is in the customer's hand; once a hold expires, the unit is back on sale. So the reservation's status is decided first, and the payment is settled to match it (`app/services/payment_settlement.py`):
- a reservation that's `dispensed` gets its payment **captured**; one that's `expired` or `cancelled` gets it **voided**;
- cancel and dispense try to settle immediately;
- the background loop runs `settle_pending` after every expiry sweep and **retries until payment and reservation agree**, for example after a provider outage;
- each order is locked with `SKIP LOCKED`, so two workers never settle the same order;
- one failing order doesn't block the others (tested and mutation-checked);
- provider capture and void are idempotent, so retrying after an ambiguous failure is always safe.

**Other details:**
- **Card-testing defence:** checkout attempts are rate-limited per user (`RATE_LIMIT_CHECKOUT_USER`, 10/min) against people testing stolen card numbers.
- **Order history:** `GET /api/v1/orders` (newest first) and `GET /api/v1/orders/{id}` return the payment status and the reservation's effective status. Someone else's order returns 404.
- **Known limitation:** if checkout authorises but then fails to commit and is never retried, the authorisation stays at the provider until it lapses there (real providers expire uncaptured authorisations after about 7 days). No money is taken.

## ADR-016: Pickup codes (2026-10-29)

**The credential.** It's created at checkout, together with the `paid` status:
- a **6-digit code** from `secrets.randbelow`, a cryptographically secure source;
- a **QR payload** `CP1:<reservation id>:<code>` for the kiosk camera.

The checkout response returns both **once**, with `Cache-Control: no-store`. An idempotent repeat of the checkout returns `pickup: null`, because the server can't show a code it doesn't store. `POST /api/v1/reservations/{id}/pickup-code` issues a new code (rate-limited per user), and the old code stops working immediately.

**Only a keyed hash is stored:** `HMAC-SHA256(PICKUP_CODE_PEPPER, "<machine id>:<code>")`.
- With only 10⁶ possible codes, a plain SHA-256 could be reversed from a database dump in under a second. The server-side pepper prevents that. Like `JWT_SECRET`, the app refuses to start in production with the default pepper or one shorter than 32 characters.
- Including the **machine id** binds the code to the one machine holding the item. The same code typed anywhere else matches nothing.

**Single use by construction.** A code resolves only while its reservation is `paid`, unexpired and at that machine. Dispensing, cancelling or expiry ends that, so no separate "used" flag can drift out of sync.

**No duplicates.** A partial unique index (`uq_reservations_active_pickup_code`, on the hash where `status = 'paid'`, migration `0005`) means two active codes can't collide at a machine. A collision is caught inside a savepoint and a fresh code is drawn (tested by forcing the generator to repeat). Finished reservations release their hash for reuse.

**Verification** (`pickup_codes.verify(machine, credential)`, which Phase 6 calls when the kiosk submits a code):
- **Input format:** it accepts exactly 6 ASCII digits or the exact `CP1` QR format, ignoring surrounding whitespace. Anything else is an invalid code.
- **QR check:** for a QR payload, the reservation id must match as well as the code.
- **No side effects:** it returns the reservation and slot to dispense without changing anything. Dispensing changes the state.

**Brute force.** **Wrong** codes count towards a per-machine limit (`RATE_LIMIT_PICKUP_FAILURES_MACHINE`, 20/min). Once over it, **every** attempt is refused until the window passes, even a correct one, so the kiosk tells the user to wait. Successful pickups don't count, so a busy machine is never locked out. With *k* active codes at a machine, a guesser needs about 10⁶/*k* tries; with *k* = 5 that's about 200,000 tries, or roughly a week of continuous typing at the kiosk, in person, on camera.

**Evidence:**
- 24 tests: issuing; hash ≠ code ≠ unkeyed hash; verifying by code and by QR; bound to its machine; 6 kinds of bad input, including Unicode digits and injection attempts; dead after dispense, cancel and expiry; rotation; uniqueness and reuse; collision regeneration; lockout; success-not-counted; the pepper requirements.
- **Mutation-tested:** removing the pepper, removing the machine binding, letting codes work after dispensing, removing the lockout, counting successes as failures, and letting a QR ignore its reservation id were each caught. Accepting non-ASCII digits survives as an *equivalent* mutant, because such codes hash differently and can never match; the ASCII-only rule is defence in depth.
