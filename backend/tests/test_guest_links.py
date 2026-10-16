"""Link ospite: sola lettura imposta dal server, revoca immediata, scadenza."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import auth, guest, main

_PASSWORD = "guest-links-pass-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(guest, "_LINKS_FILE", tmp_path / "guest_links.json")
    monkeypatch.setattr(main.settings, "admin_emails", "admin@example.com")
    auth._users.clear()
    guest._links.clear()
    admin = auth.create_user("admin@example.com", _PASSWORD)
    other = auth.create_user("other@example.com", _PASSWORD)
    assert main.app.dependency_overrides == {}, "serve il cancello vero"
    yield admin, other
    guest._links.clear()


def _login(client: TestClient, email: str) -> None:
    resp = client.post("/api/auth/login", json={"email": email, "password": _PASSWORD})
    assert resp.status_code == 200, resp.text


def _new_link(days: int = 7) -> str:
    with TestClient(main.app) as admin:
        _login(admin, "admin@example.com")
        resp = admin.post("/api/guest-links", json={"label": "Marco", "days": days})
        assert resp.status_code == 200, resp.text
        return resp.json()["url"]


def _enter(url: str) -> TestClient:
    client = TestClient(main.app)
    token = url.split("t=", 1)[1]
    resp = client.get(f"/ospite?t={token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onchain-app.html"
    return client


def test_only_admin_manages_links(env):
    with TestClient(main.app) as anon:
        assert anon.post("/api/guest-links", json={}).status_code == 401
    with TestClient(main.app) as other:
        _login(other, "other@example.com")
        assert other.post("/api/guest-links", json={}).status_code == 403
        assert other.get("/admin/ospiti").status_code == 403
    with TestClient(main.app) as admin:
        _login(admin, "admin@example.com")
        assert admin.get("/admin/ospiti").status_code == 200


def test_guest_reads_but_cannot_write(env):
    client = _enter(_new_link())
    me = client.get("/api/auth/me").json()["user"]
    assert me["guest"] is True and me["email"] == "ospite"
    page = client.get("/onchain-app.html")
    assert page.status_code == 200 and "/static/onchain/main.js" in page.text
    assert client.get("/api/strategies").status_code == 200
    assert client.get("/api/watchlist").json() == []

    for method, path, body in [
        ("POST", "/api/watchlist", {"symbol": "BTCUSDT", "asset_type": "crypto"}),
        ("POST", "/api/onchain/wallets", {"chain": "solana"}),
        ("PUT", "/api/preferences", {}),
        ("POST", "/api/issues", {"title": "x"}),
        ("PATCH", "/api/onchain/sniper-autotrade/solana", {"enabled": True}),
        ("POST", "/api/trading/order", {}),
        ("POST", "/api/guest-links", {}),
        ("DELETE", "/api/alerts/log", None),
    ]:
        resp = client.request(method, path, json=body)
        assert resp.status_code == 403, (method, path, resp.status_code)
        assert "read-only" in resp.json()["detail"]

    for path in ("/admin/status", "/admin/ospiti", "/api/errors", "/api/optimize", "/api/guest-links"):
        assert client.get(path).status_code == 403, path


def test_revoke_closes_open_sessions(env):
    url = _new_link()
    client = _enter(url)
    assert client.get("/api/strategies").status_code == 200
    link_id = guest.list_links()[0].id
    with TestClient(main.app) as admin:
        _login(admin, "admin@example.com")
        assert admin.delete(f"/api/guest-links/{link_id}").status_code == 200
    assert client.get("/api/strategies").status_code == 401
    assert client.get(f"/ospite?t={url.split('t=', 1)[1]}", follow_redirects=False).status_code == 410


def test_expired_link_is_refused(env):
    url = _new_link()
    link = guest.list_links()[0]
    client = _enter(url)
    guest._links[link.id] = link.model_copy(update={"expires_at": int(time.time()) - 1})
    assert client.get("/api/strategies").status_code == 401
    assert TestClient(main.app).get(f"/ospite?t={link.token}", follow_redirects=False).status_code == 410


def test_bad_token_and_forged_cookie(env):
    _new_link()
    anon = TestClient(main.app)
    assert anon.get("/ospite?t=nope", follow_redirects=False).status_code == 410
    link = guest.list_links()[0]
    anon.cookies.set(guest.GUEST_COOKIE, f"{link.id}:{link.expires_at}:{'0' * 64}")
    assert anon.get("/api/strategies").status_code == 401


def test_real_session_wins_over_guest_cookie(env):
    client = _enter(_new_link())
    _login(client, "other@example.com")
    assert client.get("/api/auth/me").json()["user"]["email"] == "other@example.com"
    assert client.get("/api/watchlist").status_code == 200
