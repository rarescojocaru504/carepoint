# How to read, understand and test the CarePoint code

A guide for the owner, or anyone new, who wants to really understand the codebase rather than just run it. It covers:
1. a reading order;
2. the shape the code follows everywhere;
3. a hands-on exercise that follows one request through the whole system;
4. where to find the *why*;
5. how to use the tests as documentation;
6. how to test the app, from one function to the whole story;
7. a glossary.

You'll need the stack running (`docker compose up --build --wait`, [WORKFLOW.md §2](WORKFLOW.md)) and, for the app, Expo Go on your phone.

---

## 1. Reading order (about two hours)

| # | Read | Why | Time |
|---|---|---|---|
| 1 | [`../README.md`](../README.md) and [`../docs/pitch/one-pager.md`](../docs/pitch/one-pager.md) | what the product is and does | 10 min |
| 2 | [`APP_WALKTHROUGH.md`](APP_WALKTHROUGH.md) | the customer's journey, step by step, with the file for each step | 20 min |
| 3 | [`CODE.md`](CODE.md) §1 (big picture) and §2.6 (main flows) | the map: which part talks to which | 15 min |
| 4 | **Do the exercise in §3 below** | one real request, end to end | 30 min |
| 5 | The key ADRs in §4 | why it's built this way | 30 min |
| 6 | The tests listed in §5 for whatever you care about most | exactly what the code promises | 15 min |

Keep [`CODE.md`](CODE.md) open as a map while you read code: it lists every folder, table, endpoint, service and script.

## 2. The shape the code follows everywhere

Once you know the pattern, each part of the code reads the same way.

### Backend (`backend/app/`)

```
request → api/v1/<area>.py  →  services/<area>.py  →  models/ (database)  +  Redis
            "the doorway"        "the brain"            "the memory"
```

- **`api/v1/*.py`, the doorway.** Each endpoint only checks the input (via a Pydantic schema), calls **one** service function, and turns the result or error into a response. If you see business rules in a handler, that's a bug in the pattern.
- **`schemas/*.py`, the shape of input and output.** These are strict: unknown fields are rejected, numbers are bounded, enums are closed. This is where "never trust input" lives.
- **`services/*.py`, the brain.** All the rules: who may do what, in what order, with which locks. Read these to understand behaviour.
- **`models/*.py`, the database tables** as Python classes; `alembic/versions/` holds every change to their structure.
- **`core/`, the plumbing:** settings, database and Redis connections, security (JWT, password hashing), logging, metrics.

**A good first service to read:** `services/reservations.py`, `create_reservation`. It's short and shows every pattern: a lock per customer, the Redis gate (`services/hold_store.py`), the database row, the announcement (`services/events.py`).

### App (`mobile/src/`)

```
screen  →  store (Zustand)  →  services/api.ts  →  the backend
  UI         state + actions     typed HTTP calls
```

- **`screens/`** show things and call store actions. They hold no business logic.
- **`store/`** hold the app's state (`auth`, `machines`, `reservations`, `live`) and the actions that change it.
- **`services/`:**
  - `api.ts`: every server call, typed from `openapi.json`, so a field that doesn't exist won't compile;
  - `live.ts` and `liveSync.ts`: the live connection, and what each notice makes the app re-read;
  - `tokens.ts`, `clock.ts`, `location.ts`.
- **`api-types.ts` is generated, never edited.** It comes from `npm run gen:api`.

### Dashboard (`dashboard/src/`) and kiosk (`hardware_sim/kiosk/src/`)

Small React apps. The dashboard's `api.ts` and `live.tsx` mirror the app's client and live connection. The kiosk talks only to its own machine (`hardware_sim/carepoint_sim/kiosk_api.py`), never to the backend.

### Simulator (`hardware_sim/carepoint_sim/`)

- `machine.py` is one machine: connect, heartbeat, report stock, dispense.
- `state.py` is what it saves so it survives a crash.
- `signing.py` signs its connection.

These are the pieces a real Raspberry Pi controller would reuse.

## 3. Exercise: follow one reservation through the whole system

