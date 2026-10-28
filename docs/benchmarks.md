# CarePoint benchmarks

> **Headline:** 500 customers tapping "reserve" on the **same last item at the same instant**, repeated over 10 rounds: **exactly one winner every round, never an oversell, 0 errors**. p95 response time was **0.87 s** with 2 server worker processes.

Measured on 2026-10-28 by `loadtest/race.py` and `loadtest/locustfile.py` against the full Docker Compose stack. Raw results are in [`docs/benchmarks/`](benchmarks/).

## 1. The race for the last unit (correctness under contention)

Each round targets a fresh slot. Every simulated customer waits behind one shared barrier, and they all send `POST /api/v1/reservations` at the same instant. A round **passes** only if all four hold:
- exactly `min(units, customers)` reservations succeed;
- every other request gets a clean `409 insufficient_stock`;
- no request errors;
- the API afterwards reports **0 units left**. This is checked independently, not taken from the script's own counts.

| Scenario | Server workers | Rounds passed | Requests | Errors | p50 | p95 | p99 | Throughput during the burst |
|---|---|---|---|---|---|---|---|---|
| 500 customers → **1 unit** | 1 | **10 / 10** | 5,000 | 0 | 956 ms | 1,312 ms | 1,376 ms | 368 req/s |
| 500 customers → **1 unit** | 2 | **10 / 10** | 5,000 | 0 | 621 ms | 871 ms | 932 ms | 540 req/s |
| 500 customers → **10 units** | 2 | **5 / 5** (exactly 10 winners each round) | 2,500 | 0 | 632 ms | 869 ms | 975 ms | 544 req/s |

**How to read the latency.** All 500 requests arrive within milliseconds, so a request's latency is mostly time spent **queueing behind the other 499**. A normal request on an idle server takes well under 50 ms. Roughly speaking, the slowest customer in a rush of 500 waits about a second for a definite yes or no.

## 2. Sustained mixed traffic

Locust ran **200 simulated customers for 60 s**. Each one browsed the map, opened machines and the catalogue, and sometimes reserved and then cancelled, with waits of 0.5–2 s between actions.

| Endpoint | Requests | Failures | p50 | p95 | p99 |
|---|---|---|---|---|---|
| `GET /machines/nearby` | 4,736 | 0 | 28 ms | 410 ms | 600 ms |
| `GET /machines/{id}` | 2,234 | 0 | 24 ms | 350 ms | 500 ms |
| `GET /products` | 800 | 0 | 19 ms | 360 ms | 540 ms |
| `POST /reservations` | 795 | 0 | 42 ms | 610 ms | 730 ms |
| `DELETE /reservations/{id}` | 795 | 0 | 16 ms | 160 ms | 350 ms |
| **All** | **9,360** | **0** | **26 ms** | **400 ms** | **600 ms** |

That's about **158 requests/s with 0 failures**. Every virtual user comes from one IP address, so this run used `loadtest/compose.loadtest.yml`, which raises the per-IP discovery and per-user reservation rate limits; otherwise the run would have measured the rate limiter instead of the server.

## Test environment (read before quoting numbers)

- **One machine with 4 vCPUs and 15 GB RAM** (a cloud sandbox) ran **everything**: the load generators, the API, PostgreSQL 16 with PostGIS, and Redis 7, all as Docker Compose containers.
- The client and server **competed for the same 4 cores**, and the Python load generators alone used 1–3 cores at peak. A real deployment, with clients on other machines, would see lower latencies than these.
- The API ran as Uvicorn with 1 or 2 worker processes (`WEB_CONCURRENCY`), each with the default SQLAlchemy pool of 5 + 10 connections. There was no tuning beyond that.

## What the load test found and fixed

These experiments changed the system, and the fixes are in the same PR:

1. **Sporadic "server disconnected" errors** (27 in 5,000). Uvicorn's default 5 s keep-alive closed idle connections just as clients reused them. The server now runs with `--timeout-keep-alive 75`, which is longer than typical load-balancer idle timeouts. Result: 0 errors.
2. **Losers were doing all the work.** In a rush, 499 of 500 requests lose, yet each one took a per-user lock and ran 3 database queries before Redis said no. A **fast-fail pre-check** now reads the slot's availability from Redis first. It changes no outcome, because the atomic Lua script still decides every request that gets past it. Result: p95 with 1 worker dropped from 2.1 s to 1.3 s.
3. **The load generator was the bottleneck at first.** `top` during a run showed the client at 100% CPU and the server mostly idle. The burst is now spread over several client processes using aiohttp. Initial numbers were 3–7 s, but that was the client, not CarePoint.
4. **The campus-NAT rate limit.** Hundreds of students on campus Wi-Fi share one public IP. The discovery limit per IP was raised from 300/min to 1,200/min (ADR-014).

## Reproduce

```bash
docker compose up --build -d && docker compose exec backend python -m app.scripts.seed
```
Then follow [`loadtest/README.md`](../loadtest/README.md).
