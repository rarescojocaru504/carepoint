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
├── hardware_sim/          # Python daemon simulating machine telemetry
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

## 📝 Memory & Documentation
Whenever you make a significant architectural decision (e.g., how the Redis lock is structured, how the IoT machine authenticates), document it immediately in `docs/architecture-decisions.md`.