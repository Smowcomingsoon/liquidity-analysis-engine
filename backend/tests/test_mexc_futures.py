"""MEXC futures — nessuna chiamata di rete vera: ogni test sostituisce il
trasporto con httpx.MockTransport, cosi' non possono mai spendere margine.

Il grosso di questi test sta su una cosa sola: la conversione fra monete e
contratti. Su MEXC `vol` conta contratti, e un contratto di BTC_USDT vale
0.0001 BTC — sbagliare quella divisione apre una posizione diecimila volte
piu' grande di quella chiesta, e non e' un errore che si nota dal codice.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.trading.futures import mexc as mexc_module
from app.trading.futures.base import FuturesCreds, FuturesExecutionError
from app.trading.futures.mexc import MexcFutures

_RealAsyncClient = httpx.AsyncClient
CREDS = FuturesCreds(api_key="key", api_secret="secret")


def _patch(monkeypatch, handler):
    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return _RealAsyncClient(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _run(coro):
    return asyncio.run(coro)


def _ok(data):
    return httpx.Response(200, json={"success": True, "code": 0, "data": data})


def test_no_proxy_env_var_means_direct_call(monkeypatch):
    monkeypatch.delenv("MEXC_FUTURES_PROXY", raising=False)
    assert mexc_module._proxy() is None


def test_proxy_env_var_is_read_and_reaches_the_client(monkeypatch):
    """Un IP europeo puo' essere bloccato da MEXC su /order/create (blocco
    di rete, non di chiave — vedi il commento in cima al modulo). La via
    scelta e' instradare solo questo exchange attraverso un proxy fuori UE,
    impostato con questa env var — verificato qui che arriva davvero al
    client httpx, non solo che la funzione la legge."""
    monkeypatch.setenv("MEXC_FUTURES_PROXY", "http://example-proxy:8080")
    assert mexc_module._proxy() == "http://example-proxy:8080"

    seen_kwargs = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        seen_kwargs.update(kwargs)
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(lambda r: _ok({"fairPrice": 50000}))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    _run(MexcFutures().mark_price("BTCUSDT", CREDS))
    assert seen_kwargs.get("proxy") == "http://example-proxy:8080"


def test_symbol_is_translated_to_the_mexc_form():
    a = MexcFutures()
    assert a.symbol_for("BTCUSDT") == "BTC_USDT"
    assert a.symbol_for("ETHUSDT") == "ETH_USDT"
    # Gia' nella forma MEXC: non va tradotto due volte.
    assert a.symbol_for("BTC_USDT") == "BTC_USDT"


def test_mark_price_uses_fair_price_not_last_trade(monkeypatch):
    """fairPrice e' il prezzo su cui MEXC calcola la liquidazione. Usare
    lastPrice sposterebbe la liquidazione mostrata rispetto a quella vera."""
    def handler(request):
        return _ok({"symbol": "BTC_USDT", "lastPrice": 61000, "fairPrice": 60800})

    _patch(monkeypatch, handler)
    assert _run(MexcFutures().mark_price("BTCUSDT", CREDS)) == 60800.0


def test_order_size_is_in_contracts_not_in_coins(monkeypatch):
    """1000 USDT di nozionale a 50 000, con contratti da 0.0001 BTC:
    0.02 BTC = 200 contratti. Se `vol` arrivasse in monete sarebbe 0."""
    sent = {}

    def handler(request):
        p = request.url.path
        if p == "/api/v1/contract/ticker":
            return _ok({"fairPrice": 50000})
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 0.0001})
        if p == "/api/v1/private/account/asset/USDT":
            return _ok({"availableBalance": 10000})
        if p == "/api/v1/private/order/create":
            sent.update(json.loads(request.content))
            return _ok({"orderId": "1"})
        raise AssertionError(f"percorso inatteso: {p}")

    _patch(monkeypatch, handler)
    _run(MexcFutures().place_order("BTCUSDT", "BUY", "fixed_usdt", 1000.0, 1, CREDS))

    assert sent["vol"] == 200          # 1000 / 50000 / 0.0001
    assert isinstance(sent["vol"], int)  # MEXC rifiuta i decimali
    assert sent["side"] == 1           # 1 = apri long
    assert sent["type"] == 5           # 5 = mercato
    assert sent["openType"] == 1       # 1 = isolated, mai cross


def test_short_uses_the_open_short_code(monkeypatch):
    sent = {}

    def handler(request):
        p = request.url.path
        if p == "/api/v1/contract/ticker":
            return _ok({"fairPrice": 50000})
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 0.0001})
        if p == "/api/v1/private/account/asset/USDT":
            return _ok({"availableBalance": 10000})
        if p == "/api/v1/private/order/create":
            sent.update(json.loads(request.content))
            return _ok({"orderId": "2"})
        raise AssertionError(p)

    _patch(monkeypatch, handler)
    _run(MexcFutures().place_order("BTCUSDT", "SELL", "fixed_usdt", 500.0, 1, CREDS))
    assert sent["side"] == 3           # 3 = apri short


def test_an_order_below_one_contract_is_refused_not_rounded(monkeypatch):
    """Meglio un errore che spiega, che un ordine arrotondato a zero — o,
    peggio, arrotondato a uno e quindi piu' grande del richiesto."""
    def handler(request):
        p = request.url.path
        if p == "/api/v1/contract/ticker":
            return _ok({"fairPrice": 50000})
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 1})     # un contratto = 1 BTC
        if p == "/api/v1/private/account/asset/USDT":
            return _ok({"availableBalance": 10000})
        raise AssertionError(p)

    _patch(monkeypatch, handler)
    with pytest.raises(FuturesExecutionError) as exc:
        _run(MexcFutures().place_order("BTCUSDT", "BUY", "fixed_usdt", 100.0, 1, CREDS))
    message = str(exc.value)
    assert "contracts" in message
    # Il numero di contratti da solo non dice cosa fare: il margine minimo
    # in USDT si', perche' e' l'unita' in cui chi legge sta ragionando.
    # 1 contratto = 1 BTC a 50000, leva 1x -> serve almeno 50000 USDT.
    assert "50000.00 USDT" in message


