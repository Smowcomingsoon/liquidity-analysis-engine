"""Fase 2 — paper trading: stessi guardrail della Fase 3, ma senza mai
toccare una chiave o una rete. Se un ordine viene rifiutato in simulazione,
lo sarebbe anche in reale — questo e' il punto."""

from __future__ import annotations

import asyncio

from app.trading.onchain import ledger, simulate


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_DB_FILE", tmp_path / "ledger.db")
    ledger._init_db()


def test_accepted_order_lands_in_the_ledger_as_simulated(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    result = _run(simulate.simulate_order("u1", "solana", "TOKEN", "lorenzo", "buy", 15.0, 0.5, 2.0))
    assert result["accepted"] is True
    order = ledger.get_order(result["idempotency_key"])
    assert order["mode"] == "simulated"
    assert order["status"] == "confirmed"

    pos = ledger.get_position("u1", "solana", "TOKEN")
    assert pos["quantity"] == 7.5  # 15 USD / 2.0 USD per unita'


def test_rejected_by_per_trade_cap_never_touches_the_ledger(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    result = _run(simulate.simulate_order("u1", "solana", "TOKEN", "lorenzo", "buy", 999.0, 0.5, 2.0))
    assert result["accepted"] is False
    assert "per-trade cap" in result["reason"]
    assert result["idempotency_key"] is None
    assert ledger.get_position("u1", "solana", "TOKEN") == {"quantity": 0.0, "cost_usd": 0.0}


def test_rejected_by_price_impact_never_touches_the_ledger(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    result = _run(simulate.simulate_order("u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 9.0, 2.0))
    assert result["accepted"] is False
    assert "price impact" in result["reason"]


def test_tripped_circuit_breaker_blocks_simulation_too(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for _ in range(5):
        ledger.record_trade_outcome("u1", "solana", -1.0)
    result = _run(simulate.simulate_order("u1", "solana", "TOKEN", "lorenzo", "buy", 10.0, 0.5, 2.0))
    assert result["accepted"] is False
    assert "circuit breaker" in result["reason"]
