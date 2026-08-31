import base64
import hashlib
import hmac
import time
import urllib.parse

import httpx

KRAKEN_BASE = "https://api.kraken.com"


def _kraken_pair(symbol: str) -> str:
    """Convert a generic symbol to Kraken format (e.g. BTCUSDT → XBTUSDT)."""
    if symbol.upper() == "BTCUSDT":
        return "XBTUSDT"
    return symbol


def _kraken_headers(
    api_key: str,
    api_secret: str,
    path: str,
    data: dict,
) -> dict[str, str]:
    """Build the API-Key and API-Sign headers required by Kraken private endpoints."""
    nonce = data["nonce"]
    post_body = urllib.parse.urlencode(data)
    sha256_digest = hashlib.sha256((str(nonce) + post_body).encode()).digest()
    path_bytes = path.encode()
    secret_bytes = base64.b64decode(api_secret)
    mac = hmac.new(secret_bytes, path_bytes + sha256_digest, hashlib.sha512)
    api_sign = base64.b64encode(mac.digest()).decode()
    return {
        "API-Key": api_key,
        "API-Sign": api_sign,
    }


async def _get_balance(
    api_key: str,
    api_secret: str,
    currency: str,
) -> float:
    """Query Kraken Balance endpoint and return balance for the given currency.

    Tries both "X" + currency (e.g. "XXBT") and the plain currency key.
    """
    path = "/0/private/Balance"
    url = KRAKEN_BASE + path
    nonce = str(int(time.time() * 1000))
    data: dict[str, str] = {"nonce": nonce}
    headers = _kraken_headers(api_key, api_secret, path, data)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            url,
            data=data,
            headers=headers,
        )

    if resp.status_code != 200:
        raise RuntimeError(
            f"Kraken Balance request failed with HTTP {resp.status_code}: {resp.text}"
        )

    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken Balance API error: {body['error']}")

    result: dict[str, str] = body.get("result", {})

    # Try "X"-prefixed key first (e.g. XXBT, XETH), then plain key
    x_key = "X" + currency
    if x_key in result:
        return float(result[x_key])
    if currency in result:
        return float(result[currency])

    # Currency not found — balance is zero
    return 0.0


async def _get_price(pair: str) -> float:
    """Fetch the last trade price for a Kraken trading pair via the public Ticker endpoint."""
    url = f"{KRAKEN_BASE}/0/public/Ticker"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"pair": pair})

    if resp.status_code != 200:
        raise RuntimeError(
            f"Kraken Ticker request failed with HTTP {resp.status_code}: {resp.text}"
        )

    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken Ticker API error: {body['error']}")

    result: dict = body.get("result", {})
    if not result:
        raise RuntimeError(f"Kraken Ticker returned empty result for pair {pair}")

    # The result key may differ from the requested pair name; take the first entry
    ticker_data = next(iter(result.values()))
    # "c" is the last trade closed array: [price, lot volume]
    last_price = float(ticker_data["c"][0])
    return last_price


def _base_currency(symbol: str) -> str:
    """Extract the base asset from a symbol by stripping known quote suffixes.

    Returns the raw base string (e.g. "XBT" from "XBTUSDT", "ETH" from "ETHUSDT").
    """
    kraken_sym = _kraken_pair(symbol)
    for quote in ("USDT", "USD", "EUR", "BTC", "ETH"):
        if kraken_sym.endswith(quote):
            return kraken_sym[: -len(quote)]
    return kraken_sym


async def place_order(
    symbol: str,
    side: str,           # "BUY" or "SELL"
    size_mode: str,      # "fixed_usdt" or "pct_balance"
    size_value: float,
    api_key: str,
    api_secret: str,
) -> dict:
    """Place a market order on Kraken.

    Parameters
    ----------
    symbol:     Trading pair in generic notation, e.g. "BTCUSDT".
    side:       "BUY" or "SELL" (case-insensitive).
    size_mode:  "fixed_usdt" — treat size_value as a USDT amount.
                "pct_balance" — treat size_value as a percentage of available balance.
    size_value: Numeric amount corresponding to size_mode.
    api_key:    Kraken API key.
    api_secret: Kraken API secret (base64-encoded).

    Returns
    -------
    dict: The ``result`` field from the Kraken AddOrder response.
    """
    side_upper = side.upper()
    kraken_sym = _kraken_pair(symbol)
    base_currency = _base_currency(symbol)

    path = "/0/private/AddOrder"
    url = KRAKEN_BASE + path
    nonce = str(int(time.time() * 1000))

    data: dict[str, str] = {
        "nonce": nonce,
        "ordertype": "market",
        "type": "buy" if side_upper == "BUY" else "sell",
        "pair": kraken_sym,
    }

    if side_upper == "BUY":
        if size_mode == "fixed_usdt":
            volume = size_value
        elif size_mode == "pct_balance":
            usdt_balance = await _get_balance(api_key, api_secret, "USDT")
            volume = usdt_balance * size_value / 100.0
        else:
            raise RuntimeError(f"Unknown size_mode: {size_mode!r}")

        data["volume"] = str(volume)
        data["oflags"] = "viqc"  # volume in quote currency (USDT)

    elif side_upper == "SELL":
        if size_mode == "fixed_usdt":
            last_price = await _get_price(kraken_sym)
            qty = size_value / last_price
        elif size_mode == "pct_balance":
            base_balance = await _get_balance(api_key, api_secret, base_currency)
            qty = base_balance * size_value / 100.0
        else:
            raise RuntimeError(f"Unknown size_mode: {size_mode!r}")

        data["volume"] = str(qty)
        # No oflags for sell — volume is in base currency

    else:
        raise RuntimeError(f"Invalid side: {side!r}. Expected 'BUY' or 'SELL'.")

    headers = _kraken_headers(api_key, api_secret, path, data)

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=data, headers=headers)

    if resp.status_code != 200:
        raise RuntimeError(
            f"Kraken AddOrder request failed with HTTP {resp.status_code}: {resp.text}"
        )

    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken AddOrder API error: {body['error']}")

    return body.get("result", {})
