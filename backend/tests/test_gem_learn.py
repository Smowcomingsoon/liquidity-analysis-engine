"""Decision receipts, the rejected sample, the learned-weights gate, judge drift,
text-driven scores and the pilot's reasons (30/09, "applica tutto")."""
import json

import pytest

from app.trading.onchain import gem_learn, gem_track, judge
from app.trading.onchain import leaderboard as lb

D = 86400
T0 = 1_750_000_000.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setattr(gem_track, "_STORE_FILE", tmp_path / "track.json")
    monkeypatch.setattr(gem_learn, "CANDIDATE_FILE", tmp_path / "cand.json")
    monkeypatch.setattr(gem_learn, "APPROVED_FILE", tmp_path / "approved.json")
    monkeypatch.setattr(gem_learn, "REPORT_FILE", tmp_path / "report.json")
    gem_track.reset_for_tests()
    gem_learn.reset_for_tests()
    yield
    gem_track.reset_for_tests()
    gem_learn.reset_for_tests()


def _e(token, price="1.0"):
    return {"chain": "solana", "token_address": token, "symbol": token.upper(), "price_usd": price}


def _rejected_token(sampled: bool) -> str:
    return next(f"r{i}" for i in range(500) if gem_track.sampled("solana", f"r{i}") == sampled)


def test_receipt_is_frozen_and_hash_of_its_content():
    parts = {"quality": 70, "pillar_site": 60, "chain": "solana"}
    assert gem_track.enter(_e("a"), 72.0, 3, T0, parts, "v1")
    t = gem_track._track["solana:a"]
    assert t["decision"] == "ranked" and t["parts"] == parts
    assert t["receipt"] == gem_track.receipt_of(t) and len(t["receipt"]) == 64
    assert gem_track.receipt_for("solana", "a") == t["receipt"]
    gem_track.sample("solana", "a", 2.5, T0 + 2 * D)
    assert gem_track._track["solana:a"]["receipt"] == gem_track.receipt_of(t)   # returns do not change it


def test_studied_and_sampled_rejected_are_followed_as_separate_cases():
    assert gem_track.decide(_e("s"), "studied", "tokenomics 50", 55.0, {"quality": 50}, "v1", T0)
    assert gem_track.decide(_e(_rejected_token(True)), "rejected", "rug", None, {}, "v1", T0)
    assert not gem_track.decide(_e(_rejected_token(False)), "rejected", "rug", None, {}, "v1", T0)
    # studied first, ranked later: two cases, each with its own entry price
    assert gem_track.enter(_e("s", "2.0"), 80.0, 1, T0 + D)
    assert {"solana:s#studied", "solana:s"} <= set(gem_track._track)
    gem_track.sample("solana", "s", 3.0, T0 + 8 * D)
    assert gem_track._track["solana:s#studied"]["returns"]["7"] == pytest.approx(2.0)
    assert gem_track._track["solana:s"]["returns"]["7"] == pytest.approx(0.5)
    assert set(gem_track.open_keys()) == {"solana:s", f"solana:{_rejected_token(True)}"}
    st = gem_track.stats()
    assert st["cases"] == {"ranked": 1, "studied": 1, "rejected": 1}
    week = next(h for h in st["horizons"] if h["days"] == 7)
    assert week["studied"]["n"] == 1 and week["all"]["n"] == 1


def test_short_horizons_extremes_first_hit_and_market_excess():
    gem_track._bench["SOLUSD"] = (100.0, T0)
    assert gem_track.enter(_e("x"), 60.0, 2, T0)
    gem_track.sample("solana", "x", 0.8, T0 + 0.5 * D)
    gem_track._bench["SOLUSD"] = (110.0, T0 + 1.1 * D)
    gem_track.sample("solana", "x", 1.3, T0 + 1.1 * D)
    gem_track.sample("solana", "x", 2.2, T0 + 2 * D)
    gem_track.sample("solana", "x", 0.4, T0 + 2.5 * D)
    t = gem_track._track["solana:x"]
    assert t["returns"]["1"] == pytest.approx(0.3) and t["bench"]["1"] == pytest.approx(0.1)
    assert gem_track.excess(t, 1) == pytest.approx(0.2)
    assert t["hi"] == pytest.approx(1.2) and t["lo"] == pytest.approx(-0.6)
    assert t["first"] == "double"          # +100% came before -50%


