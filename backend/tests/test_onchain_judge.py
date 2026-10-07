"""Il giudice di DeepSeek: parse, dossier, cache e tetto giornaliero."""
import asyncio
import json
import time

from app.trading.onchain import judge


def _entry(**item) -> dict:
    return {"chain": "solana", "token_address": "ABCmint", "symbol": "ABC", "name": "Abc", "market_cap": 50_000,
            "liquidity_usd": 20_000, "launched_at": time.time() - 30 * 86400,
            "pillars": {"github": {"score": 70, "facts": {"commits": "10"}, "flags": []},
                        "site": {"score": 60, "facts": {"summary": "we are the best"}, "flags": []},
                        "tokenomics": {"score": 65, "facts": {}, "flags": []}}, **item}


def test_parse_clamps_and_rejects():
    out = judge.parse(json.dumps({"score": 150, "verdict": "gem", "confidence": 2,
                                  "problems": ["a"], "strengths": ["b"], "admit_reason": "x"}))
    assert out["score"] == 100 and out["confidence"] == 1.0
    assert judge.parse(json.dumps({"score": 50, "verdict": "maybe"})) is None
    assert judge.parse("not json") is None


def test_dossier_wraps_project_text():
    d = judge.dossier(_entry(), "site too weak (20)")
    assert "<project_text>" in d and "</project_text>" in d
    assert "we are the best" in d
    assert "site too weak (20)" in d


def test_judge_caches_and_respects_daily_max(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_FILE", str(tmp_path / "judge.json"))
    judge.reset_for_tests()
    calls = []

    async def ask(messages, **kw):
        calls.append(messages)
        return json.dumps({"score": 85, "verdict": "gem", "confidence": 0.8, "problems": [], "strengths": [],
                           "admit_reason": ""})

    e = _entry()
    out = asyncio.run(judge.judge(e, "site too weak (20)", ask=ask))
    assert out["score"] == 85 and len(calls) == 1
    again = asyncio.run(judge.judge(e, "site too weak (20)", ask=ask))
    assert again["score"] == 85 and len(calls) == 1

    judge.reset_for_tests()
    monkeypatch.setattr(judge, "DAILY_MAX", 0)
    assert asyncio.run(judge.judge(e, "site too weak (20)", ask=ask)) is None
    assert len(calls) == 1


def test_import_judge_first_with_a_vetoed_board_entry(tmp_path):
    """main.py imports judge before leaderboard; leaderboard ranks its saved
    entries at import time and reads judge.VETO_MIN_CONF (deploy of #272)."""
    import subprocess
    import sys
    from pathlib import Path
    e = _entry(source="coingecko", overall={"score": 70},
               judge={"verdict": "avoid", "confidence": 0.9, "problems": ["x"]})
    (tmp_path / "onchain_leaderboard.json").write_text(json.dumps(
        {"entries": {"solana:ABCmint": e}, "rules": 99, "alerted": []}), encoding="utf-8")
    code = ("from pathlib import Path; from app.trading.onchain import storage; "
            f"storage.DATA_DIR = Path({str(tmp_path)!r}); "
            "from app.trading.onchain import judge, leaderboard; "
            "leaderboard.usage_gate = lambda e, cap_floor=True: ''; "
            "assert leaderboard.qualifies(leaderboard._entries['solana:ABCmint']).startswith('DeepSeek')")
    out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]


def _counting_judge(tmp_path, monkeypatch):
    monkeypatch.setattr(judge, "_FILE", str(tmp_path / "judge.json"))
    monkeypatch.setattr("app.trading.onchain.x_signal.lookup", lambda e, now: _none())
    monkeypatch.setattr("app.trading.onchain.telegram_public.lookup", lambda e, now: _none())
    judge.reset_for_tests()
    calls = []

    async def ask(messages, **kw):
        calls.append(kw.get("purpose"))
        return json.dumps({"score": 70, "verdict": "watch", "confidence": 0.6, "problems": [], "strengths": [],
                           "admit_reason": ""})
    return calls, ask


async def _none():
    return None


