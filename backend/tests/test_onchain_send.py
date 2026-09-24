"""Prelievo: mandare crypto (nativo o token) a un indirizzo qualsiasi —
Lorenzo: "aggiungi funzione anche per prelevare le crypto e inviarle ad
indirizzi". Stessi guardrail di un ordine manuale, mai la concentrazione
(un prelievo riduce sempre una posizione)."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.trading.onchain import ledger as onchain_ledger
from app.trading.onchain import risk


def _run(coro):
    return asyncio.run(coro)


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


def _create_wallet(client, chain="solana"):
    resp = client.post("/api/onchain/wallets", json={"chain": chain})
    assert resp.status_code == 200


def test_send_requires_a_wallet(client):
    resp = client.post("/api/onchain/send", json={
        "chain": "solana", "to_address": "Dest1111111111111111111111111111111111111",
        "amount": 0.01, "price_usd": 150.0,
    })
    assert resp.status_code == 400


def test_send_native_disabled_signs_but_never_sends(client, monkeypatch):
    _create_wallet(client)
    monkeypatch.setattr(main.settings, "onchain_execution_enabled", False)

    async def _fake_build(private_key_hex, to_address, amount_raw):
        return b"signed-native-transfer"

    sent = {"called": False}

    async def _spy_send(signed_tx_bytes):
        sent["called"] = True
        return "should-not-happen"

    monkeypatch.setattr(main.onchain_send.execution, "build_signed_solana_native_transfer", _fake_build)
    monkeypatch.setattr(main.onchain_send.execution, "send_signed_solana_tx", _spy_send)

    resp = client.post("/api/onchain/send", json={
        "chain": "solana", "to_address": "Dest1111111111111111111111111111111111111",
        "amount": 0.01, "price_usd": 150.0,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert "disabled" in body["reason"]
    assert sent["called"] is False


def test_send_native_enabled_sends_and_confirms(client, monkeypatch):
    _create_wallet(client)
    monkeypatch.setattr(main.settings, "onchain_execution_enabled", True)

    async def _fake_build(private_key_hex, to_address, amount_raw):
        assert amount_raw == int(0.01 * 10**9)  # 9 decimali di SOL, non indovinati
        return b"signed-native-transfer"

    async def _fake_send(signed_tx_bytes):
        return "fake-signature-456"

    monkeypatch.setattr(main.onchain_send.execution, "build_signed_solana_native_transfer", _fake_build)
    monkeypatch.setattr(main.onchain_send.execution, "send_signed_solana_tx", _fake_send)

    resp = client.post("/api/onchain/send", json={
        "chain": "solana", "to_address": "Dest1111111111111111111111111111111111111",
        "amount": 0.01, "price_usd": 150.0,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True
    assert body["tx_signature"] == "fake-signature-456"
    order = onchain_ledger.get_order(body["idempotency_key"])
    assert order["status"] == "confirmed"
    assert order["side"] == "sell"


def test_send_rejected_by_per_trade_cap_never_signs(client, monkeypatch):
    _create_wallet(client)
    monkeypatch.setattr(main.settings, "onchain_execution_enabled", True)

    async def _spy_build(private_key_hex, to_address, amount_raw):
        raise AssertionError("non deve mai firmare se il guardrail rifiuta")

    monkeypatch.setattr(main.onchain_send.execution, "build_signed_solana_native_transfer", _spy_build)

    resp = client.post("/api/onchain/send", json={
        "chain": "solana", "to_address": "Dest1111111111111111111111111111111111111",
        "amount": 1.0, "price_usd": 999.0,  # notional_usd = 999, ben sopra il tetto di 20
    })
    body = resp.json()
    assert body["accepted"] is False
    assert "per-trade cap" in body["reason"]


def test_send_manual_strategy_skips_concentration(client, monkeypatch):
    """Un prelievo e' sempre strategy_name='manuale': stesso bypass della
    concentrazione gia' deciso per lo swap manuale (risk.py) — riduce
    sempre una posizione, non la costruisce."""
    _create_wallet(client)
    monkeypatch.setattr(main.settings, "onchain_execution_enabled", True)
    called = {"concentration": False}

    def _spy_concentration(*args):
        called["concentration"] = True
        return False, "non dovrebbe mai arrivare qui per un prelievo"

    async def _fake_build(private_key_hex, to_address, amount_raw):
        return b"signed"

    async def _fake_send(signed_tx_bytes):
        return "sig"

    monkeypatch.setattr(risk, "check_concentration", _spy_concentration)
    monkeypatch.setattr(main.onchain_send.execution, "build_signed_solana_native_transfer", _fake_build)
    monkeypatch.setattr(main.onchain_send.execution, "send_signed_solana_tx", _fake_send)

    resp = client.post("/api/onchain/send", json={
        "chain": "solana", "to_address": "Dest1111111111111111111111111111111111111",
        "amount": 0.01, "price_usd": 150.0,
    })
    assert resp.json()["accepted"] is True
    assert called["concentration"] is False


def test_send_token_resolves_decimals_and_uses_evm_transfer(client, monkeypatch):
    _create_wallet(client, "robinhood")
    monkeypatch.setattr(main.settings, "onchain_execution_enabled", True)

    async def _fake_decimals(chain, address):
        return 6  # USDG

    async def _fake_build_token(private_key_hex, token_address, to_address, amount_raw):
        assert amount_raw == int(5.5 * 10**6)
        return b"signed-erc20-transfer"

    async def _fake_send_evm(raw_tx_bytes):
        return "0xdeadbeef"

    monkeypatch.setattr(main.onchain_token_metadata, "get_token_decimals", _fake_decimals)
    monkeypatch.setattr(main.onchain_send.execution, "build_signed_evm_token_transfer", _fake_build_token)
    monkeypatch.setattr(main.onchain_send.execution, "send_signed_evm_tx", _fake_send_evm)

    resp = client.post("/api/onchain/send", json={
        "chain": "robinhood", "to_address": "0x000000000000000000000000000000000000dEaD",
        "token_address": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "amount": 5.5, "price_usd": 1.0,
    })
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
