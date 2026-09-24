"""Interfaccia comune degli adattatori futures.

Perche' esiste. Il primo giro di futures parlava direttamente con Binance da
`main.py` e da `executor.py`: il nome dell'exchange era scritto dentro la
logica dell'endpoint. Aggiungere il secondo exchange avrebbe voluto dire un
`if` per exchange in ogni punto che tocca un ordine — e in un modulo dove un
`if` sbagliato manda denaro vero con la leva, quello e' il difetto peggiore
possibile.

Qui ogni exchange e' una classe che implementa gli stessi sei metodi. Chi
chiama non sa mai con chi sta parlando: chiede l'adattatore al registro e usa
l'interfaccia. Le differenze fra exchange (percorsi, firma, unita' di misura
degli ordini, nome del simbolo) restano tutte dentro il rispettivo file, che
e' l'unico posto dove possono fare danno.

Regole che valgono per ogni adattatore, non negoziabili:

- **Solo isolated margin.** Un conto cross fa esplodere posizioni non
  correlate a catena. Ogni adattatore deve dichiarare isolated prima
  dell'ordine, non presumerlo.
- **Il margine si legge dall'exchange prima di ogni ordine**, mai da una
  cache: e' il controllo che impedisce di aprire una posizione che non si
  puo' sostenere.
- **Chiudere non deve mai poter aprire.** Ogni chiusura usa il meccanismo
  "solo riduzione" dell'exchange (reduceOnly o l'endpoint di chiusura
  dedicato), mai un ordine di mercato al contrario calcolato a mano.
- **La leva la impone il server.** `MAX_FUTURES_LEVERAGE` e' il tetto
  globale; `max_leverage` di un adattatore puo' solo abbassarlo.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


class FuturesExecutionError(RuntimeError):
    """Errore che arriva dall'exchange, gia' tradotto in italiano dove si
    riesce a riconoscerlo. Un solo tipo per tutti gli exchange: chi chiama
    non deve sapere quale libreria di errori parla sotto."""


@dataclass(frozen=True)
class FuturesCreds:
    """Le credenziali di un exchange. La passphrase e' vuota dove non serve
    (Binance, Bybit, Kraken) e obbligatoria dove serve (OKX, Bitget): e'
    l'adattatore a dirlo, con `needs_passphrase`."""

    api_key: str
    api_secret: str
    passphrase: str = ""


@dataclass(frozen=True)
class Position:
    """Una posizione aperta, nella stessa forma per tutti gli exchange.

    La schermata Opera legge questi campi e basta: se un exchange chiama la
    stessa cosa `unRealizedProfit` e un altro `unrealisedPnl`, quel dettaglio
    muore dentro l'adattatore."""

    exchange: str
    symbol: str
    side: str            # "long" | "short"
    quantity: float
    entry_price: float
    mark_price: float
    leverage: int
    margin: float
    notional: float
    unrealized_pnl: float
    liquidation_price: float

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        # Quanto puo' muoversi il prezzo prima della liquidazione: e' il
        # numero che la schermata mette in evidenza, e va calcolato in un
        # posto solo o ogni adattatore lo sbaglierebbe a modo suo.
        mark, liq = self.mark_price, self.liquidation_price
        d["distance_to_liquidation_pct"] = ((liq / mark - 1) * 100) if (mark and liq) else None
        return d


