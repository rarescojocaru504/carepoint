# CarePoint documentation

These documents explain the finished CarePoint MVP (Phases 0–11): what was built, how the code is organised, how to run and change it, and how to present it.

| File | Read it when |
|---|---|
| [`APP_WALKTHROUGH.md`](APP_WALKTHROUGH.md) | You want to understand or present the app: the customer's journey and the failure scenarios, with where each step lives in the code. **Start here.** |
| [`READING_THE_CODE.md`](READING_THE_CODE.md) | You want to really understand the code: reading order, a hands-on exercise, tests as documentation, how to test. |
| [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) | You want to know what was built in each phase, why, and what comes next. |
| [`CODE.md`](CODE.md) | You need to find your way around the code: every component, folder, table, endpoint and main flow. |
| [`WORKFLOW.md`](WORKFLOW.md) | You're about to run, test, change or ship something: local setup, checks, branches, CI and debugging. |
| [`HANDOVER_PROMPT.md`](HANDOVER_PROMPT.md) | History: the prompt that started the local session which built Phases 9–11. |

## Other sources of truth

These documents summarise the sources below. When they disagree, the source wins.

- [`../CLAUDE.md`](../CLAUDE.md): the project rules and the MVP roadmap with ticked items. Claude Code reads it automatically.
- [`../docs/architecture-decisions.md`](../docs/architecture-decisions.md): ADR-001 to ADR-032, the reasoning behind every major design choice.
- [`../docs/machine-protocol.md`](../docs/machine-protocol.md): the versioned machine ↔ backend WebSocket protocol (revision 4). Its JSON Schema is [`../docs/machine-protocol.schema.json`](../docs/machine-protocol.schema.json).
- [`../docs/benchmarks.md`](../docs/benchmarks.md): load-test results for the reservation engine.
- [`../docs/manual-testing-guide.md`](../docs/manual-testing-guide.md): every feature by hand.
- [`../docs/pitch/`](../docs/pitch): the pitch kit.
- The code and its tests.

## The app in one paragraph

CarePoint is a network of smart vending machines for urgent personal-care items: pads, condoms, plasters and phone cables, placed around university campuses in Bucharest. A customer finds a nearby machine on a map and sees live stock. They reserve an item, which holds it for 15 minutes, pay with a mock payment, and get a one-time 6-digit code and a QR code. At the machine they type or scan the code and the machine dispenses the item. If they don't arrive in time, the hold expires, the unit goes back on sale and the payment is voided. Operators watch the fleet, stock and reservations live on a web dashboard. Everything starts with `docker compose up`, and the app runs on a phone in Expo Go.
