# Project summary: Phases 0–11

**Status on 2026-10-30: the MVP is complete.** All 12 phases (0–11) are merged into `main` through 37 pull requests, and CI is green. The backend, the machine gateway, six simulated machines with their kiosks, the operator dashboard and the customer app work together, started with one command (`docker compose up`) plus the app in Expo Go. What remains is beyond the MVP: building a physical machine and a pilot (see the end of this file).

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

## Phase 9 — Mobile app foundation (#25, #26, #27)

**What it gives you:** a customer app on your phone that signs in, shows nearby machines on a map, and shows a machine's live stock.
- **The contract:**
  - the backend writes its OpenAPI schema to `mobile/openapi.json` (`python -m app.scripts.export_openapi`), and a backend test fails if it's stale;
  - the app generates its API types from it, and a CI check fails if those are stale;
  - so a backend change that would break the app breaks CI instead.
- **The app** (`mobile/`): Expo SDK 58, strict TypeScript, Zustand stores, runs in **Expo Go**.
  - **Tokens:** the access token is kept in memory and the refresh token in the phone's secure keystore.
  - **Refresh:** a 401 triggers one shared refresh.
  - **Offline:** being offline shows "can't reach CarePoint", not "signed out".
- **Screens:**
  - sign in and register;
  - a map and list of nearby machines, coloured open / sold out / offline / maintenance, falling back to the Bucharest demo area when location is refused;
  - a machine's products, prices and availability.
- **CI:** a new "Mobile (TypeScript)" job (`tsc`, ESLint, Jest, generated types current), required for auto-merge.

**Key decisions:** ADR-024 (the typed contract, tokens, the client) and ADR-025 (navigation, location, machine states).

**Found on the way:**
- Expo's `fetch` refuses the `Request` objects that the API library builds, so on a phone every API call would have failed. The unit tests replaced `fetch` and so missed it; a run against the real stack caught it. The client now sends plain URLs, and the tests reject `Request` objects the way Expo does.

## Phase 10 — Reserve, pay and pick up in the app (#28, #29, #30)

**What it gives you:** the whole MVP story on the phone, live.
- **A customer live channel** (`GET /api/v1/events`):
  - customers see stock and machine status for any machine, but notices about **their own reservations only**, delivered on a per-user Redis channel;
  - at most 3 open streams per user;
  - maintenance changes are now announced too.
- **Reserve, pay, pick up:**
  - **Reserve** on any slot, with an idempotency key per tap;
  - a reservation screen with a countdown on the **server's** clock;
  - **Pay** with the mock card, plus a demo "declined card" link;
  - then the **QR code and the 6-digit code**;
  - cancel; outcome screens for picked up, cancelled and expired ("Reserve again");
  - **My orders**, showing whether each payment was charged.
- **Pickup codes never touch storage:** they're in memory only, dropped when a reservation ends and at sign-out, and re-issued on request after a restart.
- **Live and resilient:**
  - the app streams notices with Expo's `fetch`;
  - reconnects with backoff, refreshes the token when the stream ends, and replaces a silent stream;
  - re-reads what's on screen after reconnecting;
  - pauses in the background;
  - shows a "Reconnecting…" banner;
  - lists running holds on the map;
  - warns when the machine holding your item goes offline.

**Key decisions:** ADR-026 (the customer channel and its privacy rule), ADR-027 (the flow, codes in memory, server clock) and ADR-028 (the live connection and failure handling).

**Found on the way:**
- When a client disconnected from an event stream, the server's cleanup never ran, because anyio re-cancels every await in the cleanup. It is now shielded.
- Piping the stream through `TextDecoderStream` failed when two stream implementations met. The app now decodes the bytes itself.

## Phase 11 — MVP hardening, handover & pitch kit (#31–#37)

**What it gives you:** a system you can operate, trust and present.
- **Observability (#31, ADR-029):**
  - JSON logs with a **request id** on every line (also returned as `X-Request-ID`);
  - one access line per request;
  - `GET /metrics` in Prometheus format, covering HTTP counts and latency by route template plus machines, reservations and units available. Operators and a `METRICS_TOKEN` scraper can read it.
- **One-command demo (#32):**
  - `docker compose up --build --wait`;
  - a demo mode with short holds (`RESERVATION_HOLD_SECONDS=60`);
  - `.env.example`;
  - a real README with a quick start and an architecture diagram.
- **The whole story as one CI test (#33, ADR-030):** `e2e/story.mjs`, run on every PR.
  - The customer's side uses the app's API calls and live stream; the machine's side is the real kiosk in Playwright.
  - It covers reserve → pay → pickup → captured, and reserve → pay → expiry → voided.
  - It also records the demo video.
- **Manual testing guide (#34):** [`docs/manual-testing-guide.md`](../docs/manual-testing-guide.md).
- **Security pass (#35, ADR-031):** the whole codebase was reviewed in four areas (auth; money and stock; the machine side; channels, clients and infrastructure).
  - **The one real finding:** the demo stack exposed PostgreSQL and Redis to the local network and used public development secrets. Someone on the pitch's Wi-Fi could have recovered pickup codes and taken other people's items.
  - **The fix:** only the API is published to the network, everything else listens on `127.0.0.1`, and each installation generates its own secrets.
- **Pitch kit (#36, #37):**
  - `python -m app.scripts.demo` runs simulated customers through the real API, with real pickups at the kiosks, cancels, expiries and operator restocks;
  - in `docs/pitch/`: a one-pager, a 65 s demo video with screenshots, a hardware prototype parts list and cost estimate (about €612–1,092), and a live-demo runbook.

**Key decisions:** ADR-029 to ADR-032.

---

## The finished MVP

**The MVP user story works end to end.** A customer finds nearby machines with live stock, reserves (a 15-minute atomic hold), pays (authorised), types or scans a one-time code at the machine, and the machine dispenses (payment captured). If they don't come, the hold expires, the unit goes back on sale, and the payment is voided. Every step updates the app and the dashboard live.

**How it's verified on every change:**
- 530 backend tests against real PostgreSQL and Redis, including the race tests;
- the simulator's tests;
- 86 app tests;
- strict TypeScript builds of the kiosk, dashboard and app;
- a Docker smoke test that starts the whole stack;
- Playwright tests of the kiosk and dashboard;
- the whole-story test with expiry.

A PR merges only when all of them pass.

**Where to go next:**
- [`APP_WALKTHROUGH.md`](APP_WALKTHROUGH.md): the app explained for presenting it;
- [`READING_THE_CODE.md`](READING_THE_CODE.md): how to read, understand and test the code;
- [`../docs/manual-testing-guide.md`](../docs/manual-testing-guide.md): trying everything by hand;
- [`../docs/pitch/`](../docs/pitch): the pitch kit.

**After the MVP** (out of scope so far; see [`../docs/pitch/one-pager.md`](../docs/pitch/one-pager.md)):
1. **Build one physical prototype machine** ([`../docs/pitch/hardware-prototype.md`](../docs/pitch/hardware-prototype.md)). The protocol is ready, so the backend needs no changes.
2. **A campus pilot** with 2–3 machines.
3. **Then:**
   - a real payment provider behind the existing `PaymentProvider` interface;
   - push notifications;
   - several operators;
   - richer analytics;
   - a production cloud deployment, with its own secrets, TLS, and managed PostgreSQL and Redis.
