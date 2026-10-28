# CarePoint - Agentic Architecture Blueprint

## 🤖 AI Agent Core Directives
You are an autonomous Senior Full-Stack Engineer and Software Architect working on `CarePoint`—a Phygital O2O Smart Micro-Retail Platform. 
Your goal is to build a scalable, production-ready system utilizing FastAPI, PostgreSQL, and React Native.

1. **Think Before Coding:** Always review the current repository state and architecture before creating or modifying files.
2. **Step-by-Step Execution:** Do not attempt to build the entire stack in one commit. Break tasks into logical, testable PRs (e.g., DB Schema -> API Routes -> Mobile UI).
3. **Zero-Trust & Validation:** Assume all hardware telemetry and user inputs are untrusted. Implement strict Pydantic validation on the FastAPI layer.
4. **Self-Correction:** If a test fails, read the logs, analyze the root cause deeply, and implement a structural fix. Do not apply superficial workarounds.

## 🏗️ Technology Stack
*   **Backend:** Python 3.11+, FastAPI, SQLAlchemy (Async), Uvicorn.
*   **Database & Caching:** PostgreSQL (with PostGIS for spatial queries), Redis (for 15-minute reservation locks).
*   **Mobile App:** React Native, Expo, TypeScript, Zustand (State Management), React Native Maps.
*   **IoT Client (Simulated):** Python daemon (WebSocket client) + React SPA.
*   **Infrastructure & QA:** Docker, Docker Compose, Pytest, Playwright (E2E).

## 📂 Monorepo Structure
```text
/
├── backend/               # FastAPI application
│   ├── app/
│   │   ├── api/           # Endpoints (v1)
│   │   ├── core/          # Config, security, DB setup
│   │   ├── models/        # SQLAlchemy models
│   │   ├── schemas/       # Pydantic models
│   │   └── services/      # Business logic (e.g., Redis locking)
│   ├── tests/             # Pytest suite
│   ├── Dockerfile
│   └── requirements.txt
├── mobile/                # React Native Expo app
│   ├── src/
│   │   ├── components/
│   │   ├── screens/
│   │   ├── store/         # Zustand stores
│   │   └── services/      # API clients
│   ├── app.json
│   └── package.json
├── hardware_sim/          # Python daemon simulating machine telemetry + kiosk SPA
├── dashboard/             # Operator dashboard (React + TypeScript, Vite)
├── loadtest/              # Reservation race/load tests (Locust or k6)
├── docs/                  # ADRs, protocol spec, guides, pitch kit
└── docker-compose.yml     # Local dev orchestration
```

## 🛠️ Development Rules
1. **Python / FastAPI:**
   - Enforce PEP8 and strict type hinting.
   - Use asynchronous SQLAlchemy sessions (`AsyncSession`).
   - Keep route handlers thin; push business logic into the `services/` layer.
2. **React Native:**
   - Use strict TypeScript. No `any` types.
   - Prefer functional components and hooks.
3. **Database Setup:**
   - Use Alembic for all database migrations. Never modify the schema without a migration script.
4. **Testing:**
   - Write `pytest` fixtures for database state.
   - Ensure the Redis locking mechanism is heavily tested for race conditions.
5. **Branches, PRs & CI:**
   - One branch per feature, cut from the latest `main`, named `<type>/<short-kebab-name>`: `feature/` (new functionality), `fix/` (bug fixes), `chore/` (tooling, CI, deps), `docs/` (docs only). E.g. `feature/redis-reservation-lock`.
   - Commit and open PRs autonomously; one logical change per PR.
   - CI (`.github/workflows/ci.yml`) auto-merges a PR into `main` once lint, tests and the Docker smoke test pass. Open a PR as a **draft** or label it `do-not-merge` to hold it.
   - Before pushing, run locally from `backend/`: `ruff check .`, `ruff format --check .`, `mypy app alembic/env.py tests`, `pytest`.
6. **Docker in the Claude Code cloud sandbox** (not needed on a normal machine):
   - Start the daemon with `dockerd &`, and stop any host `postgresql`/`redis-server` that hold ports 5432/6379.
   - Docker Hub rate-limits the sandbox (HTTP 429). Pull images from `mirror.gcr.io/<image>` and `docker tag` them to their normal names.
   - Builds can't see the agent proxy's CA. Build a local `python:3.11-slim` that adds `/root/.ccr/ca-bundle.crt` and sets `PIP_CERT`/`SSL_CERT_FILE`. Then run `docker build --network host --build-arg HTTPS_PROXY=$HTTPS_PROXY -t carepoint-backend backend` and `docker compose up --no-build --pull never --wait`. Never commit these workarounds.

## 📝 Memory & Documentation
Whenever you make a significant architectural decision (e.g., how the Redis lock is structured, how the IoT machine authenticates), document it immediately in `docs/architecture-decisions.md`.
## 🗺️ MVP Roadmap

**MVP user story:** *A user opens the app and sees nearby CarePoint machines on a map with live stock. They reserve an item, which holds it for 15 minutes, and pay with a mock payment. At the machine they enter or scan a one-time pickup code, and the machine dispenses the item. If they don't arrive in time, the hold expires, the stock returns to the machine and the payment is voided.*

