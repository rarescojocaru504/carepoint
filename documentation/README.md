# CarePoint documentation

These documents hand the project over from the cloud sessions that built Phases 0–8 to a local Claude Code session that builds Phases 9–11. They are also a guide for anyone who wants to understand the finished app.

| File | Read it when |
|---|---|
| [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md) | You want to know what was built in each phase, why, and what comes next. |
| [`CODE.md`](CODE.md) | You need to find your way around the code: every component, folder, table, endpoint and main flow. |
| [`WORKFLOW.md`](WORKFLOW.md) | You're about to run, test, change or ship something: local setup, checks, branches, CI and debugging. |
| [`HANDOVER_PROMPT.md`](HANDOVER_PROMPT.md) | You're starting the local Claude Code session. Paste the prompt in it. |

## Other sources of truth

These documents summarise the sources below. When they disagree, the source wins.

- [`../CLAUDE.md`](../CLAUDE.md): the project rules and the MVP roadmap with ticked items. Claude Code reads it automatically.
- [`../docs/architecture-decisions.md`](../docs/architecture-decisions.md): ADR-001 to ADR-023, the reasoning behind every major design choice.
- [`../docs/machine-protocol.md`](../docs/machine-protocol.md): the versioned machine ↔ backend WebSocket protocol (revision 4). Its JSON Schema is [`../docs/machine-protocol.schema.json`](../docs/machine-protocol.schema.json).
- [`../docs/benchmarks.md`](../docs/benchmarks.md): load-test results for the reservation engine.
- The code and its tests.

## The app in one paragraph

CarePoint is a network of smart vending machines for urgent personal-care items: pads, condoms, plasters and phone cables, placed around university campuses in Bucharest. A customer finds a nearby machine on a map and sees live stock. They reserve an item, which holds it for 15 minutes, pay with a mock payment, and get a one-time 6-digit code and a QR code. At the machine they type or scan the code and the machine dispenses the item. If they don't arrive in time, the hold expires, the unit goes back on sale and the payment is voided. Operators watch the fleet, stock and reservations live on a web dashboard.
