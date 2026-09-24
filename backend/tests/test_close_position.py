"""
Chiudere una posizione a mano, spot e futures.

Prima esisteva solo la chiusura automatica sui segnali di uscita: chi apriva
una posizione dal sito poteva poi solo guardarla, e per uscire doveva andare
sull'app dell'exchange. Con la leva quella distanza si paga, ed e' la
situazione in cui e' finito davvero l'amico di Lorenzo con una posizione MEXC
aperta e nessun pulsante per chiuderla.

Due cose che questi test tengono ferme, ed entrambe sono scelte, non dettagli:

1. **Il kill switch non blocca una chiusura.** Serve a impedire NUOVA
   esposizione; se bloccasse anche l'uscita, spegnerlo con una posizione
   aperta significherebbe restare intrappolati dentro — il contrario di cio'
   per cui quel toggle esiste.

2. **"Nessuna posizione da chiudere" non e' un successo travestito.** Se non
   c'era niente da chiudere lo deve dire, invece di rispondere "chiusa" e
   lasciar credere di essere usciti da qualcosa.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app import trading_log as tl
from app.models import User
from app.trading.futures.base import FuturesExecutionError


class _Adapter:
    """Sta al posto dell'exchange: registra com'e' stato chiamato."""

    name = "MEXC"
    needs_passphrase = False
    max_leverage = 50
    isolated_margin = True

    def __init__(self, result=None):
        self._result = result
        self.calls: list[tuple] = []

    async def close_position(self, symbol, creds):
        self.calls.append((symbol, creds))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.fixture
def user_and_client(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "_STORE_FILE", tmp_path / "journal.json")
    tl._entries.clear()
    user = User(id="u1", email="l@example.com", password_hash="x")
    cred = type("C", (), {"api_key_enc": "a", "api_secret_enc": "b", "enabled": True})()
    user.futures_credentials["mexc"] = cred
    user.trading_credentials["mexc"] = cred
    monkeypatch.setattr(auth, "get_decrypted_futures_credential", lambda u, e: ("key", "secret", ""))
    monkeypatch.setattr(auth, "get_decrypted_credential", lambda u, e: ("key", "secret", ""))
    main.app.dependency_overrides[auth.get_current_user] = lambda: user
    with TestClient(main.app) as c:
        yield user, c
    main.app.dependency_overrides.clear()


def _close_futures(client, **kw):
    body = {"exchange": "mexc", "symbol": "KAS_USDT", "confirm": True}
    body.update(kw)
    return client.post("/api/trading/futures/close", json=body)


def _close_spot(client, **kw):
    body = {"exchange": "mexc", "symbol": "KASUSDT", "confirm": True}
    body.update(kw)
    return client.post("/api/trading/spot/close", json=body)


# ── futures ──────────────────────────────────────────────────────────────


def test_closing_a_futures_position_calls_the_exchange(user_and_client, monkeypatch):
    _, client = user_and_client
    adapter = _Adapter({"orderId": "1"})
    monkeypatch.setattr(main.futures_registry, "get", lambda e: adapter)
    resp = _close_futures(client)
    assert resp.status_code == 200
    assert resp.json()["closed"] is True
    assert adapter.calls and adapter.calls[0][0] == "KAS_USDT"


def test_nothing_to_close_is_said_plainly_not_reported_as_closed(user_and_client, monkeypatch):
    _, client = user_and_client
    monkeypatch.setattr(main.futures_registry, "get", lambda e: _Adapter(None))
    resp = _close_futures(client)
    assert resp.status_code == 200
    body = resp.json()
    assert body["closed"] is False
    assert "no open position" in body["detail"].lower()


def test_the_kill_switch_never_traps_an_open_position(user_and_client, monkeypatch):
    """Esecuzione futures disattivata: aprire e' vietato, uscire no."""
    user, client = user_and_client
    user.futures_credentials["mexc"].enabled = False
    adapter = _Adapter({"orderId": "1"})
    monkeypatch.setattr(main.futures_registry, "get", lambda e: adapter)
    resp = _close_futures(client)
    assert resp.status_code == 200, resp.text
    assert adapter.calls, "la chiusura deve arrivare all'exchange anche col kill switch spento"


def test_a_futures_close_without_confirmation_never_reaches_the_exchange(user_and_client, monkeypatch):
    _, client = user_and_client
    adapter = _Adapter({"orderId": "1"})
    monkeypatch.setattr(main.futures_registry, "get", lambda e: adapter)
    resp = _close_futures(client, confirm=False)
    assert resp.status_code == 400
    assert not adapter.calls


def test_a_failed_futures_close_is_recorded_in_the_journal(user_and_client, monkeypatch):
    user, client = user_and_client
    monkeypatch.setattr(main.futures_registry, "get",
                        lambda e: _Adapter(FuturesExecutionError("exchange irraggiungibile")))
    resp = _close_futures(client)
    assert resp.status_code == 502
    entries = tl.list_for(user.id)
    assert any(e["outcome"] == "fallito" for e in entries)


# ── spot ─────────────────────────────────────────────────────────────────


def test_closing_spot_sells_the_whole_free_balance(user_and_client, monkeypatch):
    """Sullo spot chiudere = vendere il 100% della moneta base, non una parte."""
    _, client = user_and_client
    seen: dict = {}

    async def _fake_place(exchange, symbol, signal, size_mode, size_value, *a, **kw):
        seen.update(exchange=exchange, symbol=symbol, type=signal.type,
                    size_mode=size_mode, size_value=size_value)
        return {"orderId": "9"}

    monkeypatch.setattr(main, "place_order_for_signal", _fake_place)
    resp = _close_spot(client)
    assert resp.status_code == 200, resp.text
    assert seen["type"] == "exit_long"      # -> SELL
    assert seen["size_mode"] == "pct_balance"
    assert seen["size_value"] == 100.0


def test_a_spot_close_without_confirmation_never_reaches_the_exchange(user_and_client, monkeypatch):
    _, client = user_and_client
    called = False

    async def _fake_place(*a, **kw):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(main, "place_order_for_signal", _fake_place)
    resp = _close_spot(client, confirm=False)
    assert resp.status_code == 400
    assert not called
