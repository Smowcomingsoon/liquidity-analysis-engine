"""Request timings and event-loop lag, in memory (overhaul plan, week 1).

The baseline the plan measures against: how long each endpoint takes and
how long the single uvicorn process stays blocked. Kept small on purpose —
a rolling window per route, no storage, no dependency — and read through
GET /api/perf.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

WINDOW = 500            # last N requests per route
PROBE_EVERY_S = 0.5     # how often the loop probe wakes up
LAG_WINDOW = 7200       # ~1 h of probe samples

_routes: dict[str, deque] = {}
_lags: deque = deque(maxlen=LAG_WINDOW)
_started = time.time()


def record(route: str, seconds: float, status: int) -> None:
    q = _routes.get(route)
    if q is None:
        q = _routes[route] = deque(maxlen=WINDOW)
    q.append((seconds, status))


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
    return values[k]


def snapshot() -> dict:
    routes = []
    for route, q in _routes.items():
        secs = [s for s, _ in q]
        routes.append({
            "route": route,
            "count": len(secs),
            "errors": sum(1 for _, st in q if st >= 500),
            "p50_ms": round(_pct(secs, 50) * 1000, 1),
            "p95_ms": round(_pct(secs, 95) * 1000, 1),
            "max_ms": round(max(secs) * 1000, 1) if secs else 0.0,
        })
    routes.sort(key=lambda r: -r["p95_ms"])
    lags = list(_lags)
    return {
        "since": int(_started),
        "loop_lag": {
            "samples": len(lags),
            "p95_ms": round(_pct(lags, 95) * 1000, 1),
            "max_ms": round(max(lags) * 1000, 1) if lags else 0.0,
            "over_100ms": sum(1 for x in lags if x > 0.1),
        },
        "routes": routes,
    }


async def loop_lag_probe() -> None:
    """Sleeps PROBE_EVERY_S and records how late it wakes up: the time the
    loop spent on something that did not yield."""
    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        await asyncio.sleep(PROBE_EVERY_S)
        _lags.append(max(0.0, loop.time() - t0 - PROBE_EVERY_S))


DUMP_EVERY_S = 600


async def dump_loop(path) -> None:
    """Writes snapshot() to a file on the data volume every DUMP_EVERY_S, so
    the server-perf workflow can read the numbers without a login."""
    import json

    from app.atomic_write import write_text_atomic

    while True:
        await asyncio.sleep(DUMP_EVERY_S)
        try:
            await asyncio.to_thread(write_text_atomic, path, json.dumps(snapshot()))
        except OSError:
            pass
