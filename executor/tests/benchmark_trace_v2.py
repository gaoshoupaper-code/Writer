"""Trace V2 executor 热路径基准；独立运行，不进入常规 unittest。"""

from __future__ import annotations

import asyncio
import json
import math
import tempfile
import time
from pathlib import Path

from app.platform.trace.recorder import TraceRecorder
from app.schemas.screenplay import ThreadSummary


EVENT_COUNT = 5_000


async def _run() -> dict[str, float | int]:
    with tempfile.TemporaryDirectory() as tmpdir:
        thread = ThreadSummary(
            thread_id="benchmark-thread",
            workspace_id="benchmark-workspace",
            session_name="benchmark",
            workspace_path=str(Path(tmpdir)),
            created_at="2026-07-28T00:00:00+00:00",
            updated_at="2026-07-28T00:00:00+00:00",
        )
        recorder = TraceRecorder()
        recorder.start_drain()
        handle = recorder.create_run(thread, "benchmark.trace")
        samples: list[float] = []
        for index in range(EVENT_COUNT):
            started = time.perf_counter()
            recorder.append_event(handle.trace_id, {
                "type": "tool_start",
                "status": "running",
                "source": "runtime",
                "run_id": f"benchmark-tool-{index}",
                "tool_name": "read_file",
            })
            samples.append(time.perf_counter() - started)
        await recorder.aclose()
        ordered = sorted(samples)
        p95_ms = ordered[math.ceil(len(ordered) * 0.95) - 1] * 1000
        return {
            "events": EVENT_COUNT,
            "enqueue_p95_ms": round(p95_ms, 3),
            "enqueue_max_ms": round(max(samples) * 1000, 3),
        }


def main() -> None:
    result = asyncio.run(_run())
    print(json.dumps(result, indent=2))
    if result["enqueue_p95_ms"] > 2:
        raise AssertionError(f"enqueue p95 budget exceeded: {result['enqueue_p95_ms']}ms")


if __name__ == "__main__":
    main()
