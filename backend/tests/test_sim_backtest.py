from app.trading.onchain import sim_backtest as sb


def _tok(sym, events, interval="1d"):
    return {"chain": "solana", "token": sym, "symbol": sym, "interval": interval, "events": events}


def test_trades_share_one_wallet_in_time_order():
    a = _tok("A", [{"t": 100, "side": "buy", "price": 1.0}, {"t": 300, "side": "sell", "price": 2.0, "ret": 1.0}])
    b = _tok("B", [{"t": 200, "side": "buy", "price": 1.0}, {"t": 400, "side": "sell", "price": 0.5, "ret": -0.5}])
    r = sb.replay([a, b], 10_000, 0.10)
    # A: 1000 -> 2000. B: 10% of 10000 (A at cost) = 1000 -> 500.
    assert r["equity"] == 10_500 and r["ret"] == 0.05
    assert r["trades"] == 2 and r["wins"] == 1 and not r["open"]
    assert [c["symbol"] for c in r["closed"]] == ["B", "A"]


def test_open_trade_marked_at_live_price_or_cost():
    a = _tok("A", [{"t": 100, "side": "buy", "price": 2.0}])
    r = sb.replay([a], 10_000, 0.10, marks={"solana:A": 3.0}, now=500)
    assert r["open"][0]["ret"] == 0.5 and r["equity"] == 10_500 and r["curve"][-1] == [500, 10_500]
    r = sb.replay([a], 10_000, 0.10)
    assert r["open"][0]["ret"] is None and r["equity"] == 10_000


def test_never_spends_more_than_free_cash():
    toks = [_tok(str(i), [{"t": 100 + i, "side": "buy", "price": 1.0}]) for i in range(12)]
    r = sb.replay(toks, 1000, 0.5)
    assert r["cash"] >= 0 and r["skipped"] > 0


def test_token_trades_follows_the_top_row():
    daily = {"interval": "8h", "rows": [{"trades": 2}], "best": {"events": [{"t": 1, "side": "buy"}]}}
    assert sb.token_trades({"daily": daily})[0] == "8h"
    no_daily = {"daily": {"rows": [{"trades": 0}]}, "best": {"label": "Trend EMA", "events": [{"t": 1, "side": "buy"}]}}
    assert sb.token_trades(no_daily)[0] == "5m"
    assert sb.token_trades(None) is None


def test_ledger_keeps_trades_the_window_slid_past(monkeypatch, tmp_path):
    monkeypatch.setattr(sb, "_FILE", tmp_path / "l.json")
    sb.reset_for_tests()
    first = {"best": {"label": "Trend EMA", "events": [{"t": 100, "side": "buy", "price": 1.0},
                                 {"t": 200, "side": "sell", "price": 2.0, "ret": 1.0}]}}
    later = {"best": {"label": "Trend EMA", "events": [{"t": 300, "side": "buy", "price": 2.0}]}}
    assert sb.remember("solana", "A", "A", first) and sb.remember("solana", "A", "A", later)
    assert not sb.remember("solana", "A", "A", later)
    sb.reset_for_tests()
    monkeypatch.setattr(sb, "_loaded", False)
    toks = sb.ledger_tokens()
    assert [e["t"] for e in toks[0]["events"]] == [100, 200, 300]
    r = sb.replay(toks, 10_000, 0.10)
    assert r["trades"] == 1 and len(r["open"]) == 1


def test_a_trade_moved_to_its_fill_candle_is_not_kept_twice(monkeypatch):
    sb.reset_for_tests()
    monkeypatch.setattr(sb, "_save", lambda: None)
    old = {"daily": {"interval": "1d", "rows": [{"trades": 1}], "best": {"events": [
        {"t": 86400, "side": "buy", "price": 1.0}]}}}
    sb.remember("solana", "T", "T", old, save=False)
    moved = {"daily": {"interval": "1d", "rows": [{"trades": 1}], "best": {"fills": "next_open", "events": [
        {"t": 2 * 86400, "side": "buy", "price": 1.1}]}}}
    sb.remember("solana", "T", "T", moved, save=False)
    assert [e["t"] for e in sb.ledger_tokens()[0]["events"]] == [2 * 86400]


def test_trades_of_an_earlier_timeframe_stop_where_the_new_one_starts():
    sb.reset_for_tests()
    row = {"chain": "solana", "token": "T", "interval": "1h", "events": {
        "100:buy": {"t": 100, "side": "buy", "iv": "5m", "lab": "Trend HF"}, "200:sell": {"t": 200, "side": "sell", "iv": "5m", "lab": "Trend HF"},
        "900:buy": {"t": 900, "side": "buy", "iv": "5m", "lab": "Trend HF"},
        "500:buy": {"t": 500, "side": "buy", "iv": "1h"}, "4000:sell": {"t": 4000, "side": "sell", "iv": "1h"}}}
    assert [e["t"] for e in sb._one_timeframe(row)] == [100, 200, 500, 4000]


