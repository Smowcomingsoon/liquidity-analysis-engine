"""Il radar: token giovani su Solana e Robinhood provati su tutte le
strategie. Lorenzo: "c'e' questo ticker su Solana o su rete Robinhood che
performerebbe bene con questo tipo di strategia"."""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import archive, auth, main, watchlist as watchlist_store
from app.analysis import btc_regime
from app.analysis.strategies import STRATEGY_REGISTRY
from app.models import StrategySignal
from app.trading.onchain import radar


def _pool(symbol="NEW", *, liq=20_000, mcap=80_000, age_days=5, token="Tok", pair="Pair", site=None):
    return radar.Pool(
        chain="solana", symbol=symbol, name=f"{symbol} / SOL", token_address=token, pair_address=pair,
        dex="raydium", liquidity_usd=liq, market_cap=mcap,
        created_ms=int((time.time() - age_days * 86400) * 1000), site=site,
    )


def test_filter_keeps_young_liquid_low_cap_tokens_only():
    picked, passing = radar.pick_candidates([
        _pool("OK"),
        _pool("OLD", age_days=30, token="t2"),
        _pool("THIN", liq=5_000, token="t3"),
        _pool("BIG", mcap=2_500_000, liq=400_000, token="t4"),     # oltre i $2M
        _pool("SHALLOW", liq=9_000, mcap=95_000, token="t5"),     # 9% della cap
        _pool("SOL", token="t6"),
        _pool("NOCAP", mcap=None, token="t7"),
    ])
    assert [p.symbol for p in picked] == ["OK"]
    assert passing == 1


def test_one_pool_per_ticker_the_most_liquid():
    picked, _ = radar.pick_candidates([
        _pool("CAT", liq=10_000, token="copy", pair="p1"),
        _pool("cat", liq=30_000, token="real", pair="p2"),
    ])
    assert len(picked) == 1 and picked[0].token_address == "real"


def test_gecko_pool_keeps_address_case():
    pool = radar.parse_pool("solana", {
        "attributes": {"address": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "name": "WIF / SOL",
                       "pool_created_at": "2026-09-20T10:00:00Z", "reserve_in_usd": "120000",
                       "market_cap_usd": None, "fdv_usd": "900000"},
        "relationships": {"base_token": {"data": {"id": "solana_EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"}},
                          "dex": {"data": {"id": "raydium"}}},
    })
    assert pool.pair_address == "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
    assert pool.token_address == "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
    assert pool.symbol == "WIF" and pool.market_cap == 900_000


def test_interval_needs_finer_candles_for_newborns():
    assert radar.interval_for(30) == "5m"
    assert radar.interval_for(10 * 24) == "15m"


@pytest.mark.parametrize("age_s, interval", [
    (20 * 60, "1m"), (3 * 3600, "1m"), (13 * 3600, "5m"), (2 * 86400, "15m"),
    (7 * 86400, "1h"), (25 * 86400, "4h")])
def test_timeframe_follows_the_token_history(age_s, interval):
    assert radar.auto_interval(age_s) == interval


def test_every_registry_strategy_plus_each_ready_archive_config():
    entries = [
        {"strategy_name": "Lorenzo (trend-follower)", "params": {"len": 50}, "symbol": "BTCUSDT", "interval": "1d"},
        {"strategy_name": "Lorenzo (trend-follower)", "params": {"len": 50}, "symbol": "ETHUSDT", "interval": "1d"},
        {"strategy_name": "Sconosciuta", "params": {"x": 1}},
        {"strategy_name": "Gabriele (RSI+BB reversal)", "params": {}},
    ]
    configs = radar.strategy_configs(entries)
    assert [c["strategy_name"] for c in configs[: len(STRATEGY_REGISTRY)]] == list(STRATEGY_REGISTRY)
    archived = configs[len(STRATEGY_REGISTRY):]
    assert len(archived) == 1 and archived[0]["params"] == {"len": 50} and archived[0]["source"] == "archivio"


def test_ready_entries_skips_unfinished_snapshots(monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "_STORE_FILE", tmp_path / "a.json")
    monkeypatch.setattr(archive, "_entries", [{"state": "pronto"}, {"state": "in corso"}])
    assert archive.ready_entries() == [{"state": "pronto"}]


