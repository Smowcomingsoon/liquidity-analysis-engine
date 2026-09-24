"""
Dall'archivio all'alert: accendere il monitoraggio con i parametri trovati,
non con quelli di default.

La segnalazione: dopo un'ottimizzazione, nella scheda del ticker comparivano i
parametri predefiniti della strategia. L'archivio conservava i parametri
trovati, ma restava una fotografia — per farli lavorare bisognava riscriverli a
mano, e chi non lo faceva teneva acceso un alert identico d'aspetto a uno
ottimizzato e diverso in tutto il resto.

Due regole che questi test tengono ferme:
  * i parametri li legge il server dalla voce d'archivio, non dal client;
  * si scrivono PRIMA di accendere, perche' accendere scatta la fotografia che
    finisce in archivio: fatta prima, direbbe "default" mentre gira altro.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import archive as archive_store
from app import auth, main
from app import watchlist as watchlist_store
from app.models import User

STRATEGY = "Gabriele (RSI+BB reversal)"
FOUND = {"rsi_length": 9, "bb_length": 34, "bb_mult": 2.6, "rsi_oversold": 19, "rsi_overbought": 81}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(archive_store, "_STORE_FILE", tmp_path / "archive.json")
    monkeypatch.setattr(watchlist_store, "_STORE_FILE", tmp_path / "watchlist.json")
    archive_store._entries.clear()
    watchlist_store._entries.clear()
    user = User(id="u1", email="l@example.com", password_hash="x")
    main.app.dependency_overrides[auth.get_current_user] = lambda: user
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def _archived(**kw) -> dict:
    fields = dict(owner_id="u1", symbol="KASUSDT", asset_type="crypto", exchange="mexc",
                  interval="4h", strategy_name=STRATEGY, params=dict(FOUND),
                  backtest_params={}, source="ottimizzazione")
    fields.update(kw)
    return archive_store.create(**fields)


def test_the_alert_starts_with_the_archived_parameters(client):
    entry = _archived()
    resp = client.post(f"/api/archive/{entry['id']}/apply-to-alert")
    assert resp.status_code == 200
    body = resp.json()
    assert body["params"] == FOUND

    watch = watchlist_store.list_entries("u1")
    assert len(watch) == 1
    assert STRATEGY in watch[0].strategies
    assert watch[0].strategy_params[STRATEGY] == FOUND, "l'alert gira sui parametri trovati"


def test_the_alert_uses_the_archived_timeframe(client):
    entry = _archived(interval="4h")
    client.post(f"/api/archive/{entry['id']}/apply-to-alert")
    watch = watchlist_store.list_entries("u1")[0]
    assert watch.strategy_intervals.get(STRATEGY) == "4h"


def test_the_snapshot_it_creates_is_not_the_default_configuration(client):
    """Accendere fotografa la configurazione. Se i parametri si scrivessero
    dopo l'interruttore, la fotografia direbbe 'default' mentre l'alert gira
    con altro — e l'archivio racconterebbe una cosa diversa da quella vera."""
    entry = _archived()
    client.post(f"/api/archive/{entry['id']}/apply-to-alert")
    from_alert = [e for e in archive_store.list_for("u1") if e["source"] == "alert"]
    assert from_alert, "l'attivazione va comunque registrata"
    assert from_alert[0]["params"] == FOUND


def test_applying_twice_does_not_duplicate_the_watchlist_entry(client):
    entry = _archived()
    client.post(f"/api/archive/{entry['id']}/apply-to-alert")
    client.post(f"/api/archive/{entry['id']}/apply-to-alert")
    assert len(watchlist_store.list_entries("u1")) == 1


def test_an_entry_of_someone_else_is_not_found(client):
    entry = _archived(owner_id="altro")
    assert client.post(f"/api/archive/{entry['id']}/apply-to-alert").status_code == 404


def test_an_unknown_strategy_is_refused(client):
    entry = _archived(strategy_name="Strategia che non esiste")
    resp = client.post(f"/api/archive/{entry['id']}/apply-to-alert")
    assert resp.status_code == 400
    assert "Unknown" in resp.json()["detail"]
