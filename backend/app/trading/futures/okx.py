"""OKX perpetui USDT — API v5, `instType=SWAP`.

Tre trappole di OKX, tutte capaci di far sbagliare la dimensione di un
ordine a leva:

1. **La quantita' non e' in monete, e' in contratti.** `sz=1` su BTC-USDT-SWAP
   non e' un bitcoin: e' un contratto, che vale `ctVal` monete (0.01 per BTC).
   Chi calcola `notional / prezzo` e lo manda come `sz` apre una posizione
   cento volte piu' grande di quella che voleva. Qui `ctVal` si legge
   dall'exchange per ogni simbolo, non si presume.
2. **La passphrase e' obbligatoria** e non e' la password del conto: e'
   quella scelta quando si e' creata la chiave API.
3. **HTTP 200 con `code` diverso da "0" e' un errore.** Come su Bybit, lo
   stato HTTP da solo non dice se l'ordine e' passato.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

import httpx

from app.trading.futures.base import FuturesAdapter, FuturesCreds, Position, explain

BASE = "https://www.okx.com"

_HINTS = {
    "50113": "wrong or missing passphrase",
    "50111": "invalid API key for OKX",
    "51008": "insufficient margin for the requested amount",
    "51000": "parameter rejected by OKX",
    "51004": "size exceeds the account limit for this leverage",
    "59000": "close open positions before changing margin mode",
}


class OkxFutures(FuturesAdapter):
    slug = "okx"
    name = "OKX Perpetual"
    needs_passphrase = True
    max_leverage = 10
    key_hint = "API key with the «Trade» permission and the passphrase you chose when creating it (not the account password)."

    def symbol_for(self, symbol: str) -> str:
        """BTCUSDT → BTC-USDT-SWAP. OKX e' l'unico dei cinque a non usare la
        forma incollata, e sbagliarla da' un errore che non spiega niente."""
        s = symbol.strip().upper().replace("-", "").replace("_", "")
        if s.endswith("SWAP"):
            s = s[:-4]
        for quote in ("USDT", "USDC", "USD"):
            if s.endswith(quote):
                return f"{s[: -len(quote)]}-{quote}-SWAP"
        return f"{s}-USDT-SWAP"

    def _headers(self, creds: FuturesCreds, method: str, path: str, body: str) -> dict:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        raw = ts + method.upper() + path + body
        sign = base64.b64encode(
            hmac.new(creds.api_secret.encode(), raw.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "OK-ACCESS-KEY": creds.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": creds.passphrase,
            "Content-Type": "application/json",
        }

    async def _call(self, method: str, path: str, creds: FuturesCreds, action: str,
                    body: dict | None = None, tolerate: tuple[str, ...] = ()) -> list[dict]:
        payload = json.dumps(body, separators=(",", ":")) if body else ""
        headers = self._headers(creds, method, path, payload)
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            if method == "GET":
                resp = await c.get(path, headers=headers)
            else:
                resp = await c.post(path, headers=headers, content=payload)
        if resp.status_code >= 400:
            raise explain("okx", action, resp.text, _HINTS)
        data = resp.json()
        code = str(data.get("code", "0"))
        if code != "0" and code not in tolerate:
            detail = data.get("msg") or ""
            inner = (data.get("data") or [{}])
            if inner and isinstance(inner[0], dict):
                if str(inner[0].get("sCode", "0")) in tolerate:
                    return data.get("data") or []
                detail = detail or inner[0].get("sMsg", "")
            raise explain("okx", action, f"{code} {detail}", _HINTS)
        return data.get("data") or []

    async def available_margin(self, creds: FuturesCreds) -> float:
        rows = await self._call("GET", "/api/v5/account/balance?ccy=USDT", creds, "balance")
        for row in rows:
            for d in row.get("details", []):
                if d.get("ccy") == "USDT":
                    return float(d.get("availEq") or d.get("availBal") or 0)
        return 0.0

    async def mark_price(self, symbol: str, creds: FuturesCreds) -> float:
        inst = self.symbol_for(symbol)
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.get("/api/v5/public/mark-price",
                               params={"instType": "SWAP", "instId": inst})
        rows = resp.json().get("data") or []
        if not rows:
            raise explain("okx", "price", f"instId {inst} unknown", _HINTS)
        return float(rows[0]["markPx"])

    async def _contract_value(self, inst_id: str) -> float:
        """Quante monete vale un contratto. Letto dall'exchange: e' il numero
        che separa un ordine giusto da uno cento volte piu' grande."""
        async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:
            resp = await c.get("/api/v5/public/instruments",
                               params={"instType": "SWAP", "instId": inst_id})
        rows = resp.json().get("data") or []
        if not rows:
            raise explain("okx", "instrument", f"instId {inst_id} unknown", _HINTS)
        ct_val = float(rows[0].get("ctVal") or 0)
        if ct_val <= 0:
            raise explain("okx", "instrument", f"ctVal not declared for {inst_id}", _HINTS)
        return ct_val

    async def open_positions(self, creds: FuturesCreds) -> list[Position]:
        rows = await self._call("GET", "/api/v5/account/positions?instType=SWAP", creds, "positions")
        out = []
        for p in rows:
            pos = float(p.get("pos") or 0)
            if pos == 0:
                continue
            ct_val = float(p.get("ctVal") or 1)
            out.append(Position(
                exchange=self.slug, symbol=p.get("instId", ""),
                side="long" if pos > 0 else "short",
                quantity=abs(pos) * ct_val,
                entry_price=float(p.get("avgPx") or 0),
                mark_price=float(p.get("markPx") or 0),
                leverage=int(float(p.get("lever") or 1)),
                margin=float(p.get("margin") or p.get("imr") or 0),
                notional=abs(float(p.get("notionalUsd") or 0)),
                unrealized_pnl=float(p.get("upl") or 0),
                liquidation_price=float(p.get("liqPx") or 0),
            ))
        return out

    async def place_order(self, symbol, side, size_mode, size_value, leverage, creds, max_usdt=None):
        inst = self.symbol_for(symbol)
        # 59000 = ci sono posizioni aperte e la modalita' non si puo'
        # cambiare: la leva richiesta non viene applicata, quindi e' un
        # errore vero e non lo tolleriamo.
        await self._call("POST", "/api/v5/account/set-leverage", creds, "leverage",
                         {"instId": inst, "lever": str(leverage), "mgnMode": "isolated"})
        available = await self.available_margin(creds)
        mark = await self.mark_price(inst, creds)
        notional = self._capped(self._notional(size_mode, size_value, leverage, available, mark), max_usdt)
        ct_val = await self._contract_value(inst)
        coins = float(self._qty(notional, mark, step=8))
        contracts = coins / ct_val
        if contracts < 1:
            raise explain(
                "okx", "order",
                f"the requested position is worth less than one contract ({ct_val} units)", _HINTS,
            )
        return {"data": await self._call("POST", "/api/v5/trade/order", creds, "order", {
            "instId": inst, "tdMode": "isolated",
            "side": "buy" if side == "BUY" else "sell",
            "ordType": "market", "sz": str(int(contracts)),
        })}

    async def close_position(self, symbol: str, creds: FuturesCreds) -> dict | None:
        inst = self.symbol_for(symbol)
        if not any(p.symbol == inst for p in await self.open_positions(creds)):
            return None
        # Endpoint dedicato alla chiusura: OKX chiude quello che c'e',
        # quindi non esiste una quantita' da sbagliare in questa direzione.
        return {"data": await self._call("POST", "/api/v5/trade/close-position", creds, "close",
                                         {"instId": inst, "mgnMode": "isolated"})}
