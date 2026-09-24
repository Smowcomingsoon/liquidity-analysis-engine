"""
Un 422 deve dire quale campo e cosa ci voleva.

E' l'unico errore che l'app non scrive da se': lo genera FastAPI prima di
entrare nell'endpoint, e il suo `detail` e' una lista di oggetti. In pagina
arrivava come "Errore 422" — un numero che non dice ne' quale parametro ne'
perche'. Tre modi di scrivere un numero finivano tutti li'.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.models import User


@pytest.fixture
def client():
    # Gli endpoint sono dietro autenticazione: senza un utente questi test
    # riceverebbero 401 e non arriverebbero mai a leggere il messaggio di
    # validazione, che e' l'unica cosa che vogliono verificare.
    app.dependency_overrides[auth.get_current_user] = lambda: User(
        id="test", email="test@example.com", password_hash="x"
    )
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _detail(resp) -> str:
    body = resp.json()
    assert isinstance(body["detail"], str), "il dettaglio deve essere una frase, non una lista"
    return body["detail"]


def test_empty_field_says_so(client):
    resp = client.get("/api/analyze", params={"symbol": "BTCUSDT", "len": ""})
    assert resp.status_code == 422
    detail = _detail(resp)
    assert "len" in detail
    assert "empty" in detail


def test_decimal_in_an_integer_field_names_the_field_and_the_value(client):
    resp = client.get("/api/analyze", params={"symbol": "BTCUSDT", "len": "27.5"})
    assert resp.status_code == 422
    detail = _detail(resp)
    assert "len" in detail
    assert "whole number" in detail
    assert "27.5" in detail


def test_a_value_outside_the_allowed_set_is_named(client):
    resp = client.get("/api/analyze", params={"symbol": "BTCUSDT", "average": "WMA"})
    assert resp.status_code == 422
    assert "average" in _detail(resp)


def test_several_bad_fields_are_all_reported(client):
    resp = client.get("/api/analyze",
                      params={"symbol": "BTCUSDT", "len": "", "initial_capital": ""})
    assert resp.status_code == 422
    detail = _detail(resp)
    assert "len" in detail and "initial_capital" in detail
