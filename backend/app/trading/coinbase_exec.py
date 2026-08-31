import hashlib
import hmac
import time
import uuid

import httpx

COINBASE_BASE = "https://api.coinbase.com"

_QUOTE_CURRENCIES = ("USDT", "USD", "EUR", "BTC", "ETH")


def _cb_pair(symbol: str) -> str:
    """Convert a concatenated symbol to Coinbase dash-separated pair.

    Examples:
        "BTCUSDT" -> "BTC-USDT"
        "ETHBTC"  -> "ETH-BTC"
        "EURUSDT" -> "EUR-USDT"
    """
    for quote in _QUOTE_CURRENCIES:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            return f"{base}-{quote}"
    raise RuntimeError(f"Cannot determine quote currency for symbol: {symbol!r}")


def _cb_headers(
    api_key: str,
    api_secret: str,
    method: str,
    path: str,
    body_str: str,
) -> dict[str, str]:
    """Build Coinbase Advanced Trade auth headers."""
    timestamp = str(int(time.time()))
    message = timestamp + method.upper() + path + body_str
    signature = hmac.new(
        api_secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "CB-ACCESS-KEY": api_key,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }


async def _get_balance(
    api_key: str,
    api_secret: str,
    currency: str,
) -> float:
    """Return available balance for *currency* from the Coinbase accounts list."""
    path = "/api/v3/brokerage/accounts"
    headers = _cb_headers(api_key, api_secret, "GET", path, "")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            COINBASE_BASE + path,
            headers=headers,
            timeout=10.0,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch accounts: HTTP {response.status_code} — {response.text}"
        )

    data = response.json()
    accounts: list[dict] = data.get("accounts", [])
    for account in accounts:
        if account.get("currency") == currency:
            value = account.get("available_balance", {}).get("value", "0")
            return float(value)

    raise RuntimeError(f"No account found for currency: {currency!r}")


async def _get_price(
    api_key: str,
    api_secret: str,
    product_id: str,
) -> float:
    """Return the best ask price for *product_id* from best_bid_ask."""
    path = f"/api/v3/brokerage/best_bid_ask?product_ids={product_id}"
    headers = _cb_headers(api_key, api_secret, "GET", path, "")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            COINBASE_BASE + path,
            headers=headers,
            timeout=10.0,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch best bid/ask: HTTP {response.status_code} — {response.text}"
        )

    data = response.json()
    pricebooks: list[dict] = data.get("pricebooks", [])
    if not pricebooks:
        raise RuntimeError(f"Empty pricebooks response for product: {product_id!r}")

    asks: list[dict] = pricebooks[0].get("asks", [])
    if not asks:
        raise RuntimeError(f"No asks in pricebook for product: {product_id!r}")

    return float(asks[0]["price"])


async def place_order(
    symbol: str,
    side: str,
    size_mode: str,
    size_value: float,
    api_key: str,
    api_secret: str,
) -> dict:
    """Place a market order on Coinbase Advanced Trade.

    Args:
        symbol:     Trading pair in concatenated form, e.g. ``"BTCUSDT"``.
        side:       ``"BUY"`` or ``"SELL"``.
        size_mode:  ``"fixed_usdt"`` — treat *size_value* as a USDT amount;
                    ``"pct_balance"`` — treat *size_value* as a percentage of
                    the relevant wallet balance.
        size_value: Numeric quantity whose interpretation depends on *size_mode*.
        api_key:    Coinbase Cloud developer API key.
        api_secret: Coinbase Cloud developer API secret.

    Returns:
        Parsed JSON response from the Coinbase create-order endpoint.

    Raises:
        RuntimeError: On any API error or unexpected response.
    """
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise RuntimeError(f"Invalid side: {side!r}. Must be 'BUY' or 'SELL'.")
    if size_mode not in ("fixed_usdt", "pct_balance"):
        raise RuntimeError(
            f"Invalid size_mode: {size_mode!r}. Must be 'fixed_usdt' or 'pct_balance'."
        )

    product_id = _cb_pair(symbol)

    # Determine order size field and value
    if side == "BUY":
        if size_mode == "fixed_usdt":
            quote_size = str(size_value)
        else:  # pct_balance
            # Determine quote currency from product_id (part after the dash)
            quote_currency = product_id.split("-")[1]
            usdt_balance = await _get_balance(api_key, api_secret, quote_currency)
            quote_size = str(usdt_balance * size_value / 100.0)

        order_config: dict = {"market_market_ioc": {"quote_size": quote_size}}

    else:  # SELL
        base_currency = product_id.split("-")[0]

        if size_mode == "fixed_usdt":
            price = await _get_price(api_key, api_secret, product_id)
            qty = size_value / price
            base_size = str(qty)
        else:  # pct_balance
            base_balance = await _get_balance(api_key, api_secret, base_currency)
            base_size = str(base_balance * size_value / 100.0)

        order_config = {"market_market_ioc": {"base_size": base_size}}

    import json

    body: dict = {
        "client_order_id": str(uuid.uuid4()),
        "product_id": product_id,
        "side": side,
        "order_configuration": order_config,
    }
    body_str = json.dumps(body)

    path = "/api/v3/brokerage/orders"
    headers = _cb_headers(api_key, api_secret, "POST", path, body_str)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            COINBASE_BASE + path,
            headers=headers,
            content=body_str,
            timeout=10.0,
        )

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Order placement failed: HTTP {response.status_code} — {response.text}"
        )

    result: dict = response.json()

    # Coinbase wraps errors in a success-status response; surface them clearly
    success = result.get("success", True)
    if not success:
        reason = result.get("error_response", {}).get("message", str(result))
        raise RuntimeError(f"Order rejected by Coinbase: {reason}")

    return result