Rules for working through the roadmap:
- Work through the phases **in order**. Each phase is one or more PRs, and each PR is one logical, CI-green change.
- Tick `[x]` and link the PR in the same PR that finishes an item, so this list always shows real progress.
- Record each phase's key design decisions as an ADR in `docs/architecture-decisions.md`.
- Every API phase ships with tests. Every phase that changes Compose must keep `docker compose up --wait` green in CI.

### Phase 0 — Backend foundation ✅
- [x] Docker Compose stack: PostGIS 16, Redis 7 and the backend, with healthchecks (#1)
- [x] FastAPI app with `/health` and `/ready`, async SQLAlchemy, settings, Redis client (#1)
- [x] Alembic async setup with migration `0001` enabling PostGIS (#1)
- [x] CI: lint, `mypy --strict`, tests against real services, Docker smoke test, auto-merge (#2)

### Phase 1 — Domain model & seed data ✅
- [x] Models and migration (#5):
  - `users`, with a `role` of `customer`, `operator` or `admin`
  - `machines`: serial number, name, address, `geography(Point, 4326)` location, status, `last_seen_at`, Ed25519 public key (ADR-007)
  - `products`: SKU, name, category, price in integer cents, image URL
  - `machine_slots`: machine, slot code, product, physical quantity, capacity, low-stock threshold
  - `stock_movements`: an append-only ledger of every change to a slot's quantity (restock, dispense, adjustment, telemetry correction), recording who or what made the change and why
  - `reservations`
  - `orders`
- [x] Pydantic schemas with strict validation: coordinate bounds, positive quantities, enum statuses (#5).
- [x] Idempotent seed command (`python -m app.scripts.seed`) that creates demo machines with real coordinates in one city, a product catalogue, stock and a demo operator account (#6).
- **Done when:** the migration upgrades and downgrades cleanly, `alembic check` finds nothing, and the seed runs twice without creating duplicates.

### Phase 2 — Authentication
- [x] User register and login with argon2 password hashes, short-lived JWT access tokens plus rotating refresh tokens with reuse detection, and `GET /api/v1/me` (#7).
- [ ] Machine authentication with a per-machine Ed25519 key pair: the server stores only the public key (ADR-007). Machines sign requests or WebSocket handshakes (machine ID, timestamp, nonce, body hash) and get replay protection. Write the signing scheme up as an ADR.
- [x] Role-based access: a `require_role(...)` dependency. `/api/v1/admin/*` is restricted to `operator` and `admin` (#7).
- [x] Rate limiting on auth endpoints, backed by Redis (#7).
- **Done when:** protected routes reject missing, expired or forged tokens and the wrong role, and the tests cover all of them.

### Phase 3 — Discovery & catalogue API
- [ ] `GET /api/v1/machines/nearby?lat=&lng=&radius_m=` uses PostGIS `ST_DWithin` and orders by distance. It returns distance, online status and availability counts.
- [ ] `GET /api/v1/machines/{id}` returns the machine's products with **available** quantity, meaning physical stock minus active holds.
- [ ] `GET /api/v1/products/{sku}/nearby` answers "where can I get this item nearest to me?"
- **Done when:** the tests use seeded data with known distances and check ordering, radius cut-off and the availability arithmetic.

### Phase 4 — Reservation engine (Redis 15-minute lock) ⭐ core
- [ ] `POST /api/v1/reservations` places an atomic hold on N units of a slot. A Redis Lua script checks availability and writes the hold key with a 15-minute TTL in one step. The reservation row in Postgres has status `held` and an `expires_at`.
- [ ] `DELETE /api/v1/reservations/{id}` lets the user cancel and release the hold.
- [ ] An expiry worker, based on keyspace notifications or a sweeper as a safety net, releases expired holds and marks them `expired`. Redis and Postgres are reconciled on startup.
- [ ] The hold length is a setting, `RESERVATION_HOLD_SECONDS`: 900 by default, about 60 in demo mode, so an expiry can be shown live.
- [ ] Per-user limits: a maximum number of active holds and idempotency keys on create.
- [ ] Race-condition tests:
  - N concurrent reservations for the last unit produce exactly 1 success;
  - expiry while a dispense is in flight;
  - Redis restart and reconciliation.
- [ ] Load test (Locust or k6, in `loadtest/`): for example, 500 virtual users race for the last unit and exactly one hold succeeds, with p95 latency reported. The results are saved to `docs/benchmarks.md` for the pitch.
- **Done when:** the race tests pass reliably over many repeated runs, and the lock design is documented as an ADR.

### Phase 5 — Checkout & pickup codes
- [ ] A mock payment provider behind an interface: authorise when the hold is placed, capture when the item is dispensed, void when the hold expires or is cancelled.
- [ ] Pickup credential: a single-use 6-digit code plus a QR payload. It is bound to one machine and one reservation and is valid only while the hold is active. Only its hash is stored.
- [ ] Order history: `GET /api/v1/orders`.
- **Done when:** every state transition is tested (`held` → `paid` → `dispensed` | `expired` | `cancelled`) and illegal transitions are rejected.

### Phase 6 — Machine gateway (IoT)
- [ ] WebSocket `/api/v1/ws/machines`, authenticated with the Phase 2 handshake. Every incoming message is validated against strict Pydantic models and anything malformed or out of range is rejected.
- [ ] Telemetry: heartbeat (online/offline from `last_seen_at`), stock counts per slot, door and temperature, and error events.
- [ ] Commands: `verify_code` → `dispense(slot, qty)` → the machine sends `dispense_result`, which marks the reservation dispensed and captures payment. Commands are idempotent, carry IDs and are retried on reconnect.
- [ ] Stock received from a machine is reconciled against the database and any mismatch is logged as a `stock_movements` correction.
- [ ] `docs/machine-protocol.md`: a versioned spec of the handshake, every message type, its JSON schema and the error codes, so a real machine (Raspberry Pi or ESP32) can replace the simulator without changes to the backend.
- **Done when:** integration tests with a fake machine client cover the whole loop, including disconnecting halfway through a dispense.

### Phase 7 — Hardware simulator
- [ ] `hardware_sim/`: a Python daemon (WebSocket client) that simulates one or more machines from config. It sends heartbeats and telemetry and responds to dispense commands. Faults can be injected: offline, jam, slow dispense.
- [ ] A kiosk single-page app in React: a touchscreen keypad and QR entry, slot display, and dispense animation and result.
- [ ] Both are added to `docker-compose.yml`, so `docker compose up` starts a working demo machine.
- **Done when:** the CI smoke test drives the kiosk with Playwright: enter a code, see the item dispensed, and see stock go down.

### Phase 8 — Operator dashboard
- [ ] Admin API (`/api/v1/admin/*`, Phase 2 roles):
  - list machines with status and last-seen time;
  - stock per slot;
  - list and cancel reservations;
  - record a restock or adjustment (written to `stock_movements`);
  - create, update and delete products;
  - KPI summary.
- [ ] `dashboard/`: a React and TypeScript web app (Vite), served by Compose:
  - a map and list of machines with online/offline status and last-seen time;
  - a slot grid with low-stock warnings;
  - a live reservations feed;
  - a restock form and history;
  - product management.
- [ ] KPI tiles for the pitch:
  - reservations today and over 7 days;
  - pickup rate, meaning dispensed out of held;
  - expired holds;
  - average time from hold to pickup;
  - stock-outs;
  - machine uptime.
- [ ] Live updates over the same push channel the machine gateway uses.
- **Done when:** Playwright checks that an operator can log in, see a machine go offline when the simulator stops, record a restock and see the stock update, and that a customer account can't access the dashboard.

### Phase 9 — Mobile app foundation
- [ ] `mobile/`: Expo with strict TypeScript, Zustand stores, an API client typed from the OpenAPI schema, and secure token storage.
- [ ] Screens: login and register, a map of nearby machines (React Native Maps) and a machine details page with stock.
- [ ] CI: `tsc --noEmit`, ESLint and Jest.

### Phase 10 — Mobile reserve → pay → pickup flow
- [ ] Reserve from the machine page, mock checkout, a reservation screen with a live 15-minute countdown and the pickup QR code and PIN, cancel, and order history.
- [ ] Live stock and reservation status pushed to the app over WebSocket or server-sent events, so a hold or expiry shows up straight away.
- [ ] Handling for an expired hold, a machine going offline and the app losing connection.

### Phase 11 — MVP hardening, handover & pitch kit
- [ ] An end-to-end test of the whole story in CI: API, simulator and kiosk (Playwright), driven through the same API calls the app makes.
- [ ] Structured JSON logs with request IDs, a basic metrics endpoint and a security pass with the `/security-review` skill.
- [ ] One-command demo: `docker compose up` plus seed data. The README covers quick start and architecture.
- [ ] **`docs/manual-testing-guide.md`**: a step-by-step guide to running the stack and trying every feature by hand. It covers the API through Swagger, the operator dashboard, the kiosk simulator, the mobile app in Expo Go, failure scenarios such as expiry and an offline machine, and how the pieces fit together.
- [ ] **Pitch kit** (`docs/pitch/`):
  - Demo scenario script (`python -m app.scripts.demo`). It runs simulated customers in the background, so the map, dashboard and KPIs look busy during a presentation.
  - A recorded demo video of the full flow (Playwright video), as a backup if the live demo fails.
  - An architecture diagram, which the README also uses.
  - `one-pager.md` covering the problem, the solution, how it works, the technology, the load-test results, and the roadmap after the MVP.
  - `hardware-prototype.md`: a parts list and cost estimate for one physical prototype machine (controller, dispensing mechanism, lock, enclosure), mapped onto `docs/machine-protocol.md`.

### Out of scope for the MVP
Real payment provider, refunds UI, analytics beyond the dashboard KPI tiles, managing multiple operators or companies, push notifications, production cloud deployment, and building the physical machine (the MVP specifies it and estimates its cost; building it is what the funding is for).
