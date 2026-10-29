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

## ADR-017: Machine gateway, part 1: connection and telemetry (2026-10-29)

Machines keep one WebSocket open to `/api/v1/ws/machines`. The wire format is specified in [`machine-protocol.md`](machine-protocol.md), and its JSON Schema is generated from the server's own models. A test fails if the published schema and the code drift apart.

**Handshake = ADR-010.**
- **Signing:** the upgrade request is signed like any machine request (`GET`, raw path, empty body), so there is one signing scheme for HTTP and WebSocket and one known-answer vector.
- **Versioning:** the protocol version is the WebSocket subprotocol, `carepoint.v1`. A future v2 can be offered alongside v1 during a firmware rollout.
- **Refusals:** a refused handshake gets a plain **HTTP** 400, 401 or 503 (the ASGI denial-response extension) rather than an accept-then-close. A machine can therefore tell "fix your key" apart from "retry later" without the server saying which check failed.

**One reply per message, handled in order.**
- **Matching replies:** every machine message carries an `id` and gets exactly one `ack` or `error` with `reply_to`.
- **Simple firmware:** the machine doesn't need any request tracking beyond "wait for the reply to the last message".
- **Ordering:** handling messages strictly in order also gives Phase 6 part 2 the guarantee it needs: a dispense result sent before a stock report is applied before it.

**Zero trust on every frame.**
- **Strict validation:** messages are Pydantic models with `extra="forbid"` and strict numbers, so `"5"`, `true` and `1.5` are never taken as integers, and `NaN` or out-of-range values are rejected. Free text can't contain control characters, because it ends up in logs and on the dashboard.
- **Ownership:** a machine can only touch its own slots. Slot codes are looked up within the authenticated machine, so `A1` from machine X can never reach machine Y's `A1`.
- **Limits per connection:**
  - 16 KiB per message, and text frames only;
  - 30 messages per 10 s;
  - 10 rejected messages, then the connection is closed;
  - 90 s of silence, then the connection is closed.

  Server-side failures are answered with `internal_error` (retry) and don't count as the machine's fault.
- **Decommissioning:** the machine's status is checked on every message, so decommissioning a stolen machine cuts off its live connection immediately (close code 4003), not just its next handshake.

**A session per message, not per connection.** A connection lives for hours. Holding one `AsyncSession` that long would serve stale rows from its identity map and pin a pooled database connection per machine. So each message opens and commits its own short session, from a `get_session_factory` dependency that tests can override.

**One live connection per machine.**
- **Recording the newest:** on connect, the connection's id is written to Redis (`machine-connection:<machine id>`).
- **Superseding:** every incoming message first checks that it is still the newest. If not, the connection is closed with 4000. This works across workers, and a machine that reconnected before its old socket timed out is never served on two connections.
- **Redis failure:** if Redis can't be read, the connection is assumed current. The worst case is briefly serving two connections, which is preferable to dropping a healthy machine.

**Liveness and telemetry.**
- **Online status:** connecting and every `heartbeat` set `last_seen_at`. "Online" is unchanged from ADR-011: heard from within `MACHINE_ONLINE_WINDOW_SECONDS` (90).
- **Heartbeat interval:** `welcome` tells the machine to heartbeat every `MACHINE_HEARTBEAT_INTERVAL_SECONDS` (30). The settings refuse an interval longer than half the window, so one lost heartbeat never shows a healthy machine as offline.
- **Stored telemetry:** the latest door state and temperature are stored on the machine row (migration `0006`, with a database CHECK of −40…85 °C).
- **Event log:** a new append-only `machine_events` table logs connections, disconnections, door changes and reported faults. Phase 8's uptime KPI and error feed read from it.

**Stock reports: the machine's count wins, and the ledger records why.**
- **Why the machine wins:** the machine is the only witness of what is physically inside it.
- **Corrections:** a reported count that differs from the database is applied through `record_stock_movement` as a `telemetry_correction`, with a note of both numbers. The ledger shows every correction, and the Redis quantity mirror is updated after the commit; if that update fails, reconciliation repairs it (ADR-013).
- **All or nothing:** the whole report is validated before anything is written, and rejected as a whole if any entry is unknown, over capacity, or puts items in a slot that has no product assigned. A half-applied report would leave the database matching neither the old state nor the machine.
- **Lock order:** slot rows are locked in id order, so two overlapping reports can't deadlock.

**Server details.**
- **WebSocket implementation:** Compose runs Uvicorn with `--ws websockets`, explicitly, so a missing library fails at startup instead of silently disabling WebSockets.
- **Frame size:** `--ws-max-size 65536` stops oversized frames before they are buffered.
- **Pinning:** `websockets` is pinned, because Uvicorn 0.34 uses its legacy server module.

