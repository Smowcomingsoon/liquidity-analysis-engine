"""MEXC perpetui USDT — API contract v1, simboli `BTC_USDT`.

Tre cose di MEXC che non assomigliano agli altri exchange di questo
pacchetto, e che sono anche i tre modi piu' facili di sbagliare qui:

1. **La quantita' e' in contratti, non in monete.** `vol` conta contratti,
   e un contratto vale `contractSize` monete (0.0001 BTC su BTC_USDT).
   Mandare le monete al posto dei contratti significa aprire una posizione
   diecimila volte piu' grande di quella voluta: per questo il valore del
   contratto si legge dall'exchange prima di ogni ordine e non si presume.

2. **Il lato dell'ordine e' un numero, e distingue aprire da chiudere.**
   1 = apri long, 3 = apri short, 2 = chiudi short, 4 = chiudi long. Una
   chiusura usa i codici di chiusura, che sono l'equivalente MEXC del
   `reduceOnly` degli altri: non possono aprire niente.

3. **La firma non e' sulla query string.** E' su
   `accessKey + timestamp + parametri`, dove i parametri sono la query
   ordinata per un GET e il corpo JSON esatto per un POST — come su Bybit,
   quindi il corpo si serializza una volta sola e si spedisce quello.

Nota sul perimetro: MEXC non ha licenza UE per i derivati, a differenza di
Kraken, Bybit, OKX e Bitget. E' stato aggiunto su richiesta esplicita di
Lorenzo, che ha scelto sapendolo. Vedi il commento in `registry.py`.

Nota sulla rete: `contract.mexc.com/api/v1/private/order/submit` (endpoint
usato qui fino a seed-0072) poteva rifiutare un IP europeo con un blocco
Akamai (pagina "Access Denied", non una risposta dell'API — vedi `explain()`
in base.py, che la riconosce apposta prima delle parole chiave generiche).
Il supporto MEXC ha indicato `order/create` su `api.mexc.com` come fix.
Cambiato prima solo il path (tenendo `contract.mexc.com`, ritenuto — a
torto — l'unico dettaglio giusto del messaggio) non ha risolto: stesso
blocco identico. Verificato allora il codice sorgente vero di ccxt (non un
riassunto) — MEXC ha migrato l'intera API contract, pubblica e privata, sotto
`api.mexc.com` (`/api/v1/contract/...` pubblica, `/api/v1/private/...`
privata): `contract.mexc.com` e' il dominio legacy, ancora vivo per le
letture pubbliche (`/ping` risponde) ma evidentemente non piu' quello dietro
cui MEXC lascia passare gli ordini. Il supporto aveva ragione su host E
path; l'errore di averlo scartato era mio. Il proxy extra-UE
(`MEXC_FUTURES_PROXY`, es. "http://utente:password@host:porta" o
"socks5://host:porta") resta comunque configurabile per l'eventualita' di un
futuro blocco geografico reale — mai le chiamate agli altri cinque exchange,
che restano dirette.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse

import httpx

from app.trading.futures.base import FuturesAdapter, FuturesCreds, Position, explain

BASE = "https://api.mexc.com"


# None = nessun proxy, chiamata diretta (comportamento di sempre finche' non
# viene impostata sull'ambiente del server).
def _proxy() -> str | None:
    return os.environ.get("MEXC_FUTURES_PROXY", "").strip() or None


_HINTS = {
    "600": "parameter rejected by MEXC",
    "1002": "the contract does not exist or is not enabled",
    "2001": "the key is not enabled for contract trading",
    "2005": "insufficient margin for the requested amount",
    "30004": "position too small to close this quantity",
    # Questo capita a molti conti: MEXC ha chiuso l'invio ordini via API sui
    # contratti se non e' stato abilitato esplicitamente. Senza questa
    # traduzione l'errore che arriva non spiega niente.
    "not support": "this account does not have API contract ordering enabled — ask MEXC for it",
    "maintenance": "MEXC not responding (maintenance or an issue on their side)",
}


class MexcFutures(FuturesAdapter):
    slug = "mexc"
    name = "MEXC Futures"
    needs_passphrase = False
    max_leverage = 10
    key_hint = (
        "API key with the «Contract Trading» permission. Heads up: on many MEXC accounts "
        "API contract ordering is disabled by default and must be requested "
        "from support — a correct key can still be rejected when ordering."
    )

    def symbol_for(self, symbol: str) -> str:
        """BTCUSDT -> BTC_USDT. Un simbolo gia' nella forma MEXC passa intatto."""
        s = symbol.strip().upper()
        if "_" in s:
            return s
        for quote in ("USDT", "USDC"):
            if s.endswith(quote):
                return f"{s[:-len(quote)]}_{quote}"
        return s

    # ── firma ─────────────────────────────────────────────────────────────

    def _headers(self, creds: FuturesCreds, payload: str) -> dict:
        ts = str(int(time.time() * 1000))
        raw = creds.api_key + ts + payload
        sign = hmac.new(creds.api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {
            "ApiKey": creds.api_key,
            "Request-Time": ts,
            "Signature": sign,
            "Content-Type": "application/json",
        }

    def _unwrap(self, resp: httpx.Response, action: str):
        if resp.status_code >= 400:
            raise explain("mexc", action, resp.text, _HINTS)
        body = resp.json()
        # Come Bybit: 200 non basta, l'esito vero e' nel corpo.
        if not body.get("success", True) or str(body.get("code", "0")) not in ("0", "200"):
            raise explain("mexc", action, f'{body.get("code")} {body.get("message", "")}', _HINTS)
        return body.get("data")

    async def _get(self, path: str, params: dict, creds: FuturesCreds, action: str):
        query = urllib.parse.urlencode(sorted(params.items()))
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0, proxy=_proxy()) as c:
            resp = await c.get(f"{path}?{query}" if query else path,
                               headers=self._headers(creds, query))
        return self._unwrap(resp, action)

    async def _post(self, path: str, body: dict, creds: FuturesCreds, action: str):
        payload = json.dumps(body, separators=(",", ":"))
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0, proxy=_proxy()) as c:
            resp = await c.post(path, headers=self._headers(creds, payload), content=payload)
        return self._unwrap(resp, action)

    # ── letture ───────────────────────────────────────────────────────────

    async def available_margin(self, creds: FuturesCreds) -> float:
        data = await self._get("/api/v1/private/account/asset/USDT", {}, creds, "balance")
        if not data:
            return 0.0
        return float(data.get("availableBalance") or data.get("availableOpen") or 0)

    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        # Pubblico: un prezzo non deve poter fallire per una chiave sbagliata.
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0, proxy=_proxy()) as c:
            resp = await c.get("/api/v1/contract/ticker", params={"symbol": self.symbol_for(symbol)})
        data = self._unwrap(resp, "price")
        # Un simbolo sconosciuto (o mal formato) torna qui come lista vuota,
        # non come 404 o corpo d'errore: data[0] senza guardia crasherebbe
        # con IndexError invece del messaggio chiaro sotto.
        row = (data[0] if data else None) if isinstance(data, list) else data
        if not row:
            raise explain("mexc", "price", f"symbol {symbol} unknown", _HINTS)
        # fairPrice e' il mark su cui MEXC calcola la liquidazione; lastPrice
        # e' solo l'ultimo scambio, e usarlo sposterebbe il prezzo di
        # liquidazione mostrato rispetto a quello vero.
        return float(row.get("fairPrice") or row.get("lastPrice") or 0)

    async def _contract_spec(self, symbol: str) -> tuple[float, float]:
        """Quante monete vale un contratto (`contractSize`) e quanti
        contratti serve almeno per un ordine (`minVol`). Letti ogni volta e
        mai presunti: sono i due numeri che separano un ordine giusto da uno
        enorme, o da un rifiuto che non spiega perche'. minVol NON e' sempre
        1 — assumerlo lo era su BTC_USDT (dove infatti vale 1) ma non e'
        garantito per ogni pair, e sbagliarlo per difetto rifiuta ordini
        validi mentre per eccesso li lascia passare quando non dovrebbero."""
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0, proxy=_proxy()) as c:
            resp = await c.get("/api/v1/contract/detail", params={"symbol": symbol})
        data = self._unwrap(resp, "contract data")
        # Stesso caso di mark_price: un simbolo sconosciuto torna come lista
        # vuota, non come errore — data[0] senza guardia crasherebbe qui.
        row = (data[0] if data else None) if isinstance(data, list) else data
        size = float((row or {}).get("contractSize") or 0)
        if size <= 0:
            raise explain("mexc", "contract data",
                          f"contractSize missing for {symbol}", _HINTS)
        min_vol = float((row or {}).get("minVol") or 1)
        return size, min_vol

    async def _contract_size(self, symbol: str) -> float:
        size, _ = await self._contract_spec(symbol)
        return size

    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        data = await self._get("/api/v1/private/position/open_positions", {}, creds, "positions") or []
        out = []
        for p in data:
            vol = float(p.get("holdVol") or 0)
            if vol == 0:
                continue
            sym = p.get("symbol", "")
            # positionType: 1 long, 2 short.
            is_long = int(p.get("positionType") or 1) == 1
            mark = float(p.get("holdAvgPrice") or 0)
            try:
                csize = await self._contract_size(sym)
            except Exception:
                csize = 1.0
            qty = vol * csize
            entry = float(p.get("openAvgPrice") or 0)
            out.append(Position(
                exchange=self.slug, symbol=sym,
                side="long" if is_long else "short", quantity=qty,
                entry_price=entry, mark_price=mark,
                leverage=int(float(p.get("leverage") or 1)),
                margin=float(p.get("im") or p.get("oim") or 0),
                notional=qty * mark,
                unrealized_pnl=float(p.get("realised") or 0) + (mark - entry) * qty * (1 if is_long else -1),
                liquidation_price=float(p.get("liquidatePrice") or 0),
            ))
        return out

    # ── scritture ─────────────────────────────────────────────────────────

    async def effective_notional(self, symbol, notional, mark, creds):
        """Il nozionale vero dopo l'arrotondamento ai contratti interi.

        Stessa aritmetica di place_order() qui sotto — `notional / mark /
        csize`, troncato — perche' un'anteprima che calcola in modo anche solo
        leggermente diverso dall'ordine e' peggio di nessuna anteprima.
        """
        sym = self.symbol_for(symbol)
        if mark <= 0:
            return notional, ""
        csize, _ = await self._contract_spec(sym)
        contracts = int(notional / mark / csize)
        if contracts < 1:
            return notional, ""
        real = contracts * csize * mark
        if abs(real - notional) / notional < 0.005:
            return real, ""
        return real, (f"{contracts} contracts of {csize:g} — MEXC doesn't accept fractional contracts, "
                      f"so the order uses {real:.2f} USDT instead of {notional:.2f}")

    async def place_order(self, symbol, side, size_mode, size_value, leverage, creds, max_usdt=None):
        sym = self.symbol_for(symbol)
        available = await self.available_margin(creds)
        mark = await self.mark_price(sym, creds)
        if mark <= 0:
            raise explain("mexc", "order", "market price not available", _HINTS)
        notional = self._capped(self._notional(size_mode, size_value, leverage, available, mark), max_usdt)
        csize, min_vol = await self._contract_spec(sym)
        vol = notional / mark / csize
        # Il troncamento a intero (MEXC rifiuta i decimali) puo' far
        # scendere sotto il minimo un valore che sembrava sufficiente da
        # float — es. vol=1.99 con minVol=2 diventerebbe 1 e verrebbe
        # rifiutato dall'exchange con un errore diverso da questo, che pero'
        # avremmo potuto anticipare qui.
        if int(vol) < min_vol:
            # Non e' un errore di configurazione: il nozionale richiesto
            # (spesso una % piccola del saldo, o un tetto stretto) e'
            # sotto il minimo di mercato per questo pair su MEXC — un
            # contratto vale gia' parecchi USDT a leva 1x, e su alcuni pair
            # il minimo e' piu' di un contratto (minVol), non sempre 1: va
            # letto dall'exchange, mai presunto. Il numero di contratti da
            # solo non dice a chi legge cosa fare; il margine minimo in
            # USDT si', perche' e' l'unita' in cui ragiona.
            min_notional = csize * mark * min_vol
            min_margin = min_notional / leverage
            raise explain(
                "mexc", "order",
                f"the requested size is {vol:.3f} contracts, below the minimum of {min_vol:g} "
                f"(one contract = {csize} units at {mark:.4f}, {min_notional:.2f} USDT notional) "
                f"— needs at least {min_margin:.2f} USDT of margin at {leverage}x leverage", _HINTS,
            )
        return await self._post("/api/v1/private/order/create", {
            "symbol": sym,
            "price": 0,                 # ignorato dagli ordini a mercato
            "vol": int(vol),            # contratti interi: MEXC rifiuta i decimali
            "side": 1 if side == "BUY" else 3,   # 1 apri long, 3 apri short
            "type": 5,                  # 5 = mercato
            "openType": 1,              # 1 = isolated, mai cross
            "leverage": leverage,
        }, creds, "order")

    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        sym = self.symbol_for(symbol)
        pos = next((p for p in await self.open_positions(creds) if p.symbol == sym), None)
        if pos is None:
            return None
        csize = await self._contract_size(sym)
        vol = max(1, int(round(pos.quantity / csize)))
        return await self._post("/api/v1/private/order/create", {
            "symbol": sym,
            "price": 0,
            "vol": vol,
            # 4 chiude un long, 2 chiude uno short: sono codici di chiusura,
            # l'equivalente MEXC del reduceOnly — non possono aprire nulla.
            "side": 4 if pos.side == "long" else 2,
            "type": 5,
            "openType": 1,
            "leverage": pos.leverage,
        }, creds, "close")
