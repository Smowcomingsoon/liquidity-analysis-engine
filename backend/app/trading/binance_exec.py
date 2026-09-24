import hashlib
import hmac
import time
import urllib.parse

import httpx

BINANCE_BASE = "https://api.binance.com"
MEXC_BASE = "https://api.mexc.com"

# MEXC espone la stessa API v3 di Binance — stessi percorsi, stessa firma
# HMAC, stessi nomi dei parametri — tranne l'intestazione con cui si presenta
# la chiave. Mandare a MEXC quella di Binance produce
# {"code":400,"msg":"api key required"}: la chiave c'era, ma nella busta
# sbagliata, e il messaggio dell'exchange dice "manca" perche' dal suo punto di
# vista mancava davvero. Il modulo dei saldi usa da sempre l'intestazione
# giusta, ed e' per questo che i saldi si vedevano mentre gli ordini no.
_API_KEY_HEADER = {
    "binance": "X-MBX-APIKEY",
    "mexc": "X-MEXC-APIKEY",
}

# Gli errori piu' comuni dei due exchange, tradotti in cosa fare. Il testo
# grezzo resta comunque nel messaggio: e' quello che serve per capire il caso
# che qui non e' previsto.
_HINTS = {
    "api key required": "the key never reached the exchange",
    "signature for this request is not valid": "the secret does not match the key",
    "invalid api key": "invalid key, or not enabled for spot trading",
    "api key info invalid": "invalid key for this exchange",
    "invalid symbol": "this pair does not exist on this exchange",
    "oversold": "insufficient balance for the requested amount",
    "insufficient position": "insufficient balance for the requested amount",
    "not authorized": "the key lacks order permission (spot trading)",
    # Non "ip" e basta: comparirebbe dentro parole qualunque del messaggio.
    "ip white": "the server IP is not whitelisted for this key",
    "ip address": "the server IP is not whitelisted for this key",
    # MEXC risponde cosi' quando la chiave puo' fare ordini ma non puo'
    # leggere il saldo — succede solo con "% saldo" (che deve leggerlo prima
    # di calcolare quanto ordinare), mai con un importo fisso, che salta
    # questo passaggio: da qui il sintomo "fisso funziona, percentuale no".
    "no authority": "the key lacks balance-read (account) permission — needed on top of trading",
    "no permission to access the endpoint": "the key lacks balance-read (account) permission — needed on top of trading",
}


def _explain(exchange: str, action: str, body: str) -> RuntimeError:
    """L'errore dell'exchange piu' cosa significa, quando lo sappiamo."""
    low = body.lower()
    for needle, hint in _HINTS.items():
        if needle in low:
            return RuntimeError(f"{exchange} {action} error: {body} — {hint}")
    return RuntimeError(f"{exchange} {action} error: {body}")


def _base_asset(symbol: str) -> str:
    """Strip USDT or BTC suffix to get the base asset (e.g. BTCUSDT → BTC)."""
    for quote in ("USDT", "BTC", "ETH", "USDC"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)]
    return symbol


def _sign(secret: str, params: dict) -> str:
    """Add timestamp, build query string, append HMAC-SHA256 signature."""
    params["timestamp"] = int(time.time() * 1000)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


async def place_order(
    exchange: str,
    symbol: str,
    side: str,
    size_mode: str,
    size_value: float,
    api_key: str,
    api_secret: str,
    max_usdt: float | None = None,
) -> dict:
    if exchange not in _API_KEY_HEADER:
        raise RuntimeError(f"Exchange not handled by this executor: {exchange}")
    base_url = BINANCE_BASE if exchange == "binance" else MEXC_BASE
    headers = {_API_KEY_HEADER[exchange]: api_key}

    async with httpx.AsyncClient(base_url=base_url, headers=headers) as client:
        order_params: dict = {"symbol": symbol, "side": side, "type": "MARKET"}

        if side == "BUY":
            if size_mode == "fixed_usdt":
                quote_qty = size_value
            else:
                resp = await client.get(f"/api/v3/account?{_sign(api_secret, {})}")
                if resp.status_code >= 400:
                    raise _explain(exchange, "account", resp.text)
                balances = {b["asset"]: float(b["free"]) for b in resp.json()["balances"]}
                free_usdt = balances.get("USDT", 0.0)
                quote_qty = free_usdt * size_value / 100
            # Tetto per singolo ingresso, indipendente dal size_mode — mai
            # applicato lato SELL (vedi place_order_for_signal in executor.py).
            if max_usdt:
                quote_qty = min(quote_qty, max_usdt)
            if exchange == "mexc":
                # MEXC dichiara quoteOrderQty a documentazione, ma su piu'
                # pair (specie quelli meno liquidi) lo ignora e tratta il
                # numero come quantita' di moneta base — un ordine da "10
                # USDT" diventa "10 monete", spesso molto piu' del saldo
                # disponibile. Bypassato calcolando la quantita' noi stessi
                # dal prezzo corrente e mandando "quantity", come gia' si fa
                # per il lato SELL qui sotto — MEXC quella la rispetta sempre.
                price_resp = await client.get(f"/api/v3/ticker/price?symbol={symbol}")
                if price_resp.status_code >= 400:
                    raise _explain(exchange, "price", price_resp.text)
                price = float(price_resp.json()["price"])
                if price <= 0:
                    raise RuntimeError("Market price not available for this symbol")
                qty = quote_qty / price
                order_params["quantity"] = f"{qty:.6f}".rstrip("0").rstrip(".")
            else:
                order_params["quoteOrderQty"] = quote_qty

        else:  # SELL
            if size_mode == "fixed_usdt":
                price_resp = await client.get(f"/api/v3/ticker/price?symbol={symbol}")
                if price_resp.status_code >= 400:
                    raise _explain(exchange, "price", price_resp.text)
                price = float(price_resp.json()["price"])
                qty = size_value / price
            else:
                base = _base_asset(symbol)
                resp = await client.get(f"/api/v3/account?{_sign(api_secret, {})}")
                if resp.status_code >= 400:
                    raise _explain(exchange, "account", resp.text)
                balances = {b["asset"]: float(b["free"]) for b in resp.json()["balances"]}
                free = balances.get(base, 0.0)
                qty = free * size_value / 100
            if qty <= 0:
                raise RuntimeError("Computed quantity is zero or negative — insufficient balance or price unavailable")
            order_params["quantity"] = f"{qty:.6f}".rstrip("0").rstrip(".")

        signed_qs = _sign(api_secret, order_params)
        order_resp = await client.post(f"/api/v3/order?{signed_qs}")
        if order_resp.status_code >= 400:
            raise _explain(exchange, "order", order_resp.text)
        return order_resp.json()
