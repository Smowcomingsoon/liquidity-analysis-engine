"""La memoria del radar. Lorenzo, dopo VISE: "dobbiamo implementare una
specie di coscienza, imparare dai segnali, da cosa effettivamente succede,
e migliorare il sistema"."""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.trading.onchain import legit, outcomes

T0 = 1_790_000_000.0
H = 3600.0
TRADED = {"buys_h1": 30.0, "sells_h1": 12.0, "sells_h24": 40.0, "buy_ratio": 0.71, "vol_h1": 9_000.0,
          "chg_h1": 12.0, "mcap": 80_000.0}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(outcomes, "_STORE_FILE", tmp_path / "outcomes.json")
    outcomes.reset_for_tests()
    yield
    outcomes.reset_for_tests()


def _item(token="Tok", *, security=90, github=80, cap=80_000, liq=20_000, chain="solana"):
    return {"chain": chain, "symbol": token.upper(), "name": token, "token_address": token, "pair_address": f"P{token}",
            "market_cap": cap, "liquidity_usd": liq, "age_hours": 12,
            "checks": [{"key": "security", "ok": True, "score": security},
                       {"key": "github", "ok": True, "score": github},
                       {"key": "gem", "ok": True}],
            "report": {"score": (security + github) // 2}}


def test_journal_keeps_the_strongest_decision_and_its_moment():
    assert outcomes.observe(_item(), "rejected", T0, "nome da meme", "radar", now=T0)
    assert outcomes.observe(_item(), "rejected", T0 + H, now=T0 + H) is None
    e = outcomes.observe(_item(), "proposed", T0 + 2 * H, now=T0 + 2 * H)
    assert e["decision"] == "proposed" and e["previous"] == "rejected" and e["at"] == T0 + 2 * H
    assert e["features"]["checks"] == {"security": {"ok": True, "score": 90}, "github": {"ok": True, "score": 80}}
    assert e["features"]["liq_mcap"] == 0.25
    assert outcomes.observe({**_item("x"), "pair_address": ""}, "listed") is None


def test_holder_concentration_is_learned_in_the_right_direction():
    for i in range(30):
        rug = i % 3 == 0
        it = _item(f"h{i}")
        it["checks"][0]["facts"] = {"top_holder_pct": 12.0 if rug else 3.0, "top10_pct": 30.0 if rug else 15.0}
        e = outcomes.observe(it, "listed", T0, now=T0)
        assert e["features"]["top_holder_pct"] in (12.0, 3.0)
        outcomes.apply_sample(e, 1.0, 20_000, T0)
        outcomes.apply_sample(e, 0.05 if rug else 1.05, 20_000, T0 + outcomes.HORIZON_S)
    sig = {s["key"]: s for s in outcomes.signals(T0 + outcomes.HORIZON_S)}
    assert sig["top_holder_pct"]["rug_auc"] == 1.0 and sig["top_holder_pct"]["verdict"] == "prevede i rug"


def test_liquidity_pulled_is_a_rug_even_if_price_holds():
    e = outcomes.observe(_item(), "listed", T0, now=T0)
    outcomes.apply_sample(e, 1.0, 20_000, T0)
    outcomes.apply_sample(e, 1.1, 15_000, T0 + H)
    assert outcomes.outcome(e, T0 + H) is None
    outcomes.apply_sample(e, 0.9, 3_000, T0 + 2 * H)
    assert e["rug"] and e["closed"] and "liquidità" in e["rug_why"]
    assert outcomes.outcome(e) == "rug" and outcomes.rug_label(e) is True


def test_price_collapse_and_vanished_pool_are_rugs():
    a = outcomes.observe(_item("a"), "listed", T0, now=T0)
    outcomes.apply_sample(a, 1.0, 20_000, T0)
    outcomes.apply_sample(a, 0.07, 19_000, T0 + 600)     # VISE: -93% in pochi minuti
    assert a["rug"] and a["rug_why"].startswith("prezzo")
    b = outcomes.observe(_item("b"), "listed", T0, now=T0)
    outcomes.apply_sample(b, 1.0, 20_000, T0)
    for i in range(outcomes.MISSING_TICKS):
        outcomes.apply_sample(b, None, None, T0 + (i + 1) * 600)
    assert b["rug_why"] == "pool sparito"


def test_good_is_known_early_non_rug_after_three_days_flat_at_the_horizon():
    e = outcomes.observe(_item(), "proposed", T0, now=T0)
    outcomes.apply_sample(e, 1.0, 20_000, T0)
    outcomes.apply_sample(e, 1.2, 20_000, T0 + H)
    assert outcomes.rug_label(e, T0 + H) is None
    assert outcomes.rug_label(e, T0 + outcomes.RUG_FINAL_S) is False
    outcomes.apply_sample(e, 1.6, 20_000, T0 + 2 * H)
    assert outcomes.outcome(e, T0 + 2 * H) == "finto"      # nessuna vendita vista: prezzo finto
    outcomes.apply_sample(e, 1.7, 20_000, T0 + 3 * H, TRADED)
    assert outcomes.outcome(e, T0 + 3 * H) == "buono"
    f = outcomes.observe(_item("f"), "proposed", T0, now=T0)
    outcomes.apply_sample(f, 1.0, 20_000, T0)
    outcomes.apply_sample(f, 0.45, 20_000, T0 + outcomes.HORIZON_S)
    assert f["closed"] and outcomes.outcome(f) == "crollo"


def test_auc_matches_the_definition():
    assert outcomes.auc([3, 4, 1, 2], [True, True, False, False]) == 1.0
    assert outcomes.auc([1, 2, 3, 4], [True, True, False, False]) == 0.0
    assert outcomes.auc([1, 1], [True, False]) == 0.5
    assert outcomes.auc([1, 2], [True, True]) is None


def _history(n=80, informative=True, seed=0):
    """n token passati: con `informative` i rug hanno la sicurezza bassa."""
    rng = np.random.default_rng(seed)
    for i in range(n):
        rug = i % 3 == 0
        sec = (rng.uniform(20, 55) if rug else rng.uniform(60, 100)) if informative else rng.uniform(20, 100)
        e = outcomes.observe(_item(f"t{i}", security=int(sec), github=int(rng.uniform(40, 100))),
                             "listed", T0, now=T0)
        outcomes.apply_sample(e, 1.0, 20_000, T0)
        outcomes.apply_sample(e, 0.05 if rug else 1.05, 20_000, T0 + outcomes.HORIZON_S)


def test_model_waits_for_enough_outcomes():
    _history(20)
    m = outcomes.train(T0 + outcomes.HORIZON_S)
    assert not m["active"] and "servono" in m["why"]
    assert outcomes.judge(_item(), [], None) is None


def test_model_enters_only_when_it_predicts_out_of_sample():
    _history(90, informative=False)
    assert not outcomes.train(T0 + outcomes.HORIZON_S)["active"]
    outcomes.reset_for_tests()
    _history(90)
    m = outcomes.train(T0 + outcomes.HORIZON_S)
    assert m["active"] and m["cv_auc"] >= outcomes.MODEL_MIN_AUC
    bad = outcomes.judge(_item("new", security=25), _item("new", security=25)["checks"], {"score": 50})
    good = outcomes.judge(_item("new", security=95), _item("new", security=95)["checks"], {"score": 90})
    assert bad["ok"] is False and good["ok"] is True and bad["key"] == "memory"
    top = outcomes.signals(T0 + outcomes.HORIZON_S)[0]
    assert top["key"] == "security" and top["rug_auc"] > 0.9 and top["verdict"] == "prevede i rug"


def test_lessons_split_passed_and_rejected_and_list_the_misses():
    e = outcomes.observe(_item("vise"), "proposed", T0, now=T0)
    outcomes.apply_sample(e, 1.0, 20_000, T0)
    outcomes.apply_sample(e, 0.07, 1_000, T0 + 600)
    r = outcomes.observe(_item("moon"), "rejected", T0, "GitHub: nessun commit", now=T0)
    outcomes.apply_sample(r, 1.0, 20_000, T0)
    outcomes.apply_sample(r, 3.0, 60_000, T0 + H, TRADED)
    out = outcomes.lessons(T0 + H)
    assert out["passed"]["rug"] == 1 and out["rejected"]["buono"] == 1
    assert [s["symbol"] for s in out["slipped"]] == ["VISE"]
    assert [m["symbol"] for m in out["missed"]] == ["MOON"]
    assert out["notes"][0].startswith("Passati dal filtro: 100% rug")


def test_late_entries_take_their_entry_from_the_candles():
    e = outcomes.observe(_item(), "rejected", T0, now=T0 + 10 * H)
    assert e["late"]
    idx = pd.date_range(pd.Timestamp(T0 - 1800, unit="s", tz="UTC"), periods=8, freq="15min")
    df = pd.DataFrame({"open": [5, 2, 2.1, 2, 1.5, 0.2, 0.1, 0.1], "high": [5, 2.5, 2.2, 2, 1.5, 0.3, 0.1, 0.1],
                       "low": [5, 1.9, 2, 1.8, 0.3, 0.1, 0.1, 0.1], "close": [2, 2.1, 2, 1.5, 0.2, 0.1, 0.1, 0.1]},
                      index=idx)
    outcomes.replay_candles(e, df)
    assert e["entry"] == {"price": 2.1, "liq": None, "at": T0, "from": "candles"}
    assert e["rug"] and round(e["min_ret"], 2) == -0.95
    assert outcomes.outcome(e) == "rug"


def test_collect_reads_radar_and_listings(monkeypatch):
    from app.trading.onchain import listings, radar
    monkeypatch.setattr(radar, "snapshot", lambda: {"proposals": [{**_item("p"), "found_at": T0}],
                                                    "tested": [_item("t")]})
    monkeypatch.setattr(listings, "recent", lambda: [{**_item("l"), "found_at": T0}])
    monkeypatch.setattr(listings, "rejected", lambda: [{**_item("r"), "at": T0, "reason": "meme"}])
    assert outcomes.collect(T0 + 60) == 4
    assert {e["decision"] for e in outcomes._journal.values()} == {"proposed", "tested", "listed", "rejected"}
    assert outcomes.collect(T0 + 120) == 0


def test_memory_check_blocks_only_with_the_gate_on(monkeypatch):
    check = {"key": "memory", "label": "Memoria", "ok": False, "detail": "somiglia ai rug passati: rischio 80%",
             "score": 20}
    monkeypatch.setattr(outcomes, "judge", lambda *a: dict(check))

    async def fake(*a, **k):
        return None

    async def site(*a, **k):
        return {"ok": True, "title": "Proj", "text": "a real project " * 50, "final_url": "https://p.io"}

    async def gh(*a, **k):
        return legit._check("github", "GitHub", True, "ok", 80), {"repo": "x"}

    monkeypatch.setattr(legit, "rugcheck", lambda *a: _const({"lpLockedPct": 100, "risks": []}))
    monkeypatch.setattr(legit, "goplus_solana", lambda *a: _const({"mintable": {"status": "0"}}))
    monkeypatch.setattr(legit, "fetch_site", site)
    monkeypatch.setattr(legit, "check_github", gh)
    monkeypatch.setattr(legit, "ai_review", fake)
    token = {"chain": "solana", "symbol": "PROJ", "name": "Project", "token_address": "Tok", "market_cap": 50_000}
    links = {"websites": ["https://p.io"], "socials": [{"type": "twitter", "url": "https://x.com/p"}]}

    def run():
        return asyncio.run(legit.assess(token, links, None))

    monkeypatch.setattr(settings, "onchain_memory_gate", False)
    off = run()
    assert off["status"] == "ok" and off["checks"][-1]["key"] == "memory"
    monkeypatch.setattr(settings, "onchain_memory_gate", True)
    on = run()
    assert on["status"] == "fail" and on["reason"].startswith("Memoria")


async def _value(v):
    return v


def _const(v):
    return _value(v)


def test_only_rugs_we_let_through_go_on_the_wall():
    for token, decision in (("in", "proposed"), ("out", "rejected")):
        e = outcomes.observe(_item(token), decision, T0, now=T0)
        outcomes.apply_sample(e, 1.0, 20_000, T0)
        outcomes.apply_sample(e, 0.05, 20_000, T0 + 600)
    wall = [e["symbol"] for e in outcomes.shame()]
    assert wall == ["VISE", "IN"]
    assert outcomes.without_shamed([_item("in"), _item("ok")]) == [_item("ok")]


def test_a_pump_nobody_can_sell_is_fake_not_a_missed_winner():
    """JIRACHI "+6877%": 300 acquisti, zero vendite."""
    e = outcomes.observe(_item("jirachi"), "rejected", T0, "nome da meme coin", now=T0)
    only_buys = {**TRADED, "sells_h1": 0.0, "sells_h24": 0.0, "buy_ratio": 1.0}
    outcomes.apply_sample(e, 1.0, 20_000, T0, only_buys)
    outcomes.apply_sample(e, 69.0, 20_000, T0 + H, only_buys)
    assert outcomes.outcome(e, T0 + H) == "finto"
    out = outcomes.lessons(T0 + H)
    assert out["missed"] == [] and out["study"]["fakes"][0]["symbol"] == "JIRACHI"
    thin = outcomes.observe(_item("thin"), "rejected", T0, "nome da meme coin", now=T0)
    outcomes.apply_sample(thin, 1.0, 9_000, T0, TRADED)
    outcomes.apply_sample(thin, 5.0, 4_000, T0 + H, TRADED)   # al massimo non c'era da uscire
    assert outcomes.outcome(thin, T0 + H) == "finto"


def _rejected(token, reason, *, up, market=TRADED, liq=20_000, now=T0, close=False):
    it = _item(token)
    it["checks"] = [{"key": "name", "ok": False}] if "meme" in reason else it["checks"]
    e = outcomes.observe(it, "rejected", now, reason, now=now)
    outcomes.apply_sample(e, 1.0, liq, now, market)
    outcomes.apply_sample(e, 1.0 + up, liq, now + H, market)
    if close:
        outcomes.apply_sample(e, 1.0 + up, liq, now + outcomes.HORIZON_S, market)
    return e


def test_study_groups_the_misses_by_why_we_rejected_them():
    _rejected("m1", "nome da meme coin", up=5.0, close=True)
    _rejected("m2", "nome da meme coin", up=0.1, close=True)
    _rejected("s1", "RugCheck: segnalato come rug", up=0.0, close=True)
    out = outcomes.study(T0 + H)
    by = {r["key"]: r for r in out["by_reason"]}
    assert by["name"]["n"] == 2 and by["name"]["buono"] == 1 and by["name"]["win_rate"] == 0.5
    assert by["security"]["buono"] == 0
    assert [w["symbol"] for w in out["winners"]] == ["M1"]
    assert {t["key"] for t in out["traits"]} >= {"liq", "buys_h1"}


def test_old_entries_get_their_reject_reason_from_the_text():
    e = outcomes.observe(_item("old"), "rejected", T0, "GitHub: nessun GitHub", now=T0)
    e["features"].pop("failed")
    assert outcomes.failed_key(e) == "github"


def test_meme_lane_takes_soft_rejects_alive_sellable_and_safe():
    now = T0 + H
    good = _rejected("pepe", "nome da meme coin", up=0.3, liq=50_000)
    copy = _rejected("pepe2", "nome da meme coin", up=0.3)
    copy["symbol"] = "PEPE"                                   # stesso ticker, meno liquidita'
    _rejected("unsafe", "RugCheck: segnalato come rug", up=0.3)
    _rejected("nosell", "nome da meme coin", up=0.3, market={**TRADED, "sells_h1": 0.0, "sells_h24": 0.0})
    _rejected("dry", "nome da meme coin", up=0.3, liq=3_000)
    _rejected("old", "nome da meme coin", up=0.3, now=T0 - 3 * 86400)
    board = outcomes.meme_board(now)
    assert [i["symbol"] for i in board["items"]] == ["PEPE"]
    assert "azzardo" in board["warning"] and board["items"][0]["failed"] == "name"
    assert board["items"][0]["token_address"] == "pepe"
    good["safety"] = {"ok": False, "detail": "un wallet ha il 40%"}
    assert [i["token_address"] for i in outcomes.meme_board(now)["items"]] == ["pepe2"]


def test_meme_safety_uses_rugcheck_and_holder_concentration(monkeypatch):
    now = T0 + H
    _rejected("pepe", "nome da meme coin", up=0.3)
    _rejected("whale", "nome da meme coin", up=0.3)

    async def rugcheck(mint, client):
        holders = [{"owner": "w", "pct": 40.0}] if mint == "whale" else [{"owner": "w", "pct": 4.0}]
        return {"lpLockedPct": 100, "risks": [], "topHolders": holders}
    monkeypatch.setattr(legit, "rugcheck", rugcheck)
    assert asyncio.run(outcomes._meme_safety(None, now)) == 2
    assert [i["symbol"] for i in outcomes.meme_board(now)["items"]] == ["PEPE"]


def test_winner_model_ranks_the_lane_once_it_predicts():
    for i in range(90):
        win = i % 3 == 0
        m = {**TRADED, "buys_h1": 200.0 if win else 10.0, "vol_h1": 90_000.0 if win else 1_000.0}
        _rejected(f"w{i}", "nome da meme coin", up=2.0 if win else 0.05, market=m, close=True)
    model = outcomes.train_winners(T0 + outcomes.HORIZON_S)
    assert model["active"] and model["cv_auc"] >= outcomes.WIN_MIN_AUC
    hot = outcomes.win_chance({**_item("x"), **outcomes.features(_item("x")), "buys_h1": 200.0, "vol_h1": 90_000.0})
    cold = outcomes.win_chance({**outcomes.features(_item("y")), "buys_h1": 10.0, "vol_h1": 1_000.0})
    assert hot > cold
