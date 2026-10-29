# WORKFLOW.md: how to run, change, test and ship CarePoint

This covers working on CarePoint on your own computer: setting up, running the stack, developing each component, checking your work before you push, and getting a PR through CI to an automatic merge. It applies to a person and to a local Claude Code session alike.

> The "Docker in the Claude Code cloud sandbox" rules in `CLAUDE.md` (mirror images, the CA-patched base image, `--no-build`) are **only for the cloud sandbox**. On a normal computer, ignore them and use the plain commands below.

---

## 1. One-time setup

**Install:**
- **Git**, and the GitHub CLI `gh`, logged in with `gh auth login`, for opening PRs from the terminal;
- **Docker Desktop** (Windows or macOS) or Docker Engine with the Compose plugin (Linux);
- **Python 3.11 or newer** (CI uses 3.11);
- **Node.js 20** and npm;
- for the mobile app: **Expo Go** on your phone (App Store or Play Store), and optionally Android Studio for an emulator.

**Clone the repository:**

```bash
git clone https://github.com/rarescojocaru504/carepoint.git
```

```bash
cd carepoint
```

**Windows:** the repository forces LF line endings for scripts (`.gitattributes`), so Docker init scripts work even with `core.autocrlf=true`. Run the commands below in PowerShell or Git Bash. Where you see `source .venv/bin/activate`, Windows uses `.venv\Scripts\activate`.

---

## 2. Run the whole stack

```bash
docker compose up --build --wait
```

On first start this:
1. runs the database migrations;
2. seeds the demo data, because `DEMO_INIT=1` is the default;
3. issues the six demo machines' keys;
4. starts six simulated machines, which come online within a few seconds.

| Open | What you get |
|---|---|
| http://localhost:8000/docs | Swagger UI for the whole API |
| http://localhost:3000 | Operator dashboard |
| http://localhost:8080 | Kiosk list; `/kiosk/CP-BUC-0001` is one machine's screen |

**Demo accounts:**

| Role | Email | Password |
|---|---|---|
| Customer | `customer@carepoint.demo` | `carepoint-customer-demo` |
| Operator | `operator@carepoint.demo` | `carepoint-operator-demo` |
| Admin | `admin@carepoint.demo` | `carepoint-admin-demo` |

**Useful commands:**

```bash
docker compose logs -f backend simulator
```

```bash
docker compose exec backend python -m app.scripts.seed
```

```bash
docker compose stop simulator
```

```bash
docker compose down --volumes
```

`down --volumes` wipes the database, Redis and the machine keys, so the next `up` starts fresh.

**A pickup by hand, in about a minute:**
1. In Swagger, log in as the customer (`POST /auth/login`), then use **Authorize** with the access token.
2. Call `GET /machines/nearby?lat=44.44&lng=26.08&radius_m=20000` and pick a machine.
3. Call `GET /machines/{id}` and pick a `slot_id` with stock.
4. Call `POST /reservations` with `{"slot_id": "<slot_id>"}`, then `POST /reservations/{id}/checkout` with `{"payment_method": "pm_card_ok"}`. Note `pickup.code`.
5. Open that machine's kiosk and type the code. The item dispenses, and the dashboard updates live.

**To show an expiry live,** start the stack with a short hold:

```bash
RESERVATION_HOLD_SECONDS=60 docker compose up --build --wait
```

For this to work, add `RESERVATION_HOLD_SECONDS: ${RESERVATION_HOLD_SECONDS:-900}` to the backend's `environment` in `docker-compose.yml` first. That is a small Phase 11 "one-command demo" task.

---

## 3. Developing each component

### Backend (`backend/`)

Run Postgres and Redis in Docker, and the code and tests on your machine. The defaults already point at `localhost`.

**Start the services:**

```bash
docker compose up -d db redis
```

**Create a virtual environment and install the dependencies:**

```bash
cd backend && python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt
```

**Apply the migrations:**

```bash
alembic upgrade head
```

**Run the API with auto-reload:**

```bash
uvicorn app.main:app --reload --port 8000
```

