"""Bybit perpetui USDT — API v5, `category=linear`.

Due cose di Bybit che non assomigliano a nessun altro exchange:

1. **La firma non e' sulla query string.** E' su
   `timestamp + api_key + recv_window + payload`, dove `payload` e' la query
   string per un GET e il corpo JSON *esatto* per un POST. Esatto vuol dire
   la stringa che viene spedita: se si serializza due volte con separatori
   diversi la firma non torna, quindi il corpo si costruisce una volta sola
   e si manda quella.
2. **HTTP 200 non vuol dire che sia andata.** L'errore vero sta in
   `retCode` dentro il corpo. Fidarsi dello stato HTTP qui significa
   scambiare un ordine rifiutato per un ordine eseguito.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

import httpx

from app.trading.futures.base import FuturesAdapter, FuturesCreds, Position, explain

BASE = "https://api.bybit.com"
RECV_WINDOW = "10000"

_HINTS = {
    "10003": "invalid API key for Bybit",
    "10004": "the secret does not match the key",
    "110007": "insufficient margin for the requested amount",
    "110043": "leverage was already at the requested value",
    "10016": "Bybit not responding (maintenance or an issue on their side)",
    "not modified": "setting already at the requested value",
}


class BybitFutures(FuturesAdapter):
    slug = "bybit"
    name = "Bybit Perpetual"
    needs_passphrase = False
    max_leverage = 10
    key_hint = "API key with the «Contract — Orders & Positions» permission. If the account uses an IP whitelist, add the server IP."

    def _headers(self, creds: FuturesCreds, payload: str) -> dict:
        ts = str(int(time.time() * 1000))
        raw = ts + creds.api_key + RECV_WINDOW + payload
        sign = hmac.new(creds.api_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY": creds.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: dict, creds: FuturesCreds, action: str) -> dict:
        query = urllib.parse.urlencode(params)
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.get(f"{path}?{query}", headers=self._headers(creds, query))
        return self._unwrap(resp, action)

    async def _post(self, path: str, body: dict, creds: FuturesCreds, action: str,
                    tolerate: tuple[int, ...] = ()) -> dict:
        # Una sola serializzazione: la stringa firmata e quella spedita
        # devono essere lo stesso oggetto, non due stringhe equivalenti.
        payload = json.dumps(body, separators=(",", ":"))
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.post(path, headers=self._headers(creds, payload), content=payload)
        return self._unwrap(resp, action, tolerate)

    def _unwrap(self, resp: httpx.Response, action: str, tolerate: tuple[int, ...] = ()) -> dict:
        if resp.status_code >= 400:
            raise explain("bybit", action, resp.text, _HINTS)
        data = resp.json()
        code = data.get("retCode")
        if code and code not in tolerate:
            raise explain("bybit", action, f'{code} {data.get("retMsg", "")}', _HINTS)
        return data.get("result") or {}

    async def available_margin(self, creds: FuturesCreds) -> float:
        res = await self._get("/v5/account/wallet-balance",
                              {"accountType": "UNIFIED"}, creds, "balance")
        for acct in res.get("list", []):
            free = acct.get("totalAvailableBalance")
            if free not in (None, ""):
                return float(free)
            for coin in acct.get("coin", []):
                if coin.get("coin") == "USDT":
                    return float(coin.get("availableToWithdraw") or coin.get("walletBalance") or 0)
        return 0.0

    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        # Endpoint pubblico: nessuna firma, e quindi nessun motivo di
        # fallire per una chiave sbagliata quando serve solo un prezzo.
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.get("/v5/market/tickers",
                               params={"category": "linear", "symbol": self.symbol_for(symbol)})
        res = self._unwrap(resp, "price")
        rows = res.get("list") or []
        if not rows:
            raise explain("bybit", "price", f"symbol {symbol} unknown", _HINTS)
        return float(rows[0]["markPrice"])

    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        res = await self._get("/v5/position/list",
                              {"category": "linear", "settleCoin": "USDT"}, creds, "positions")
        out = []
        for p in res.get("list", []):
            size = float(p.get("size") or 0)
            if size == 0:
                continue
            out.append(Position(
                exchange=self.slug, symbol=p.get("symbol", ""),
                side="long" if p.get("side") == "Buy" else "short", quantity=size,
                entry_price=float(p.get("avgPrice") or 0),
                mark_price=float(p.get("markPrice") or 0),
                leverage=int(float(p.get("leverage") or 1)),
                margin=float(p.get("positionIM") or p.get("positionBalance") or 0),
                notional=float(p.get("positionValue") or 0),
                unrealized_pnl=float(p.get("unrealisedPnl") or 0),
                liquidation_price=float(p.get("liqPrice") or 0),
            ))
        return out

    async def _prepare(self, symbol: str, leverage: int, creds: FuturesCreds) -> None:
        lev = str(leverage)
        # 110026 = "gia' isolated con questa leva". Non e' un errore: e' lo
        # stato che volevamo raggiungere, e trattarlo come tale evita di
        # bloccare ogni ordine dopo il primo sullo stesso simbolo.
        await self._post("/v5/position/switch-isolated",
                         {"category": "linear", "symbol": symbol, "tradeMode": 1,
                          "buyLeverage": lev, "sellLeverage": lev},
                         creds, "isolated margin", tolerate=(110026,))
        # 110043 = leva gia' impostata a quel valore, stessa logica.
        await self._post("/v5/position/set-leverage",
                         {"category": "linear", "symbol": symbol,
                          "buyLeverage": lev, "sellLeverage": lev},
                         creds, "leverage", tolerate=(110043,))

    async def place_order(self, symbol, side, size_mode, size_value, leverage, creds, max_usdt=None):
        sym = self.symbol_for(symbol)
        await self._prepare(sym, leverage, creds)
        available = await self.available_margin(creds)
        mark = await self.mark_price(sym, creds)
        notional = self._capped(self._notional(size_mode, size_value, leverage, available, mark), max_usdt)
        qty = self._qty(notional, mark, step=3)
        return await self._post("/v5/order/create", {
            "category": "linear", "symbol": sym,
            "side": "Buy" if side == "BUY" else "Sell",
            "orderType": "Market", "qty": qty,
        }, creds, "order")

    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        sym = self.symbol_for(symbol)
        pos = next((p for p in await self.open_positions(creds) if p.symbol == sym), None)
        if pos is None:
            return None
        return await self._post("/v5/order/create", {
            "category": "linear", "symbol": sym,
            "side": "Sell" if pos.side == "long" else "Buy",
            "orderType": "Market", "qty": f"{pos.quantity:.3f}".rstrip("0").rstrip("."),
            # Solo riduzione: questo ordine non puo' in nessun caso aprire
            # una posizione nuova, nemmeno se la quantita' fosse sbagliata.
            "reduceOnly": True,
        }, creds, "close")
