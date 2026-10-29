# CODE.md: how the CarePoint code is organised

This is a map of the codebase: what each part does, where it lives and how the parts talk to each other. It's written so that a developer, or Claude Code, can extend the system safely, and so that the owner can understand the finished app. For the *why* behind each choice, follow the ADR links into [`docs/architecture-decisions.md`](../docs/architecture-decisions.md).

---

## 1. The big picture

```
                         ┌──────────────────────── Docker Compose ─────────────────────────┐
                         │                                                                  │
 Customer phone          │   ┌───────────────┐   SQL (asyncpg)   ┌────────────────────┐    │
 (mobile/, Phase 9-10) ──┼──▶│               │──────────────────▶│ PostgreSQL+PostGIS │    │
   REST + live events    │   │   backend     │                   │  durable records   │    │
                         │   │   FastAPI     │   Lua scripts,    └────────────────────┘    │
 Operator browser        │   │   :8000       │   pub/sub, limits ┌────────────────────┐    │
 (dashboard/, :3000) ────┼──▶│               │──────────────────▶│      Redis 7       │    │
   REST + SSE via nginx  │   │  + background │                   │ holds, rate limits,│    │
                         │   │    jobs       │                   │ nonces, pub/sub    │    │
                         │   └──────▲────────┘                   └────────────────────┘    │
                         │          │ WebSocket /api/v1/ws/machines (Ed25519-signed)        │
                         │   ┌──────┴────────┐                                              │
 Person at a machine ────┼──▶│  simulator    │  6 simulated machines + kiosk screens :8080  │
   kiosk (keypad / QR)   │   │ hardware_sim/ │  (a real Raspberry Pi/ESP32 would go here)   │
                         │   └───────────────┘                                              │
                         └──────────────────────────────────────────────────────────────────┘
```

| Service | Folder | Port | Built with |
|---|---|---|---|
| `db` | `infra/postgres/init/` creates the `carepoint_test` database | 5432 | `postgis/postgis:16-3.4` |
| `redis` | – | 6379 | `redis:7-alpine` with AOF persistence |
| `backend` | `backend/` | 8000 | Python 3.11, FastAPI, SQLAlchemy 2 (async), Alembic, redis-py |
| `simulator` | `hardware_sim/` | 8080 | Python 3.11 (aiohttp) and a React kiosk built in a Node stage |
| `dashboard` | `dashboard/` | 3000 | React, TypeScript and Vite, served by nginx, which proxies `/api` to the backend |
| *(on your phone)* | `mobile/` | Expo dev server | Expo SDK 58 / React Native, TypeScript, Zustand |

