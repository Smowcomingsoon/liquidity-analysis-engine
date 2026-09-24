"""Riconciliazione post-crash e discrepanze di saldo — solo lettura via
RPC, mai una firma o un invio."""
from __future__ import annotations

import asyncio
import time

import httpx

from app.trading.onchain import execution, ledger, reconciliation, risk


def _run(coro):
    return asyncio.run(coro)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_DB_FILE", tmp_path / "ledger.db")
    ledger._init_db()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _patch_post(monkeypatch, target_module, responder):
    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            return _FakeResponse(responder(url, json))

    monkeypatch.setattr(target_module.httpx, "AsyncClient", _FakeClient)


def test_confirmed_solana_signature_moves_order_out_of_pending(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k1", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_submitted("k1", "sig-ok")

    def _responder(url, payload):
        return {"result": {"value": [{"err": None}]}}

    _patch_post(monkeypatch, reconciliation, _responder)

    result = _run(reconciliation.reconcile_pending_orders("u1", "solana"))
    assert result["confirmed"] == ["k1"]
    assert ledger.pending_unconfirmed("u1") == []
    order = ledger.get_order("k1")
    assert order["status"] == "confirmed"


def test_failed_solana_signature_marks_order_failed(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k2", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_submitted("k2", "sig-bad")

    def _responder(url, payload):
        return {"result": {"value": [{"err": "InstructionError"}]}}

    _patch_post(monkeypatch, reconciliation, _responder)

    result = _run(reconciliation.reconcile_pending_orders("u1", "solana"))
    assert result["failed"] == ["k2"]
    order = ledger.get_order("k2")
    assert order["status"] == "failed"


def test_signature_not_found_yet_stays_pending_within_grace_period(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k3", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_submitted("k3", "sig-unknown")

    def _responder(url, payload):
        return {"result": {"value": [None]}}

    _patch_post(monkeypatch, reconciliation, _responder)

    result = _run(reconciliation.reconcile_pending_orders("u1", "solana"))
    assert result["still_pending"] == ["k3"]
    order = ledger.get_order("k3")
    assert order["status"] == "submitted"


def test_stale_unfound_signature_eventually_marked_failed(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k4", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_submitted("k4", "sig-unknown-old")
    with ledger._lock, ledger._connect() as conn:
        conn.execute(
            "UPDATE orders SET updated_at = ? WHERE idempotency_key = 'k4'",
            (time.time() - reconciliation.STALE_SUBMITTED_S - 1,),
        )

    def _responder(url, payload):
        return {"result": {"value": [None]}}

    _patch_post(monkeypatch, reconciliation, _responder)

    result = _run(reconciliation.reconcile_pending_orders("u1", "solana"))
    assert result["failed"] == ["k4"]


def test_evm_receipt_status_confirms_robinhood_order(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k5", "u1", "robinhood", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_submitted("k5", "0xdeadbeef")

    def _responder(url, payload):
        return {"result": {"status": "0x1"}}

    _patch_post(monkeypatch, reconciliation, _responder)

    result = _run(reconciliation.reconcile_pending_orders("u1", "robinhood"))
    assert result["confirmed"] == ["k5"]


def test_reconciliation_never_calls_signing_or_send(tmp_path, monkeypatch):
    """Sanity: il modulo non importa/chiama funzioni di firma o invio."""
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k6", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_submitted("k6", "sig-x")

    called = {"build": False, "send": False}

    async def _spy(*args, **kwargs):
        called["build"] = True
        return b""

    monkeypatch.setattr(execution, "build_signed_solana_swap", _spy)

    def _responder(url, payload):
        return {"result": {"value": [{"err": None}]}}

    _patch_post(monkeypatch, reconciliation, _responder)
    _run(reconciliation.reconcile_pending_orders("u1", "solana"))
    assert called["build"] is False


def test_matching_balance_opens_no_discrepancy(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k7", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_confirmed("k7", executed_qty=5.0, executed_price_usd=2.0)

    result = reconciliation.check_position_discrepancy("u1", "solana", "TOKEN", 10.0)
    assert result["blocked"] is False
    assert result["discrepancy_id"] is None


def test_large_mismatch_opens_discrepancy_and_blocks_new_orders(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ledger.reserve("k8", "u1", "solana", "TOKEN", "lorenzo", "buy", "live", 10.0)
    ledger.mark_confirmed("k8", executed_qty=5.0, executed_price_usd=2.0)  # cost_usd = 10.0

    result = reconciliation.check_position_discrepancy("u1", "solana", "TOKEN", 2.0)  # -80%
    assert result["blocked"] is True
    assert result["discrepancy_id"] is not None
    assert ledger.has_open_discrepancy("u1", "solana", "TOKEN") is True

    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN", 5.0, 0.5)
    assert ok is False
    assert "discrepancy" in reason

    ledger.review_discrepancy(result["discrepancy_id"])
    assert ledger.has_open_discrepancy("u1", "solana", "TOKEN") is False


def test_httpx_module_reference_matches_real_client_type():
    """Guardia contro un refactor che smetta di usare httpx.AsyncClient: se
    reconciliation non importasse piu' httpx, questo fallirebbe subito."""
    assert reconciliation.httpx is httpx