def test_min_vol_above_one_is_respected_not_assumed_to_be_one(monkeypatch):
    """Bug reale trovato dopo una segnalazione (KASUSDT): il minimo di
    mercato di MEXC non e' sempre 1 contratto, e' `minVol` — un campo del
    contratto che va letto, non presunto. Un ordine da 2 contratti passava
    con la vecchia soglia fissa (>= 1) mentre l'exchange lo avrebbe
    rifiutato con minVol=3."""
    def handler(request):
        p = request.url.path
        if p == "/api/v1/contract/ticker":
            return _ok({"fairPrice": 0.0352})
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 1000, "minVol": 3})
        if p == "/api/v1/private/account/asset/USDT":
            return _ok({"availableBalance": 1000})
        raise AssertionError(p)

    _patch(monkeypatch, handler)
    # 2 contratti di nozionale (2*1000*0.0352 = 70.4 USDT), sotto minVol=3.
    with pytest.raises(FuturesExecutionError) as exc:
        _run(MexcFutures().place_order("KASUSDT", "BUY", "fixed_usdt", 70.4, 1, CREDS))
    message = str(exc.value)
    assert "minimum of 3" in message


def test_closing_a_long_uses_the_close_code_and_cannot_open(monkeypatch):
    """4 chiude un long: e' il reduceOnly di MEXC. Un ordine di apertura al
    contrario (3 = apri short) raddoppierebbe l'esposizione invece di
    chiuderla."""
    sent = {}

    def handler(request):
        p = request.url.path
        if p == "/api/v1/private/position/open_positions":
            return _ok([{
                "symbol": "BTC_USDT", "holdVol": 200, "positionType": 1,
                "openAvgPrice": 50000, "holdAvgPrice": 51000,
                "leverage": 3, "im": 333.0, "liquidatePrice": 34000,
            }])
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 0.0001})
        if p == "/api/v1/private/order/create":
            sent.update(json.loads(request.content))
            return _ok({"orderId": "3"})
        raise AssertionError(p)

    _patch(monkeypatch, handler)
    _run(MexcFutures().close_position("BTCUSDT", CREDS))
    assert sent["side"] == 4           # 4 = chiudi long
    assert sent["vol"] == 200