If the Compose `backend` service is also running, stop it first (`docker compose stop backend`) or use another port.

**Run the tests.** They use the `carepoint_test` database and Redis DB 15:

```bash
pytest
```

**After changing a model, create a migration:**

```bash
alembic revision --autogenerate -m "short description"
```

Then rename the file to the existing pattern, `YYYY_MM_DD_NNNN_name.py`, read it, and check it both ways:

```bash
alembic upgrade head && alembic downgrade -1 && alembic upgrade head && alembic check
```

**After changing the machine protocol models,** regenerate the published schema:

```bash
python -m app.scripts.export_protocol_schema > ../docs/machine-protocol.schema.json
```

**After changing an endpoint or a Pydantic schema,** regenerate the OpenAPI file the mobile app's types come from. The script writes the file itself (a shell redirect on Windows would write CRLF or UTF-16):

```bash
python -m app.scripts.export_openapi
```

### Simulator and kiosk (`hardware_sim/`)

**Install the simulator's dependencies and run its tests:**

```bash
cd hardware_sim && python -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt && pytest
```

**Build the kiosk** (strict `tsc` plus Vite):

```bash
cd hardware_sim/kiosk && npm ci && npm run build
```

The simulator normally runs in Compose. After changing it, rebuild just that service:

```bash
docker compose up -d --build simulator
```

### Dashboard (`dashboard/`)

**Build it:**

```bash
cd dashboard && npm ci && npm run build
```

**Run it with hot reload:**

```bash
npm run dev
```

The dev server uses port 3000 and proxies `/api` to the backend on `localhost:8000` (`vite.config.ts`). Stop the Compose `dashboard` service first (`docker compose stop dashboard`), because it uses the same port.

### Mobile (`mobile/`, from Phase 9)

**Start the Expo dev server:**

```bash
cd mobile && npm ci && npx expo start
```

Then scan the QR code with **Expo Go**.

The phone can't reach `localhost`. Point the app at your computer's LAN IP:

```bash
EXPO_PUBLIC_API_URL=http://192.168.1.23:8000 npx expo start
```

Replace `192.168.1.23` with your own IP: run `ipconfig` on Windows, or `ipconfig getifaddr en0` on macOS. The phone and computer must be on the same Wi-Fi, and the firewall must allow inbound port 8000. The Android emulator uses `http://10.0.2.2:8000` instead.

### Browser end-to-end tests (`e2e/`)

These run against the live Compose stack:

```bash
cd e2e && npm ci && npx playwright install chromium
```

```bash
node kiosk-smoke.mjs && node dashboard-smoke.mjs
```

`dashboard-smoke.mjs` stops and restarts the `simulator` container itself, so it needs the `docker` command available. Screenshots go to `e2e/artifacts/`.

---

## 4. Checks to run before every push

These are the same checks CI runs. Run them for every component you touched.

| Folder | Commands |
|---|---|
| `backend/` | `ruff check .` · `ruff format --check .` · `mypy app alembic/env.py tests` · `pytest` |
| `hardware_sim/` | `ruff check .` · `ruff format --check .` · `mypy carepoint_sim tests scripts` · `pytest` |
| `hardware_sim/kiosk/` | `npm run build` |
| `dashboard/` | `npm run build` |
| `mobile/` | `npm run gen:api` (no diff) · `npm run typecheck` · `npm run lint` · `npm test` |
| Compose changed? | `docker compose up --build --wait`, then the e2e scripts |

`ruff format .`, without `--check`, fixes the formatting for you.

---

## 5. Branches, PRs and CI

**1. Branch from the latest `main`,** one logical change per branch, named `<type>/<short-kebab-name>` where the type is `feature/`, `fix/`, `chore/` or `docs/`:

```bash
git checkout main && git pull && git checkout -b feature/mobile-foundation
```

**2. Commit with clear messages:** a short imperative title, then a body explaining why. If a Claude session wrote the commit, it adds its attribution lines.

**3. Push and open a PR:**

```bash
git push -u origin feature/mobile-foundation
```

```bash
gh pr create --fill
```

