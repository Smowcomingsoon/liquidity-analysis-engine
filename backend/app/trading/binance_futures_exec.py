"""Esecuzione futures su Binance (USDT-M perpetui) — /fapi/v1/...

Modulo separato da binance_exec.py apposta (vedi il piano "Operazioni
futures"): l'API futures di Binance non e' una variante dei percorsi spot,
e' un prodotto diverso — dominio diverso (fapi.binance.com), leva e margin
mode da dichiarare esplicitamente prima dell'ordine, endpoint di posizione e
margine propri. Tenerlo separato garantisce che un bug qui non possa mai
toccare l'esecuzione spot.

Solo isolated margin: vedi FuturesCredential.margin_mode e il piano — un
conto cross fa esplodere posizioni non correlate a catena.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse

import httpx

FAPI_BASE = "https://fapi.binance.com"

_HINTS = {
    "api key required": "the key never reached the exchange",
    "signature for this request is not valid": "the secret does not match the key",
    "invalid api key": "invalid key, or not enabled for futures trading",
    "api key info invalid": "invalid key for this exchange",
    "invalid symbol": "this perpetual does not exist on Binance Futures",
    "margin is insufficient": "insufficient margin for the requested amount",
    "not authorized": "the key lacks futures order permission",
    "ip white": "the server IP is not whitelisted for this key",
    "ip address": "the server IP is not whitelisted for this key",
    "leverage is invalid": "leverage not allowed for this symbol",
}


class FuturesExecutionError(RuntimeError):
    pass


def _explain(action: str, body: str) -> FuturesExecutionError:
    low = body.lower()
    for needle, hint in _HINTS.items():
        if needle in low:
            return FuturesExecutionError(f"binance futures {action} error: {body} — {hint}")
    return FuturesExecutionError(f"binance futures {action} error: {body}")


def _sign(secret: str, params: dict) -> str:
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


async def get_available_margin_usdt(client: httpx.AsyncClient, api_secret: str) -> float:
    """Margine USDT libero sul wallet futures — letto per davvero
    dall'exchange, mai fidandosi di un saldo in cache, prima di ogni ordine
    (controllo pre-trade richiesto dal piano)."""
    resp = await client.get(f"/fapi/v2/balance?{_sign(api_secret, {})}")
    if resp.status_code >= 400:
        raise _explain("balance", resp.text)
    for entry in resp.json():
        if entry.get("asset") == "USDT":
            return float(entry.get("availableBalance", 0.0))
    return 0.0


async def get_mark_price(client: httpx.AsyncClient, symbol: str) -> float:
    resp = await client.get(f"/fapi/v1/premiumIndex?symbol={symbol}")
    if resp.status_code >= 400:
        raise _explain("premiumIndex", resp.text)
    return float(resp.json()["markPrice"])


async def get_position_risk(client: httpx.AsyncClient, symbol: str, api_secret: str) -> dict | None:
    """Posizione aperta reale per il simbolo, con la leva e il prezzo di
    liquidazione che l'exchange calcola per davvero — usato dopo l'ordine
    per confermare che quanto piazzato corrisponde a quanto previsto."""
    resp = await client.get(f"/fapi/v2/positionRisk?symbol={symbol}&{_sign(api_secret, {})}")
    if resp.status_code >= 400:
        raise _explain("positionRisk", resp.text)
    for pos in resp.json():
        if pos.get("symbol") == symbol and float(pos.get("positionAmt", 0)) != 0:
            return pos
    return None


async def get_open_positions(client: httpx.AsyncClient, api_secret: str) -> list[dict]:
    """Tutte le posizioni aperte, non solo quella di un simbolo. Serve alla
    schermata Opera: finche' una posizione a leva resta aperta, saperla
    aperta e' piu' importante di qualunque altro numero della pagina."""
    resp = await client.get(f"/fapi/v2/positionRisk?{_sign(api_secret, {})}")
    if resp.status_code >= 400:
        raise _explain("positionRisk", resp.text)
    return [p for p in resp.json() if float(p.get("positionAmt", 0)) != 0]


async def _set_leverage(client: httpx.AsyncClient, symbol: str, leverage: int, api_secret: str) -> None:
    params = {"symbol": symbol, "leverage": leverage}
    resp = await client.post(f"/fapi/v1/leverage?{_sign(api_secret, params)}")
    if resp.status_code >= 400:
        raise _explain("leverage", resp.text)


async def _set_isolated_margin(client: httpx.AsyncClient, symbol: str, api_secret: str) -> None:
    params = {"symbol": symbol, "marginType": "ISOLATED"}
    resp = await client.post(f"/fapi/v1/marginType?{_sign(api_secret, params)}")
    if resp.status_code >= 400:
        body = resp.text
        # Binance risponde 400 "No need to change margin type" se e' gia'
        # isolated per quel simbolo — non e' un errore, e' lo stato che
        # volevamo gia' raggiunto.
        if "no need to change" not in body.lower():
            raise _explain("marginType", body)


async def close_position(symbol: str, api_key: str, api_secret: str) -> dict | None:
    """Chiude per intero la posizione aperta su questo simbolo, se c'e'
    (reduceOnly: non puo' mai aprire o ingrandire una posizione, solo
    ridurla) — usato per exit_long/exit_short e come primo passo di un
    flip, prima di aprire nella nuova direzione. Restituisce None se non
    c'era nessuna posizione da chiudere."""
    headers = {"X-MBX-APIKEY": api_key}
    async with httpx.AsyncClient(base_url=FAPI_BASE, headers=headers, timeout=15.0) as client:
        pos = await get_position_risk(client, symbol, api_secret)
        if pos is None:
            return None
        amt = float(pos["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        qty = abs(amt)
        order_params = {
            "symbol": symbol, "side": side, "type": "MARKET",
            "quantity": f"{qty:.6f}".rstrip("0").rstrip("."),
            "reduceOnly": "true",
        }
        resp = await client.post(f"/fapi/v1/order?{_sign(api_secret, order_params)}")
        if resp.status_code >= 400:
            raise _explain("close", resp.text)
        return resp.json()


async def place_futures_order(
    symbol: str,
    side: str,  # "BUY" | "SELL"
    size_mode: str,
    size_value: float,
    leverage: int,
    api_key: str,
    api_secret: str,
    max_usdt: float | None = None,
) -> dict:
    """Piazza un ordine market su Binance Futures, dichiarando prima leva e
    margin mode isolated. Il controllo di margine sufficiente e la
    conferma esplicita sono responsabilita' del chiamante (endpoint
    /api/trading/futures/order) — questa funzione presume che siano gia'
    passati, per restare un'unica cosa: mandare l'ordine."""
    headers = {"X-MBX-APIKEY": api_key}
    async with httpx.AsyncClient(base_url=FAPI_BASE, headers=headers, timeout=15.0) as client:
        await _set_isolated_margin(client, symbol, api_secret)
        await _set_leverage(client, symbol, leverage, api_secret)

        mark = await get_mark_price(client, symbol)
        if mark <= 0:
            raise FuturesExecutionError("Market price not available for this symbol")

        if size_mode == "fixed_usdt":
            notional = size_value * leverage
        elif size_mode == "base_qty":
            # Quantita' di moneta base gia' data (es. 0.05 BTC): niente
            # conversione da un importo, e' gia' quello che si vuole
            # comprare — vedi FuturesAdapter._notional per lo stesso
            # principio sugli altri exchange.
            notional = size_value * mark
        else:
            available = await get_available_margin_usdt(client, api_secret)
            margin = available * size_value / 100
            notional = margin * leverage

        # Tetto per singolo ingresso — mai applicato da close_position, che
        # chiude sempre per intero. Vedi FuturesAdapter._capped.
        if max_usdt:
            notional = min(notional, max_usdt)

        qty = notional / mark
        if qty <= 0:
            raise FuturesExecutionError("Computed quantity is zero or negative — insufficient margin")

        order_params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": f"{qty:.6f}".rstrip("0").rstrip(".")}
        resp = await client.post(f"/fapi/v1/order?{_sign(api_secret, order_params)}")
        if resp.status_code >= 400:
            raise _explain("order", resp.text)
        return resp.json()
