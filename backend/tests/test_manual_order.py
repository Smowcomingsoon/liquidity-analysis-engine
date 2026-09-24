"""
L'ordine manuale: il pair si verifica prima di mandare denaro, e il tentativo
finisce nel diario.

Due difetti nella stessa segnalazione. Il primo era l'intestazione della chiave
(vedi test_mexc_order.py). Il secondo e' che il pair si scrive a mano, senza
niente che confermi che esista sull'exchange scelto: un nome sbagliato tornava
indietro come errore dell'exchange, cioe' dopo aver provato a comprare. E il
fallimento viveva solo nel riquadro di stato, che sparisce ricaricando la
pagina — mentre il diario esiste apposta per quello.
"""

from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import auth
from app import main
from app import trading_log as tl
from app.models import User
from app.providers.base import ProviderError


class _Provider:
    def __init__(self, frame): self._frame = frame
    async def fetch_ohlcv(self, symbol, interval, outputsize, since_ms=None):
        if isinstance(self._frame, Exception):
            raise self._frame
        return self._frame


_CANDLES = pd.DataFrame({"open": [1.0, 1.0], "high": [1.0, 1.0],
                         "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [1.0, 1.0]})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "_STORE_FILE", tmp_path / "journal.json")
    tl._entries.clear()
    user = User(id="u1", email="l@example.com", password_hash="x")
    cred = type("C", (), {"api_key_enc": "a", "api_secret_enc": "b"})()
    user.trading_credentials["mexc"] = cred
    user.trading_credentials["kraken"] = cred
    monkeypatch.setattr(auth, "get_decrypted_credential", lambda u, e: ("key", "secret", ""))
    main.app.dependency_overrides[auth.get_current_user] = lambda: user
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def _order(client, **kw):
    body = {"exchange": "mexc", "symbol": "KASUSDT", "side": "BUY",
            "size_mode": "fixed_usdt", "size_value": 50.0}
    body.update(kw)
    return client.post("/api/trading/order", json=body)


def test_an_unknown_pair_never_reaches_the_exchange(client, monkeypatch):
    sent = []

    async def _never(*a, **k):
        sent.append(a)
        return {}

    monkeypatch.setattr(main, "get_provider", lambda *a, **k: _Provider(ProviderError("no such symbol")))
    monkeypatch.setattr(main, "place_order_for_signal", _never)
    resp = _order(client, symbol="NONESISTE")
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]
    assert sent == [], "nessun ordine deve partire per un pair che non esiste"


def test_an_empty_result_counts_as_not_found(client, monkeypatch):
    monkeypatch.setattr(main, "get_provider", lambda *a, **k: _Provider(pd.DataFrame()))
    monkeypatch.setattr(main, "place_order_for_signal", None)
    resp = _order(client, symbol="VUOTO")
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_a_good_order_goes_through_and_is_journalled(client, monkeypatch):
    async def _ok(*a, **k):
        return {"orderId": 7, "status": "FILLED"}

    monkeypatch.setattr(main, "get_provider", lambda *a, **k: _Provider(_CANDLES))
    monkeypatch.setattr(main, "place_order_for_signal", _ok)
    resp = _order(client)
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "KASUSDT"
    entries = tl.list_for("u1")
    assert len(entries) == 1
    assert entries[0]["outcome"] == "eseguito"
    assert entries[0]["strategy_name"] == "manuale"


def test_a_failed_order_is_journalled_too(client, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError('mexc order error: {"code":400,"msg":"api key required"}')

    monkeypatch.setattr(main, "get_provider", lambda *a, **k: _Provider(_CANDLES))
    monkeypatch.setattr(main, "place_order_for_signal", _boom)
    resp = _order(client)
    assert resp.status_code == 502
    entries = tl.list_for("u1")
    assert len(entries) == 1
    assert entries[0]["outcome"] == "fallito"
    assert "api key required" in entries[0]["detail"]


def test_an_exchange_without_a_data_provider_is_not_blocked(client, monkeypatch):
    """Kraken, Coinbase e Bitstamp non hanno un provider dati qui: il pair non
    si puo' verificare, e rifiutare l'ordine per questo sarebbe inventare un
    controllo che non abbiamo fatto."""
    async def _ok(*a, **k):
        return {"id": "x"}

    def _no_provider(*a, **k):
        raise AssertionError("non deve nemmeno cercare un provider")

    monkeypatch.setattr(main, "get_provider", _no_provider)
    monkeypatch.setattr(main, "place_order_for_signal", _ok)
    user_cred = _order(client, exchange="kraken")
    assert user_cred.status_code == 200
