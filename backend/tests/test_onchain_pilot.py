"""Il pilota: quello che il wallet on-chain fa da solo, visibile da
/api/onchain/pilot/{chain}. Lorenzo: "voglio sapere cosa sta monitorando,
voglio vedere cosa sta scansionando"."""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, main, trading_log, watchlist as watchlist_store
from app.trading.onchain import ledger as onchain_ledger
from app.trading.onchain import pilot as onchain_pilot


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(watchlist_store, "_STORE_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(onchain_ledger, "_DB_FILE", tmp_path / "ledger.db")
    monkeypatch.setattr(main.onchain_listings, "recent", lambda: [])

    async def _vet_ok(candidate):
        return {"status": "ok", "reason": "", "checks": []}
    monkeypatch.setattr(main, "_pilot_vet", _vet_ok)
    monkeypatch.setattr(onchain_pilot, "_STORE_FILE", tmp_path / "pilot.json")
    monkeypatch.setattr(trading_log, "_STORE_FILE", tmp_path / "journal.json")
    onchain_ledger._init_db()
    auth._users.clear()
    watchlist_store._entries.clear()
    trading_log._entries.clear()
    onchain_pilot.reset_for_tests()
    user = auth.create_user("trader@example.com", "correct-horse-battery-9")
    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.get_user(user.id)
    with TestClient(main.app) as c:
        c.owner_id = user.id
        yield c
    main.app.dependency_overrides.clear()
    watchlist_store._entries.clear()
    trading_log._entries.clear()
    onchain_pilot.reset_for_tests()


def _candidate(**overrides):
    from app.sniper.cache import Candidate
    defaults = dict(
        chain_id="solana", token_address="MintAddr111111111111111111111111111",
        pair_address="PoolAddr222222222222222222222222", symbol="NEWCOIN", name="New Coin",
        dex_id="raydium", pair_url="https://dexscreener.com/x", price_usd=0.01,
        market_cap=100_000, liquidity_usd=5000.0, volume_h24=1000, volume_h1=100,
        age_hours=2.0, buy_sell_ratio=1.2, website=None,
    )
    defaults.update(overrides)
    return Candidate(**defaults)


def _scan(monkeypatch, candidates):
    async def _fake_fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
        return pd.DataFrame({"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]})
    monkeypatch.setattr(main.settings, "sniper_enabled", True)
    monkeypatch.setattr("app.sniper.cache.get_candidates", lambda: candidates)
    # Sulla classe, non sull'istanza: un attributo d'istanza lasciato dal
    # ripristino di monkeypatch oscurerebbe le patch di classe dei test dopo.
    monkeypatch.setattr(main.dexscreener_provider.__class__, "fetch_ohlcv", _fake_fetch_ohlcv)
    _run(main._sniper_onchain_bridge_tick())


def test_pilot_off_by_default_and_says_why_it_would_not_work(client):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    body = client.get("/api/onchain/pilot/solana").json()
    assert body["enabled"] is False
    assert body["working"] is False
    assert body["tokens"] == []
    assert body["scan"]["last_at"] is None
    health = {h["key"]: h for h in body["health"]}
    assert health["wallet"]["ok"] is True
    assert set(health) == {"wallet", "scanner", "signals", "execution", "breaker"}


def test_scan_shows_what_was_seen_added_and_rejected(client, monkeypatch):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    client.patch("/api/onchain/sniper-autotrade/solana", json={"enabled": True})
    good = _candidate()
    thin = _candidate(symbol="THIN", token_address="Thin1111111111111111111111111111111",
                      pair_address="ThinPool11111111111111111111111", liquidity_usd=120.0)
    _scan(monkeypatch, [good, thin])

    body = client.get("/api/onchain/pilot/solana").json()
    assert body["enabled"] is True
    assert body["scan"]["seen_today"] == 2
    assert body["scan"]["accepted_today"] == 1
    assert body["scan"]["next_at"] == body["scan"]["last_at"] + main._SNIPER_ONCHAIN_POLL_SECONDS

    [token] = body["tokens"]
    assert token["symbol"] == "NEWCOIN"
    assert token["state"] == "watch"
    assert token["liquidity_at_add"] == 5000.0
    assert token["events"][-1]["text"].startswith("Added to watch")

    [rejected] = body["rejected"]
    assert rejected["symbol"] == "THIN"
    assert "liquidity" in rejected["reason"]


def test_second_scan_counts_each_token_once_and_adds_nothing_new(client, monkeypatch):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    client.patch("/api/onchain/sniper-autotrade/solana", json={"enabled": True})
    _scan(monkeypatch, [_candidate()])
    _scan(monkeypatch, [_candidate()])
    scan = client.get("/api/onchain/pilot/solana").json()["scan"]
    assert scan["seen_today"] == 1
    assert scan["accepted_today"] == 1


def test_skipped_signal_is_shown_with_its_reason(client, monkeypatch):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    client.patch("/api/onchain/sniper-autotrade/solana", json={"enabled": True})
    _scan(monkeypatch, [_candidate()])
    [entry] = watchlist_store.list_entries(client.owner_id)
    trading_log.record(
        client.owner_id, symbol=entry.symbol, exchange=entry.exchange,
        strategy_name=main._SNIPER_ONCHAIN_STRATEGY, signal_type="entry_long",
        outcome="saltato", detail="tetto giornaliero raggiunto", price=0.01,
    )
    [token] = client.get("/api/onchain/pilot/solana").json()["tokens"]
    assert token["state"] == "blocked"
    assert token["reason"] == "tetto giornaliero raggiunto"
    assert any(e["text"].startswith("buy signal: skipped") for e in token["events"])


def test_evaluation_time_is_reported(client, monkeypatch):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    client.patch("/api/onchain/sniper-autotrade/solana", json={"enabled": True})
    _scan(monkeypatch, [_candidate()])
    [entry] = watchlist_store.list_entries(client.owner_id)
    onchain_pilot.record_evaluation(entry.id, price=0.02, now=1234.0)
    [token] = client.get("/api/onchain/pilot/solana").json()["tokens"]
    assert token["last_eval_at"] == 1234.0
    assert token["last_price"] == 0.02
