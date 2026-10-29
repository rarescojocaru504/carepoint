# Handover prompt for a local Claude Code session

**How to use it:**
1. Open a terminal in your local clone of the repository and update it: `git checkout main && git pull`.
2. Start Claude Code with `claude`.
3. Paste everything inside the box below as your first message.

---

```text
You are taking over CarePoint, a smart vending network plus customer app for urgent
personal-care items, which I will pitch to my faculty for funding. Phases 0-8 of the
MVP roadmap are finished and merged (backend, Redis reservation engine, payments and
pickup codes, machine gateway, hardware simulator with kiosk, operator dashboard).
Your job is to finish Phases 9, 10 and 11, one phase at a time, on my computer.

1. Get oriented before writing any code. Read, in this order:
   - CLAUDE.md (project rules and the roadmap; ticked items are done)
   - documentation/PROJECT_SUMMARY.md (what each phase built, and the plan for 9-11)
   - documentation/CODE.md (map of the code, data model, API, main flows, test fixtures)
   - documentation/WORKFLOW.md (local setup, checks, branches, CI, debugging)
   - docs/architecture-decisions.md (ADR-001 to ADR-023), at least ADR-009, 011, 012,
     015, 016, 021, 022 and 023, because Phases 9-10 build on them
   Then look at the real code these documents point to. If a document and the code
   disagree, trust the code and fix the document in your next PR.

2. Check the environment. Confirm that git, gh (logged in), docker compose,
   Python 3.11+ and Node 20 work. Run `docker compose up --build --wait` and confirm
   all six simulated machines are online (WORKFLOW.md section 2). Run the backend
   tests as described in WORKFLOW.md section 3. Tell me about anything missing
   before you go further.
   This is my own computer, not the cloud sandbox: ignore the "Docker in the Claude
   Code cloud sandbox" rules in CLAUDE.md and use the plain Docker commands.

3. Follow the project rules:
   - One branch per logical change, cut from the latest main, named feature/, fix/,
     chore/ or docs/ plus a short kebab-case name. Commit and open PRs with gh yourself.
   - CI auto-merges a PR when every job is green. Before every push, run the checks in
     WORKFLOW.md section 4 for each part you touched, and the Docker stack plus e2e
     scripts if you changed Compose or a UI. If CI goes red, read the log, find the
     root cause and push a fix. Never skip or disable a test. Prefer merging main
     into a pushed branch over force-pushing.
   - Backend: strict typing (mypy --strict), thin handlers with logic in services/,
     strict Pydantic input models, an Alembic migration for every schema change, and
     tests against real Postgres and Redis using the fixtures in CODE.md section 2.7,
     including race and failure cases.
   - Mobile: Expo, strict TypeScript with no `any`, functional components and hooks,
     Zustand stores, an API client typed from the backend's OpenAPI schema, tokens in
     expo-secure-store, and never a pickup code saved to disk.
   - In the same PR that finishes a roadmap item, tick it in CLAUDE.md with the PR
     number. Record each phase's key decisions as a new ADR in
     docs/architecture-decisions.md, starting at ADR-024.

4. Do the work in this order. PROJECT_SUMMARY.md has the suggested PR split for
   each phase.
   - Phase 9, mobile foundation:
     - 9-A: OpenAPI export script, mobile/openapi.json and a drift test.
     - 9-B: the Expo app with stores, typed client, secure tokens, and a CI job
       "Mobile" (tsc --noEmit, ESLint, Jest, generated types up to date) added to
       the auto-merge needs.
     - 9-C: login/register, a map of nearby machines and a machine detail screen.
   - Phase 10, reserve, pay and pickup in the app:
     - 10-A: a customer live-events channel in the backend. Privacy rule: customers
       see stock notices for any machine, but reservation notices only for their own
       reservations. Write an ADR for it.
     - 10-B: reserve, mock checkout, a reservation screen with a live countdown, the
       QR code and 6-digit PIN, cancel, and order history.
     - 10-C: live updates in the app, plus handling for an expired hold, an offline
       machine and a lost connection.
   - Phase 11: the end-to-end test of the whole story in CI, JSON logs with request
     IDs, a metrics endpoint, a /security-review pass, the one-command demo and a
     proper README, docs/manual-testing-guide.md, and the pitch kit in docs/pitch/
     (demo script, Playwright video, architecture diagram, one-pager, hardware
     prototype cost estimate).

   For anything I must check on my phone (Expo Go), give me exact steps: the command,
   the LAN-IP setting (EXPO_PUBLIC_API_URL) and what I should see.

5. How to work with me:
   - Take it slow and step by step. Keep PRs small.
   - After each PR merges, give me a one-line status.
   - When a whole phase is done, give me a short report: what was built, how it was
     tested, how I can try it myself, and anything I should know. Then stop and ask
     before starting the next phase.
   - If a requirement is unclear or a decision is really mine (for example adding a
     paid service or changing the scope), ask me. Otherwise use your judgement and
     record it in an ADR.

6. When all phases are done:
   - Update documentation/PROJECT_SUMMARY.md, CODE.md and WORKFLOW.md so they
     describe the finished app, including mobile/ and everything from Phase 11.
   - Write documentation/APP_WALKTHROUGH.md: a plain-language tour that lets me, the
     owner, understand and present the app. Cover what happens, in order, when a
     customer opens the app, reserves, pays and picks up, and where each step lives
     in the code. Also cover the failure scenarios (expiry, offline machine, jam) and
     how to demo each one.
   - Make sure the MVP user story in CLAUDE.md works end to end with
     `docker compose up` and the app in Expo Go.

Start with step 1. After step 2, tell me what you found and wait for my go-ahead
before starting Phase 9.
```