def test_rsi_bb_5m_trades_enter_the_wallet_named_but_never_long_short(monkeypatch):
    sb.reset_for_tests()
    monkeypatch.setattr(sb, "_save", lambda: None)
    dip = {"best": {"label": "RSI+BB", "events": [{"t": 100, "side": "buy", "price": 0.1}]}}
    assert sb.remember("base", "ORDO", "ORDO", dip)
    ls = {"best": {"label": "RSI+BB L/S", "events": [{"t": 100, "side": "buy", "price": 0.1}]}}
    assert sb.token_trades(ls) is None
    sb._ledger["base:OLD"] = {"chain": "base", "token": "OLD", "interval": "5m",
                              "events": {"100:buy": {"t": 100, "side": "buy", "iv": "5m"}}}
    toks = {t["token"]: t for t in sb.ledger_tokens()}
    r = sb.replay([toks["ORDO"], toks["OLD"]], 10_000)
    assert sorted(p["strategy"] for p in r["open"]) == ["RSI+BB", "unknown"]


def test_the_top_row_best_is_a_long_spot_strategy_never_long_short():
    from app import main
    from app.trading.onchain import strategy_lab
    rows = [{"label": "RSI+BB L/S", "return_pct": 90.0, "trades": 3},
            {"label": "RSI+BB", "return_pct": 80.0, "trades": 3},
            {"label": "Trend HF", "return_pct": 5.0, "trades": 2}]
    assert strategy_lab._best({"rows": rows})["label"] == "RSI+BB"
    drawn = {"label": "RSI+BB", "events": [{"t": 1, "side": "buy"}, {"t": 2, "side": "sell"},
                                           {"t": 3, "side": "buy"}, {"t": 4, "side": "sell"},
                                           {"t": 5, "side": "buy"}, {"t": 6, "side": "sell"}]}
    assert main._lab_summary({"rows": rows, "best": drawn})["best"]["label"] == "RSI+BB"
    # no arrows for that row on the chart: not listed (seed-0242)
    assert main._lab_summary({"rows": rows})["best"] is None
    assert main._lab_summary({"rows": rows[:1]})["best"] is None


def test_top_hides_rows_with_no_spot_strategy_trade(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app import main
    from app.trading.onchain import strategy_lab
    ents = [{"chain": "base", "token_address": t, "symbol": t, "on_board": True} for t in ("A", "B", "C")]
    labs = {"A": {"rows": [{"label": "Trend EMA", "return_pct": 4.0, "trades": 2, "win_rate": 50.0}],
                  "best": {"label": "Trend EMA", "events": [{"t": 1, "side": "sell"}, {"t": 2, "side": "sell"}]}},
            "B": {"rows": [{"label": "RSI+BB L/S", "return_pct": 90.0, "trades": 3}]}, "C": None}
    monkeypatch.setattr(main, "_top_entries", lambda n=50, narrative="": [dict(e) for e in ents][:n])
    monkeypatch.setattr(strategy_lab, "get", lambda chain, token: labs[token])
    monkeypatch.setattr(strategy_lab, "status", lambda top: {})
    out = asyncio.run(main.onchain_top(n=15, user=SimpleNamespace(id="u")))
    assert [e["symbol"] for e in out["entries"]] == ["A", "C"]      # C: backtest still on its way


def test_old_unlabelled_5m_trades_get_the_strategy_that_made_them(monkeypatch):
    sb.reset_for_tests()
    monkeypatch.setattr(sb, "_save", lambda: None)
    sb._ledger["base:X"] = {"chain": "base", "token": "X", "interval": "5m", "events": {
        "1000:buy": {"t": 1000, "side": "buy", "iv": "5m"},
        "1600:sell": {"t": 1600, "side": "sell", "iv": "5m", "ret": 0.1},
        "5000:buy": {"t": 5000, "side": "buy", "iv": "5m", "lab": "not trend"},
        "7000:buy": {"t": 7000, "side": "buy", "iv": "5m"},
        "10:buy": {"t": 10, "side": "buy", "iv": "5m"}}}
    assert sb.needs_relabel("base", "X")
    n = sb.relabel("base", "X", {"Trend HF": {(1300, "buy"), (1600, "sell")}, "RSI+BB": {(5000, "buy")}},
                   window_from=500)
    ev = sb._ledger["base:X"]["events"]
    assert n == 4 and ev["1000:buy"]["lab"] == "Trend HF" and ev["1600:sell"]["lab"] == "Trend HF"
    assert ev["5000:buy"]["lab"] == "RSI+BB" and ev["7000:buy"]["lab"] == "not trend" and "lab" not in ev["10:buy"]
    assert [sb.strategy_of(e) for e in sb.ledger_tokens()[0]["events"]] == [
        "unknown", "Trend HF", "Trend HF", "RSI+BB", "RSI+BB"]
