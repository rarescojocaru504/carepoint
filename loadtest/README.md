# Load tests

Two tools; results and methodology are in [`docs/benchmarks.md`](../docs/benchmarks.md).

| Tool | Question it answers |
|---|---|
| `race.py` | Do *N* simultaneous reservations for the same stock ever oversell? How long does the slowest customer wait? |
| `locustfile.py` | How does the API hold up under sustained, realistic browse-and-reserve traffic? |

## Setup

```bash
pip install -r loadtest/requirements.txt
docker compose up --build -d
docker compose exec backend python -m app.scripts.seed
```

The fixtures are customers with pre-issued tokens, plus machines at 0,0 so they never show on the demo map. Tokens last 15 minutes, so run the tests straight after creating them:

```bash
docker compose exec backend python -m app.scripts.loadtest_fixtures create --users 500 --slots 10 --quantity 1 --out /tmp/fixtures.json
docker compose cp backend:/tmp/fixtures.json fixtures.json
```

## The race for the last unit

```bash
python loadtest/race.py --fixtures fixtures.json --rounds 10 --concurrency 500 --processes 2
```

- It exits non-zero if any round oversells, has the wrong number of winners, errors, or leaves stock behind.
- Use `--quantity 10` when creating fixtures to race 500 customers for 10 units.
- Use `WEB_CONCURRENCY=2 docker compose up -d` to run 2 API worker processes.

## Sustained traffic

Every Locust user comes from your one IP, so for this run start the stack with the load-test override, which lifts the per-IP and per-user limits. Otherwise you'd be measuring the rate limiter:

```bash
docker compose -f docker-compose.yml -f loadtest/compose.loadtest.yml up -d
```

```bash
LOADTEST_FIXTURES=fixtures.json locust -f loadtest/locustfile.py --headless -u 200 -r 50 -t 60s --host http://localhost:8000
```

Restart normally (`docker compose up -d`) afterwards.

## Clean up

```bash
docker compose exec backend python -m app.scripts.loadtest_fixtures cleanup
```
