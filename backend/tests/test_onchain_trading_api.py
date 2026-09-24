"""Endpoint di Fase 2/3: quote (vera, via rete), simulazione, ledger,
posizioni, stato del rischio, reset del circuit breaker."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.trading.onchain import execute as onchain_execute
from app.trading.onchain import ledger as onchain_ledger

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


def test_quote_solana_real_network_call(client):
    resp = client.post("/api/onchain/quote/solana", json={
        "input_mint": SOL_MINT, "output_mint": USDC_MINT, "amount_lamports": 10_000_000,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["in_amount"] == 10_000_000
    assert body["out_amount"] > 0
    assert body["price_impact_pct"] >= 0


def test_simulate_then_positions_and_risk_status(client):
    resp = client.post("/api/onchain/simulate", json={
        "chain": "solana", "token": "TOKEN", "strategy_name": "lorenzo", "side": "buy",
        "notional_usd": 15.0, "price_impact_pct": 0.5, "price_usd": 2.0,
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True

    positions = client.get("/api/onchain/positions/solana").json()
    assert positions == [{"token": "TOKEN", "quantity": 7.5, "cost_usd": 15.0}]

    status = client.get("/api/onchain/risk/status/solana").json()
    assert status["daily_spent_usd"] == 15.0
    assert status["daily_cap_usd"] == 100.0
    assert status["per_trade_cap_usd"] == 20.0
    assert status["execution_enabled"] is False
    assert status["circuit_breaker"]["tripped"] is False


def test_simulate_rejected_by_cap_reports_reason(client):
    resp = client.post("/api/onchain/simulate", json={
        "chain": "solana", "token": "TOKEN", "strategy_name": "lorenzo", "side": "buy",
        "notional_usd": 999.0, "price_impact_pct": 0.5, "price_usd": 2.0,
    })
    body = resp.json()
    assert body["accepted"] is False
    assert "cap" in body["reason"]


def test_circuit_breaker_reset_via_api(client):
    owner_id = client.owner_id
    for _ in range(5):
        onchain_ledger.record_trade_outcome(owner_id, "solana", -1.0)
    assert client.get("/api/onchain/risk/status/solana").json()["circuit_breaker"]["tripped"] is True

    resp = client.post("/api/onchain/risk/circuit-breaker/solana/reset")
    assert resp.status_code == 200
    assert resp.json()["tripped"] is False
    assert client.get("/api/onchain/risk/status/solana").json()["circuit_breaker"]["tripped"] is False


def test_execute_solana_real_quote_signs_but_does_not_send(client, monkeypatch):
    """Quote reale via Jupiter (rete vera), firma mockata: verifica il
    percorso end-to-end dell'endpoint senza mai toccare la rete Solana per
    l'invio. onchain_execution_enabled resta False (default): il guardrail
    finale deve fermarsi lui, non l'endpoint."""
    auth.create_onchain_wallet(client.owner_id, "solana")

    async def _fake_build(private_key_hex, quote):
        return b"signed-tx-bytes"

    monkeypatch.setattr(onchain_execute.execution, "build_signed_solana_swap", _fake_build)

    resp = client.post("/api/onchain/execute/solana", json={
        "token": "USDC", "strategy_name": "lorenzo", "side": "buy",
        "input_mint": SOL_MINT, "output_mint": USDC_MINT, "amount_lamports": 10_000_000,
        "notional_usd": 15.0, "price_usd": 2.0,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert "disabled" in body["reason"]


def test_execute_solana_without_wallet_is_rejected(client):
    resp = client.post("/api/onchain/execute/solana", json={
        "token": "USDC", "strategy_name": "lorenzo", "side": "buy",
        "input_mint": SOL_MINT, "output_mint": USDC_MINT, "amount_lamports": 10_000_000,
        "notional_usd": 15.0, "price_usd": 2.0,
    })
    assert resp.status_code == 400


ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
ROBINHOOD_USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"


def test_execute_robinhood_real_quote_signs_but_does_not_send(client, monkeypatch):
    """Quote reale via Factory + QuoterV2 (rete vera), firma mockata —
    stesso schema del test Solana equivalente. execution_enabled resta
    False (default): l'endpoint deve fermarsi al guardrail finale."""
    auth.create_onchain_wallet(client.owner_id, "robinhood")

    async def _fake_build(private_key_hex, token_in, token_out, fee, amount_in, amount_out_minimum):
        return b"signed-evm-tx"

    monkeypatch.setattr(onchain_execute.execution, "build_signed_robinhood_swap", _fake_build)

    resp = client.post("/api/onchain/execute/robinhood", json={
        "token": "USDG", "strategy_name": "lorenzo", "side": "buy",
        "token_in": ROBINHOOD_WETH, "token_out": ROBINHOOD_USDG, "fee": 3000,
        "amount_in": 10**16, "notional_usd": 15.0, "price_usd": 1.0,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert "disabled" in body["reason"]


def test_quote_robinhood_real_network_call(client):
    resp = client.post("/api/onchain/quote/robinhood", json={
        "token_in": ROBINHOOD_WETH, "token_out": ROBINHOOD_USDG, "fee": 3000, "amount_in": 10**16,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["out_amount"] > 0
    assert body["price_impact_pct"] >= 0
    # con l'RPC giu' il preventivo arriva dall'aggregatore, senza pool v3
    assert body["route"] == "aggregator" or body["pool"].startswith("0x")


def test_quote_robinhood_without_a_pool_is_rejected(client):
    resp = client.post("/api/onchain/quote/robinhood", json={
        "token_in": ROBINHOOD_WETH, "token_out": "0x000000000000000000000000000000000000dEaD",
        "fee": 3000, "amount_in": 10**16,
    })
    assert resp.status_code == 400
    assert "No market" in resp.json()["detail"]


def test_execute_robinhood_without_wallet_is_rejected(client):
    resp = client.post("/api/onchain/execute/robinhood", json={
        "token": "USDG", "strategy_name": "lorenzo", "side": "buy",
        "token_in": ROBINHOOD_WETH, "token_out": ROBINHOOD_USDG, "fee": 3000,
        "amount_in": 10**16, "notional_usd": 15.0, "price_usd": 1.0,
    })
    assert resp.status_code == 400


def test_execute_robinhood_unknown_pool_is_rejected(client):
    auth.create_onchain_wallet(client.owner_id, "robinhood")
    fake_token = "0x000000000000000000000000000000000000dEaD"
    resp = client.post("/api/onchain/execute/robinhood", json={
        "token": "FAKE", "strategy_name": "lorenzo", "side": "buy",
        "token_in": ROBINHOOD_WETH, "token_out": fake_token, "fee": 3000,
        "amount_in": 10**16, "notional_usd": 15.0, "price_usd": 1.0,
    })
    assert resp.status_code == 400
    assert "No pool" in resp.json()["detail"]


def test_kill_switch_blocks_the_new_endpoints_too(client, monkeypatch):
    monkeypatch.setattr(main.settings, "onchain_enabled", False)
    assert client.post("/api/onchain/quote/solana", json={
        "input_mint": SOL_MINT, "output_mint": USDC_MINT, "amount_lamports": 1,
    }).status_code == 400
    assert client.get("/api/onchain/positions/solana").status_code == 400
    assert client.get("/api/onchain/risk/status/solana").status_code == 400
