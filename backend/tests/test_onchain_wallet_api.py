"""Flusso vero (via TestClient, non solo le funzioni interne) di creazione,
saldo e rivelazione approvata dal 'telefono' di un wallet on-chain —
nessuna rete reale: solo l'RPC del saldo e' finto."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.trading.onchain import reveal_requests as onchain_reveal

_PASSWORD = "correct-horse-battery-9"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    auth._users.clear()
    user = auth.create_user("wallet-test@example.com", _PASSWORD)
    onchain_reveal._requests.clear()
    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.get_user(user.id)
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


def test_onchain_pages_load_without_a_session():
    """La shell HTML deve caricarsi sempre, come "/" — altrimenti
    onchain-approve.html non si potrebbe MAI apire da un telefono che non
    ha ancora fatto login, il caso normale di chi scansiona il QR."""
    assert main.app.dependency_overrides == {}, "questo test vuole il cancello vero, non un mock"
    with TestClient(main.app) as anon:
        for path in ("/onchain-app.html", "/onchain-approve.html", "/onchain-passkey-setup.html"):
            resp = anon.get(path)
            assert resp.status_code == 200, path
            assert "text/html" in resp.headers["content-type"]


def test_create_then_list_wallet(client):
    resp = client.post("/api/onchain/wallets", json={"chain": "solana"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["address"]
    assert body["enabled"] is False

    listed = client.get("/api/onchain/wallets").json()
    assert listed[0]["address"] == body["address"]


def test_create_is_idempotent(client):
    a = client.post("/api/onchain/wallets", json={"chain": "solana"}).json()
    b = client.post("/api/onchain/wallets", json={"chain": "solana"}).json()
    assert a["address"] == b["address"]


def test_reveal_without_request_token_is_rejected(client):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    resp = client.post("/api/onchain/wallets/solana/reveal", json={"token": "not-a-real-token"})
    assert resp.status_code == 403


def test_full_reveal_flow_requires_phone_approval(client):
    client.post("/api/onchain/wallets", json={"chain": "robinhood"})
    req = client.post("/api/onchain/wallets/robinhood/reveal/request").json()
    token = req["token"]
    assert req["qr_data_uri"].startswith("data:image/png;base64,")
    assert token in req["approve_url"]

    # non ancora approvato dal "telefono": la rivelazione deve restare bloccata
    still_pending = client.post("/api/onchain/wallets/robinhood/reveal", json={"token": token})
    assert still_pending.status_code == 403
    assert client.get(f"/api/onchain/wallets/reveal/status/{token}").json()["status"] == "pending"

    # password sbagliata sul "telefono": non approva niente
    wrong = client.post("/api/onchain/reveal/approve", json={
        "email": "wallet-test@example.com", "password": "wrong-password", "token": token,
    })
    assert wrong.status_code == 401

    ok = client.post("/api/onchain/reveal/approve", json={
        "email": "wallet-test@example.com", "password": _PASSWORD, "token": token,
    })
    assert ok.status_code == 200
    assert client.get(f"/api/onchain/wallets/reveal/status/{token}").json()["status"] == "approved"

    revealed = client.post("/api/onchain/wallets/robinhood/reveal", json={"token": token})
    assert revealed.status_code == 200
    body = revealed.json()
    assert body["private_key"].startswith("0x")
    assert body["address"]

    # uso unico: lo stesso token non rivela una seconda volta
    again = client.post("/api/onchain/wallets/robinhood/reveal", json={"token": token})
    assert again.status_code == 403


def test_reveal_token_scoped_to_its_own_chain(client):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    client.post("/api/onchain/wallets", json={"chain": "robinhood"})
    req = client.post("/api/onchain/wallets/solana/reveal/request").json()
    client.post("/api/onchain/reveal/approve", json={
        "email": "wallet-test@example.com", "password": _PASSWORD, "token": req["token"],
    })
    # approvato per solana, non vale per robinhood
    wrong_chain = client.post("/api/onchain/wallets/robinhood/reveal", json={"token": req["token"]})
    assert wrong_chain.status_code == 403


def test_reveal_stream_yields_approved_immediately_if_already_approved(client):
    client.post("/api/onchain/wallets", json={"chain": "solana"})
    req = client.post("/api/onchain/wallets/solana/reveal/request").json()
    token = req["token"]
    client.post("/api/onchain/reveal/approve", json={
        "email": "wallet-test@example.com", "password": _PASSWORD, "token": token,
    })
    with client.stream("GET", f"/api/onchain/wallets/reveal/stream/{token}") as resp:
        assert resp.status_code == 200
        line = next(resp.iter_lines())
        assert "approved" in line


def test_reveal_stream_pushes_the_moment_the_phone_approves(client):
    import concurrent.futures

    client.post("/api/onchain/wallets", json={"chain": "solana"})
    req = client.post("/api/onchain/wallets/solana/reveal/request").json()
    token = req["token"]
    seen: list[str] = []

    def read_stream():
        with client.stream("GET", f"/api/onchain/wallets/reveal/stream/{token}") as resp:
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    seen.append(line)
                if "approved" in line:
                    break

    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(read_stream)
        import time
        time.sleep(0.5)
        client.post("/api/onchain/reveal/approve", json={
            "email": "wallet-test@example.com", "password": _PASSWORD, "token": token,
        })
        future.result(timeout=10)

    assert any("pending" in line for line in seen)
    assert any("approved" in line for line in seen)


def test_kill_switch_blocks_every_onchain_endpoint(client, monkeypatch):
    monkeypatch.setattr(main.settings, "onchain_enabled", False)
    assert client.get("/api/onchain/wallets").status_code == 400
    assert client.post("/api/onchain/wallets", json={"chain": "solana"}).status_code == 400


def test_passkey_register_options_requires_session(client):
    # sessione presente in questo fixture (dependency_overrides) — verifica solo che risponda con opzioni valide
    resp = client.post("/api/auth/passkey/register/options")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state_token"]
    assert '"challenge"' in body["options"]


def test_passkey_register_verify_rejects_bad_credential(client):
    opts = client.post("/api/auth/passkey/register/options").json()
    resp = client.post("/api/auth/passkey/register/verify", json={
        "state_token": opts["state_token"], "credential": {"id": "not-real"},
    })
    assert resp.status_code == 400


def test_reveal_passkey_options_without_any_passkey_registered(client):
    resp = client.post("/api/onchain/reveal/passkey/options", json={"email": "wallet-test@example.com"})
    assert resp.status_code == 400


def test_reveal_passkey_verify_with_unknown_email(client):
    resp = client.post("/api/onchain/reveal/passkey/verify", json={
        "email": "nobody@example.com", "token": "x", "state_token": "y", "credential": {"id": "z"},
    })
    assert resp.status_code == 401


def test_balance_reads_public_rpc(client, monkeypatch):
    client.post("/api/onchain/wallets", json={"chain": "solana"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": 2_000_000_000}})

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("proxies", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    resp = client.get("/api/onchain/wallets/solana/balance")
    assert resp.status_code == 200
    assert resp.json()["native_balance"] == pytest.approx(2.0)


def test_balance_includes_usd_value_from_price_feed(client, monkeypatch):
    from app.trading.onchain import prices as onchain_prices

    client.post("/api/onchain/wallets", json={"chain": "solana"})
    onchain_prices._cache.clear()
    onchain_prices._btc_cache = None

    def rpc_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": 2_000_000_000}})

    def price_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"attributes": {"token_prices": {"x": "150.0"}}}})

    real_client = httpx.AsyncClient

    def dispatch(request: httpx.Request) -> httpx.Response:
        if "kraken" in str(request.url):
            return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {"c": ["60000.0", "1"]}}})
        return price_handler(request) if "geckoterminal" in str(request.url) else rpc_handler(request)

    def factory(*args, **kwargs):
        kwargs.pop("proxies", None)
        return real_client(*args, transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    resp = client.get("/api/onchain/wallets/solana/balance")
    body = resp.json()
    assert body["native_price_usd"] == pytest.approx(150.0)
    assert body["native_usd_value"] == pytest.approx(300.0)  # 2.0 SOL * 150 USD
    assert body["btc_price_usd"] == pytest.approx(60000.0)


def test_balance_native_usd_value_is_none_when_price_feed_fails(client, monkeypatch):
    from app.trading.onchain import prices as onchain_prices

    client.post("/api/onchain/wallets", json={"chain": "solana"})
    onchain_prices._cache.clear()
    onchain_prices._btc_cache = None

    def handler(request: httpx.Request) -> httpx.Response:
        if "geckoterminal" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"value": 2_000_000_000}})

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("proxies", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    resp = client.get("/api/onchain/wallets/solana/balance")
    body = resp.json()
    assert body["native_price_usd"] is None
    assert body["native_usd_value"] is None
    assert body["btc_price_usd"] is None