def _case(i, good):
    q = 80 if good else 40
    return {"at": T0 + i, "score": q, "first": "double" if good else "halve", "returns": {"7": 1.5 if good else -0.6},
            "parts": {"quality": q + (i % 5), "pillar_site": 50 + (i % 7), "judge": q, "mcap": 1e6, "chain": "solana"}}


def test_report_ranks_inputs_and_train_waits_for_enough_cases():
    cases = [_case(i, i % 3 == 0) for i in range(30)]
    rep = gem_learn.report(cases)
    week = next(h for h in rep["horizons"] if h["days"] == 7)
    assert week["n"] == 30 and week["inputs"][0]["ic"] > 0.8
    assert not gem_learn.train(cases)["ready"]          # 30 < MIN_N


def test_learned_model_changes_nothing_until_approved(tmp_path):
    cases = [_case(i, i % 3 == 0) for i in range(60)]
    cand = gem_learn.train(cases)
    assert cand["weights"] and cand["n"] == 60
    e = {"chain": "solana", "token_address": "z", "overall": {"score": 70}, "similarity": {"score": 0},
         "pillars": {"tokenomics": {"score": 70}}}
    hand = lb.final_score(e)
    body = gem_learn._model_body(cand)
    gem_learn.APPROVED_FILE.write_text(json.dumps({"model": body, "hash": "bad", "approved": True, "reviewer": "L"}))
    assert gem_learn.approved() is None and lb.final_score(e) == hand
    gem_learn.APPROVED_FILE.write_text(json.dumps({"model": body, "hash": gem_learn.fingerprint(body),
                                                   "approved": True, "reviewer": "Lorenzo"}))
    assert gem_learn.approved() is not None and gem_learn.approved_hash()
    assert lb.final_score(e) != hand


def test_judge_drift_only_on_same_facts_and_prompt():
    seen = {"liq": 1e5, "cap": 1e6, "rules": "", "pillars": {"github": 60, "site": 60, "tokenomics": 60}}
    old = {"v": judge.PROMPT_VERSION, "facts": seen, "score": 80, "verdict": "gem", "at": T0}
    assert judge.drifted(old, {"score": 75, "verdict": "gem"}, seen) is None
    d = judge.drifted(old, {"score": 30, "verdict": "avoid"}, seen)
    assert d["from_score"] == 80 and d["to_verdict"] == "avoid"
    moved = {**seen, "liq": 1e4}
    assert judge.drifted(old, {"score": 30, "verdict": "avoid"}, moved) is None
    assert judge.drifted({**old, "v": "old"}, {"score": 30, "verdict": "avoid"}, seen) is None


def test_score_moved_by_site_text_alone_is_flagged_and_cleared():
    e = {"chain": "solana", "token_address": "t", "pillars": {"github": {"score": 60}, "site": {"score": 40},
                                                              "tokenomics": {"score": 60}},
         "overall": {"score": 50}, "similarity": {"score": 0}}
    old = {n: e["pillars"][n]["score"] for n in ("github", "site", "tokenomics")}
    before = 50.0
    e["pillars"]["site"] = {"score": 90}
    e["overall"] = {"score": 70}
    lb._text_moved(e, before, old, T0)
    assert e["text_moved"]["from"] == 50.0 and e["text_moved"]["site_to"] == 90
    old = {n: e["pillars"][n]["score"] for n in ("github", "site", "tokenomics")}
    e["pillars"]["github"] = {"score": 80}
    lb._text_moved(e, lb.final_score(e), old, T0 + 1)
    assert "text_moved" not in e


def test_trade_carries_report_and_receipt():
    from app.trading.onchain import autotrader
    gem_track.enter(_e("p"), 70.0, 1, T0, {"quality": 70}, "v1")
    why = autotrader._why({"chain": "solana", "token_address": "p", "quant_system": {"report_hash": "abc"}})
    assert why == {"report_hash": "abc", "gem_receipt": gem_track._track["solana:p"]["receipt"]}