**Who trusts whom:**
- **Customers and operators** use a **JWT** (`Authorization: Bearer …`).
- **Machines** sign every handshake with their own **Ed25519 private key**; the backend stores only the public key.
- **The kiosk screen** talks only to its own machine controller (the simulator's local HTTP API) and never to the backend.
- **All input is untrusted:** every body, query and machine message is validated by strict Pydantic models.

**Where the truth lives:**
- **PostgreSQL** is the durable source of truth.
- **Redis** is the fast **atomic gate** for holds, plus the rate limits, nonces and pub/sub. If Redis loses its data, it's rebuilt from Postgres (ADR-012, ADR-013).
- **For stock in a slot, the machine's own count wins:** stock reports become ledger corrections.

---

## 2. Backend (`backend/`)

### 2.1 Layout

```
backend/
├── app/
│   ├── main.py            FastAPI app, lifespan (starts/stops background jobs), routers
│   ├── api/
│   │   ├── deps.py        shared dependencies: DbSession, RedisClient, CurrentUser,
│   │   │                  require_role(...), rate_limit_by_ip(...)
│   │   ├── health.py      GET /health, GET /ready
│   │   ├── sse.py         shared server-sent-event streaming (operator and customer streams)
│   │   └── v1/            one module per area (thin handlers; logic is in services/)
│   ├── core/              config.py (Settings), db.py (engine, sessions), redis.py, security.py (JWT, argon2),
│   │                      logging.py (JSON logs), request_context.py (request ids, access log,
│   │                      HTTP metrics), metrics.py (Prometheus metric definitions)
│   ├── models/            SQLAlchemy 2 typed models (Mapped[...]); enums.py
│   ├── schemas/           Pydantic request/response models (strict, InputModel forbids extra fields)
│   ├── services/          all business logic (see 2.4)
│   └── scripts/           command-line entry points (python -m app.scripts.<name>)
├── alembic/versions/      migrations 0001 … 0008 (never change the schema without one)
├── tests/                 pytest suite (459 tests), see 2.7
├── Dockerfile, requirements*.txt, pyproject.toml (ruff, mypy --strict), pytest.ini
```

**The rule:** route handlers in `api/v1/` only parse input, call one service function and shape the response. Everything else belongs in `services/`.

### 2.2 Settings (`app/core/config.py`)

Settings are read from environment variables; the names are the field names in upper case.

| Setting | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://carepoint:carepoint@localhost:5432/carepoint` | must use asyncpg |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `JWT_SECRET`, `PICKUP_CODE_PEPPER` | dev defaults | **production refuses to start with the defaults** |
| `ACCESS_TOKEN_TTL_SECONDS` | 900 | |
| `REFRESH_TOKEN_TTL_SECONDS` | 30 days | |
| `RESERVATION_HOLD_SECONDS` | 900 | set about 60 to demo an expiry live |
| `MAX_ACTIVE_RESERVATIONS_PER_USER` | 3 | |
| `CUSTOMER_EVENT_STREAMS_PER_USER` | 3 | open `GET /api/v1/events` streams per user (ADR-026) |
| `MACHINE_ONLINE_WINDOW_SECONDS` | 90 | how long since the last message before a machine counts as offline |
| `MACHINE_HEARTBEAT_INTERVAL_SECONDS` | 30 | sent to machines in `welcome` |
| `DISPENSE_TIMEOUT_SECONDS` | 120 | how far an accepted pickup extends the hold |
| `EXPIRY_SWEEP_INTERVAL_SECONDS` | 15 | |
| `RECONCILE_INTERVAL_SECONDS` | 300 | |
| `BACKGROUND_WORKERS_ENABLED` | true | tests set it false and drive the jobs directly |
| `RATE_LIMIT_*` | various | `"<count>/<seconds>"` |
| `CURRENCY` | `RON` | |
| `LOG_FORMAT` | `json` | `json` (one object per line, with `request_id`) or `text` (ADR-029) |
| `LOG_LEVEL` | `INFO` | |
| `METRICS_TOKEN` | unset | bearer token for a scraper reading `GET /metrics`; operators can always read it |

### 2.3 Data model (`app/models/`, migrations in `alembic/versions/`)

| Table | Holds | Notes |
|---|---|---|
| `users` | email, argon2 hash, full name, `role` (`customer` / `operator` / `admin`), active flag | |
| `refresh_tokens` | hashed refresh tokens with a `family_id` | rotated on every use; reuse revokes the family (ADR-009) |
| `machines` | serial, name, address, `location geography(Point,4326)`, `status` (`active` / `maintenance` / `decommissioned`), `last_seen_at`, `disconnected_at`, Ed25519 public key, firmware version, door, temperature | |
| `machine_slots` | machine, slot code (`A1`…), product, `quantity`, capacity, low-stock threshold | `quantity` is physical units |
| `products` | SKU, name, description, category, `price_cents`, currency, image URL, active flag | |
| `stock_movements` | **append-only ledger**: slot, delta, `quantity_after`, reason (`initial` / `restock` / `dispense` / `adjustment` / `telemetry_correction`), actor user or reservation, note | database triggers forbid UPDATE, DELETE and TRUNCATE; a CHECK forbids stock below zero |
| `reservations` | user, slot, quantity, unit and total price, `status`, `expires_at`, hashed pickup code | statuses: `held → paid → dispensed`, or `cancelled` / `expired` |
| `orders` | one per paid reservation: amount and `payment_status` (`authorized` / `captured` / `voided` / `failed`), provider reference | |
| `dispense_commands` | one per accepted pickup: command id, slot, quantity, status (`open` / `succeeded` / `failed` / `abandoned`) | a partial unique index allows at most one open command per reservation |
| `machine_events` | append-only machine log: connected, disconnected, door opened or closed, errors | |

**Two rules for the schema:**
- **Enums are `VARCHAR` plus a CHECK constraint** (`db_enum` in `models/enums.py`), not native Postgres enums.
- **Every schema change needs a migration**, and `alembic check` must report nothing.

### 2.4 Services (`app/services/`): where the logic is

| Area | Module | What it does |
|---|---|---|
| **Auth** | `auth.py` | register, login, refresh rotation with reuse detection, logout |
| | `rate_limit.py` | fixed-window counters in Redis |
| | `machine_auth.py` | verifies the CAREPOINT-ED25519-V1 signature, the clock-skew window and the single-use nonce (ADR-010) |
| | `machine_provisioning.py`, `demo_keys.py` | register machine public keys; issue demo keys for the simulator |
| **Discovery** | `discovery.py`, `geo.py` | the nearby-machines, machine-detail and product-nearby queries (PostGIS `ST_DWithin`, ordered by distance) |
| | `availability.py` | available = physical units − active holds (ADR-011) |
| | `machine_status.py` | `is_online()`: seen within the window **and** not disconnected since |
| **Reservations ⭐** | `hold_store.py` | **the Redis Lua scripts**: reserve, release, rebuild and stock delta. Per slot: `cp:slot:{id}:qty`, `cp:slot:{id}:holds` (a sorted set of reservation id → expiry in ms) and `cp:slot:{id}:holdqty` (ADR-012) |
| | `reservations.py` | create (Redis hold first, then the Postgres row), list, get, cancel; per-user advisory lock, the active-hold limit, idempotency keys |
| | `reservation_expiry.py` | the sweeper: lapsed `held`/`paid` reservations become `expired` (`MATERIALIZED` CTE plus `FOR UPDATE SKIP LOCKED`) |
| | `hold_reconciliation.py` | applies only the difference between Redis and Postgres (ADR-013) |
| | `background.py` | the loops for sweep + settle and reconcile, started from the lifespan in `main.py` |
| **Money & pickup** | `payments.py` | `PaymentProvider` interface and `MockPaymentProvider` (`pm_card_ok`, `pm_card_declined`, `pm_card_provider_error`) |
| | `checkout.py` | held → paid: authorise, create the order and issue the pickup code, all under a row lock |
| | `payment_settlement.py` | makes each payment match its reservation: capture if dispensed, void if expired or cancelled; retried until they agree |
| | `pickup_codes.py` | issue, verify and rotate the 6-digit code and `CP1:<reservation>:<code>` QR; only a peppered HMAC is stored |
| | `orders.py` | order history |
| **Machines** | `machine_gateway.py` | one live connection per machine (`open_connection` / `close_connection`), message dispatch (`handle_text`), commands to the machine via Redis pub/sub `cp:machine-commands:<id>` |
| | `machine_telemetry.py` | heartbeat, telemetry, errors and connection events; stock reports become ledger corrections |
| | `pickups.py` | `request_pickup` (verify_code → dispense command, hold extended), `record_dispense_result`, resends after a reconnect, partial and late results (ADR-018) |
| | `dispensing.py` | `complete_dispense`: the ledger `dispense` movement, status `dispensed`, Redis hold and quantity updated together |
| | `inventory.py` | ledger writes: `record_stock_movement` |
| **Operators** | `admin.py` | operator queries and actions: machines, slots, stock changes (also sent as `adjust_stock`), reservations, products |
| | `kpis.py` | KPI summary, computed from the tables on each request |
| | `ops_metrics.py` | the business gauges for `GET /metrics` (machines by state, reservations by status, units available) |
| | `events.py` | `publish()` of change notices to `cp:events` (ADR-022); `publish_reservation()` also tells the owner on `cp:user-events:<user id>`; `customer_view()` filters what customers see; the per-user stream counter (ADR-026) |
| **Demo** | `seeding.py`, `demo_data.py` | the idempotent demo dataset: 6 Bucharest machines, 12 products, 3 accounts |

### 2.5 HTTP API (all under `/api/v1` except health)

The live interactive docs are at **http://localhost:8000/docs** (Swagger) and the schema at `/openapi.json`.

| Area | Method and path | Who |
|---|---|---|
| Health | `GET /health`, `GET /ready` | anyone |
| Metrics | `GET /metrics` (Prometheus text; not in the OpenAPI schema) | `METRICS_TOKEN`, or operator, admin |
| Auth | `POST /auth/register`, `/auth/login`, `/auth/refresh`, `/auth/logout` | anyone (rate-limited) |
| Me | `GET /me` | logged in |
| Discovery | `GET /machines/nearby?lat&lng&radius_m&limit`, `GET /machines/{id}`, `GET /products`, `GET /products/{sku}/nearby` | anyone (rate-limited per IP) |
| Reservations | `POST /reservations`, `GET /reservations`, `GET /reservations/{id}`, `DELETE /reservations/{id}` | customer (own reservations only; someone else's returns 404) |
| Checkout | `POST /reservations/{id}/checkout` `{payment_method}` → `{reservation, order, pickup}` | customer |
| Pickup code | `POST /reservations/{id}/pickup-code` (re-issue) | customer |
| Orders | `GET /orders`, `GET /orders/{id}` | customer |
| Operator | `GET /admin/machines`, `GET`/`PATCH /admin/machines/{id}`, `GET /admin/slots/{id}/movements`, `POST /admin/stock-movements`, `GET /admin/reservations`, `POST /admin/reservations/{id}/cancel`, `GET`/`POST /admin/products`, `PATCH`/`DELETE /admin/products/{id}`, `GET /admin/kpis` | operator, admin |
| Operator live | `GET /admin/events` (server-sent events, every notice) | operator, admin |
| Customer live | `GET /events` (server-sent events: availability and machine status anywhere, own reservations only; at most 3 open per user) | any signed-in user |
| Users | `GET /admin/users` | admin only |
| Machine HTTP | `POST /machine/heartbeat` (signed) | machine |
| Machine WS | `WS /ws/machines` (subprotocol `carepoint.v1`, signed handshake) | machine |

**Errors** use FastAPI's `{"detail": ...}`. Business errors carry a stable `code`, for example `insufficient_stock`, `too_many_active`, `not_cancellable`, `payment_declined`, `pickup_in_progress`.

### 2.6 Main flows, step by step

**A. Reserve** (`reservations.create_reservation`)
1. A Postgres advisory lock per user, so the limit and idempotency checks can't race.
2. The Redis Lua script removes expired holds, computes `available = qty − Σholds` and either adds the hold atomically or refuses. If Redis was empty, the slot is rebuilt from Postgres and the script retried.
3. The `reservations` row is inserted (`held`, `expires_at`). If that fails, the Redis hold is released.
4. A `reservation/created` notice is published after commit.

**B. Pay** (`checkout.checkout`)
1. Lock the reservation row and check it is `held` and unexpired.
2. The provider authorises the amount (idempotency key `checkout:<id>`). A decline or outage is returned **before** anything is written.
3. The order (`authorized`) is created, the status becomes `paid`, and a pickup code is issued (stored as an HMAC).
4. The response includes `pickup.code` and `pickup.qr_payload` **only this once**.

**C. Pick up at the machine** (`machine_ws` → `machine_gateway` → `pickups` → `dispensing`)
1. Kiosk → simulator → backend: `verify_code {code | qr}`.
2. `pickup_codes.verify` finds the paid reservation at **this** machine; too many wrong codes lock the machine.
3. `pickups.request_pickup` extends the hold to at least now + 120 s (Redis first), creates a `dispense_commands` row, and replies `dispense {command_id, slot, quantity}`.
4. The machine dispenses and sends `dispense_result`. Then `record_dispense_result` → `complete_dispense` writes the ledger movement, sets `dispensed`, updates Redis and captures the payment.
5. **What can go wrong:**
   - partial dispense: cancelled and voided;
   - a result that arrives after expiry: stock recorded, nobody charged;
   - lost connection: the command is resent on reconnect, and the machine executes it at most once.

**D. Expire** (`background` → `reservation_expiry` → `payment_settlement`)
- Every 15 s, lapsed `held`/`paid` reservations become `expired`, their holds are tidied, and their payments are voided.
- Availability never waited for this: an expired hold stopped counting at its `expires_at`.

**E. Operator restock** (`admin` → `inventory` → `machine_gateway`)
- `POST /admin/stock-movements` writes the ledger and the slot quantity, and updates the Redis quantity mirror.
- It then publishes `adjust_stock` on `cp:machine-commands:<id>`; the worker holding that machine's WebSocket forwards it, and the machine confirms.
- The change is refused while the machine is offline.

### 2.7 Tests (`backend/tests/`)

**Fixtures** (`conftest.py`, `world.py`, `fake_machine.py`):

| Fixture | Gives you |
|---|---|
| `db_session` | a session inside an outer transaction (savepoints), rolled back after the test, so code under test may `commit()` freely |
| `redis` | a real Redis on **logical DB 15**, flushed before and after |
| `api` | an `httpx.AsyncClient` against the app, using `db_session` and `redis` |
| `world` | **committed** rows for concurrency tests (one connection per request, as in production), cleaned up afterwards. Its machines are in the Southern Ocean so they never show up in other tests' geo queries |
| `gateway_url` | a **real uvicorn server** on a free port, for WebSocket and SSE tests |
| `make_machine` | a `FakeMachine` that signs, connects and speaks the protocol |
| `provider` | a `MockPaymentProvider` |

**Other things to know:**
- **Integration tests use real Postgres and Redis.** Without them, the tests skip; in CI, `REQUIRE_SERVICES=1` makes them fail instead.
- **Race tests** fire real concurrent requests and assert exact counts. Several features were **mutation-tested**: the code was broken on purpose to check the tests catch it (the ADRs list which).
- **Redis pub/sub ignores database numbers.** In CI, pytest runs next to the live simulator, so live-notice tests filter by their own `machine_id`.

### 2.8 Scripts (`python -m app.scripts.<name>`)

| Script | What it does |
|---|---|
| `seed` | idempotent demo data; prints the demo accounts |
| `demo_init --keys-dir DIR` | seed plus issue demo machine keys. Compose runs it when `DEMO_INIT=1` |
| `provision_machine` | register a real machine's public key |
| `export_protocol_schema` | regenerate `docs/machine-protocol.schema.json`; the drift test `test_published_json_schema_matches_the_code` fails until you do |
| `export_openapi` | regenerate `mobile/openapi.json`, the source of the mobile app's API types; the drift test `test_published_openapi_matches_the_code` fails until you do |
| `loadtest_fixtures` | data for `loadtest/` |

---

## 3. Machine protocol (`docs/machine-protocol.md`, revision 4)

- **Handshake:** a GET to `/api/v1/ws/machines` with the `X-CarePoint-*` signature headers (machine serial, timestamp, nonce, signature over method, path and empty-body hash).
  - Refused handshakes get a real HTTP 400, 401 or 503.
  - Close codes: 1003, 1008, 1009, 4000 (superseded by a newer connection), 4003 and 4008.
- **Server → machine:**
  - `welcome`: machine id, heartbeat interval and the **planogram**, i.e. the slots with their products and quantities;
  - `dispense`;
  - `adjust_stock`;
  - replies (`ack`, `error`, `verify_result`, …).
- **Machine → server:** `heartbeat`, `telemetry` (door, temperature), `error_event`, `stock_report`, `verify_code`, `dispense_result`. Each carries an `id`, and each gets exactly one reply.
- **§6, the dispensing rules:**
  - the machine executes each `command_id` **at most once**;
  - it stores its results until they're acknowledged;
  - after a reconnect it sends stored results **before** its stock report;
  - a dispense interrupted by power loss is reported, never repeated.

---

## 4. Hardware simulator (`hardware_sim/`)

```
carepoint_sim/
├── __main__.py     loads config (config/demo.json), runs every machine + the kiosk API
├── config.py       SimConfig / MachineConfig / Faults (Pydantic)
├── signing.py      the client side of CAREPOINT-ED25519-V1 (no backend imports, on purpose)
├── state.py        durable JSON state per machine (counts, outbox, executed results, in-flight)
├── machine.py      SimulatedMachine: connect → welcome → lifecycle (heartbeat/telemetry/stock)
│                   + reader (dispense / adjust_stock); reconnect with backoff
└── kiosk_api.py    the local controller API for the kiosk screen (:8080)
kiosk/              React + TS touchscreen (Keypad, QrScanner, SlotGrid, OperatorPanel)
scripts/pickup_smoke.py   pay in the API, enter the code at a simulated machine (CI)
tests/              against a FakeGateway (tests/fake_gateway.py), 25 tests
```

**Kiosk API** (`kiosk_api.py`):
- `GET /api/machines` and `GET /api/machines/{serial}`: snapshots;
- `GET /api/machines/{serial}/events`: server-sent events (`dispensing`, `unit_dispensed`, `dispensed`, online and offline);
- `POST /api/machines/{serial}/verify`: submit a code;
- `PUT /api/machines/{serial}/faults`: switch faults on and off;
- `POST /api/machines/{serial}/restock`.

The screen for one machine is at `http://localhost:8080/kiosk/<serial>`.

**Faults** (`Faults`): `offline`, `jam` (next or always), `partial_next`, `seconds_per_unit` (`CP-BUC-0005` dispenses slowly, at 4 s per unit), `drop_next_result`.

**Python 3.11 lesson (#22):** don't use `asyncio.TaskGroup` for the connection session, and use `asyncio.timeout` rather than `asyncio.wait_for`. On 3.11 either one can hide a cancellation. See the comments in `machine.py`.

---

## 5. Operator dashboard (`dashboard/`)

```
src/
├── api.ts          typed client for /api/v1/admin/* + session (access token in memory,
│                   refresh token in sessionStorage, one refresh at a time) + subscribeNotices()
├── live.tsx        React context: the live-notice stream (fetch-based SSE, reconnect/backoff)
├── App.tsx         login, role check ("No access" for customers), sidebar, routes
├── views/          Overview (KPIs + map + machine list), MachineView (slots, restock, history,
│                   events), Reservations (live feed, filter, cancel), Products (CRUD)
├── components/     MachineMap (Leaflet), KpiTiles
└── format.ts       money/time formatting
nginx.conf          serves the SPA, proxies /api to backend:8000 with buffering off (SSE)
```

**How it stays live:** a view subscribes to notices for its topic and machine, then re-fetches through the REST API. Notices never carry data themselves, only "something changed".

---

## 6. Mobile app (`mobile/`)

Expo SDK 58, strict TypeScript, runs in Expo Go (ADR-024).

```
mobile/
├── App.tsx                 root: restores the session, then a native stack whose screens
│                           depend on the auth state (SignIn | Map → Machine → Reservation,
│                           Orders)
├── app.json, package.json, tsconfig.json (strict), eslint.config.js, jest.setup.tsx
├── openapi.json            exported by the backend (drift-tested), source of the API types
├── tools/gen-api/          the openapi-typescript generator, pinned apart (needs TypeScript 5)
└── src/
    ├── config.ts           API_URL from EXPO_PUBLIC_API_URL (default http://localhost:8000)
    ├── navigation.ts       RootStackParamList: the screens and their parameters
    ├── format.ts           prices (integer cents → "12.99 RON") and distances
    ├── machineState.ts     maintenance > offline > sold out > open, with labels and colours
    ├── messages.ts         API error codes → sentences for the customer
    ├── services/
    │   ├── api-types.ts    GENERATED by `npm run gen:api`; never edit by hand (CI checks it)
    │   ├── api.ts          openapi-fetch client: `api.GET("/api/v1/...")`, unwrap(), ApiError,
    │   │                   errorMessage(), one shared refresh on 401, signIn/register/signOut.
    │   │                   Requests go to fetch as (url, init), never as a Request object:
    │   │                   Expo's fetch refuses those (ADR-025)
    │   ├── clock.ts        the server's clock (from each response's Date header) for countdowns
    │   ├── live.ts         the live connection: GET /api/v1/events read with fetch, reconnect
    │   │                   with backoff, token refresh, silent-stream watchdog (ADR-028)
    │   ├── liveSync.ts     what each notice makes the stores re-read (debounced)
    │   ├── sse.ts          server-sent-events parser
    │   ├── location.ts     phone position or the Bucharest demo centre
    │   └── tokens.ts       access token in memory, refresh token in expo-secure-store
    ├── store/              Zustand: auth (restoring / unreachable / signedOut / signedIn),
    │                       machines (centre, nearby list, details; loading / ready / error),
    │                       reservations (reserve, pay, cancel, my reservations and orders;
    │                       pickup codes in memory only, ADR-027),
    │                       live (connection status: idle / connecting / live / reconnecting)
    ├── screens/            SignInScreen (sign in + register), MapScreen (map + list),
    │                       MachineScreen (slots, prices, availability, Reserve),
    │                       ReservationScreen (countdown, pay, QR + PIN, cancel, outcome),
    │                       OrdersScreen (active reservations + order history)
    ├── components/         ui.tsx (Button, StateBadge, Banner, colours), Countdown, PickupCode,
    │                       ActiveReservations (running holds on the map), LiveUpdates
    │                       (foreground/background lifecycle + "Reconnecting…" banner)
    └── test-utils/         fakeServer (a fake API as global fetch) and fixtures
```

**Calling the API:** `const machine = await unwrap(api.GET("/api/v1/machines/{machine_id}", { params: { path: { machine_id } } }))`. Paths, parameters and the result type all come from `openapi.json`; `unwrap` throws an `ApiError` with FastAPI's `detail` (and `.code` for business errors).

**For pickup (Phase 10):**
- the QR payload is exactly `qr_payload` from checkout, `CP1:<reservation id>:<code>`;
- the kiosk's scanner accepts only `CP1:` payloads;
- pickup codes are kept in memory only, never written to disk.

## 7. Other folders

| Folder | Contents |
|---|---|
| `e2e/` | Playwright **library** scripts against the live Compose stack: `kiosk-smoke.mjs`, `dashboard-smoke.mjs`, and `story.mjs`, the whole MVP story through the app's API calls plus the kiosk, with expiry when holds are short, optionally recorded as a captioned video (ADR-030). Output goes to `e2e/artifacts/` |
| `loadtest/` | `race.py` (precise last-unit race) and `locustfile.py`, with `compose.loadtest.yml` |
| `docs/` | ADRs, machine protocol and schema, benchmarks. Phase 11 adds `manual-testing-guide.md` and `pitch/` |
| `infra/postgres/init/` | creates the `carepoint_test` database on first start |
| `.github/workflows/ci.yml` | CI jobs, see `WORKFLOW.md` |