Do this with the stack running, Swagger open (http://localhost:8000/docs) and a terminal.

1. **Watch the logs** in a terminal:

   ```bash
   docker compose logs -f backend --no-log-prefix
   ```

2. **Sign in** in Swagger (`POST /api/v1/auth/login` as the customer), then **Authorize** with the token.
3. **Reserve:** `POST /api/v1/reservations` with a `slot_id` from `GET /api/v1/machines/{id}`.
   - In the **response headers**, copy `x-request-id`.
   - In the terminal, find that id. Every line the server wrote for your request carries it, ending with the access line: `"route": "/api/v1/reservations", "status": 201, "duration_ms": …`.
4. **Now read the code that just ran**, in order:
   1. `backend/app/api/v1/reservations.py`, the `POST` handler: input checked by `schemas/reservation.py`, `ReservationCreate`;
   2. `backend/app/services/reservations.py`, `create_reservation`: the per-customer lock, the limit, the idempotency key;
   3. `backend/app/services/hold_store.py`: the Lua script that atomically decides whether there's a unit for you;
   4. back in `create_reservation`: the database row, then `events.publish_reservation`;
   5. `backend/app/services/events.py`: one notice for operators, one for you only.
5. **Watch it land:** open the dashboard's **Reservations** page; your row is there. With the app open, your phone shows it under **My orders**. That came through `api/v1/events.py` → the app's `services/live.ts` → `services/liveSync.ts`.
6. **Pay and pick up:** `POST /api/v1/reservations/{id}/checkout`, then type `pickup.code` on the kiosk. Follow `services/checkout.py`, then `services/pickups.py` (`request_pickup`, then `record_dispense_result`), then `services/dispensing.py`, then `services/payment_settlement.py`.

After this you've seen every layer and both live channels.

## 4. Where the "why" lives: the key ADRs

[`../docs/architecture-decisions.md`](../docs/architecture-decisions.md) has 32 ADRs. Each says what was decided, why, and what evidence backs it. Start with these:

| ADR | Decision | Read it to understand |
|---|---|---|
| 012 | Holds live in Redis sorted sets, checked and written by one Lua script | why nothing can oversell, even with 500 people at once |
| 013 | Expiry sweeper and Redis ↔ Postgres repair | why a Redis restart or a dead job doesn't break holds |
| 015 | Authorise at checkout, capture at dispense, void on expiry | why a customer is charged only for an item in hand |
| 016 | Pickup codes stored as keyed hashes, bound to one machine | why a database leak doesn't reveal codes |
| 007, 010 | Machines have their own Ed25519 keys and sign their connection | why a fake machine can't connect |
| 018 | Dispense commands are numbered, executed at most once, results kept until acknowledged | why a dropped connection never double-dispenses or double-charges |
| 022, 026 | Live updates are notices, not data; customers only hear about their own reservations | how the dashboard and app stay live and private |
| 024, 027, 028 | The app's typed client, codes in memory, the live connection | how the app is put together |
| 031 | The security pass and the network fix | what was checked, and what the demo exposes |

## 5. The tests are the most precise documentation

Each test says exactly what the code promises, and the suite runs on every change. When you want to know "what happens if…", look for the test.

| Behaviour | Read |
|---|---|
| Reserving, limits, idempotency, **the race tests** (40 customers for the last unit, 20 rounds) | `backend/tests/test_reservations.py` |
| The Redis gate itself | `backend/tests/test_hold_store.py` |
| Paying, declines, every state transition | `backend/tests/test_checkout.py` |
| Pickup codes: format, binding to a machine, lockout | `backend/tests/test_pickup_codes.py` |
| A full pickup through a fake machine, including disconnects mid-dispense | `backend/tests/test_machine_pickup.py` |
| Machine sign-in, replay protection | `backend/tests/test_machine_auth.py` |
| Expiry and Redis repair | `backend/tests/test_expiry_and_reconciliation.py` |
| Customers see only their own reservations | `backend/tests/test_customer_events.py` |
| Request ids, logs, metrics | `backend/tests/test_observability.py` |
| The app's client: token refresh, one refresh at a time | `mobile/src/services/__tests__/api.test.ts` |
| The app's live connection: reconnect, backoff, silent streams | `mobile/src/services/__tests__/live.test.ts` |
| The app's screens: pay, code, cancel, expiry | `mobile/src/screens/__tests__/reservation.test.tsx` |
| The simulated machine's protocol rules | `hardware_sim/tests/test_machine.py` |
| The whole story, end to end | `e2e/story.mjs` |

**Tip:** to see what a test exercises, run just that test verbosely and watch the logs:

```bash
cd backend && python -m pytest -v "tests/test_checkout.py::test_concurrent_checkout_and_cancel_end_consistent"
```

## 6. How to test the app

From smallest to biggest. [`WORKFLOW.md`](WORKFLOW.md) §3–4 has the setup for each.

| Level | What | Command | Time |
|---|---|---|---|
| One function | a single test | `cd backend && python -m pytest tests/test_checkout.py -k declined` | seconds |
| One part | backend (needs `docker compose up -d db redis`) | `cd backend && python -m pytest` | ~3 min |
| | simulator | `cd hardware_sim && python -m pytest` | seconds |
| | app (and the generated types are current) | `cd mobile && npm run gen:api && npm run typecheck && npm run lint && npm test` | ~1 min |
| | dashboard / kiosk build | `cd dashboard && npm run build`; `cd hardware_sim/kiosk && npm run build` | ~1 min |
| In a browser | kiosk and dashboard against the running stack | `cd e2e && node kiosk-smoke.mjs && node dashboard-smoke.mjs` | ~2 min |
| **The whole story** | reserve → pay → kiosk → captured, and expiry → voided | `RESERVATION_HOLD_SECONDS=40 docker compose up -d --wait backend`, then `cd e2e && node story.mjs` | ~2 min |
| By hand | every feature, step by step, with what you should see | [`../docs/manual-testing-guide.md`](../docs/manual-testing-guide.md) | as long as you like |
| On your phone | the real app in Expo Go | the guide's section 6 | 10 min |
| Under load | 500 customers racing for one unit | [`../loadtest/README.md`](../loadtest/README.md) | ~10 min |

CI runs everything from "one part" to "the whole story" on every pull request, and merges only when all of it passes.

## 7. Glossary

| Term | Meaning |
|---|---|
| **Hold** | a reserved unit, kept for 15 minutes (`RESERVATION_HOLD_SECONDS`) |
| **Available** | physical units in a slot minus active holds |
| **Authorise / capture / void** | reserve money on the card / take it / release it |
| **Pickup code, QR payload** | the 6 digits, and `CP1:<reservation id>:<code>` in the QR |
| **Slot** | one spiral in a machine (A1…B4), holding one product |
| **Ledger** (`stock_movements`) | the append-only history of every stock change: restock, dispense, adjustment, correction |
| **Notice** | a small live message saying *what* changed, never the data itself |
| **Sweeper** | the background job that marks lapsed holds expired, every 15 s |
| **Reconciliation** | the job that repairs Redis from PostgreSQL |
| **Planogram** | which product is in which slot, sent to a machine when it connects |
| **ADR** | Architecture Decision Record: a short note on one design decision and its reasons |
| **Request id** | the `X-Request-ID` that ties a request to all its log lines |