class FuturesAdapter(abc.ABC):
    """Un exchange di futures. Sottoclassi in questo pacchetto, una per file."""

    slug: str                      # chiave nelle credenziali e nelle API
    name: str                      # come si chiama per chi legge lo schermo
    needs_passphrase: bool = False
    max_leverage: int = 10         # tetto proprio dell'adattatore, mai sopra MAX_FUTURES_LEVERAGE
    # Falso solo dove l'exchange non offre isolated: allora il rischio non e'
    # confinato alla posizione, e chi guarda deve saperlo prima di premere —
    # non e' un dettaglio da nascondere per uniformita'. Vedi kraken.py.
    isolated_margin: bool = True
    # Cosa dire a chi crea la chiave API: ogni exchange chiama il permesso
    # in modo diverso, e una chiave spot su un conto futures da' un errore
    # che non spiega niente.
    key_hint: str = ""

    def symbol_for(self, symbol: str) -> str:
        """Da `BTCUSDT` (la forma che usa tutta l'app) al nome che vuole
        questo exchange. Default: nessuna traduzione."""
        return symbol.strip().upper()

    @abc.abstractmethod
    async def available_margin(self, creds: FuturesCreds) -> float:
        """Margine USDT libero sul conto futures, letto dall'exchange."""

    @abc.abstractmethod
    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        """Prezzo mark del perpetuo — quello su cui si calcola la
        liquidazione, non l'ultimo prezzo scambiato."""

    @abc.abstractmethod
    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        """Tutte le posizioni aperte sul conto futures."""

    @abc.abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,          # "BUY" | "SELL"
        size_mode: str,     # "fixed_usdt" | "pct_balance" | "base_qty"
        size_value: float,
        leverage: int,
        creds: FuturesCreds,
        max_usdt: float | None = None,  # tetto sul nozionale del SINGOLO ingresso
    ) -> dict:
        """Apre a mercato, dopo aver dichiarato isolated e la leva."""

    @abc.abstractmethod
    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        """Chiude per intero la posizione su questo simbolo. `None` se non
        c'era niente da chiudere — non e' un errore."""

    async def effective_notional(
        self,
        symbol: str,
        notional: float,
        mark: float,
        creds: FuturesCreds,
    ) -> tuple[float, str]:
        """Quanto verrebbe impegnato DAVVERO, e perche' se differisce.

        L'anteprima di un ordine mostrava l'importo chiesto (es. l'80% del
        saldo) come se fosse quello che sarebbe finito a mercato. Su exchange
        che quantizzano la dimensione — MEXC conta contratti interi, e un
        contratto su KAS vale mille monete — l'ordine vero ne usa meno: 40
        USDT chiesti diventavano 34,40, senza che niente lo dicesse prima.

        Il default non arrotonda niente (chi non quantizza non deve
        dichiarare nulla), quindi aggiungere questo metodo non cambia il
        comportamento di nessun adattatore che non lo sovrascriva.
        """
        return notional, ""

    # ── utilita' condivise ────────────────────────────────────────────────

    def _notional(
        self, size_mode: str, size_value: float, leverage: int, available: float,
        mark: float | None = None,
    ) -> float:
        """Valore nozionale della posizione, in USDT.

        `base_qty` e' l'unico modo dove size_value e' gia' una quantita' di
        moneta base (es. 0.05 BTC), non un importo — richiede il prezzo mark
        per convertirla in nozionale, a differenza degli altri due modi che
        ragionano gia' in USDT/percentuale. Aggiunto apposta perche' un
        importo in dollari e una quantita' in token sono facili da scambiare
        per errore quando si scrive lo stesso numero in un campo che sembra
        uguale: 500 puo' voler dire 500 USDT o 500 BTC, e la differenza e'
        dell'ordine dei milioni.
        """
        if size_mode == "base_qty":
            if not mark or mark <= 0:
                raise FuturesExecutionError("Market price not available for this symbol")
            return size_value * mark
        margin = size_value if size_mode == "fixed_usdt" else available * size_value / 100
        return margin * leverage

    @staticmethod
    def _capped(notional: float, max_usdt: float | None) -> float:
        """Tetto sul nozionale di un SINGOLO ingresso — mai chiamato da
        close_position, che chiude sempre per intero. Vedi WatchlistEntry.
        size_cap_usdt: impostato per strategia, non per exchange, perche' la
        credenziale exchange e' condivisa da tutte le strategie che ci
        operano sopra."""
        return min(notional, max_usdt) if max_usdt else notional

    @staticmethod
    def _qty(notional: float, mark: float, step: int = 6) -> str:
        if mark <= 0:
            raise FuturesExecutionError("Market price not available for this symbol")
        qty = notional / mark
        if qty <= 0:
            raise FuturesExecutionError("Computed quantity is zero or negative — insufficient margin")
        return f"{qty:.{step}f}".rstrip("0").rstrip(".")


# Traduzioni degli errori comuni a piu' exchange. Ogni adattatore ci
# aggiunge le proprie; queste valgono ovunque perche' descrivono errori di
# configurazione della chiave, che sono uguali dappertutto.
COMMON_HINTS = {
    "api key": "invalid key, or not enabled for futures trading",
    "signature": "the secret does not match the key",
    "permission": "the key lacks futures order permission",
    "not authorized": "the key lacks futures order permission",
    "ip ": "the server IP is not whitelisted for this key",
    "insufficient": "insufficient margin for the requested amount",
    "passphrase": "wrong or missing passphrase",
    "timestamp": "server clock out of sync with the exchange",
    "leverage": "leverage not allowed for this symbol",
    "symbol": "this perpetual does not exist on this exchange",
}


def explain(exchange: str, action: str, body: str, hints: dict[str, str] | None = None) -> FuturesExecutionError:
    low = body.lower()
    # Una pagina HTML di un CDN (Akamai/Edgesuite e simili) davanti
    # all'exchange, non una risposta dell'API — capita quando il server che
    # gira l'app viene bloccato a livello di rete prima ancora di arrivare
    # all'exchange (es. un IP europeo su un endpoint che l'exchange nega per
    # motivi regolatori sui derivati, indipendentemente da chiave o
    # permessi). Va riconosciuta PRIMA delle parole chiave generiche: quella
    # pagina contiene spesso "permission"/"access", che altrimenti verrebbe
    # letta come un problema della chiave API — diagnosi sbagliata, e
    # incolperebbe una chiave che non c'entra.
    if "<html" in low and ("access denied" in low or "edgesuite" in low or "akamai" in low):
        return FuturesExecutionError(
            f"{exchange} futures {action}: the request was blocked by a firewall/CDN "
            f"in front of the exchange, not by an API key error — likely a network block "
            f"on the server placing the order (e.g. an EU IP on an endpoint the exchange denies "
            f"for derivatives, regardless of permissions or credentials)."
        )
    for needle, hint in {**COMMON_HINTS, **(hints or {})}.items():
        if needle in low:
            return FuturesExecutionError(f"{exchange} futures {action}: {body} — {hint}")
    return FuturesExecutionError(f"{exchange} futures {action}: {body}")
