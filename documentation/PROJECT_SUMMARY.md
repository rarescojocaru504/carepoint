# Project summary: Phases 0–8 (done) and Phases 9–11 (next)

**Status on 2026-10-29:** Phases 0–8 are merged into `main`, CI is green, and 23 PRs have been merged. The backend, machine gateway, hardware simulator with kiosk, and operator dashboard all work together with `docker compose up`. What's left is the customer mobile app (Phases 9 and 10) and the MVP hardening and pitch kit (Phase 11).

Numbers like (#11) are pull requests in `rarescojocaru504/carepoint`. ADR numbers refer to [`docs/architecture-decisions.md`](../docs/architecture-decisions.md).

---

## Phase 0 — Backend foundation (#1, #2)

**What it gives you:** a running, tested skeleton.
- A Docker Compose stack with **PostGIS 16**, **Redis 7** and the **FastAPI** backend, each with a healthcheck.
- `GET /health` (liveness) and `GET /ready` (readiness, which checks the database and Redis).
- Async SQLAlchemy, typed settings (pydantic-settings) and a shared Redis client.
- An Alembic async setup; migration `0001` enables PostGIS.
- **CI** (`.github/workflows/ci.yml`): ruff, `mypy --strict`, pytest against real Postgres and Redis, and a Docker smoke test. When everything passes, CI **auto-merges the PR**.

**Key decisions:** ADR-001 to ADR-005. The readiness probe is separate from liveness. Integration tests run against real services rather than mocks. CI merges a PR only when every job is green.

## Phase 1 — Domain model & seed data (#5, #6)

**What it gives you:** the core tables and a realistic demo dataset.
- **Tables:**
  - `users`, with the roles customer, operator and admin;
  - `machines`, with a PostGIS `geography(Point, 4326)` location, a status, `last_seen_at` and an Ed25519 public key;
  - `products`, with prices in integer cents (RON);
  - `machine_slots`, with quantity, capacity and a low-stock threshold;
  - `stock_movements`, an **append-only ledger**: a database trigger refuses updates and deletes, and refuses stock below zero;
  - `reservations` and `orders`.
- **Pydantic schemas** with strict validation: coordinate bounds, positive quantities and enum statuses.
- **The seed command** `python -m app.scripts.seed` is idempotent. It creates:
  - 6 machines around Bucharest campuses (`CP-BUC-0001` to `0006`, with `0006` in maintenance);
  - 12 products;
  - starting stock written through the ledger;
  - 3 demo accounts: `admin@`, `operator@` and `customer@carepoint.demo`, with the passwords `carepoint-<role>-demo`.

**Key decisions:** ADR-006 to ADR-008. Enums are stored as `VARCHAR` with a `CHECK` constraint rather than native Postgres enums, so they're easier to migrate. The demo catalogue deliberately has no medicines, because of EU rules on unattended sales.

## Phase 2 — Authentication (#7, #8)

**What it gives you:** secure access for people and for machines.
- **People:**
  - register and login with **argon2** password hashes;
  - **JWT access tokens** that last 15 minutes;
  - **rotating refresh tokens with reuse detection**: reusing an old refresh token revokes the whole family;
  - `GET /api/v1/me`.
- **Roles:** a `require_role(...)` dependency. `/api/v1/admin/*` is for operators and admins only, and the role is re-read from the database on every request.
- **Rate limiting** backed by Redis, on login (per IP and per email), registration and refresh.
- **Machines:** each has its own **Ed25519 key pair**, and the server stores only the public key. Requests and WebSocket handshakes are signed with **CAREPOINT-ED25519-V1**: machine id, timestamp, nonce and body hash. A nonce store in Redis provides **replay protection**.

**Key decisions:** ADR-007 explains why keys instead of shared secrets, ADR-009 covers user authentication, roles and rate limits, and ADR-010 specifies the signing scheme, including a known-answer test vector.

## Phase 3 — Discovery & catalogue API (#9)

**What it gives you:** "what's near me and what's in stock?"
- `GET /api/v1/machines/nearby?lat=&lng=&radius_m=` uses PostGIS `ST_DWithin` and orders the results by distance. Each machine comes with its distance, online status and availability counts.
- `GET /api/v1/machines/{id}` returns each slot with its product and **available** quantity, meaning physical stock minus active holds.
- `GET /api/v1/products` returns the catalogue, and `GET /api/v1/products/{sku}/nearby` answers "where is the nearest machine with this item?".
- These endpoints are public (no login) and rate-limited per IP.

**Key decision:** ADR-011, which defines "available".

## Phase 4 — Reservation engine ⭐ (#11, #12, #13)

**What it gives you:** the core promise. Nobody can buy a unit someone else is holding, and a hold never outlives its 15 minutes.
- **Endpoints:** `POST /api/v1/reservations` places an atomic hold on N units of a slot. `DELETE /api/v1/reservations/{id}` cancels it, and `GET /api/v1/reservations[/{id}]` lists or reads them.
- **The lock** is a **Redis Lua script**: it checks availability and records the hold in one atomic step. Each slot has a sorted set of holds scored by expiry time, so **expiry is part of the arithmetic**: an expired hold stops counting even if every background job is down. Postgres keeps the durable record.
- **Background jobs:**
  - the **expiry sweeper** (every 15 s) marks lapsed holds `expired`;
  - **reconciliation** (at startup and every 5 minutes) repairs any drift between Redis and Postgres, for example after a Redis restart.
- **Limits:** at most 3 active holds per user, idempotency keys on create, and rate limits.
- **Evidence:**
  - the race tests ran 40 concurrent customers for the last unit over 20 rounds, with exactly one winner each round;
  - the load test (`loadtest/`) ran **500 simultaneous customers for the last unit: 10/10 rounds correct, 0 errors, p95 0.87 s** (`docs/benchmarks.md`);
  - mutation testing: a deliberately broken check-then-act lock failed 33 tests.

**Key decisions:** ADR-012 (holds in Redis, and the write order that keeps every failure safe), ADR-013 (sweeper, reconciliation and dispensing) and ADR-014 (load testing).

## Phase 5 — Checkout & pickup codes (#14, #15)

**What it gives you:** payment, and a way to prove at the machine that you paid.
- **Mock payments** behind a `PaymentProvider` interface (`authorize`, `capture`, `void`). Test tokens: `pm_card_ok`, `pm_card_declined` (402) and `pm_card_provider_error` (503).
  - **Checkout** (`POST /reservations/{id}/checkout`) **authorises** the payment: held → paid.
  - **Dispense** **captures** it.
  - **Expiry or cancel** **voids** it.
  - A **settlement job** retries until the payment matches the reservation.
- **Pickup credential:** a single-use **6-digit code** plus a **QR payload** `CP1:<reservation id>:<code>`, returned once at checkout.
  - Only an HMAC-SHA256 of the code is stored, keyed with a server pepper and bound to the machine.
  - The code is valid only while the reservation is paid and unexpired.
  - `POST /reservations/{id}/pickup-code` re-issues it.
  - Wrong guesses lock the machine for a while.
- **Order history:** `GET /api/v1/orders[/{id}]`.
- **Reservation states:**

```
held ──checkout──▶ paid ──dispense──▶ dispensed
  ├─cancel─▶ cancelled ◀─cancel─┤
  └─expiry─▶ expired  ◀─expiry─┘
```

**Key decisions:** ADR-015 (checkout, settlement, "payment follows the reservation") and ADR-016 (pickup codes and brute-force protection).

## Phase 6 — Machine gateway (IoT) (#16, #17)

**What it gives you:** real-time, authenticated control of physical machines.
- **The connection:** WebSocket `/api/v1/ws/machines` with subprotocol `carepoint.v1`, authenticated by the signed Phase 2 handshake. Each machine has **one live connection**; a newer one replaces the old.
- **Validation:** every message is checked against **strict Pydantic models**, and malformed or out-of-range input is rejected. Further limits: 16 KiB per message, 30 messages per 10 s, 10 rejected messages before disconnect, and a 90 s idle timeout.
- **Telemetry:**
  - heartbeats keep the machine "online";
  - door state and temperature;
  - error events;
  - per-slot **stock reports**. The machine's count is the truth, and any mismatch is written to the ledger as a `telemetry_correction`.
- **Pickup:** the machine sends `verify_code`. The backend then:
  1. checks the code;
  2. extends the hold so it can't expire mid-dispense;
  3. creates a `dispense_commands` row and sends `dispense(slot, qty)`;
  4. on `dispense_result`, marks the reservation dispensed and captures payment.
- **Edge cases:** commands are **idempotent**, carry IDs and are **resent after a reconnect**. A partial dispense cancels and voids the reservation; a late result records the stock but charges nobody.
- **The spec:** `docs/machine-protocol.md` (revision 4) and a generated JSON Schema, kept in sync by a test, so a real Raspberry Pi or ESP32 can replace the simulator.

**Key decisions:** ADR-017 (connection, limits and telemetry) and ADR-018 (pickup and the dispensing rules in protocol §6).

## Phase 7 — Hardware simulator & kiosk (#18, #19, #22)

**What it gives you:** six working demo machines without any hardware.
- **The daemon:** `hardware_sim/` is a Python (aiohttp) daemon that simulates machines from `config/demo.json`.
  - It speaks the real protocol with real keys.
  - It keeps durable state (stock counts, unsent results, in-flight dispenses), so it survives restarts like real hardware.
  - Faults you can inject: offline, jam, partial dispense, slow dispense, and losing the connection right after dispensing.
- **The kiosk:** `hardware_sim/kiosk/` is a React touchscreen app for one machine.
  - A 6-digit keypad and QR entry (camera or paste).
  - A slot grid with live counts, and a dispense animation.
  - Clear messages for every refusal.
  - An operator panel (⚙) for switching faults on and off live during a presentation.
- **The URLs:** `docker compose up` starts the six machines; the kiosks are at **http://localhost:8080**.
- **The CI test:** Playwright drives the kiosk: wrong code refused, right code dispensed, stock goes down, and QR pickup works.
- **#22** fixed a Python 3.11 bug where the simulator couldn't be stopped cleanly.

**Key decisions:** ADR-019 (the simulator's at-most-once dispensing, outbox and crash recovery) and ADR-020 (the kiosk talks only to its own machine and never sees the key).

## Phase 8 — Operator dashboard (#20, #21, #23)

**What it gives you:** an operations console for the business side of the pitch.
- **The operator API** (`/api/v1/admin/*`): machines, slots and stock; restocks and adjustments, written to the ledger **and** sent to the machine as `adjust_stock`; reservations, with filtering and cancel; product create, update and delete; and a **KPI summary**.
- **Live notices:** Redis pub/sub `cp:events`, streamed to operators as server-sent events at `GET /api/v1/admin/events`. The dashboard re-reads whatever changed.
- **The dashboard:** `dashboard/` is a React app (Vite) at **http://localhost:3000**. It has:
  - a map and list of machines with live status;
  - a slot grid with low-stock and sold-out warnings;
  - a restock form and history;
  - a live reservations feed;
  - product management;
  - KPI tiles: reservations, pickup rate, expired holds, average time from hold to pickup, stock-outs, machines online and uptime.
- **The CI test:** Playwright checks that a customer is refused, that a restock reaches the dashboard and the machine, that new reservations appear live, product CRUD, and that machines go offline when the simulator stops and come back when it restarts.

**Key decisions:** ADR-021 (operator API, and stock changes refused while a machine is offline), ADR-022 (live notices) and ADR-023 (the dashboard, and where tokens are stored).

---

## Phase 9 — Mobile app foundation (next)

**Goal:** a customer app in `mobile/` that signs in, shows nearby machines on a map, and shows a machine's live stock. It's a solid base for Phase 10.

**Scope, from `CLAUDE.md`:**
1. **Project:** `mobile/` with **Expo**, **strict TypeScript** (no `any`), **Zustand** stores, an **API client typed from the OpenAPI schema**, and **secure token storage** (`expo-secure-store`).
2. **Screens:** login and register, a **map of nearby machines** (React Native Maps) and a **machine details page** with stock.
3. **CI:** `tsc --noEmit`, ESLint and Jest.

**Suggested PRs** (each one CI-green, with the roadmap items ticked in the same PR):
- **9-A `chore/openapi-export`:** add `backend/app/scripts/export_openapi.py`, which prints `app.openapi()` as stable JSON. Commit its output as `mobile/openapi.json`, and add a backend drift test like the existing protocol-schema test (`test_published_json_schema_matches_the_code` in `tests/test_machine_gateway.py`). From then on, the API and the app's types can't drift apart silently.
- **9-B `feature/mobile-foundation`:**
  - the Expo app with React Navigation (or Expo Router);
  - a Zustand `auth` store;
  - token storage in SecureStore: the access token in memory, the refresh token in SecureStore;
  - an API client in `src/services/` with types generated by `openapi-typescript` from `mobile/openapi.json`. It refreshes the token once on a 401, like `dashboard/src/api.ts`;
  - a new CI job, **Mobile (TypeScript)**, running `npm ci`, a check that the generated types are current, `tsc --noEmit`, ESLint and Jest; add it to `automerge.needs`;
  - an ADR-024.
- **9-C `feature/mobile-discovery`:**
  - a login/register screen;
  - a map screen using `GET /machines/nearby`, with markers for online, offline, maintenance and sold out, and location permission falling back to the Bucharest demo centre `44.44, 26.08`;
  - a machine screen using `GET /machines/{id}`, with slots, products, prices and availability;
  - Jest tests for the stores and the API client, with `fetch` mocked.

**Watch out for:**
- **Phone URL:** a phone running Expo Go can't reach `localhost`. Use `EXPO_PUBLIC_API_URL=http://<your PC's LAN IP>:8000`; the Android emulator uses `10.0.2.2`. The PC's firewall must allow port 8000.
- **Expo version:** Expo Go supports only the **current** Expo SDK. Create the project with the current `create-expo-app` template and pin the versions it gives you.
- **No CORS:** the backend has no CORS middleware. Native apps don't need it. Only add it, restricted to development origins, if you also want Expo **web**.

## Phase 10 — Mobile reserve → pay → pickup flow (next)

**Goal:** the whole MVP user story in the app.

**Scope, from `CLAUDE.md`:**
1. Reserve from the machine page, mock checkout, a reservation screen with a **live 15-minute countdown** and the **pickup QR code and PIN**, cancel, and order history.
2. **Live stock and reservation status pushed to the app** over WebSocket or server-sent events, so a hold or an expiry shows up straight away.
3. Handling for an **expired hold**, a **machine going offline** and the **app losing connection**.

**APIs that already exist:**

| Step | Call |
|---|---|
| Reserve | `POST /api/v1/reservations` with `{slot_id, quantity, idempotency_key}`; returns 201, or 200 when an idempotent retry returns the existing reservation |
| Pay | `POST /api/v1/reservations/{id}/checkout` with `{payment_method: "pm_card_ok"}`; the response includes `pickup: {code, qr_payload, expires_at}`, **shown only once** |
| Show the code again | `POST /api/v1/reservations/{id}/pickup-code`; issues a new code and the old one stops working |
| Cancel | `DELETE /api/v1/reservations/{id}` |
| Status | `GET /api/v1/reservations/{id}`; a lapsed hold already reads as `expired` |
| History | `GET /api/v1/orders` and `GET /api/v1/orders/{id}` |

**Suggested PRs:**
- **10-A `feature/customer-events`, backend:** a **customer** live channel; today's stream is for operators only.
  - **Privacy:** a customer may see stock notices for any machine, but reservation notices only for **their own** reservations. Today's `cp:events` reservation notices don't carry a user or reservation id, so add a per-user channel (for example `cp:user-events:<user_id>`) published after commit, or extend the notices carefully.
  - **Transport:** reuse the SSE pattern in `app/api/v1/admin_events.py`, as a `GET /api/v1/events` that requires login and ends when the token expires.
  - **Client:** React Native's `fetch` can't read a streaming body, so the app needs an SSE client that can send an `Authorization` header (for example `react-native-sse`), or use a WebSocket.
  - Write an ADR-025 and tests modelled on `tests/test_admin_events.py`.
- **10-B `feature/mobile-reserve-pay`:**
  - reserve, pay and the reservation screen;
  - the countdown from `expires_at`, using the server's time as reference;
  - the QR code (`react-native-qrcode-svg`) and the 6-digit PIN;
  - cancel, and order history;
  - **never save the pickup code to disk**; keep it in memory and re-issue it if it's lost.
- **10-C `feature/mobile-live-resilience`:**
  - live updates for the stock on the machine screen and for the reservation status;
  - an expired screen with a "reserve again" action;
  - an offline-machine warning, which disables Reserve;
  - a reconnect with backoff and a "reconnecting…" banner;
  - refetching state when the app returns to the foreground.

## Phase 11 — MVP hardening, handover & pitch kit (after Phase 10)

- **End-to-end test:** one CI test for the whole story through the same API calls the app makes: API, then simulator, then kiosk with Playwright.
- **Observability and security:** structured JSON logs with request IDs, a basic metrics endpoint, and a security pass with the `/security-review` skill.
- **One-command demo and docs:** `docker compose up` plus seed data, and a README with a quick start and architecture.
- **`docs/manual-testing-guide.md`:** how to try every feature by hand: Swagger, the dashboard, the kiosk, the app in Expo Go, and failure scenarios.
- **Pitch kit in `docs/pitch/`:**
  - `python -m app.scripts.demo` (simulated customers that make the dashboard look busy);
  - a Playwright demo video;
  - an architecture diagram;
  - `one-pager.md`;
  - `hardware-prototype.md` (parts list and cost estimate).
