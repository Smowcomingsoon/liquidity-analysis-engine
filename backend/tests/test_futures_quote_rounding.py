"""
L'anteprima di un ordine futures deve dire quanto verra' impegnato DAVVERO.

Caso reale: "80%, quindi doveva essere 40 dollari ma ne ha usati 34 e
qualcosa". Nessun bug nell'ordine — su MEXC la dimensione si conta in
contratti interi, e un contratto su KAS vale 1000 monete: a ~0,0344 USDT
l'una, un contratto sono ~34,40 USDT. 40 USDT sono 1,16 contratti, che
troncati fanno 1. Il difetto era nell'anteprima, che mostrava i 40 chiesti
come se fossero quelli che sarebbero finiti a mercato.

L'anteprima esiste proprio per non avere sorprese dopo il click: se mostra un
numero diverso da quello che l'ordine usera', non sta facendo il suo lavoro.
"""

from __future__ import annotations

import asyncio

import pytest

from app.trading.futures.base import FuturesCreds
from app.trading.futures.mexc import MexcFutures

KAS_CONTRACT = 1000.0     # monete per contratto
KAS_MARK = 0.0344         # USDT per moneta -> un contratto vale ~34,40


def _run(coro):
    """Il progetto non usa pytest-asyncio: stesso schema di test_mexc_futures.py."""
    return asyncio.run(coro)


@pytest.fixture
def adapter(monkeypatch):
    a = MexcFutures()

    async def _spec(sym):
        return KAS_CONTRACT, 1

    monkeypatch.setattr(a, "_contract_spec", _spec)
    return a


def test_the_preview_reports_what_the_order_will_really_use(adapter):
    """40 USDT chiesti su KAS diventano un contratto solo: ~34,40."""
    real, note = _run(adapter.effective_notional("KASUSDT", 40.0, KAS_MARK, FuturesCreds("k", "s")))
    assert real == pytest.approx(34.4, abs=0.01)
    assert "1 contracts" in note
    assert "34.40" in note


def test_an_amount_that_lands_on_a_whole_contract_says_nothing(adapter):
    """Niente nota quando non c'e' niente da spiegare: sarebbe solo rumore."""
    exact = 3 * KAS_CONTRACT * KAS_MARK
    real, note = _run(adapter.effective_notional("KASUSDT", exact, KAS_MARK, FuturesCreds("k", "s")))
    assert real == pytest.approx(exact)
    assert note == ""


def test_below_one_contract_the_preview_does_not_pretend_to_round(adapter):
    """Sotto il minimo l'ordine verra' rifiutato con un messaggio suo
    (place_order lo calcola apposta): l'anteprima non deve inventare uno
    zero, che si leggerebbe come "gratis"."""
    real, note = _run(adapter.effective_notional("KASUSDT", 10.0, KAS_MARK, FuturesCreds("k", "s")))
    assert real == 10.0
    assert note == ""


def test_the_preview_matches_the_arithmetic_the_real_order_uses(adapter):
    """Stessa formula dell'ordine vero: notional / mark / csize, troncato.

    Se le due divergessero, l'anteprima tornerebbe a mentire — in modo piu'
    sottile di prima, che e' peggio.
    """
    for requested in (40.0, 55.0, 100.0, 34.39, 68.81):
        real, _ = _run(adapter.effective_notional("KASUSDT", requested, KAS_MARK, FuturesCreds("k", "s")))
        contracts_as_order_would = int(requested / KAS_MARK / KAS_CONTRACT)
        if contracts_as_order_would >= 1:
            assert real == pytest.approx(contracts_as_order_would * KAS_CONTRACT * KAS_MARK)


def test_an_exchange_that_does_not_quantise_is_left_alone(adapter):
    """Il default della classe base non arrotonda: aggiungere questo metodo
    non deve cambiare il comportamento degli altri cinque exchange."""
    from app.trading.futures.base import FuturesAdapter

    real, note = _run(FuturesAdapter.effective_notional(
        adapter, "ETHUSDT", 40.0, 2000.0, FuturesCreds("k", "s")),
    )
    assert real == 40.0
    assert note == ""
