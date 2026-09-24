"""Fase 4 — l'ordine vero: stessi guardrail di simulate.py, ma con firma e
(solo se acceso) invio reali. Firma e invio sono mockati qui: la loro
correttezza e' verificata altrove (test_onchain_execution.py) con rete vera."""
from __future__ import annotations

import asyncio
import uuid

from app.trading.onchain import execute, ledger


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_DB_FILE", tmp_path / "ledger.db")
    ledger._init_db()


async def _fake_build_signed(private_key_hex, quote):
    return b"signed-tx-bytes"


async def _fake_send(signed_tx_bytes):
    return "fake-signature-123"


def _quote():
    return {"in_amount": 1_000_000, "out_amount": 2_000_000, "price_impact_pct": 0.5, "raw": {}}


def test_unsupported_chain_is_rejected_honestly(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    result = _run(execute.execute_order(
        "u1", "ethereum", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is False
    assert "unsupported" in result["reason"]
    assert result["idempotency_key"] is None


def _robinhood_quote():
    return {
        "token_in": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "token_out": "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168",
        "fee": 3000, "amount_in": 10**16, "amount_out_minimum": 25_000_000,
    }


def test_robinhood_execution_disabled_signs_but_never_sends(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", False)

    async def _fake_build_robinhood(private_key_hex, token_in, token_out, fee, amount_in, amount_out_minimum):
        return b"signed-evm-tx"

    monkeypatch.setattr(execute.execution, "build_signed_robinhood_swap", _fake_build_robinhood)
    sent = {"called": False}

    async def _spy_send(raw_tx_bytes):
        sent["called"] = True
        return "should-not-happen"

    monkeypatch.setattr(execute.execution, "send_signed_evm_tx", _spy_send)

    result = _run(execute.execute_order(
        "u1", "robinhood", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _robinhood_quote(),
    ))
    assert result["accepted"] is False
    assert "disabled" in result["reason"]
    assert sent["called"] is False


def test_robinhood_execution_enabled_sends_and_confirms(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", True)

    async def _fake_build_robinhood(private_key_hex, token_in, token_out, fee, amount_in, amount_out_minimum):
        return b"signed-evm-tx"

    async def _fake_send_evm(raw_tx_bytes):
        return "0xfaketxhash"

    monkeypatch.setattr(execute.execution, "build_signed_robinhood_swap", _fake_build_robinhood)
    monkeypatch.setattr(execute.execution, "send_signed_evm_tx", _fake_send_evm)

    result = _run(execute.execute_order(
        "u1", "robinhood", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _robinhood_quote(),
    ))
    assert result["accepted"] is True
    assert result["tx_signature"] == "0xfaketxhash"
    order = ledger.get_order(result["idempotency_key"])
    assert order["status"] == "confirmed"


def test_rejected_by_risk_guardrail_never_signs(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    result = _run(execute.execute_order(
        "u1", "solana", "TOKEN", "lorenzo", "buy", 999.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is False
    assert "per-trade cap" in result["reason"]
    assert result["idempotency_key"] is None


def test_execution_disabled_signs_but_never_sends(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", False)
    # Jito spento qui: questo test verifica il ramo generico "esecuzione
    # disattivata", non il routing Jito (che ha i suoi test dedicati sotto).
    monkeypatch.setattr(execute.settings, "onchain_use_jito", False)
    monkeypatch.setattr(execute.execution, "build_signed_solana_swap", _fake_build_signed)
    sent = {"called": False}

    async def _spy_send(signed_tx_bytes):
        sent["called"] = True
        return "should-not-happen"

    monkeypatch.setattr(execute.execution, "send_signed_solana_tx", _spy_send)

    result = _run(execute.execute_order(
        "u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is False
    assert "disabled" in result["reason"]
    assert sent["called"] is False
    order = ledger.get_order(result["idempotency_key"])
    assert order["status"] == "failed"


def test_execution_enabled_sends_and_confirms(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", True)
    monkeypatch.setattr(execute.settings, "onchain_use_jito", False)
    monkeypatch.setattr(execute.execution, "build_signed_solana_swap", _fake_build_signed)
    monkeypatch.setattr(execute.execution, "send_signed_solana_tx", _fake_send)

    result = _run(execute.execute_order(
        "u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is True
    assert result["tx_signature"] == "fake-signature-123"
    order = ledger.get_order(result["idempotency_key"])
    assert order["status"] == "confirmed"
    pos = ledger.get_position("u1", "solana", "TOKEN")
    assert pos["quantity"] == 5.0  # 10 USD / 2.0 USD per unita'


def test_execution_enabled_with_jito_sends_bundle(tmp_path, monkeypatch):
    """Con onchain_use_jito acceso (default), il percorso Solana prova
    prima il bundle Jito (mockato: nessuna rete vera) e usa il bundle_id,
    prefissato 'jito:', come tx_signature."""
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", True)
    monkeypatch.setattr(execute.settings, "onchain_use_jito", True)
    monkeypatch.setattr(execute.execution, "build_signed_solana_swap", _fake_build_signed)

    async def _fake_price(chain):
        return 150.0

    async def _fake_tip_tx(private_key_hex, tip_account, tip_lamports):
        assert tip_account in execute.jito.JITO_TIP_ACCOUNTS
        assert tip_lamports >= execute.jito.JITO_MIN_TIP_LAMPORTS
        return b"signed-tip-tx"

    async def _fake_send_bundle(signed_tx_bytes, tip_tx_bytes):
        assert signed_tx_bytes == b"signed-tx-bytes"
        assert tip_tx_bytes == b"signed-tip-tx"
        return "bundle-abc123"

    async def _spy_send_direct(signed_tx_bytes):
        raise AssertionError("non deve ricadere sull'invio diretto se Jito funziona")

    monkeypatch.setattr(execute.prices, "native_price_usd", _fake_price)
    monkeypatch.setattr(execute.execution, "build_signed_solana_tip_tx", _fake_tip_tx)
    monkeypatch.setattr(execute.jito, "send_bundle_via_jito", _fake_send_bundle)
    monkeypatch.setattr(execute.execution, "send_signed_solana_tx", _spy_send_direct)

    result = _run(execute.execute_order(
        "u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is True
    assert result["tx_signature"] == "jito:bundle-abc123"


def test_jito_failure_falls_back_to_direct_send(tmp_path, monkeypatch):
    """Se Jito non risponde in modo sensato, l'ordine non va perso: ricade
    sull'invio diretto via RPC (il percorso esistente, mai rimosso)."""
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", True)
    monkeypatch.setattr(execute.settings, "onchain_use_jito", True)
    monkeypatch.setattr(execute.execution, "build_signed_solana_swap", _fake_build_signed)

    async def _fake_price(chain):
        return 150.0

    async def _broken_tip_tx(private_key_hex, tip_account, tip_lamports):
        raise RuntimeError("RPC blockhash non disponibile in questo test")

    monkeypatch.setattr(execute.prices, "native_price_usd", _fake_price)
    monkeypatch.setattr(execute.execution, "build_signed_solana_tip_tx", _broken_tip_tx)
    monkeypatch.setattr(execute.execution, "send_signed_solana_tx", _fake_send)

    result = _run(execute.execute_order(
        "u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is True
    assert result["tx_signature"] == "fake-signature-123"


def test_duplicate_idempotency_key_never_signs_twice(tmp_path, monkeypatch):
    """Un retry con la stessa idempotency_key (es. un timeout di rete su una
    firma gia' andata a buon fine) non deve mai firmare una seconda volta."""
    _fresh_db(tmp_path, monkeypatch)
    monkeypatch.setattr(execute.settings, "onchain_execution_enabled", True)

    fixed = uuid.UUID("11111111-1111-1111-1111-111111111111")
    monkeypatch.setattr(execute.uuid, "uuid4", lambda: fixed)
    # Un ordine con quella chiave e' gia' "in corso" (pending), senza aver
    # ancora aggiornato posizioni: la concentrazione resta a zero.
    ledger.reserve(f"live-{fixed.hex}", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)

    signed = {"called": False}

    async def _spy_build(private_key_hex, quote):
        signed["called"] = True
        return b"should-not-happen"

    monkeypatch.setattr(execute.execution, "build_signed_solana_swap", _spy_build)
    monkeypatch.setattr(execute.execution, "send_signed_solana_tx", _fake_send)

    result = _run(execute.execute_order(
        "u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0, "deadbeef", _quote(),
    ))
    assert result["accepted"] is False
    assert "duplicate" in result["reason"]
    assert signed["called"] is False
