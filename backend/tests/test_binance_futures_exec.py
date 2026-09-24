"""Esecuzione futures su Binance — mai una vera chiamata di rete: ogni test
sostituisce httpx.AsyncClient con un trasporto finto (httpx.MockTransport),
cosi' questi test non possono mai spendere margine vero per sbaglio.
"""

from __future__ import annotations

import asyncio
import urllib.parse

import httpx
import pytest

from app.trading import binance_futures_exec as bfe


_RealAsyncClient = httpx.AsyncClient


def _client_factory(handler):
    def factory(*, base_url, headers, timeout):
        transport = httpx.MockTransport(handler)
        return _RealAsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport)
    return factory


def _run(coro):
    return asyncio.run(coro)


def test_place_order_sets_isolated_margin_and_leverage_before_ordering(monkeypatch):
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(200, json={"code": 200, "msg": "success"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 5, "symbol": "BTCUSDT"})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"markPrice": "50000.0"})
        if request.url.path == "/fapi/v1/order":
            return httpx.Response(200, json={"orderId": 1, "status": "FILLED"})
        raise AssertionError(f"unexpected path {request.url.path}")

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    result = _run(bfe.place_futures_order("BTCUSDT", "BUY", "fixed_usdt", 100.0, 5, "key", "secret"))
    assert result["orderId"] == 1
    # Isolated margin e leva vanno dichiarate PRIMA dell'ordine, sempre in
    # quest'ordine: un ordine mandato con la leva sbagliata di prima non e'
    # un dettaglio, e' l'intero senso del controllo.
    assert calls[0] == "/fapi/v1/marginType"
    assert calls[1] == "/fapi/v1/leverage"
    assert calls[-1] == "/fapi/v1/order"


def test_already_isolated_is_not_an_error(monkeypatch):
    """Binance risponde 400 'No need to change margin type' se il simbolo
    e' gia' isolated — non deve bloccare l'ordine."""
    def handler(request):
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change margin type."})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"markPrice": "3000.0"})
        if request.url.path == "/fapi/v1/order":
            return httpx.Response(200, json={"orderId": 2})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    result = _run(bfe.place_futures_order("ETHUSDT", "BUY", "fixed_usdt", 50.0, 3, "key", "secret"))
    assert result["orderId"] == 2


def test_a_real_margin_type_error_still_raises(monkeypatch):
    def handler(request):
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -1, "msg": "Invalid API-key"})
        raise AssertionError("non deve procedere oltre marginType")

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    with pytest.raises(bfe.FuturesExecutionError, match="Invalid API-key"):
        _run(bfe.place_futures_order("BTCUSDT", "BUY", "fixed_usdt", 50.0, 3, "key", "secret"))


def test_pct_balance_sizes_against_real_available_margin(monkeypatch):
    seen_notional = {}

    def handler(request):
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(200, json={"msg": "success"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 2})
        if request.url.path == "/fapi/v2/balance":
            return httpx.Response(200, json=[{"asset": "USDT", "availableBalance": "1000.0"}])
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"markPrice": "100.0"})
        if request.url.path == "/fapi/v1/order":
            q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
            seen_notional["qty"] = float(q["quantity"])
            return httpx.Response(200, json={"orderId": 3})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    _run(bfe.place_futures_order("BTCUSDT", "BUY", "pct_balance", 50.0, 2, "key", "secret"))
    # 50% di 1000 USDT di margine, leva 2x = 1000 di notional, / prezzo 100 = 10.
    assert seen_notional["qty"] == pytest.approx(10.0, rel=1e-6)


def test_base_qty_sends_the_exact_quantity_typed(monkeypatch):
    """size_value in base_qty e' gia' la quantita' di moneta base voluta
    (es. 0.05 BTC): non va ricalcolata da un importo, e la leva non deve
    moltiplicarla — a differenza di fixed_usdt/pct_balance, dove la leva
    amplifica il nozionale, qui la quantita' e' esattamente quella scritta."""
    seen = {}

    def handler(request):
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(200, json={"msg": "success"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 10})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"markPrice": "50000.0"})
        if request.url.path == "/fapi/v1/order":
            q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
            seen["qty"] = float(q["quantity"])
            return httpx.Response(200, json={"orderId": 4})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    _run(bfe.place_futures_order("BTCUSDT", "BUY", "base_qty", 0.05, 10, "key", "secret"))
    assert seen["qty"] == pytest.approx(0.05, rel=1e-6)


def test_close_position_uses_reduce_only_and_the_opposite_side(monkeypatch):
    posted = {}

    def handler(request):
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "positionAmt": "0.5"}])
        if request.url.path == "/fapi/v1/order":
            posted.update(dict(urllib.parse.parse_qsl(request.url.query.decode())))
            return httpx.Response(200, json={"orderId": 9})
        raise AssertionError(request.url.path)

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    result = _run(bfe.close_position("BTCUSDT", "key", "secret"))
    assert result["orderId"] == 9
    assert posted["side"] == "SELL"  # posizione long (positionAmt positivo) si chiude vendendo
    assert posted["reduceOnly"] == "true"


def test_close_position_with_nothing_open_returns_none(monkeypatch):
    def handler(request):
        if request.url.path == "/fapi/v2/positionRisk":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "positionAmt": "0"}])
        raise AssertionError("non deve mandare un ordine se non c'e' niente da chiudere")

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    result = _run(bfe.close_position("BTCUSDT", "key", "secret"))
    assert result is None


def test_zero_mark_price_is_rejected_before_sizing_a_quantity(monkeypatch):
    def handler(request):
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(200, json={"msg": "ok"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 1})
        if request.url.path == "/fapi/v1/premiumIndex":
            return httpx.Response(200, json={"markPrice": "0"})
        raise AssertionError("non deve arrivare a piazzare un ordine")

    monkeypatch.setattr(bfe.httpx, "AsyncClient", _client_factory(handler))
    with pytest.raises(bfe.FuturesExecutionError, match="not available"):
        _run(bfe.place_futures_order("BTCUSDT", "BUY", "fixed_usdt", 100.0, 1, "key", "secret"))