**Known limits:**
- **Offline lag:** a machine that loses power shows as offline after up to 90 s, not instantly.
- **Key rotation:** rotating a machine's key doesn't drop an already-open connection; decommissioning does.
- **Rate limits are per connection and in memory:** each connection lives on one worker, and reconnecting needs a fresh signed handshake.

**Evidence:**
- **49 tests** run against a real Uvicorn server and a real WebSocket client that signs its handshake as firmware would. They cover:
  - the happy path;
  - 6 forged or stale handshakes, plus a replayed one, no subprotocol and a decommissioned machine;
  - heartbeat and telemetry storage, door-change events and error events;
  - stock correction in Postgres, the ledger and Redis;
  - 3 kinds of whole-report rejection;
  - cross-machine isolation;
  - 20 kinds of malformed message;
  - the invalid-message, flood, binary, oversize, idle and superseded closes;
  - decommissioning while connected;
  - the disconnect log;
  - schema drift.
- **Mutation-tested:** each of these mutants was caught:
  - removing the subprotocol check;
  - the machine scope on slot lookups;
  - the capacity check and the unassigned-slot check;
  - the supersede check;
  - the rate limit and the invalid-message counter;
  - counting server errors as the machine's fault;
  - logging the door on every reading;
  - the Redis mirror;
  - the decommission check;
  - the size limit and the text-only check.

## ADR-018: Machine gateway, part 2: pickup and dispense commands (2026-10-29)

The pickup at the machine is `verify_code` → `verify_result` + `dispense` → `dispense_result`, over the ADR-017 connection. The machine's duties are in `machine-protocol.md` §6. This ADR covers the server side.

**Commands are rows.**
- **The table:** every `dispense` sent is a row in `dispense_commands` (migration `0007`), and its UUID is the `command_id` on the wire.
- **Matching results:** a result, resent after any number of reconnects, always finds exactly the command it belongs to. A result for another machine's command is `unknown_command`.
- **One open dispense per reservation:** a partial unique index (`status = 'open'`) allows at most one open dispense per reservation, as a backstop to the in-progress check.
- **Status values:** `open` → `succeeded` | `failed` | `abandoned`.

**The expiry can't overtake a dispense.**
- **Extending the hold:** accepting a code extends the hold to at least now + `DISPENSE_TIMEOUT_SECONDS` (120), Redis first and then Postgres. If Postgres then fails, Redis just holds the units longer than needed, which is safe and tidied by the sweeper or reconciliation. The new `hold_store.extend` script uses `ZADD GT`, so an extension never shortens a hold, and it restores the hold if Redis had lost it.
- **Why it matters:** without the extension, a code typed 10 s before expiry would release the units for sale while they were coming out of the machine.
- **While a dispense is open:**
  - a second `verify_code` gets `pickup_in_progress`;
  - the customer can't cancel (409 `pickup_in_progress`), because cancelling would void the payment for goods already being delivered;
  - stock reports leave that slot alone (its count is changing, and the result accounts for it).

**Results: charge only for a complete, on-time pickup.**

| Result | Reservation | Payment | Stock ledger |
|---|---|---|---|
| `dispensed`, all units, before the deadline | `dispensed` | captured | `dispense` −N |
| `failed`, 0 units | stays `paid`; the same code can be retried | still authorised; voided if it expires | none |
| `failed`, some units | `cancelled` | voided | `dispense` −k, noted as partial |
| anything after the deadline (or after the sweeper marked it `abandoned`) | unchanged | not captured (voided at expiry) | `dispense` −k, noted as late |

- **Late or partial results are never charged.** That is the simple, customer-friendly rule, and the case is rare.
- **The deadline is what counts:** a result one second after `expires_at` is late even if the sweeper hasn't run yet.
- **Results are idempotent:** a resent result for a finished command is acknowledged and changes nothing (tested: one ledger row, one capture).
- **Contradictory results are rejected:** a result that contradicts its command, such as `dispensed` with fewer units, is rejected before anything is written.

**Disconnects.**
- **On every (re)connect:** after `welcome`, the server resends every open command whose reservation is still pickupable. It can't know whether the machine received a command; the machine's at-most-once rule (by `command_id`) makes the resend safe.
- **After the deadline:** the command isn't resent, because nobody is waiting at the machine any more.
- **Abandoning:** the expiry sweeper marks the open commands of reservations it expires as `abandoned`, in the same transaction.

**Stock stays right even when results are lost.**
- **Late results:** a late result still records the units that came out, so stock is right when the machine comes back much later.
- **Order of results and stock reports:** the machine sends stored results before any stock report. If a machine breaks that rule, or lost its storage, its stock report is authoritative (ADR-017).
- **Self-healing:** any double count is corrected by the next report, and an interim error only ever *under*-states stock, which can't oversell.