def test_a_liquidity_drop_invalidates_the_reading_at_once(tmp_path, monkeypatch):
    """Issue #358 I04: a change in liquidity updates the risk without waiting three days."""
    calls, ask = _counting_judge(tmp_path, monkeypatch)
    t0 = 1_800_000_000.0
    asyncio.run(judge.judge(_entry(), "", t0, ask=ask))
    asyncio.run(judge.judge(_entry(liquidity_usd=19_000), "", t0 + 7 * 3600, ask=ask))   # noise: kept
    assert calls == ["judge/first"]
    out = asyncio.run(judge.judge(_entry(liquidity_usd=9_000), "", t0 + 7 * 3600, ask=ask))
    assert calls == ["judge/first", "judge/liquidity"] and out["why"] == "liquidity -55%"
    # the new reading is the new reference: the same level does not re-trigger
    asyncio.run(judge.judge(_entry(liquidity_usd=9_000), "", t0 + 20 * 3600, ask=ask))
    assert len(calls) == 2


def test_an_invalidation_waits_the_cooldown(tmp_path, monkeypatch):
    calls, ask = _counting_judge(tmp_path, monkeypatch)
    t0 = 1_800_000_000.0
    asyncio.run(judge.judge(_entry(), "", t0, ask=ask))
    asyncio.run(judge.judge(_entry(liquidity_usd=5_000), "", t0 + 3600, ask=ask))
    assert len(calls) == 1
    asyncio.run(judge.judge(_entry(liquidity_usd=5_000), "", t0 + judge.RECHECK_GAP_S, ask=ask))
    assert len(calls) == 2


def test_unchanged_facts_are_not_read_again_until_the_safety_refresh(tmp_path, monkeypatch):
    calls, ask = _counting_judge(tmp_path, monkeypatch)
    t0 = 1_800_000_000.0
    asyncio.run(judge.judge(_entry(), "site too weak (20)", t0, ask=ask))
    asyncio.run(judge.judge(_entry(market_cap=60_000), "site too weak (21)", t0 + judge.TTL_S + 60, ask=ask))
    assert calls == ["judge/first"]
    asyncio.run(judge.judge(_entry(), "", t0 + judge.TTL_S + 120, ask=ask))            # rules changed
    assert calls[-1] == "judge/rules"
    asyncio.run(judge.judge(_entry(), "", t0 + judge.TTL_S + 120 + judge.MAX_AGE_S, ask=ask))
    assert calls[-1] == "judge/periodic"


def test_pillar_and_cap_moves_invalidate():
    old = judge.judged_facts(_entry(), "")
    assert judge.what_changed(old, judge.judged_facts(_entry(), "")) == ""
    assert "cap" in judge.what_changed(old, judge.judged_facts(_entry(market_cap=200_000), ""))
    moved = _entry()
    moved["pillars"]["github"]["score"] = 40
    assert judge.what_changed(old, judge.judged_facts(moved, "")) == "github pillar 70->40"
    assert judge.what_changed(None, old) == ""


def test_out_of_budget_keeps_the_old_reading_marked_stale(tmp_path, monkeypatch):
    calls, ask = _counting_judge(tmp_path, monkeypatch)
    t0 = 1_800_000_000.0
    asyncio.run(judge.judge(_entry(), "", t0, ask=ask))
    monkeypatch.setattr(judge, "DAILY_MAX", 0)
    out = asyncio.run(judge.judge(_entry(liquidity_usd=1_000), "", t0 + judge.RECHECK_GAP_S, ask=ask))
    assert out["stale"] == "liquidity -95%" and out["score"] == 70 and len(calls) == 1


def test_the_kept_reading_is_refreshed_before_the_board_stops_counting_it():
    from app.trading.onchain import leaderboard
    assert judge.TTL_S < judge.MAX_AGE_S
    assert leaderboard._fresh({"at": 1}, judge.TTL_S, judge.MAX_AGE_S)


def test_wants_reading_when_prompt_changed():
    e = {"chain": "solana", "token_address": "OLD", "market_cap": 1e5, "liquidity_usd": 5e4, "pillars": {}}
    assert judge.wants_reading(e, "")
    judge._cache["solana:OLD"] = {"v": judge.PROMPT_VERSION - 1, "at": time.time(), "facts": judge.judged_facts(e, "")}
    assert judge.wants_reading(e, "")
    judge._cache["solana:OLD"]["v"] = judge.PROMPT_VERSION
    assert not judge.wants_reading(e, "")
    judge._cache.pop("solana:OLD")

def test_review_brief_carries_the_telegram_numbers():
    e = _entry(judge={"verdict": "watch", "score": 60, "problems": [], "x": None,
                      "tg": {"handle": "abc", "subs": 500}})
    brief = judge.review_brief([e])
    line = json.loads(brief.splitlines()[1])
    assert line["judge"]["tg"] == {"handle": "abc", "subs": 500}
    assert '"subs": 500' in brief
