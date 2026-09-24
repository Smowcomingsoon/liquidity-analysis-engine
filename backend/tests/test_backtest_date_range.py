"""
Il Lab testava sempre la strategia su tutta la storia disponibile: nessun
modo di isolare un periodo preciso (es. solo il 2023, o solo l'ultimo mese
di un evento particolare). Segnalazione ee624db5: un periodo di riferimento
subito dopo il selettore del pair, con data di inizio e fine.

Il ritaglio va fatto PRIMA del backtest, non dopo sui trade risultanti:
tagliare i trade lascerebbe dentro operazioni aperte fuori dal periodo, o
ne perderebbe di valide aperte prima e chiuse dentro la finestra.
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
_RNG = np.random.default_rng(42)
_CLOSE = 100 + np.cumsum(_RNG.normal(0, 1, N))
_CANDLES = pd.DataFrame(
    {
        "open": _CLOSE,
        "high": _CLOSE + 1,
        "low": _CLOSE - 1,
        "close": _CLOSE,
        "volume": np.full(N, 1000.0),
    },
    index=_DATES,
)


class _Provider:
    def __init__(self, frame):
        self._frame = frame

    async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
        if isinstance(self._frame, Exception):
            raise self._frame
        return self._frame


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "get_provider", lambda *a, **k: _Provider(_CANDLES))
    user = User(id="u1", email="l@example.com", password_hash="x")
    main.app.dependency_overrides[auth.get_current_user] = lambda: user
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def _deep(client, **kw):
    params = {"symbol": "DATERANGEUSDT", "asset_type": "crypto", "exchange": "mexc",
              "ltf_interval": "1d", "strategy_name": "Lorenzo (trend-follower)"}
    params.update(kw)
    return client.get("/api/backtest/deep", params=params)


def test_without_a_range_the_whole_history_is_used(client):
    resp = _deep(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_start"] == int(_DATES[0].timestamp() * 1000)
    assert body["data_end"] == int(_DATES[-1].timestamp() * 1000)


def test_a_range_narrows_the_series_before_the_backtest_runs(client):
    resp = _deep(client, start_date="2023-03-01", end_date="2023-03-31")
    assert resp.status_code == 200
    body = resp.json()
    start = pd.Timestamp(body["data_start"], unit="ms", tz="UTC")
    end = pd.Timestamp(body["data_end"], unit="ms", tz="UTC")
    assert start >= pd.Timestamp("2023-03-01", tz="UTC")
    assert end < pd.Timestamp("2023-04-01", tz="UTC")


def test_end_date_is_inclusive_of_the_whole_day(client):
    resp = _deep(client, start_date="2023-03-01", end_date="2023-03-01")
    assert resp.status_code == 200
    body = resp.json()
    # Una sola candela giornaliera nel range: end_date inclusivo, non un
    # confine che la esclude per un microsecondo.
    assert body["data_start"] == body["data_end"]


def test_a_range_outside_the_data_is_a_clear_400_not_an_empty_result(client):
    resp = _deep(client, start_date="2030-01-01", end_date="2030-01-31")
    assert resp.status_code == 400
    assert "period" in resp.json()["detail"].lower()


def test_only_start_date_keeps_everything_after_it(client):
    resp = _deep(client, start_date="2023-06-01")
    assert resp.status_code == 200
    body = resp.json()
    start = pd.Timestamp(body["data_start"], unit="ms", tz="UTC")
    end = pd.Timestamp(body["data_end"], unit="ms", tz="UTC")
    assert start >= pd.Timestamp("2023-06-01", tz="UTC")
    assert end == _DATES[-1]