**4. CI** (`.github/workflows/ci.yml`) runs these jobs on every PR:

| Job | Checks |
|---|---|
| Lint & type check | backend ruff and `mypy --strict` |
| Tests (PostGIS + Redis) | backend pytest against real services |
| Hardware simulator | ruff, mypy and pytest |
| Kiosk (TypeScript) | strict build |
| Dashboard (TypeScript) | strict build |
| Mobile (TypeScript) | generated API types current, strict `tsc`, ESLint, Jest (Node 22) |
| Docker Compose smoke test | the full stack, seeding twice, 6 machines online, `pickup_smoke.py`, the kiosk and dashboard Playwright tests, then pytest inside the backend container |
| Auto-merge | when all of the above pass, **merges the PR into `main`** and deletes the branch |

Every job above is in `automerge.needs`; a new job must be added there in the same PR.

**5. To hold a PR back** from merging, open it as a **draft** or add the label `do-not-merge`.

**6. When CI fails:**
- Read the job log. Reproduce the failure locally, find the root cause, fix it, and push.
- Never skip or disable a test to get green.
- If the failure isn't caused by your change, fix it in a separate `fix/` PR and bring the same fix into yours.

**7. Prefer merging `main` into your branch** over rebasing a branch you've already pushed.

**8. Keep the roadmap current:** tick the item in `CLAUDE.md` and add its PR number **in the same PR** that finishes it. Record each phase's key decisions as a new ADR in `docs/architecture-decisions.md`; the next numbers are ADR-024 and up.

---

## 6. How to build a phase

1. **Read first:** the phase in `CLAUDE.md`, the plan in `documentation/PROJECT_SUMMARY.md`, and the ADRs and code it builds on (`documentation/CODE.md` says where).
2. **Split it into PRs** of one logical change each, with tests, CI-green, in order. Backend changes go before the UI that uses them.
3. **Backend changes:**
   - keep handlers thin and put the logic in `services/`;
   - use strict Pydantic input models (`InputModel` forbids unknown fields);
   - add a migration for any schema change;
   - write tests with the fixtures in `CODE.md` §2.7, including races and failure paths;
   - publish change notices **after commit**.
4. **Frontend changes:**
   - strict TypeScript with no `any`;
   - functional components and hooks;
   - handle every error state the API documents;
   - never persist secrets or pickup codes in plain storage.
5. **Before pushing,** run the checks in §4 and the stack with e2e when Compose or the UI changed. Then re-read your own diff critically.
6. **Document it:** an ADR for each decision, ticks in `CLAUDE.md`, and an update to `documentation/` if the structure changed.
7. **After merge,** pull `main` and start the next branch from it.

---

## 7. Debugging tips

- **A machine is "offline" on the dashboard.** Check `docker compose logs simulator`. A machine counts as online only if it was seen within 90 s **and** hasn't disconnected since. `CP-BUC-0006` is in *maintenance* on purpose.
- **"insufficient_stock" when stock looks free.** Other people's active holds count against availability. Check `GET /admin/reservations?status=active`.
- **The code is refused at the kiosk.** A code works only at the machine it was issued for, and only while the reservation is `paid` and unexpired. After 20 wrong codes per minute, the machine is locked briefly.
- **Redis and Postgres seem out of sync.** Reconciliation runs at startup and every 5 minutes. Restarting the backend forces a pass.
- **Tests skip instead of running.** Postgres or Redis isn't reachable: run `docker compose up -d db redis`. CI sets `REQUIRE_SERVICES=1`, which makes such tests fail rather than skip.
- **Live-notice tests see extra notices.** Redis pub/sub ignores database numbers. Filter by your own `machine_id`, as `tests/test_admin_events.py` does.
- **asyncio on Python 3.11.** Don't wrap long-lived sessions in `asyncio.TaskGroup`, and prefer `asyncio.timeout` over `asyncio.wait_for`; both can hide a cancellation on 3.11 (#22).
- **Seeing everything.** `docker compose logs --no-color > logs.txt` collects every service's output in one file.
