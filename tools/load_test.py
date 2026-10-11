"""Local overload simulation. No token, Discord login, API calls, or member actions."""

import argparse
import asyncio
import json
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from statezero.config import Settings  # noqa: E402
from statezero.operations import Operations, percentile  # noqa: E402
from statezero.security import UserError  # noqa: E402
from statezero.storage import AsyncStore  # noqa: E402


async def run(actions, concurrency, latency):
    config = Settings()
    operations = Operations(config.limits, config.operations.action_timeout_seconds)
    peak = 0
    lags = []
    stop = asyncio.Event()

    async def sample_loop():
        while not stop.is_set():
            started = time.monotonic()
            await asyncio.sleep(0.005)
            lags.append(max(0, time.monotonic() - started - 0.005))

    tracemalloc.start()
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as directory:
        store = AsyncStore(Path(directory) / "simulation.db")
        await store.health()
        sampler = asyncio.create_task(sample_loop())

        async def action(user_id):
            nonlocal peak
            try:
                async with operations.action(
                    SimpleNamespace(user=SimpleNamespace(id=user_id))
                ):
                    peak = max(peak, operations.active)
                    await store.setting("verification_locked", "0")
                    await asyncio.sleep(latency)
                    await store.audit(user_id, "Simulated action", outcome="success")
            except UserError:
                return

        try:
            for first in range(0, actions, concurrency):
                await asyncio.gather(
                    *(
                        action(user_id)
                        for user_id in range(first, min(first + concurrency, actions))
                    )
                )
            report = {
                "simulation_only": True,
                "requested_actions": actions,
                "burst_concurrency": concurrency,
                "mock_api_latency_seconds": latency,
                "elapsed_seconds": round(time.monotonic() - started, 4),
                "peak_active_actions": peak,
                "event_loop_lag_seconds_p95": percentile(lags),
                "database_seconds_p95": percentile(store.latencies),
                "tracemalloc_peak_bytes": tracemalloc.get_traced_memory()[1],
                **operations.snapshot(),
            }
        finally:
            stop.set()
            await sampler
            await store.close()
            tracemalloc.stop()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions", type=int, default=5000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--api-latency-ms", type=float, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        not 1 <= args.actions <= 100000
        or not 1 <= args.concurrency <= 1000
        or not 0 <= args.api_latency_ms <= 1000
    ):
        parser.error("Use 1–100000 actions, 1–1000 concurrency, and 0–1000 ms latency")
    report = asyncio.run(
        run(args.actions, args.concurrency, args.api_latency_ms / 1000)
    )
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    print(text)
    return (
        0
        if report["active_actions"] == 0
        and report["peak_active_actions"] <= Settings().limits.max_concurrent_actions
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
