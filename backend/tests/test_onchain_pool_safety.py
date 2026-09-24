"""Allowlist pool (seguito del piano wallet on-chain, Fase 3): liquidita'
minima, eta' minima, crollo di liquidita' nell'ultima ora. fetch_pool_snapshot
e' verificato anche con una vera chiamata di rete a DexScreener (pubblica,
nessuna chiave) su un pool Solana reale e molto liquido — stesso stile di
test_onchain_quotes.py per jupiter_quote."""
from __future__ import annotations

import asyncio

import httpx

from app.trading.onchain import ledger, pool_safety

_RealAsyncClient = httpx.AsyncClient
# Pool Raydium SOL/USDC su Solana, uno dei piu' liquidi in assoluto — usato
# solo per verificare che il parsing di una risposta reale funzioni.
SOL_USDC_RAYDIUM_POOL = "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"


def _client_factory(handler):
    def factory(*args, **kwargs):
        transport = httpx.MockTransport(handler)
        kwargs.pop("proxies", None)
        return _RealAsyncClient(*args, transport=transport, **kwargs)
    return factory


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_DB_FILE", tmp_path / "ledger.db")
    ledger._init_db()


def test_fetch_pool_snapshot_real_network_call():
    result = _run(pool_safety.fetch_pool_snapshot("solana", SOL_USDC_RAYDIUM_POOL))
    assert result is not None
    assert result["liquidity_usd"] > 0


def test_fetch_pool_snapshot_unsupported_chain_returns_none():
    # Robinhood Chain: troppo nuova per DexScreener, dichiarato nel modulo.
    assert _run(pool_safety.fetch_pool_snapshot("robinhood", "0xabc")) is None


def test_fetch_pool_snapshot_parses_mocked_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "pairs": [{"liquidity": {"usd": 12345.6}, "pairCreatedAt": 1_700_000_000_000}],
        })

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    result = _run(pool_safety.fetch_pool_snapshot("solana", "Fake111111111111111111111111111111111111111"))
    assert result == {"liquidity_usd": 12345.6, "created_at": 1_700_000_000.0}


def test_fetch_pool_snapshot_returns_none_on_missing_liquidity(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pairs": []})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    assert _run(pool_safety.fetch_pool_snapshot("solana", "x")) is None


def test_check_liquidity_minimum_rejects_when_unverifiable():
    ok, reason = pool_safety.check_liquidity_minimum(None, 3000.0)
    assert ok is False
    assert "cannot be verified" in reason


def test_check_liquidity_minimum_rejects_below_threshold():
    ok, reason = pool_safety.check_liquidity_minimum(2999.99, 3000.0)
    assert ok is False
    assert "below the required minimum" in reason
    ok, _ = pool_safety.check_liquidity_minimum(3000.0, 3000.0)
    assert ok is True


def test_check_pool_age_rejects_too_young(monkeypatch):
    import time
    now = 1_800_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)
    ok, reason = pool_safety.check_pool_age(now - 10 * 60, minimum_minutes=30.0)
    assert ok is False
    assert "minimum age" in reason
    ok, _ = pool_safety.check_pool_age(now - 31 * 60, minimum_minutes=30.0)
    assert ok is True


def test_check_pool_age_rejects_when_unverifiable():
    ok, reason = pool_safety.check_pool_age(None)
    assert ok is False
    assert "cannot be verified" in reason


def test_check_liquidity_drop_passes_without_history():
    ok, _ = pool_safety.check_liquidity_drop(1000.0, None)
    assert ok is True


def test_check_liquidity_drop_rejects_over_50_percent():
    ok, _ = pool_safety.check_liquidity_drop(4999.0, 10000.0, max_drop_pct=50.0)
    assert ok is False
    ok, _ = pool_safety.check_liquidity_drop(5001.0, 10000.0, max_drop_pct=50.0)
    assert ok is True


def test_ledger_max_pool_liquidity_since_tracks_history(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    now = 1_800_000_000.0
    ledger.record_pool_liquidity("solana", "poolX", 10000.0, recorded_at=now - 1800)
    ledger.record_pool_liquidity("solana", "poolX", 4000.0, recorded_at=now)
    assert ledger.max_pool_liquidity_since("solana", "poolX", since=now - 3600) == 10000.0
    # Fuori dalla finestra di un'ora: non conta piu'.
    assert ledger.max_pool_liquidity_since("solana", "poolX", since=now - 900) == 4000.0
    assert ledger.max_pool_liquidity_since("solana", "poolY") is None
