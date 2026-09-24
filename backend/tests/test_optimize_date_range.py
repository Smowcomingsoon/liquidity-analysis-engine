"""
Il periodo del Lab vale anche per l'ottimizzazione, non solo per il backtest.

Segnalazione 6ba09078. Quando il periodo e' stato aggiunto (ee624db5) l'ho
applicato al solo `/api/backtest/deep`, lasciando fuori l'ottimizzatore e
scrivendolo come scelta: "la segnalazione parla esplicitamente di backtest".
Il risultato pratico era pero' che il Lab mandava start_date/end_date anche
all'ottimizzazione — il frontend usa la stessa buildLabQuery() per entrambe —
e il backend li ignorava in silenzio: i parametri venivano cercati su tutta la
storia disponibile e poi il loro backtest, quello si' ritagliato, veniva
mostrato accanto. Due misure diverse presentate come la stessa.

Ignorare un parametro che arriva e' peggio che rifiutarlo: non lascia traccia.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.models import User

N = 400
_DATES = pd.date_range("2023-01-01", periods=N, freq="D", tz="UTC")
_RNG = np.random.default_rng(7)
_CLOSE = 100 + np.cumsum(_RNG.normal(0, 1, N))
_CANDLES = pd.DataFrame(
    {"open": _CLOSE, "high": _CLOSE + 1, "low": _CLOSE - 1,
     "close": _CLOSE, "volume": np.full(N, 1000.0)},
    index=_DATES,
)


class _Provider:
    async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
        return _CANDLES


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "get_provider", lambda *a, **k: _Provider())
    user = User(id="u1", email="l@example.com", password_hash="x")
    main.app.dependency_overrides[auth.get_current_user] = lambda: user
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


@pytest.fixture
def seen_df(monkeypatch):
    """Cattura la serie che arriva davvero all'ottimizzatore."""
    captured: dict = {}
    real = main._optimize_payload

    async def _spy(strategy_name, df, *a, **kw):
        captured["df"] = df
        return await real(strategy_name, df, *a, **kw)

    monkeypatch.setattr(main, "_optimize_payload", _spy)
    return captured


def _optimize(client, **kw):
    params = {"symbol": "RANGEUSDT", "asset_type": "crypto", "exchange": "mexc",
              "ltf_interval": "1d", "strategy_name": "Lorenzo (trend-follower)",
              "mode": "random"}
    params.update(kw)
    return client.get("/api/optimize", params=params)


def test_without_a_range_the_optimizer_sees_the_whole_history(client, seen_df):
    resp = _optimize(client)
    assert resp.status_code == 200, resp.text
    assert len(seen_df["df"]) == N


def test_the_optimizer_searches_only_inside_the_requested_period(client, seen_df):
    resp = _optimize(client, start_date="2023-03-01", end_date="2023-05-31")
    assert resp.status_code == 200, resp.text
    df = seen_df["df"]
    assert df.index.min() >= pd.Timestamp("2023-03-01", tz="UTC")
    assert df.index.max() < pd.Timestamp("2023-06-01", tz="UTC")
    assert len(df) < N


def test_a_period_with_no_candles_is_a_clear_400_not_a_silent_full_run(client):
    resp = _optimize(client, start_date="2030-01-01", end_date="2030-01-31")
    assert resp.status_code == 400
    assert "period" in resp.json()["detail"].lower()