def test_closing_a_short_uses_the_other_close_code(monkeypatch):
    sent = {}

    def handler(request):
        p = request.url.path
        if p == "/api/v1/private/position/open_positions":
            return _ok([{
                "symbol": "BTC_USDT", "holdVol": 50, "positionType": 2,
                "openAvgPrice": 50000, "holdAvgPrice": 49000,
                "leverage": 2, "im": 100.0, "liquidatePrice": 60000,
            }])
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 0.0001})
        if p == "/api/v1/private/order/create":
            sent.update(json.loads(request.content))
            return _ok({"orderId": "4"})
        raise AssertionError(p)

    _patch(monkeypatch, handler)
    _run(MexcFutures().close_position("BTCUSDT", CREDS))
    assert sent["side"] == 2           # 2 = chiudi short


def test_closing_nothing_returns_none_and_sends_no_order(monkeypatch):
    def handler(request):
        if request.url.path == "/api/v1/private/position/open_positions":
            return _ok([])
        raise AssertionError("non doveva essere spedito nessun ordine")

    _patch(monkeypatch, handler)
    assert _run(MexcFutures().close_position("BTCUSDT", CREDS)) is None


def test_a_body_that_says_failure_raises_even_on_http_200(monkeypatch):
    """MEXC risponde 200 anche quando rifiuta: l'esito vero e' nel corpo."""
    def handler(request):
        return httpx.Response(200, json={"success": False, "code": 2001,
                                         "message": "contract trading not support"})

    _patch(monkeypatch, handler)
    with pytest.raises(FuturesExecutionError) as exc:
        _run(MexcFutures().mark_price("BTCUSDT", CREDS))
    # L'errore piu' comune su MEXC va spiegato, non riportato com'e'.
    assert "not enabled for contract trading" in str(exc.value)


def test_unknown_symbol_gives_a_specific_message_not_a_bare_not_found(monkeypatch):
    """Bug segnalato da Lorenzo: su Futures/MEXC il pair "non si trova". La
    causa vera era l'assenza di autocomplete sul campo (frontend, nessun
    exchange futures ce l'aveva) — la conversione di formato qui e' gia'
    corretta (vedi test sopra). Questo test blocca una regressione diversa:
    se MEXC non riconosce un simbolo, l'errore deve nominare il simbolo,
    non un "non trovato" generico che sembra un problema di rete."""
    def handler(request):
        return _ok([])   # MEXC risponde 200 con lista vuota per un contratto inesistente

    _patch(monkeypatch, handler)
    with pytest.raises(FuturesExecutionError) as exc:
        _run(MexcFutures().mark_price("NOTAREALCOINUSDT", CREDS))
    assert "NOTAREALCOINUSDT" in str(exc.value)
    assert "unknown" in str(exc.value)


def test_positions_are_converted_from_contracts_to_coins(monkeypatch):
    def handler(request):
        p = request.url.path
        if p == "/api/v1/private/position/open_positions":
            return _ok([{
                "symbol": "BTC_USDT", "holdVol": 200, "positionType": 1,
                "openAvgPrice": 50000, "holdAvgPrice": 51000,
                "leverage": 3, "im": 333.0, "liquidatePrice": 34000,
            }])
        if p == "/api/v1/contract/detail":
            return _ok({"contractSize": 0.0001})
        raise AssertionError(p)

    _patch(monkeypatch, handler)
    positions = _run(MexcFutures().open_positions(CREDS))
    assert len(positions) == 1
    pos = positions[0]
    assert pos.quantity == pytest.approx(0.02)     # 200 contratti x 0.0001
    assert pos.side == "long"
    assert pos.liquidation_price == 34000
    # Il long e' in guadagno: 51000 contro 50000 di carico.
    assert pos.unrealized_pnl > 0
