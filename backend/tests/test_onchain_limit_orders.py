"""Piano DEX: ordini limite — creazione, elenco, annullamento, e il ciclo
che li esegue quando il prezzo tocca il target (motore mockato: la rete
vera per quote/prezzo/chiave e' fuori scope qui, verificata altrove)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.trading.onchain import ledger as onchain_ledger
from app.trading.onchain import prices as onchain_prices

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(onchain_ledger, "_DB_FILE", tmp_path / "ledger.db")
    onchain_ledger._init_db()
    auth._users.clear()
    user = auth.create_user("trader@example.com", "correct-horse-battery-9")
    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.get_user(user.id)
    with TestClient(main.app) as c:
        c.owner_id = user.id
        yield c
    main.app.dependency_overrides.clear()


def _create_wallet(client):
    resp = client.post("/api/onchain/wallets", json={"chain": "solana"})
    assert resp.status_code == 200


def test_limit_order_requires_a_native_leg(client):
    _create_wallet(client)
    resp = client.post("/api/onchain/limit-orders", json={
        "chain": "solana", "token_in": USDC_MINT, "token_out": "SomeOtherTokenMint111111111111111111111111",
        "token_in_symbol": "USDC", "token_out_symbol": "OTHER",
        "token_in_decimals": 6, "token_out_decimals": 9,
        "amount_in": 10.0, "direction": "buy_below", "target_price_usd": 1.0,
    })
    assert resp.status_code == 400
    assert "native" in resp.json()["detail"]


def test_create_list_and_cancel_limit_order(client):
    _create_wallet(client)
    created = client.post("/api/onchain/limit-orders", json={
        "chain": "solana", "token_in": SOL_MINT, "token_out": USDC_MINT,
        "token_in_symbol": "SOL", "token_out_symbol": "USDC",
        "token_in_decimals": 9, "token_out_decimals": 6,
        "amount_in": 0.5, "direction": "sell_above", "target_price_usd": 300.0,
    }).json()
    assert created["status"] == "open"

    listed = client.get("/api/onchain/limit-orders/solana").json()
    assert len(listed) == 1 and listed[0]["id"] == created["id"]

    history = client.get("/api/onchain/history/solana").json()
    assert history["limit_orders"][0]["id"] == created["id"]

    cancel = client.delete(f"/api/onchain/limit-orders/{created['id']}")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True

    # Un ordine gia' cancellato non si puo' cancellare una seconda volta.
    assert client.delete(f"/api/onchain/limit-orders/{created['id']}").status_code == 404


def test_limit_order_loop_fills_when_target_is_reached(client, monkeypatch):
    _create_wallet(client)
    order = client.post("/api/onchain/limit-orders", json={
        "chain": "solana", "token_in": USDC_MINT, "token_out": SOL_MINT,
        "token_in_symbol": "USDC", "token_out_symbol": "SOL",
        "token_in_decimals": 6, "token_out_decimals": 9,
        "amount_in": 20.0, "direction": "buy_below", "target_price_usd": 150.0,
    }).json()

    # token_in e' USDC, non il nativo: il nativo di solana e' SOL (token_out).
    # amount_in=20 USDC -> out_amount deve rendere un prezzo SOL <= 150 per
    # scattare buy_below. native_is_in=False -> price = out_amount*native_usd/amount_in.
    # Con native_usd=150 e out_amount=20/150=0.1333 SOL, price=0.1333*150/20=1.0 <= 150: scatta sempre.
    async def _fake_native_price_150(chain):
        return 150.0

    async def _fake_jupiter_quote(input_mint, output_mint, amount, slippage_bps):
        return {"in_amount": amount, "out_amount": int(0.1333 * 10**9), "price_impact_pct": 0.1, "raw": {}}

    async def _fake_execute_order(*args, **kwargs):
        return {"accepted": True, "reason": "", "idempotency_key": "live-fake", "tx_signature": "sig", "correlation_id": "c1"}

    def _fake_decrypt(user, chain):
        return "deadbeef"

    monkeypatch.setattr(onchain_prices, "native_price_usd", _fake_native_price_150)
    monkeypatch.setattr(main.onchain_quotes, "jupiter_quote", _fake_jupiter_quote)
    monkeypatch.setattr(main.onchain_execute, "execute_order", _fake_execute_order)
    monkeypatch.setattr(auth, "get_decrypted_onchain_private_key", _fake_decrypt)

    asyncio.run(main._check_limit_order(onchain_ledger.get_limit_order(order["id"])))

    refreshed = onchain_ledger.get_limit_order(order["id"])
    assert refreshed["status"] == "filled"
    assert refreshed["filled_idempotency_key"] == "live-fake"
