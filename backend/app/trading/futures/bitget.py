"""Bitget perpetui USDT — API v2, `productType=USDT-FUTURES`.

Come OKX vuole una passphrase e firma `timestamp + metodo + percorso +
corpo`, ma con due differenze che contano:

- il percorso firmato comprende la query string, quindi per un GET si firma
  `/api/v2/...?a=1&b=2` e non solo il percorso;
- l'esito sta in `code`, e "00000" e' l'unico valore che significa riuscito.

La quantita' e' in monete (non in contratti come OKX), quindi qui il calcolo
e' quello normale.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse

import httpx

from app.trading.futures.base import FuturesAdapter, FuturesCreds, Position, explain

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
MARGIN_COIN = "USDT"

_HINTS = {
    "40009": "the secret does not match the key",
    "40012": "invalid API key for Bitget",
    "40014": "the key lacks futures order permission",
    "40018": "the server IP is not whitelisted for this key",
    "40037": "API key does not exist",
    "22002": "no open position to close",
    "45110": "amount below Bitget's minimum for this symbol",
}


class BitgetFutures(FuturesAdapter):
    slug = "bitget"
    name = "Bitget Futures"
    needs_passphrase = True
    max_leverage = 10
    key_hint = "API key with the «Futures Trading» permission and the passphrase you chose when creating it."

    def _headers(self, creds: FuturesCreds, method: str, path: str, body: str) -> dict:
        ts = str(int(time.time() * 1000))
        sign = base64.b64encode(
            hmac.new(creds.api_secret.encode(), (ts + method.upper() + path + body).encode(),
                     hashlib.sha256).digest()
        ).decode()
        return {
            "ACCESS-KEY": creds.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": creds.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    async def _call(self, method: str, path: str, creds: FuturesCreds, action: str,
                    params: dict | None = None, body: dict | None = None,
                    tolerate: tuple[str, ...] = ()):
        full = path + ("?" + urllib.parse.urlencode(params) if params else "")
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._headers(creds, method, full, payload)
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            if method == "GET":
                resp = await c.get(full, headers=headers)
            else:
                resp = await c.post(full, headers=headers, content=payload)
        if resp.status_code >= 400:
            raise explain("bitget", action, resp.text, _HINTS)
        data = resp.json()
        code = str(data.get("code", "00000"))
        if code != "00000" and code not in tolerate:
            raise explain("bitget", action, f'{code} {data.get("msg", "")}', _HINTS)
        return data.get("data")

    async def available_margin(self, creds: FuturesCreds) -> float:
        data = await self._call("GET", "/api/v2/mix/account/accounts", creds, "balance",
                                params={"productType": PRODUCT})
        for acct in data or []:
            if acct.get("marginCoin") == MARGIN_COIN:
                return float(acct.get("available") or 0)
        return 0.0

    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        sym = self.symbol_for(symbol)
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.get("/api/v2/mix/market/ticker",
                               params={"symbol": sym, "productType": PRODUCT})
        rows = (resp.json() or {}).get("data") or []
        if not rows:
            raise explain("bitget", "price", f"symbol {sym} unknown", _HINTS)
        row = rows[0]
        return float(row.get("markPrice") or row.get("indexPrice") or row.get("lastPr") or 0)

    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        data = await self._call("GET", "/api/v2/mix/position/all-position", creds, "positions",
                                params={"productType": PRODUCT, "marginCoin": MARGIN_COIN})
        out = []
        for p in data or []:
            size = float(p.get("total") or 0)
            if size == 0:
                continue
            out.append(Position(
                exchange=self.slug, symbol=p.get("symbol", ""),
                side="long" if p.get("holdSide") == "long" else "short", quantity=abs(size),
                entry_price=float(p.get("openPriceAvg") or 0),
                mark_price=float(p.get("markPrice") or 0),
                leverage=int(float(p.get("leverage") or 1)),
                margin=float(p.get("marginSize") or 0),
                notional=abs(float(p.get("total") or 0)) * float(p.get("markPrice") or 0),
                unrealized_pnl=float(p.get("unrealizedPL") or 0),
                liquidation_price=float(p.get("liquidationPrice") or 0),
            ))
        return out

    async def place_order(self, symbol, side, size_mode, size_value, leverage, creds, max_usdt=None):
        sym = self.symbol_for(symbol)
        common = {"symbol": sym, "productType": PRODUCT, "marginCoin": MARGIN_COIN}
        # 40808/22002 qui significano "era gia' cosi'": lo stato voluto e'
        # raggiunto, non c'e' niente da correggere.
        await self._call("POST", "/api/v2/mix/account/set-margin-mode", creds, "isolated margin",
                         body={**common, "marginMode": "isolated"}, tolerate=("40808", "22002"))
        await self._call("POST", "/api/v2/mix/account/set-leverage", creds, "leverage",
                         body={**common, "leverage": str(leverage)}, tolerate=("40808",))
        available = await self.available_margin(creds)
        mark = await self.mark_price(sym, creds)
        notional = self._capped(self._notional(size_mode, size_value, leverage, available, mark), max_usdt)
        qty = self._qty(notional, mark, step=4)
        return {"data": await self._call("POST", "/api/v2/mix/order/place-order", creds, "order",
                                         body={**common, "marginMode": "isolated",
                                               "size": qty, "side": side.lower(),
                                               "orderType": "market"})}

    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        sym = self.symbol_for(symbol)
        if not any(p.symbol == sym for p in await self.open_positions(creds)):
            return None
        # Endpoint di chiusura dedicato: non accetta una quantita', quindi
        # non puo' aprire niente per sbaglio.
        return {"data": await self._call("POST", "/api/v2/mix/order/close-positions", creds,
                                         "close",
                                         body={"symbol": sym, "productType": PRODUCT},
                                         tolerate=("22002",))}
