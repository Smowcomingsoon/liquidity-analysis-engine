"""Kraken Futures (perpetui multi-collateral `PF_`) — futures.kraken.com.

Kraken e' il diverso dei cinque, in tre modi che vanno detti invece che
nascosti dietro l'interfaccia comune:

1. **Non ha isolated margin.** Il conto futures di Kraken e' a margine
   condiviso: la garanzia e' una sola per tutte le posizioni, e una
   posizione che va male intacca le altre. Le altre quattro piattaforme qui
   dentro aprono in isolated, dove il massimo che si perde e' il margine di
   quella posizione. Per questo `isolated_margin = False`: l'app lo dichiara
   nell'anteprima invece di far credere che il rischio sia confinato.
   L'argine che resta e' la leva massima per simbolo, che impostiamo prima
   di ogni ordine.
2. **La firma e' SHA-256 dentro HMAC-SHA-512, con il secret in base64** — e
   il percorso firmato e' quello senza il prefisso `/derivatives`.
3. **Il prezzo di liquidazione non arriva con la posizione.** Lo calcoliamo
   con la stessa formula del backtest (`futures_math.liquidation_price`), ed
   e' una stima: e' segnato come tale dove viene mostrato.

Nomi dei simboli: `BTCUSDT` diventa `PF_XBTUSD` — Kraken chiama il bitcoin
XBT e i suoi perpetui hanno il prefisso PF_.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

from app.analysis.futures_math import DEFAULT_MAINTENANCE_MARGIN_RATE, liquidation_price
from app.trading.futures.base import FuturesAdapter, FuturesCreds, Position, explain

BASE = "https://futures.kraken.com"
PREFIX = "/derivatives/api/v3"

_HINTS = {
    "apiLimitExceeded": "too many requests in a short time",
    "authenticationError": "invalid key, secret or signature for Kraken Futures",
    "insufficientFunds": "insufficient margin for the requested amount",
    "invalidSize": "quantity below Kraken's minimum for this symbol",
    "marketSuspended": "this market is currently suspended",
    "invalidArgument": "parameter rejected by Kraken",
    "nonceBelowThreshold": "server clock out of sync with the exchange",
}

_BASE_ALIASES = {"BTC": "XBT"}


class KrakenFutures(FuturesAdapter):
    slug = "kraken"
    name = "Kraken Futures"
    needs_passphrase = False
    max_leverage = 10
    isolated_margin = False
    key_hint = "Key created on futures.kraken.com (not the spot one), with «Full access» or «Place orders» permission."

    def symbol_for(self, symbol: str) -> str:
        s = symbol.strip().upper()
        if s.startswith("PF_") or s.startswith("PI_"):
            return s
        for quote in ("USDT", "USDC", "USD"):
            if s.endswith(quote):
                base = s[: -len(quote)]
                break
        else:
            base = s
        return "PF_" + _BASE_ALIASES.get(base, base) + "USD"

    def _authent(self, creds: FuturesCreds, post_data: str, nonce: str, path: str) -> str:
        # Il percorso firmato e' senza "/derivatives": e' la parte che
        # cambia fra la documentazione e quello che si manda davvero, ed e'
        # l'errore che fa fallire ogni chiamata con "authenticationError".
        endpoint = path.replace("/derivatives", "", 1)
        sha = hashlib.sha256((post_data + nonce + endpoint).encode()).digest()
        return base64.b64encode(
            hmac.new(base64.b64decode(creds.api_secret), sha, hashlib.sha512).digest()
        ).decode()

    async def _call(self, method: str, path: str, creds: FuturesCreds, action: str,
                    params: dict | None = None) -> dict:
        query = urllib.parse.urlencode(params or {})
        nonce = str(int(time.time() * 1000))
        headers = {
            "APIKey": creds.api_key,
            "Nonce": nonce,
            "Authent": self._authent(creds, query, nonce, path),
        }
        url = path + ("?" + query if query and method == "GET" else "")
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            if method == "GET":
                resp = await c.get(url, headers=headers)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                resp = await c.request(method, path, headers=headers, content=query)
        if resp.status_code >= 400:
            raise explain("kraken", action, resp.text, _HINTS)
        data = resp.json()
        if data.get("result") == "error" or data.get("error"):
            raise explain("kraken", action, str(data.get("error") or data), _HINTS)
        return data

    async def available_margin(self, creds: FuturesCreds) -> float:
        data = await self._call("GET", f"{PREFIX}/accounts", creds, "balance")
        accounts = data.get("accounts", {})
        flex = accounts.get("flex") or {}
        if flex:
            return float(flex.get("availableMargin") or 0)
        for acct in accounts.values():
            if isinstance(acct, dict) and "availableMargin" in acct:
                return float(acct["availableMargin"] or 0)
        return 0.0

    async def _tickers(self) -> list[dict]:
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.get(f"{PREFIX}/tickers")
        return (resp.json() or {}).get("tickers") or []

    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        sym = self.symbol_for(symbol)
        for t in await self._tickers():
            if (t.get("symbol") or "").upper() == sym:
                return float(t.get("markPrice") or t.get("last") or 0)
        raise explain("kraken", "price", f"symbol {sym} unknown", _HINTS)

    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        data = await self._call("GET", f"{PREFIX}/openpositions", creds, "positions")
        rows = data.get("openPositions") or []
        if not rows:
            return []
        marks = {(t.get("symbol") or "").upper(): float(t.get("markPrice") or 0)
                 for t in await self._tickers()}
        out = []
        for p in rows:
            size = float(p.get("size") or 0)
            if size == 0:
                continue
            sym = (p.get("symbol") or "").upper()
            entry = float(p.get("price") or 0)
            mark = marks.get(sym, entry)
            lev = int(float(p.get("maxFixedLeverage") or 1) or 1)
            side = "long" if (p.get("side") or "").lower() == "long" else "short"
            notional = abs(size) * mark
            out.append(Position(
                exchange=self.slug, symbol=sym, side=side, quantity=abs(size),
                entry_price=entry, mark_price=mark, leverage=lev,
                margin=notional / lev if lev else 0.0,
                notional=notional,
                unrealized_pnl=(mark - entry) * size if side == "long" else (entry - mark) * abs(size),
                # Stima: Kraken non la manda con la posizione. Stessa formula
                # del backtest, cosi' i due numeri non possono divergere.
                liquidation_price=liquidation_price(entry, side, lev, DEFAULT_MAINTENANCE_MARGIN_RATE),
            ))
        return out

    async def place_order(self, symbol, side, size_mode, size_value, leverage, creds, max_usdt=None):
        sym = self.symbol_for(symbol)
        # Kraken non ha isolated: la leva massima per simbolo e' l'unico
        # argine che si puo' imporre prima dell'ordine, e lo imponiamo.
        await self._call("PUT", f"{PREFIX}/leveragepreferences", creds, "leverage",
                         {"symbol": sym, "maxLeverage": str(leverage)})
        available = await self.available_margin(creds)
        mark = await self.mark_price(sym, creds)
        notional = self._capped(self._notional(size_mode, size_value, leverage, available, mark), max_usdt)
        qty = self._qty(notional, mark, step=4)
        return await self._call("POST", f"{PREFIX}/sendorder", creds, "order", {
            "orderType": "mkt", "symbol": sym,
            "side": "buy" if side == "BUY" else "sell", "size": qty,
        })

    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        sym = self.symbol_for(symbol)
        pos = next((p for p in await self.open_positions(creds) if p.symbol == sym), None)
        if pos is None:
            return None
        return await self._call("POST", f"{PREFIX}/sendorder", creds, "close", {
            "orderType": "mkt", "symbol": sym,
            "side": "sell" if pos.side == "long" else "buy",
            "size": f"{pos.quantity:.4f}".rstrip("0").rstrip("."),
            # Solo riduzione: se la posizione fosse gia' chiusa da un altro
            # canale, questo ordine non ne apre una al contrario.
            "reduceOnly": "true",
        })