def _sig(t, kind):
    return StrategySignal(time=t, type=kind, price=1.0, label="")


def test_chart_draws_buys_and_sells_only():
    events = [_sig(1, "entry_short"), _sig(2, "entry_long"), _sig(3, "entry_long"),
              _sig(4, "exit_long"), _sig(5, "exit_short"), _sig(6, "entry_long"), _sig(7, "exit_long")]
    assert [(e["time"], e["kind"]) for e in radar.long_events(events)] == [
        (2, "in"), (4, "out"), (6, "in"), (7, "out")]


def test_marker_sits_on_the_candle_where_the_buy_fills():
    """Lorenzo: "non appare nel grafico gli ingressi in modo preciso". Il
    backtest compra all'apertura della candela dopo il segnale: la freccia va li'."""
    idx = pd.date_range("2026-09-24", periods=4, freq="1min", tz="UTC")
    df = pd.DataFrame({"open": [1.0, 2.0, 3.0, 4.0], "high": 5.0, "low": 0.5, "close": [1.5, 2.5, 3.5, 4.5]}, index=idx)
    t = [int(ts.timestamp()) for ts in idx]
    [buy, sell] = radar.long_events([_sig(t[0], "entry_long"), _sig(t[2], "exit_long")], df)
    assert (buy["time"], buy["price"], buy["signal_time"]) == (t[1], 2.0, t[0])
    assert (sell["time"], sell["price"]) == (t[3], 4.0)


def test_spot_backtest_never_shorts_and_chart_matches_its_trades():
    df = _series()
    cfg = {"strategy_name": "Lorenzo (trend-follower)", "params": {}, "label": "Lorenzo", "source": "base"}
    result, events, position = radar.run_spot(df, cfg)
    assert result.trades and all(tr.direction == "long" for tr in result.trades)
    assert {e.type for e in events} <= {"entry_long", "exit_long"}
    buys = [e for e in radar.long_events(events, df) if e["kind"] == "in"]
    t_next = {int(a.timestamp()): int(b.timestamp()) for a, b in zip(df.index, df.index[1:])}
    closed = [t_next[tr.entry_time] for tr in result.trades]
    # l'ultimo acquisto ancora aperto e' sul grafico ma non e' un trade chiuso
    assert [b["time"] for b in buys] == closed + ([buys[-1]["time"]] if len(buys) > len(closed) else [])
    assert len(buys) - len(closed) == (1 if position > 0 else 0)


def _series(n=600):
    rng = np.random.default_rng(3)
    close = 1 + 0.4 * np.sin(np.linspace(0, 18, n)) + np.cumsum(rng.normal(0, 0.004, n)) + np.linspace(0, 1, n)
    close = np.maximum(close, 0.05)
    idx = pd.date_range("2026-09-10", periods=n, freq="15min", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1000.0}, index=idx)


