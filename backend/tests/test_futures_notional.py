"""FuturesAdapter._notional() e' condivisa da tutti gli adattatori futures
(bybit, okx, bitget, kraken, mexc — binance ha la sua copia in
binance_futures_exec.py, testata a parte). Un test qui solo copre il calcolo
per tutti insieme, invece di ripeterlo in ogni file di adattatore.
"""

from __future__ import annotations

import pytest

from app.trading.futures.base import FuturesExecutionError, explain
from app.trading.futures.kraken import KrakenFutures

adapter = KrakenFutures()


def test_fixed_usdt_ignores_available_and_uses_leverage():
    # 100 USDT di margine, leva 5x = 500 di nozionale, qualunque sia il saldo.
    assert adapter._notional("fixed_usdt", 100.0, 5, available=99999.0) == pytest.approx(500.0)


def test_pct_balance_uses_available_and_leverage():
    # 20% di 1000 USDT disponibili = 200 di margine, leva 3x = 600 di nozionale.
    assert adapter._notional("pct_balance", 20.0, 3, available=1000.0) == pytest.approx(600.0)


def test_base_qty_is_quantity_times_mark_ignoring_leverage():
    # 0.05 BTC a 50000 = 2500 di nozionale — la leva non moltiplica una
    # quantita' gia' data, solo un importo (a differenza degli altri due modi).
    assert adapter._notional("base_qty", 0.05, 10, available=0.0, mark=50000.0) == pytest.approx(2500.0)


def test_base_qty_without_mark_price_fails_loudly():
    """Un nozionale calcolato senza prezzo sarebbe silenziosamente zero — un
    ordine 'gratis' non e' un errore piu' innocuo, e' peggio: parte."""
    with pytest.raises(FuturesExecutionError, match="Market price not available"):
        adapter._notional("base_qty", 0.05, 10, available=0.0, mark=None)


def test_base_qty_with_zero_mark_price_fails_loudly():
    with pytest.raises(FuturesExecutionError, match="Market price not available"):
        adapter._notional("base_qty", 0.05, 10, available=0.0, mark=0.0)


def test_capped_clamps_notional_to_the_max():
    # Tetto per singolo ingresso: 600 di nozionale con tetto 200 diventa 200.
    assert adapter._capped(600.0, 200.0) == pytest.approx(200.0)


def test_capped_leaves_notional_untouched_when_below_the_max():
    assert adapter._capped(150.0, 200.0) == pytest.approx(150.0)


def test_capped_with_no_max_is_a_noop():
    # 0/None significano "nessun tetto" — vedi WatchlistEntry.size_cap_usdt.
    assert adapter._capped(600.0, None) == pytest.approx(600.0)
    assert adapter._capped(600.0, 0.0) == pytest.approx(600.0)


def test_a_cdn_block_page_is_not_read_as_a_key_permission_error():
    """Bug reale: un blocco Akamai/Edgesuite davanti all'exchange (rete, non
    chiave) contiene spesso la parola 'permission' nell'HTML — letta dalle
    parole chiave generiche, diventava 'alla chiave manca il permesso',
    incolpando una chiave che non c'entra nulla col problema vero."""
    body = (
        '<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>'
        'You don\'t have permission to access "http://api.mexc.com/api/v1/private/order/create" '
        'on this server.<P>Reference #18.9ad53b17.1789225538.fbe04e1a</P></BODY></HTML>'
    )
    err = explain("mexc", "ordine", body)
    message = str(err)
    assert "manca il permesso" not in message.lower()
    assert "firewall/cdn" in message.lower()


def test_a_real_api_permission_error_still_gets_the_key_hint():
    err = explain("mexc", "ordine", '{"code": 2001, "msg": "no permission"}')
    assert "key" in str(err).lower()
