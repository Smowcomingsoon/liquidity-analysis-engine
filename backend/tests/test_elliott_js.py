"""Elliott Waves detector (frontend/static/elliott.js, segnalazione 7f9cd5ed)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ELLIOTT = Path(__file__).resolve().parents[2] / "frontend" / "static" / "elliott.js"
NODE = shutil.which("node")


def _candles_through(prices, steps=12):
    """Straight legs between turning prices, `steps` bars each."""
    out, t = [], 0
    for a, b in zip(prices, prices[1:]):
        for k in range(steps):
            p = a + (b - a) * k / steps
            out.append({"time": t, "open": p, "high": p * 1.001, "low": p * 0.999, "close": p})
            t += 1
    for _ in range(steps):
        p = prices[-1]
        out.append({"time": t, "open": p, "high": p * 1.001, "low": p * 0.999, "close": p})
        t += 1
    return out


def _detect(candles):
    js = (
        f"const ew=require({json.dumps(str(ELLIOTT))});"
        f"process.stdout.write(JSON.stringify(ew.detect({json.dumps(candles)},5)))"
    )
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_finds_bull_motive_then_abc_with_fib():
    # 0-5 motive, then a-b-c, then a leg to close the last pivot.
    out = _detect(_candles_through([108, 100, 120, 110, 150, 135, 170, 150, 160, 140, 155]))
    assert [m["dir"] for m in out["motives"]] == ["bull"]
    assert [round(p["price"]) for p in out["motives"][0]["points"]] == [100, 120, 110, 150, 135, 170]
    assert len(out["corrections"]) == 1
    assert out["corrections"][0]["invalid"] is False
    assert round(out["fib"]["levels"]["0.618"], 1) == round(170 - 70 * 0.618, 1)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_wave4_into_wave1_territory_is_not_a_motive():
    out = _detect(_candles_through([108, 100, 120, 110, 150, 115, 170, 150]))
    assert out["motives"] == []


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_bear_motive_mirrors():
    out = _detect(_candles_through([162, 170, 150, 160, 120, 135, 100, 115]))
    assert [m["dir"] for m in out["motives"]] == ["bear"]