def test_evaluate_tries_all_and_never_proposes_the_lookahead_strategy():
    df = _series()
    out = radar.evaluate(df, radar.strategy_configs([]))
    assert {r["strategy_name"] for r in out["tried"]} <= set(STRATEGY_REGISTRY)
    assert out["hold_pct"] == pytest.approx((df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100)
    if out["best"]:
        assert out["best"]["strategy_name"] not in radar.UNRELIABLE
        assert out["best"]["trades"] >= radar.MIN_TRADES and out["best"]["return_pct"] > 0


class _T:
    def __init__(self, direction, pnl_pct):
        self.direction, self.pnl_pct = direction, pnl_pct


def test_long_only_metrics_ignore_shorts_and_pay_dex_costs():
    m = radar._long_only([_T("long", 10.0), _T("short", 50.0), _T("long", -5.0)], commission_pct=0.3)
    assert m["trades"] == 2
    assert m["return_pct"] == pytest.approx(((1.094) * (1 - 0.056) - 1) * 100)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(watchlist_store, "_STORE_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(radar, "_STORE_FILE", tmp_path / "radar.json")
    auth._users.clear()
    watchlist_store._entries.clear()
    radar.reset_for_tests()
    user = auth.create_user("trader@example.com", "correct-horse-battery-9")
    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.get_user(user.id)
    with TestClient(main.app) as c:
        c.owner_id = user.id
        yield c
    main.app.dependency_overrides.clear()
    watchlist_store._entries.clear()


def test_radar_endpoint_before_first_scan(client):
    body = client.get("/api/onchain/radar").json()
    assert body["proposals"] == [] and body["last_scan"] == 0
    assert body["filters"]["age_max_days"] == radar.AGE_MAX_DAYS


@pytest.fixture
def btc():
    """RSI+BB BTC band reads BTC's weekly band and fails without it, as the
    server does until the first BTC refresh: give it a BTC that rises and falls."""
    idx = pd.date_range("2019-01-01", pd.Timestamp.now(tz="UTC").normalize().tz_localize(None), freq="D", tz="UTC")
    btc_regime.set_closes(pd.Series(30_000 + 15_000 * np.sin(np.arange(len(idx)) / 120), index=idx), pin=True)
    yield
    btc_regime.clear()


def test_chart_returns_candles_and_long_events_per_strategy(client, monkeypatch, btc):
    async def fake_ohlcv(self, symbol, interval, outputsize, since_ms=None):
        assert symbol == "NEW@solana:PoolAddr" and interval == "15m"
        return _series()
    monkeypatch.setattr(radar.DexScreenerProvider, "fetch_ohlcv", fake_ohlcv)
    radar._chart_cache.clear()
    body = client.get("/api/onchain/radar/chart",
                      params={"chain": "solana", "pool": "PoolAddr", "symbol": "NEW", "interval": "15m"}).json()
    assert len(body["candles"]) == 600 and body["active"] == []
    assert {s["strategy_name"] for s in body["strategies"]} == set(STRATEGY_REGISTRY)
    for s in body["strategies"]:
        assert all(e["kind"] in ("in", "out") for e in s["events"])


def test_follow_turns_on_an_alert_never_auto_trade(client):
    req = {"chain": "solana", "pool": "PoolAddr", "symbol": "NEW", "token_address": "TokAddr",
           "interval": "15m", "strategy_name": "Gabriele (RSI+BB reversal)", "params": {"rsi_length": 10},
           "enabled": True}
    assert client.post("/api/onchain/radar/follow", json=req).json()["active"] == ["Gabriele (RSI+BB reversal)"]
    entry = watchlist_store.list_entries(client.owner_id)[0]
    assert entry.symbol == "NEW@solana:PoolAddr" and entry.interval == "15m"
    assert entry.base_token_address == "TokAddr"
    assert entry.strategy_params["Gabriele (RSI+BB reversal)"] == {"rsi_length": 10}
    assert not any(entry.auto_trade.values())
    off = client.post("/api/onchain/radar/follow", json={**req, "enabled": False}).json()
    assert off["active"] == []


def test_follow_rejects_unknown_strategy(client):
    r = client.post("/api/onchain/radar/follow", json={
        "chain": "solana", "pool": "P", "symbol": "X", "token_address": "T", "interval": "5m",
        "strategy_name": "Inventata", "enabled": True})
    assert r.status_code == 400


def test_every_new_listing_gets_the_best_strategy_by_backtest():
    out = radar.summarize_listing_backtest(_series(), "1m", radar.strategy_configs([]))
    assert out["state"] in ("ok", "weak", "none") and out["bars"] == 600
    if out["state"] != "none":
        assert out["strategy_name"] not in radar.UNRELIABLE and out["trades"] >= 1


def test_listing_too_young_for_a_backtest(monkeypatch):
    class P:
        async def fetch_ohlcv(self, symbol, interval, outputsize):
            assert interval == "1m" and symbol == "NEW@solana:Pool"
            return _series(20)
    item = {"chain": "solana", "symbol": "NEW", "pair_address": "Pool", "created_at": time.time() - 900}
    out = asyncio.run(radar.backtest_listing(item, P()))
    assert out["state"] == "young" and out["bars"] == 20


def test_listing_update_reaches_open_pages(tmp_path, monkeypatch):
    from app.trading.onchain import listings as L
    monkeypatch.setattr(L, "_STORE_FILE", tmp_path / "l.json")
    L.reset_for_tests()
    L._listings.append({"chain": "solana", "pair_address": "P", "symbol": "NEW"})
    q = L.subscribe()
    L.update_listing("solana", "P", backtest={"state": "ok"})
    assert q.get_nowait() == ("update", {"chain": "solana", "pair_address": "P", "symbol": "NEW",
                                         "backtest": {"state": "ok"}})
    L.reset_for_tests()


def test_newborn_pool_gets_candles_from_its_trades():
    from app.providers.dexscreener import candles_from_trades
    trades = [
        {"block_timestamp": "2026-09-23T21:48:50Z", "kind": "sell", "price_from_in_usd": "0.9", "volume_in_usd": "5"},
        {"block_timestamp": "2026-09-23T21:48:10Z", "kind": "buy", "price_to_in_usd": "1.2", "volume_in_usd": "3"},
        {"block_timestamp": "2026-09-23T21:47:30Z", "kind": "buy", "price_to_in_usd": "1.0", "volume_in_usd": "2"},
    ]
    rows = candles_from_trades(trades, 60)
    t0 = int(pd.Timestamp("2026-09-23T21:47:00Z").timestamp())
    assert rows == [[t0 + 60, 1.2, 1.2, 0.9, 0.9, 8.0], [t0, 1.0, 1.0, 1.0, 1.0, 2.0]]


def test_exceptions_take_grown_projects_the_normal_filter_skips():
    """DREGG: 88 giorni, $2.5M di cap, liquidita' 8% della cap."""
    site = "https://www.dregg.net/"
    dregg = _pool("DREGG", liq=203_000, mcap=2_520_000, age_days=88, site=site)
    normal = _pool("OK", site=site)
    thin = _pool("THIN", liq=20_000, mcap=3_000_000, age_days=60, site=site)
    ancient = _pool("OLD", liq=500_000, mcap=5_000_000, age_days=800, site=site)
    huge = _pool("HUGE", liq=5_000_000, mcap=90_000_000, age_days=60, site=site)
    taken = _pool("TAKEN", liq=300_000, mcap=3_000_000, age_days=60, site=site)
    nosite = _pool("NOSITE", liq=900_000, mcap=3_000_000, age_days=60)
    meme = _pool("PEPEDOGE", liq=900_000, mcap=3_000_000, age_days=60, site=site)
    picked = radar.pick_exceptions([dregg, normal, thin, ancient, huge, taken, nosite, meme], [_pool("TAKEN")])
    assert [p.symbol for p in picked] == ["DREGG"] and picked[0].exception is True
    assert radar.pick_candidates([dregg])[0] == []


def test_exception_queue_rotates_so_every_candidate_gets_its_turn(monkeypatch):
    now = 1_800_000_000
    cands = [_pool(f"T{i}", token=f"t{i}") for i in range(5)]
    monkeypatch.setattr(radar, "_verdicts", {
        "solana:t0": (now - 60, {"status": "fail"}),            # scartato di recente: salta
        "solana:t1": (now - radar.VERDICT_TTL_S - 1, {"status": "fail"}),  # scaduto: in fondo
        "solana:t4": (now - radar.VERDICT_TTL_S - 1, {"status": "ok"}),    # gia' passato: in testa
    })
    assert [p.token_address for p in radar.exception_queue(cands, now, limit=3)] == ["t4", "t2", "t3"]


def test_exceptions_come_first_among_proposals():
    rows = [{"return_pct": 90}, {"exception": True, "return_pct": -5, "report": {"score": 89}},
            {"return_pct": 10}, {"exception": True, "return_pct": 50, "report": {"score": 86}}]
    ranked = sorted(rows, key=radar._proposal_rank)
    assert [r["return_pct"] for r in ranked] == [-5, 50, 90, 10]


def test_pool_from_dexscreener():
    p = radar.pool_from_dexscreener({
        "chainId": "solana", "dexId": "pumpswap", "pairAddress": "Pair1",
        "baseToken": {"address": "Mint1", "name": "dregg", "symbol": "DREGG"},
        "info": {"websites": [{"url": "https://www.dregg.net/"}]},
        "liquidity": {"usd": 203_000}, "marketCap": 2_520_000, "pairCreatedAt": 1_750_000_000_000})
    assert (p.symbol, p.token_address, p.pair_address, p.market_cap) == ("DREGG", "Mint1", "Pair1", 2_520_000)
    assert p.pair_url == "https://dexscreener.com/solana/Pair1" and p.site == "https://www.dregg.net/"


def test_grown_pools_come_from_pumpfun_by_market_cap():
    import httpx

    def handler(req):
        url = str(req.url)
        if "pump.fun" in url:
            if req.url.params["offset"] != "150":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"mint": "Mint1", "usd_market_cap": 2_520_000},
                                             {"mint": "Tiny", "usd_market_cap": 10_000},
                                             {"mint": "Giant", "usd_market_cap": 900_000_000}])
        if "dexscreener" in url:
            assert url.endswith("/solana/Mint1")
            return httpx.Response(200, json=[{"chainId": "solana", "pairAddress": "Pair1", "dexId": "pumpswap",
                                              "baseToken": {"address": "Mint1", "symbol": "DREGG"},
                                              "liquidity": {"usd": 203_000}, "marketCap": 2_520_000,
                                              "pairCreatedAt": 1_750_000_000_000}])
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await radar._fetch_grown_pools(c)
    pools = asyncio.run(go())
    assert [p.symbol for p in pools] == ["DREGG"]


