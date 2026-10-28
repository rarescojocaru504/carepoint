"""The anti-oversell experiment: N customers reserve the same stock at the same instant.

Each round targets a fresh slot holding Q units (from loadtest_fixtures). The N
customers are split across P client processes (one Python event loop cannot
generate 500 simultaneous requests without itself becoming the bottleneck).
Every process warms its connections, then all fire POST /api/v1/reservations
at one shared wall-clock instant.

A round passes only if exactly min(Q, N) reservations succeed, every other
request is a clean 409, nothing errors, and the API afterwards reports 0 units
left on that slot.

    python loadtest/race.py --fixtures fixtures.json --rounds 10 --processes 4
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import httpx

CLEAN = (201, 409)


@dataclass
class Round:
    slot_id: str
    expected_wins: int
    statuses: Counter[int] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    latencies_ms: list[float] = field(default_factory=list)
    burst_ms: float = 0.0
    available_after: int | None = None

    @property
    def ok(self) -> bool:
        return (
            self.statuses[201] == self.expected_wins
            and set(self.statuses) <= set(CLEAN)
            and self.available_after == 0
        )


def percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))]


def _fire(base_url: str, tokens: list[str], slot_id: str, start_at: float) -> dict:  # type: ignore[type-arg]
    """One client process: warm up, wait for the shared instant, fire, report.

    aiohttp rather than httpx here: at hundreds of concurrent requests per
    process, httpx's per-request CPU cost made the load generator itself the
    bottleneck (measured with top during a run).
    """

    async def run() -> dict:  # type: ignore[type-arg]
        statuses: Counter[int] = Counter()
        errors: Counter[str] = Counter()
        latencies: list[float] = []
        ends: list[float] = []
        connector = aiohttp.TCPConnector(limit=len(tokens), keepalive_timeout=60)
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(base_url, connector=connector, timeout=timeout) as client:

            async def warm() -> None:
                async with client.get("/health") as response:
                    await response.read()

            await asyncio.gather(*(warm() for _ in tokens))
            go = asyncio.Event()
            body = json.dumps({"slot_id": slot_id})

            async def customer(token: str) -> None:
                await go.wait()
                t0 = time.perf_counter()
                try:
                    async with client.post(
                        "/api/v1/reservations",
                        data=body,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    ) as response:
                        await response.read()
                        statuses[response.status] += 1
                except (aiohttp.ClientError, TimeoutError) as exc:
                    statuses[-1] += 1
                    errors[type(exc).__name__] += 1
                latencies.append((time.perf_counter() - t0) * 1000)
                ends.append(time.time())

            tasks = [asyncio.create_task(customer(t)) for t in tokens]
            await asyncio.sleep(max(0.0, start_at - time.time()))
            go.set()
            await asyncio.gather(*tasks)
        return {"statuses": dict(statuses), "errors": dict(errors), "latencies": latencies,
                "last_end": max(ends)}  # fmt: skip

    return asyncio.run(run())


def run_round(
    pool: ProcessPoolExecutor,
    base_url: str,
    tokens: list[str],
    slot: dict[str, str],
    quantity: int,
    processes: int,
) -> Round:
    result = Round(slot["slot_id"], expected_wins=min(quantity, len(tokens)))
    start_at = time.time() + 1.5  # time for every process to warm up
    shares = [tokens[i::processes] for i in range(processes)]
    futures = [pool.submit(_fire, base_url, share, slot["slot_id"], start_at) for share in shares]
    last_end = start_at
    for future in futures:
        part = future.result()
        result.statuses.update({int(k): v for k, v in part["statuses"].items()})
        result.errors.update(part["errors"])
        result.latencies_ms += part["latencies"]
        last_end = max(last_end, part["last_end"])
    result.burst_ms = (last_end - start_at) * 1000

    detail = httpx.get(f"{base_url}/api/v1/machines/{slot['machine_id']}", timeout=30).json()
    result.available_after = next(
        s["available"] for s in detail["slots"] if s["slot_id"] == slot["slot_id"]
    )
    return result


def main(args: argparse.Namespace) -> int:
    fixtures = json.loads(args.fixtures.read_text())
    tokens: list[str] = fixtures["tokens"][: args.concurrency]
    quantity: int = fixtures["quantity"]
    if args.rounds > len(fixtures["slots"]):
        print("Not enough slots in the fixtures for that many rounds", file=sys.stderr)
        return 2

    rounds: list[Round] = []
    with ProcessPoolExecutor(max_workers=args.processes) as pool:
        for i in range(args.rounds):
            r = run_round(
                pool, args.base_url, tokens, fixtures["slots"][i], quantity, args.processes
            )
            rounds.append(r)
            other = {k: v for k, v in r.statuses.items() if k not in CLEAN}
            print(
                f"round {i + 1:>2}: {len(tokens)} customers, {quantity} unit(s) -> "
                f"{r.statuses[201]} reserved, {r.statuses[409]} refused, other {other}, "
                f"left {r.available_after} | p50 {percentile(r.latencies_ms, 50):.0f} ms, "
                f"p95 {percentile(r.latencies_ms, 95):.0f} ms, burst {r.burst_ms:.0f} ms "
                f"{'OK' if r.ok else 'FAIL'}"
                + (f" transport errors: {dict(r.errors)}" if r.errors else "")
            )

    latencies = [ms for r in rounds for ms in r.latencies_ms]
    summary = {
        "customers_per_round": len(tokens),
        "units_per_slot": quantity,
        "client_processes": args.processes,
        "rounds": len(rounds),
        "rounds_passed": sum(r.ok for r in rounds),
        "total_requests": len(latencies),
        "reserved": sum(r.statuses[201] for r in rounds),
        "refused_409": sum(r.statuses[409] for r in rounds),
        "errors": sum(v for r in rounds for k, v in r.statuses.items() if k not in CLEAN),
        "latency_ms": {
            "p50": round(percentile(latencies, 50), 1),
            "p95": round(percentile(latencies, 95), 1),
            "p99": round(percentile(latencies, 99), 1),
            "max": round(max(latencies), 1),
            "mean": round(statistics.fmean(latencies), 1),
        },
        "burst_ms_mean": round(statistics.fmean(r.burst_ms for r in rounds), 1),
        "throughput_rps_during_burst": round(
            len(latencies) / (sum(r.burst_ms for r in rounds) / 1000), 1
        ),
    }
    print(json.dumps(summary, indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps(summary, indent=2))
    return 0 if summary["rounds_passed"] == len(rounds) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--fixtures", type=Path, default=Path("fixtures.json"))
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=500)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--json-out", type=Path)
    sys.exit(main(parser.parse_args()))