**Lock order:** everywhere it is the reservation row, then its command. That covers `request_pickup`, `record_dispense_result`, cancellation and the sweeper, so no two of them can deadlock. The sweeper's `SKIP LOCKED` also means it never waits on a pickup in progress.

**A wrong code is not a protocol error.** `verify_result` refusals (`invalid_code`, `too_many_attempts` with `retry_after_seconds`, `pickup_in_progress`, `machine_unavailable`) never count towards the connection's invalid-message limit. Otherwise a few customer typos would disconnect the machine. Guessing is held back by the per-machine lockout instead (ADR-016).

**Evidence:**
- **26 tests** against a real Uvicorn server and a fake machine client. They cover:
  - pay → code at the machine → dispensed and captured, with the ledger and Redis checked, and the code dead afterwards;
  - QR;
  - hold extension in Postgres and Redis;
  - wrong code not counted as invalid; lockout including the correct code; another machine's code; maintenance;
  - no second dispense or cancel while one is open;
  - a failed dispense, then a retry with the same code;
  - partial: cancelled, voided, −1 in the ledger;
  - a resent result applied once;
  - 3 results that contradict their command; unknown and foreign commands; 5 malformed results;
  - **a command missed during a disconnect, resent on reconnect;**
  - **a disconnect after dispensing but before the result** (outbox, resend, duplicate ignored, one capture);
  - **a machine gone past the deadline:** abandoned and voided, then a late result recorded as stock, nobody charged;
  - a late result before the sweeper has run;
  - no resend after the deadline;
  - a stock report leaving a slot mid-dispense alone.
- **Mutation testing:** 16 mutants were planted and all were caught. The first run let one survive: resending commands after the deadline. Adding the missing test (`test_no_resend_once_the_deadline_has_passed`) caught it.

## ADR-019: Hardware simulator (2026-10-29)

`hardware_sim/` is a Python daemon (aiohttp) that plays the demo machines against the real machine gateway and serves their local kiosk API. It runs in Compose as `simulator`, so `docker compose up` brings up six online machines on the map.

**Written from the spec, not from the backend.**
- **No shared code:** the simulator imports nothing from `backend/`. Its request signing is a fresh implementation of ADR-010, checked against the published known-answer vector. Its message handling follows `machine-protocol.md`.
- **What that proves:** a device can be built from the document alone, which is the point of Phase 6's spec.
- **Separate dependencies:** the simulator has its own pinned dependencies and its own CI job (ruff, `mypy --strict`, pytest).

**The planogram comes from the server (protocol revision 3).** `welcome` now carries `slots`: product, capacity and the server's count.
- **First connection:** a fresh machine adopts those counts.
- **After that:** its own persisted counts are what it reports.
- **No local layout file:** a simulator (or a new real machine) needs no slot configuration of its own, only its serial number and key.

**It obeys the §6 dispensing rules, durably.** Per-machine state is one JSON file, written atomically (temporary file, `fsync`, rename) after every change, like a device writing to flash. It holds:
- the physical counts;
- the executed commands and their results;
- the outbox of results not yet acknowledged;
- the dispense in progress.

What the rules mean in practice:
- **At most once:** a resent `dispense` is answered from storage, never dispensed twice.
- **Interrupted dispenses:** a dispense interrupted by a crash is reported as `failed` / `power_loss` with the units counted so far, never restarted.
- **Results before counts:** after every reconnect, stored results go first and then the stock report. The mechanism runs on its own task, so a dispense finishes even if the connection drops during it.
- **Reconnecting:** exponential backoff with jitter, a fresh signed handshake each time, and the key file re-read on every attempt, so a re-issued demo key is picked up without a restart.