def test_attention_pools_are_the_tokens_people_talk_about():
    import httpx

    def handler(req):
        url = str(req.url)
        if "token-profiles" in url or "token-boosts" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "tokenAddress": "Mint1"},
                                             {"chainId": "bsc", "tokenAddress": "0xabc"}])
        if "tokens/v1/solana/Mint1" in url:
            return httpx.Response(200, json=[{"chainId": "solana", "pairAddress": "Pair1",
                                              "baseToken": {"address": "Mint1", "symbol": "TECH"},
                                              "liquidity": {"usd": 50_000}, "marketCap": 400_000}])
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await radar._fetch_attention_pools(c, "solana")
    assert [p.symbol for p in asyncio.run(go())] == ["TECH"]


def test_rugs_already_suffered_vanish_from_the_radar_and_land_on_the_wall(client, monkeypatch):
    """Lorenzo: "fammi sparire VISE che e' un RUGPULL, crea sezione ironica
    dove ci andranno a finire tutte le inculate subite"."""
    from app.trading.onchain import outcomes
    vise = outcomes.SHAME_SEED[0]
    monkeypatch.setattr(outcomes, "_journal", {})
    radar._state["proposals"] = [{"chain": "solana", "symbol": "VISE", "token_address": vise["token_address"]},
                                 {"chain": "solana", "symbol": "GOOD", "token_address": "Good"}]
    body = client.get("/api/onchain/radar").json()
    assert [p["symbol"] for p in body["proposals"]] == ["GOOD"]
    assert [s["symbol"] for s in body["shame"]] == ["VISE"]


def test_radar_feed_is_small_and_counts_the_whole_day(client, monkeypatch):
    """The scanner tab timed out on phones: /radar shipped every listing and
    rejection with its checks. /radar/feed ships only what the feed shows."""
    import time as _t
    from app.trading.onchain import listings, outcomes
    now = _t.time()
    monkeypatch.setattr(outcomes, "_journal", {})
    monkeypatch.setattr(listings, "_listings", [
        {"chain": "solana", "symbol": f"P{i}", "token_address": f"p{i}", "pair_address": f"pp{i}",
         "found_at": now - i * 60, "created_at": now - i * 60, "checks": [{"key": "site"}],
         "market_cap": 100_000} for i in range(100)])
    monkeypatch.setattr(listings, "_rejected", [
        {"chain": "solana", "symbol": f"R{i}", "token_address": f"r{i}", "at": now - i * 300 - 30}
        for i in range(500)])
    body = client.get("/api/onchain/radar/feed").json()
    assert len(body["listings"]) + len(body["rejected"]) == main._RADAR_FEED_MAX
    assert body["listings"][0]["symbol"] == "P0" and body["rejected"][0]["symbol"] == "R0"
    assert body["counts"]["rejected_24h"] == 288
    assert "proposals" not in body and "tested" not in body
