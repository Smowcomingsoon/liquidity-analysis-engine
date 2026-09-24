"""Registro degli exchange futures.

Un posto solo decide quali exchange esistono per i futures. Prima quella
decisione era scritta tre volte — in `executor.py`, in `main.py` e nel
`<select>` del frontend — e le tre copie potevano non essere d'accordo.

**Perimetro: leva accessibile dall'Europa.** MiCA e le misure ESMA sui
derivati hanno spostato il campo: non basta che un exchange abbia i futures,
serve che li offra a un utente europeo. Quelli qui dentro lo fanno. Restano
fuori, e non e' una dimenticanza:

- **Coinbase, Bitstamp** — non offrono derivati a leva al retail UE. Sono
  nell'app per lo spot e li' restano.
- **MEXC, KuCoin** — hanno i futures, ma non la licenza per offrirli in UE.
  Aggiungerli e' un file in questo pacchetto piu' una riga qui: la struttura
  e' pronta, la decisione e' di chi la prende.

Chi chiama non importa mai un adattatore per nome: chiede `get(slug)`.
"""

from __future__ import annotations

from app.trading.futures.base import (
    FuturesAdapter,
    FuturesCreds,
    FuturesExecutionError,
    Position,
)
from app.trading.futures.binance import BinanceFutures
from app.trading.futures.bitget import BitgetFutures
from app.trading.futures.bybit import BybitFutures
from app.trading.futures.kraken import KrakenFutures
from app.trading.futures.mexc import MexcFutures
from app.trading.futures.okx import OkxFutures

# L'ordine e' quello con cui compaiono sullo schermo: Binance per primo
# perche' e' l'unico che ha gia' girato in produzione qui dentro.
_ADAPTERS: tuple[FuturesAdapter, ...] = (
    BinanceFutures(),
    BybitFutures(),
    OkxFutures(),
    BitgetFutures(),
    KrakenFutures(),
    # MEXC non ha licenza UE per i derivati, a differenza dei quattro sopra:
    # e' qui perche' Lorenzo l'ha chiesto sapendolo, non per una svista.
    MexcFutures(),
)

ADAPTERS: dict[str, FuturesAdapter] = {a.slug: a for a in _ADAPTERS}
SUPPORTED_FUTURES_EXCHANGES: tuple[str, ...] = tuple(ADAPTERS)


def get(slug: str) -> FuturesAdapter:
    adapter = ADAPTERS.get((slug or "").strip().lower())
    if adapter is None:
        raise ValueError(f"Futures not supported for this exchange: {slug}")
    return adapter


def describe() -> list[dict]:
    """Come si presentano gli exchange a chi deve sceglierne uno: il nome,
    se vuole una passphrase, il suo tetto di leva, e se il margine e'
    davvero isolato. Il frontend legge questo invece di avere una lista
    propria che invecchia da sola."""
    return [
        {
            "exchange": a.slug,
            "name": a.name,
            "needs_passphrase": a.needs_passphrase,
            "max_leverage": a.max_leverage,
            "isolated_margin": a.isolated_margin,
            "key_hint": a.key_hint,
        }
        for a in _ADAPTERS
    ]


__all__ = [
    "ADAPTERS",
    "SUPPORTED_FUTURES_EXCHANGES",
    "FuturesAdapter",
    "FuturesCreds",
    "FuturesExecutionError",
    "Position",
    "describe",
    "get",
]