**Fault injection** is set from the config or at runtime with `PUT /api/machines/{serial}/faults`:
- **`offline`:** the machine disconnects and stays away until cleared.
- **`jam`:** `next` or `always`; nothing comes out, and the machine reports a `failed` result plus an `error_event`.
- **`partial_next`:** the next multi-unit dispense stops after one unit.
- **`seconds_per_unit`:** a slow dispense (the demo's CP-BUC-0005 is deliberately slow).
- **`drop_next_result`:** dispense, then lose the connection before reporting. This demonstrates the resend-and-outbox path live.

**The local kiosk API** (`:8080`) is what the touchscreen talks to: snapshot, server-sent events, `verify`, faults and restock.
- **Like a real machine:** the screen talks to the local controller, which holds the key; the key never reaches a browser.
- **Unauthenticated:** this is acceptable because on hardware it listens on localhost. In Compose it is published for the demo, and the module docstring warns not to expose it.

**Demo keys.**
- **Issued at startup:** with `DEMO_INIT=1` (the Compose default), the backend container seeds the demo data and runs `app.scripts.demo_init`. That issues the six demo machines' keys into the `machine_keys` volume, which the simulator mounts read-only.
- **Idempotent:** a key file that matches the registered public key is kept, so a restart doesn't disconnect anything. A missing or stale file (for example after the database was re-created) gets a new key pair.
- **Development only:** it refuses with `ENVIRONMENT=production`, where machines generate their own keys (ADR-010).
- **Why no separate service:** running this inside the backend's startup avoids a separate one-shot container, which `docker compose up --wait` handles inconsistently across Compose versions.

**Evidence:**
- **21 simulator tests** against a fake gateway that verifies every handshake signature. They cover:
  - the known-answer vector;
  - the protocol order (stock report → telemetry → heartbeat);
  - adopting counts once and then using its own;
  - dispensing; at-most-once delivery on a resend;
  - the result surviving a dropped connection, with no ack, and being sent first;
  - the drop-result fault;
  - jam with an error event; partial; an empty slot;
  - crash recovery with `power_loss`;
  - `unknown_command` handling;
  - the offline fault; retried refusals; commands sent straight after `welcome`;
  - the kiosk API: snapshot, verify plus the SSE event order, input validation, offline, fault validation, restock.
- **2 backend tests** for demo keys: issued once and kept; replaced when lost or stale.
- **Locally against the real backend:** pay → code at the kiosk API → dispensed and captured, availability −1. With `drop_next_result`, the machine reconnected and the order was still captured. A `jam` left the order paid.
- **Docker:** all 6 machines online on the map; `pickup_smoke.py` passes; the full backend suite passes in the container; 0 errors in the backend and simulator logs.
- **CI:** the Docker job checks that all six machines come online and runs `hardware_sim/scripts/pickup_smoke.py`.

## ADR-020: The kiosk screen (2026-10-29)

`hardware_sim/kiosk/` is the touchscreen of a CarePoint machine: a React and strict TypeScript single-page app built with Vite. The simulator image builds it in a Node stage and serves it, so `http://localhost:8080` lists the simulated machines and `/kiosk/<serial>` is one machine's screen.

**It talks only to its own machine.**
- **The local API:** the screen calls the simulator's kiosk API (ADR-019): a snapshot, server-sent events, `verify`, faults and restock. On real hardware that API is the controller on the same board.
- **The key stays local:** the browser never sees the machine's key and never talks to the backend directly. A compromised screen therefore can't impersonate the machine, and the backend has no browser-facing machine API to defend.
- **Live updates:** these come over server-sent events, which reconnect on their own: `dispensing` → `unit_dispensed` → `dispensed`, plus online and offline. The screen's state follows the machine's events rather than the HTTP reply to `verify`, so a dispense that the server resends after a reconnect also shows up.

**What the customer sees**
- **The main screen:** a 6-digit keypad, which submits automatically at six digits and also works with a physical keyboard.
- **"Scan QR code instead":** the camera with jsQR, accepting only `CP1:` payloads, or a paste field when there's no camera.
- **The machine's window:** a slot grid with live counts and a pickup tray, where a parcel drops in for each unit released.

Every server refusal has its own message. A failed dispense tells the customer the truth of ADR-018:
- **Nothing came out:** "your order is still valid, try the same code again".
- **Some items came out:** "only k item(s) came out, your order was cancelled and you will not be charged".

After too many wrong codes the keypad locks and counts down `retry_after_seconds`.

**An operator panel for presenters** (⚙ in the corner) switches the simulator's faults on and off live, and can restock the machine. Offline, jam, partial dispense and "lose the connection right after dispensing" can each be shown on stage in seconds.

**Done-when test.** `e2e/kiosk-smoke.mjs` drives the kiosk in Chromium with Playwright against the Compose stack, in CI's Docker job:
1. A customer logs in, reserves and pays through the API.
2. A wrong code typed on the on-screen keypad is refused on screen.
3. The right code shows "Dispensing from A1" and then "Please take your item".
4. Stock goes down by 1 on the kiosk, in the app's availability and in the order (dispensed and captured).
5. A second order is picked up through the QR dialog.

Screenshots of each step are uploaded as a CI artifact. They also serve as pitch material.

- **No test-runner dependency:** the test is a plain Node script using the `playwright` library rather than the test runner, so it runs anywhere with one dependency.
- **A separate CI job for the screen:** a `Kiosk (TypeScript)` job runs the strict type check and the production build.

## ADR-021: Operator API (2026-10-29)

`/api/v1/admin/*` is restricted to the `operator` and `admin` roles (ADR-009). The role is read from the database on every request.

| Endpoint | Purpose |
|---|---|
| `GET /machines`, `GET /machines/{id}` | Machines with online status, last seen, telemetry, units, low-stock and sold-out slots, and active reservations. The detail view adds each slot (physical / held / available) and the latest 20 events |
| `PATCH /machines/{id}` | `active` or `maintenance`. `decommissioned` is **admin only**, because it also cuts off the machine's live connection (ADR-017) |
| `POST /stock-movements`, `GET /slots/{id}/movements` | Record a restock or adjustment; see the slot's ledger with who did what |
| `GET /reservations?status=&machine_id=`, `POST /reservations/{id}/cancel` | All customers' reservations. `active` means held or paid and unexpired; `expired` includes lapsed holds not yet swept. Cancelling follows the customer's own rules: stock is released, the payment voided, and it is refused while a dispense is in progress |
| `GET/POST /products`, `PATCH/DELETE /products/{id}` | The whole catalogue, inactive products included. A product used by any slot can't be deleted (409: deactivate it instead), so sales history stays intact |
| `GET /kpis` | The dashboard tiles: reservations in the last 24 h and 7 days; pickup rate (dispensed ÷ finished reservations); expired holds; average time from hold to pickup; stock-outs and low-stock slots; machines online; 24-hour uptime |

**Restocks reach the machine (protocol revision 4).**
- **Why:** a machine keeps its own count (ADR-017). If the dashboard only wrote `+6` to the database, the machine's next `stock_report` would "correct" it straight back.
- **The command:** a restock or adjustment is therefore written to the ledger with the operator as actor, mirrored to Redis, and sent to the machine as an `adjust_stock` command. The machine applies it once per `command_id` and confirms with a matching stock report.
- **Delivery:** commands are published on a per-machine Redis channel (`cp:machine-commands:<id>`), which the machine's gateway connection subscribes to. It subscribes *before* sending `welcome`, so nothing published in between is missed. Delivery works whichever worker holds the socket, and the same channel will carry other server-to-machine commands.
- **Offline machines:** a stock change is **refused while the machine is offline** (409 `machine_offline`). The response says whether a live connection received the command (`machine_notified`).

**Validation of operator input.** A restock only adds units. An adjustment may go either way but needs a note. Both change at most 100 units at a time. The ledger itself refuses anything below zero or over capacity (409 `stock_conflict`).

**Offline at once.**
- **The change:** a new `machines.disconnected_at` column (migration `0008`) is set when the *newest* connection of a machine closes. "Online" is now: heard from within the window **and** not disconnected since (`services/machine_status.is_online`, also used by discovery).
- **Effect:** stopping the simulator, or a machine closing its connection, shows as offline straight away instead of after 90 seconds. A crash without a close is still caught by the window.
- **Superseded connections:** when a superseded connection closes, nothing is marked and no `disconnected` event is logged. The machine never went away, and the event log is what uptime is computed from.

**KPIs are computed on request.** They are plain SQL over the source tables. At MVP scale (tens of machines, thousands of reservations) this is cheap and always agrees with the rest of the dashboard. The details:
- **Pickup rate:** uses the *effective* status, so a lapsed hold counts as expired.
- **Hold to pickup:** the time from the reservation to its `dispense` ledger row.
- **Uptime:** each machine's connected share of the last 24 h, from its connect and disconnect events and the state at the window start. A connection that ended without a disconnect event (a crash) counts only until the online window after the machine was last heard from. This is a pure function with its own unit tests.

**Evidence:**
- **29 API tests**, against the real database:
  - every view is denied to anonymous (401) and customer (403) callers;
  - a customer can't change stock;
  - online, stale and disconnected machines;
  - stock totals and warnings, holds per slot, recent events;
  - an operator can pause a machine, and only an admin can decommission one;
  - a restock is recorded with its actor, mirrored to Redis, and appears in the history;
  - 7 bad stock changes are refused (over capacity, below zero, an adjustment without a note, a negative restock, zero, a system-only reason, a string number), and offline is refused;
  - reservation filters, including lapsed-but-unswept holds;
  - an operator cancel voids the payment, and a finished reservation is refused;
  - the product lifecycle, and deleting a product a slot uses is refused;
  - KPI deltas and an exact pickup rate;
  - 6 uptime cases.
- **3 live tests** over the real gateway:
  - a restock reaches the connected machine as `adjust_stock`, and the machine's confirming report needs no correction;
  - a disconnect shows offline within seconds and a reconnect shows online again;
  - a superseded connection's close neither marks the machine offline nor logs a disconnect.
- **1 simulator test:** `adjust_stock` is counted once, confirmed, and clamped at zero.

## ADR-022: Live change notices for operators (2026-10-29)

`GET /api/v1/admin/events` is a server-sent-events stream for dashboards. It carries **notices, not data**:

```
data: {"topic": "stock", "machine_id": "…", "change": "restock", "at": "2026-10-29T11:50:02+00:00"}
```

| Topic | Changes |
|---|---|
| `machine` | `connected`, `disconnected`, `telemetry`, `fault` |
| `stock` | `restock`, `adjustment`, `corrected` (a machine's stock report), `dispensed`, `partial`, `late` |
| `reservation` | `created`, `paid`, `dispensing`, `dispensed`, `partial`, `cancelled`, `expired` |

**Why notices only.**
- **One source of truth:** the dashboard re-reads what it shows through the admin API after a notice. Its view can't drift from what the API would say, there's no second set of payload schemas to version, and access control stays in one place.
- **Wide notices are fine:** a notice with `machine_id: null` (the expiry sweeper's, which covers several machines) means "refresh them all".
- **Cost:** a refetch is cheap at dashboard scale.

**Transport.**
- **Publishing:** services publish to one Redis pub/sub channel (`cp:events`) **after they commit**. That covers the machine gateway (connections, telemetry, faults, stock reports), reservations, checkout, dispensing, pickups, the expiry sweeper and operators' stock changes.
- **Same mechanism as machine commands:** these notices travel the same way as machine commands do (ADR-021), so any worker's change reaches every worker's dashboards.
- **Best effort:** a lost notice costs one stale view until the next notice or refresh, never a failed operation (tested with a broken Redis).

**The stream itself.**
- **Authorisation:** `Authorization: Bearer` is required, and the role is checked as for every admin route. Browsers' `EventSource` can't send headers, so dashboards read the stream with `fetch()`; tokens never go in URLs, which end up in logs.
- **No database connection:** authorisation uses a short-lived session that is closed before streaming starts. A dashboard left open for an hour holds no database connection.
- **Ends at token expiry:** the client reconnects with a fresh token, so a demoted or deactivated operator stops receiving notices within one token lifetime.
- **Buffering:** a keep-alive comment is sent every 15 s, and `X-Accel-Buffering: no` plus `Cache-Control: no-transform` stop proxies from buffering the stream.

**Found on the way.** A machine that closed its socket while the gateway was still sending replies made the handler raise `WebSocketDisconnect` and log an ASGI traceback. That is now an ordinary disconnect. The regression test fails three times out of three without the fix. To make it possible, the test server's logs now reach pytest's `caplog`.

**Evidence:**
- **Over a real server:**
  - the stream refuses anonymous (401), customer (403) and forged-token (401) callers;
  - an operator sees the connect, the restock (and the machine receives `adjust_stock`), a reservation created, telemetry, and the disconnect, each tagged with the right machine;
  - the stream closes when its token expires.
- **Publishers:** a reservation's life (created → paid → cancelled, and created → expired) publishes exactly the expected notices in order.
- **Failure:** publishing with Redis down doesn't raise.

## ADR-023: The operator dashboard (2026-10-29)

`dashboard/` is a React and strict TypeScript single-page app built with Vite and served by nginx at **http://localhost:3000**. Nginx also proxies `/api` to the backend, so the browser talks to one origin: no CORS, and the bearer token is never sent anywhere else.

**Views**
- **Overview:**
  - **KPI tiles** (ADR-021): reservations, pickup rate, expired holds, hold to pickup, stock-outs, machines online and uptime.
  - **An OpenStreetMap map** (Leaflet circle markers coloured online, offline or maintenance).
  - **The machine list**, with last seen, units and warnings.
- **Machine:**
  - **Telemetry:** temperature, door, firmware and last seen.
  - **A slot grid** with low-stock and sold-out warnings, and held/free counts.
  - **For a selected slot:** the restock or adjustment form, and its ledger history with who did what.
  - **Recent events,** and maintenance on or off.
- **Reservations:** a live feed across customers with status filters; new rows flash, and operators can cancel.
- **Products:** add, edit name or price, deactivate or reactivate, and delete (refused for products in use).

**Live, without polling.** One notice stream per tab (ADR-022) feeds every view.
- **Refetching:** a view refetches what a notice says changed, debounced by 250 ms, and filtered by machine on the machine page.
- **Missed notices:** it refetches everything when the stream reconnects, because notices may have been missed while it was down.
- **Reading the stream:** it is read with `fetch()`, because `EventSource` can't send the token.
- **Expiry:** when the stream ends at token expiry, the client refreshes the token and reconnects with backoff.
- **Status:** a "Live" indicator shows the stream's state.

**Sessions.**
- **Where tokens live:** the 15-minute access token is kept in memory only. The rotating refresh token (ADR-009) is in `sessionStorage`, so a reload keeps the operator signed in and closing the tab signs them out.
- **Refreshing:** a 401 triggers one refresh, de-duplicated across concurrent requests, so a refresh token is never presented twice and family reuse detection isn't tripped. If it fails, the operator signs in again.
- **Customers:** customer accounts are refused in the UI ("No access"), and the API independently refuses them (401/403 on every admin route, tested in ADR-021).

**Hardening.** Nginx sends:
- **A strict Content-Security-Policy:**
  - `default-src 'self'`;
  - images only from the app itself and OpenStreetMap's tile servers;
  - `connect-src 'self'`;
  - `frame-ancestors 'none'`.
- **Other headers:** `X-Content-Type-Options` and `Referrer-Policy`.
- **Streaming:** `proxy_buffering off` for the event stream.
- **Caching:** hashed assets are cached immutably, and `index.html` never.

**KPI fix found on the way.** Uptime used to divide by a full 24 hours, so a fresh demo showed "uptime 0%" after a few minutes. Each machine is now measured from its first connection, at most 24 hours back, and a machine that has never connected is left out rather than counted as 0%. A test checks 100%, 50% and no data.

**Done-when test.** `e2e/dashboard-smoke.mjs` runs Playwright in Chromium against the Compose stack, in CI's Docker job:
1. A **customer** account gets "No access".
2. The **operator** sees all 6 machines, none offline, and the live indicator on.
3. A **restock** of +2 on CP-BUC-0003 A1 through the form updates the dashboard (2 → 4) **and the machine's own count** (2 → 4, via `adjust_stock`), and appears in the history under the operator's email.
4. A reservation made by a customer through the API **appears in the live feed**.
5. A product is added, deactivated and deleted.
6. **`docker compose stop simulator`** turns every machine on the open overview from online to offline, without a reload (CP-BUC-0006 shows "maintenance", as seeded).
7. Starting the simulator again brings them back online.

The test also fails on any uncaught page error. That check caught a Leaflet teardown bug (the map removed mid-animation), now fixed. Screenshots of each step are uploaded as a CI artifact. A `Dashboard (TypeScript)` CI job runs the strict type check and the production build.

## ADR-024: The mobile app's foundation (2026-10-29)

`mobile/` is the customer app: **Expo SDK 58** (React Native 0.88, React 19) in strict TypeScript, created from the official `blank-typescript` template.

**Expo Go, no native code.** Everything the MVP needs (secure storage, maps, location, QR codes) works inside Expo Go, so the owner can run the app on their own phone by scanning a QR code, with no Xcode, Android Studio or developer accounts. Expo Go supports only the current SDK, so the versions the template chose are pinned by `package-lock.json` and upgraded together with `npx expo install --fix`.

**One API contract, checked twice.** The app never hand-writes an API type.
1. The backend writes its OpenAPI schema to `mobile/openapi.json` (`python -m app.scripts.export_openapi`). A backend test fails if the file is stale (PR #25).
2. `npm run gen:api` turns it into `src/services/api-types.ts` with `openapi-typescript`. The file is committed, and the CI job **Mobile (TypeScript)** regenerates it and fails on any difference. The generator lives in its own small package, `mobile/tools/gen-api/`, with its own lockfile: openapi-typescript 7 requires TypeScript 5 as a peer, while Expo SDK 58 ships TypeScript 6. Keeping it apart avoids `--legacy-peer-deps` for the whole app, and the app is type-checked with the TypeScript version Expo supports.
3. Calls go through `openapi-fetch`, so every path, parameter, body and response is checked by `tsc`. Renaming a field in a Pydantic schema breaks the app's type check in the same PR, not on a phone.

The whole schema is exported, operator routes included. It is already public at `/openapi.json`; the app simply doesn't call the admin routes, and the server enforces roles anyway (ADR-009).

**Tokens** (`src/services/tokens.ts`):
- The 15-minute access token lives **in memory only**.
- The rotating refresh token lives in **`expo-secure-store`** (iOS Keychain, Android Keystore), so the app stays signed in across restarts. Nothing session-related is written to plain storage.
- **Pickup codes are never written to disk** (Phase 10): they stay in memory and are re-issued with `POST /reservations/{id}/pickup-code` if lost.

**Refreshing** (`src/services/api.ts`), modelled on the dashboard (ADR-023):
- A 401 triggers **one refresh shared by all requests** that hit it, then each request is retried once with its body intact. A rotated refresh token is therefore never presented twice, which the server would treat as theft and answer by revoking the whole login (ADR-009).
- The refresh token is deleted **only when the server rejects it** (401 or 422). A network error, a 429 or a 503 keeps it. A phone that starts in a lift or on a train is therefore "unreachable, try again", not signed out.
- When the server does reject it, the auth store hears about it (`onSessionEnded`) and shows the sign-in screen.

**State.** Zustand stores in `src/store/`. The `auth` store has four states: `restoring` (app start), `unreachable`, `signedOut` and `signedIn`.

**Configuration.** The API address comes from `EXPO_PUBLIC_API_URL`, inlined by Expo at bundle time, defaulting to `http://localhost:8000`. A phone needs the computer's LAN address, for example `http://192.168.1.23:8000`; the Android emulator uses `http://10.0.2.2:8000`. The backend needs no CORS for this, because native apps aren't browsers.

**Checks.** The CI job **Mobile (TypeScript)** runs `npm ci`, the generated-types check, `tsc --noEmit` (strict, plus `noUncheckedIndexedAccess`), ESLint (`eslint-config-expo` with `no-explicit-any` as an error) and Jest (`jest-expo`). It is in `automerge.needs`. It uses Node 22, which React Native 0.88 requires; the other jobs stay on Node 20.

**Evidence.** Jest tests with a fake API server installed as `fetch`: the access token is never written to SecureStore; a 401 refreshes once and retries with the same body; three concurrent 401s cause exactly one refresh; a rejected refresh token signs the user out; a network error or 429 keeps the token; sign-out works offline; every FastAPI error shape is read; the auth store restores, reports "unreachable", registers, signs in and out.

## ADR-025: Mobile sign-in, map and machine screens (2026-10-29)

**Navigation.** React Navigation's native stack (`@react-navigation/native-stack`), rather than Expo Router. The app has three screens in Phase 9 and a handful more in Phase 10; an explicit, typed stack (`src/navigation.ts`) is easier to follow and test than file-based routes. The stack depends on the auth store: signed out, only **SignIn** exists; signed in, **Map** and **Machine** exist. Signing out, or the server ending the session (ADR-024), therefore drops the customer back to sign-in without any navigation calls.

**Sign in first.** Browsing machines doesn't need an account (ADR-011), but reserving does, and the MVP story starts with a customer who reserves. One entry point keeps the app and the demo simple. The sign-in screen also registers. It checks the form before calling the server (name, email, and the backend's 12-character password minimum) and otherwise shows the server's own message.

**Where to search from** (`src/services/location.ts`):
- The phone's position if the customer allows location access (`expo-location`, foreground only, last known position first so the map appears quickly).
- Otherwise, or if location services fail, the **Bucharest demo area** (44.44, 26.08), with a banner saying so.
- If the phone is somewhere with no machines within 20 km, for example a presenter outside Bucharest, the empty list offers **"Show the Bucharest demo area"**.
- The search radius is the API's maximum, 20 km, which covers the demo city.

**What a machine looks like** (`src/machineState.ts`). One function decides, in this order, so the most important fact wins:

| State | When | Colour |
|---|---|---|
| Maintenance | `status` isn't `active` | grey |
| Offline | not online (ADR-021) | red |
| Sold out | nothing available | amber |
| Open | otherwise | teal |

The map pins and the list badges use the same colours. The machine screen shows each stocked slot with its product, price (integer cents formatted as `12.99 RON`), what is **available** (physical stock minus active holds, ADR-011), a low-stock flag, and a warning when the machine is offline or under maintenance. Pull to refresh reloads; Phase 10 adds live updates.

**State.** A Zustand `machines` store holds the search centre and its source, the nearby list and each machine's detail as `loading` / `ready` / `error`. A failed reload keeps the last good data on screen with an error banner, rather than blanking the list.

**Found on the way: Expo's fetch can't take a Request object.** In SDK 58 the global `fetch` is Expo's native `expo/fetch`. `openapi-fetch` builds a `Request`, and the 9-B client passed it straight on, which `expo/fetch` refuses ("Unsupported BodyInit type"). The Jest tests didn't catch it because they replaced `fetch` entirely. The client now re-sends each request as a plain URL plus init: headers as a record, and the JSON body read once as text, which also serves the retry after a refresh. The test fake now rejects anything but a URL string, as Expo does; the old client fails 7 tests against it. A one-off run of the real client and stores against the Compose stack, through a Node HTTP adapter with the same contract, signed in, listed the six machines with distances, loaded a machine's slots and signed out.

**Evidence.** 39 Jest tests in total. Beyond ADR-024's: formatting; the machine-state order; locating with permission granted, refused, or failing; the query parameters sent; keeping the last list on a failed reload; machine details and a 404; and screen tests with React Native Testing Library (v14, async): sign-in success, a wrong password shows the server's message, form checks before any request, the map list's states and navigation, the demo-area fallback, a connection error, and the machine screen's prices, counts, low-stock, sold-out and offline warning. Android and iOS bundles build with `npx expo export`.
