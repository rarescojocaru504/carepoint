# CarePoint

**Urgent personal-care items, reserved on your phone and picked up from a smart vending machine nearby.**

A student needs pads, plasters, condoms or a phone cable *now*. CarePoint shows the nearest machines on a map with live stock. You reserve the item, which holds it for 15 minutes, and pay. At the machine you type or scan a one-time code and the item comes out. If you don't make it in time, the hold expires, the item goes back on sale and you're not charged.

This repository is the complete MVP:
- the backend;
- the customer app;
- the operator dashboard;
- a simulator for six machines with their touchscreens.

One command runs all of it.

![CarePoint architecture](docs/pitch/architecture.svg)

---

## Quick start

You need **Docker Desktop** (or Docker Engine with Compose).

```bash
docker compose up --build --wait
```

The first run takes a few minutes. It:
1. builds the images;
2. creates the database;
3. seeds six demo machines around Bucharest campuses, a product catalogue and three accounts;
4. starts the machines, which come online within seconds.

| Open | What you get |
|---|---|
| http://localhost:3000 | **Operator dashboard**: map of machines, live stock, reservations, KPIs |
| http://localhost:8080 | **Machine screens** (kiosks): pick a machine, then type or scan a pickup code |
| http://localhost:8000/docs | **API** (Swagger): try every endpoint |

| Demo account | Email | Password |
|---|---|---|
| Customer | `customer@carepoint.demo` | `carepoint-customer-demo` |
| Operator | `operator@carepoint.demo` | `carepoint-operator-demo` |
| Admin | `admin@carepoint.demo` | `carepoint-admin-demo` |

**The customer app** runs on your phone through **Expo Go**. Start it with your computer's Wi-Fi address (find it with `ipconfig` on Windows):

```bash
cd mobile && npm ci && EXPO_PUBLIC_API_URL=http://<your-LAN-IP>:8000 npx expo start
```

Scan the QR code with Expo Go and sign in as the demo customer. [`documentation/WORKFLOW.md`](documentation/WORKFLOW.md) has the PowerShell version and firewall tips.

**Demo mode:** to show a hold expiring on stage, use one-minute holds:

```bash
RESERVATION_HOLD_SECONDS=60 docker compose up --build --wait
```

You can also copy [`.env.example`](.env.example) to `.env`.

**Start from scratch:** `docker compose down --volumes` wipes the database, Redis and machine keys.

---

## The story, end to end

1. **Find.** The app shows machines near you on a map, each open, sold out, offline or in maintenance, and each machine's products with live availability (stock minus other people's holds).
2. **Reserve.** One tap places an atomic 15-minute hold. A Redis Lua script makes sure that when 500 people race for the last unit, exactly one wins ([benchmarks](docs/benchmarks.md)).
3. **Pay.** A mock payment provider *authorises* the amount. You get a 6-digit code and a QR code, bound to that one machine. The server stores only a keyed hash of the code.
4. **Pick up.** At the machine you type or scan the code. The machine dispenses, and the payment is *captured* only then.
5. **Or not.** If the hold runs out, the item goes back on sale and the payment is *voided*. Everything updates live: the app, the dashboard, the machine.

Failure handling is built in: a machine going offline, a jammed or partial dispense, a lost connection mid-dispense, a phone losing its connection, Redis restarting. Each is handled and tested; see the [architecture decisions](docs/architecture-decisions.md).

---

## Architecture

| Part | Folder | Built with |
|---|---|---|
| Backend API, machine gateway, background jobs | [`backend/`](backend) | Python 3.11, FastAPI, async SQLAlchemy, Alembic, redis-py |
| Database | (Compose) | PostgreSQL 16 + PostGIS, for geo queries |
| Holds, live notices, rate limits | (Compose) | Redis 7 |
| Customer app | [`mobile/`](mobile) | Expo (React Native), strict TypeScript, Zustand, API types generated from OpenAPI |
| Operator dashboard | [`dashboard/`](dashboard) | React, TypeScript, Vite, Leaflet, served by nginx |
| Machine simulator + kiosk screens | [`hardware_sim/`](hardware_sim) | Python (aiohttp) daemon; React kiosk |
| Browser end-to-end tests | [`e2e/`](e2e) | Playwright |
| Load tests | [`loadtest/`](loadtest) | race script + Locust |

**Key choices**, each explained in an ADR in [`docs/architecture-decisions.md`](docs/architecture-decisions.md):
- **Postgres is the source of truth; Redis is the fast, atomic gate** for holds (ADR-012). An expired hold stops counting the instant it expires, even if every background job is down.
- **Authorise at checkout, capture at dispense, void on expiry** (ADR-015). A customer is charged only for an item in their hand.
- **Machines authenticate with their own Ed25519 keys** and speak a versioned, documented protocol ([`docs/machine-protocol.md`](docs/machine-protocol.md)), so a real Raspberry Pi or ESP32 can replace the simulator without changing the backend (ADR-007, ADR-010, ADR-017, ADR-018).
- **Live updates are notices, not data** (ADR-022, ADR-026). Clients re-read what changed through the normal API, and customers only ever hear about their own reservations.
- **Zero trust:** every input is validated by strict Pydantic models; pickup codes, refresh tokens and passwords are stored only as hashes; tokens never go in URLs.

---

## Quality

- **CI on every pull request** ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)):
  - lint and `mypy --strict`;
  - over 500 backend tests against real Postgres and Redis, including race tests;
  - the simulator's tests;
  - strict TypeScript builds of the kiosk, dashboard and app, and the app's Jest tests;
  - a Docker smoke test that starts the whole stack and drives the kiosk and dashboard in a real browser.

  A PR merges automatically only when every job is green.
- **Load test:** 500 simultaneous customers racing for the last unit; exactly one hold in every round, with no errors ([`docs/benchmarks.md`](docs/benchmarks.md)).
- **Observability:** JSON logs with a request id on every line, and a Prometheus `/metrics` endpoint (ADR-029).

---

## Documentation

| Document | For |
|---|---|
| [`documentation/PROJECT_SUMMARY.md`](documentation/PROJECT_SUMMARY.md) | what each phase built, and why |
| [`documentation/CODE.md`](documentation/CODE.md) | a map of the code, data model, API and main flows |
| [`documentation/WORKFLOW.md`](documentation/WORKFLOW.md) | running, testing, debugging and shipping changes |
| [`docs/architecture-decisions.md`](docs/architecture-decisions.md) | every significant design decision (ADRs) |
| [`docs/machine-protocol.md`](docs/machine-protocol.md) | the machine ↔ backend protocol, for building real hardware |
| [`docs/benchmarks.md`](docs/benchmarks.md) | load-test results |

The project was built in phases, tracked in the roadmap in [`CLAUDE.md`](CLAUDE.md).
