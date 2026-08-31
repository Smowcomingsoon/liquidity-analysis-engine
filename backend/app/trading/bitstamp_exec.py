import hashlib
import hmac
import time

import httpx

BITSTAMP_BASE = "https://www.bitstamp.net"


def _bitstamp_pair(symbol: str) -> str:
    """BTCUSDT -> btcusdt"""
    return symbol.lower()


def _auth_params(api_key: str, api_secret: str) -> dict:
    nonce = str(int(time.time() * 1e6))
    message = nonce + api_key
    sig = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()
    return {"key": api_key, "signature": sig, "nonce": nonce}


async def _get_balance(client: httpx.AsyncClient, pair: str, api_key: str, api_secret: str) -> dict:
    url = f"{BITSTAMP_BASE}/api/v2/balance/{pair}/"
    params = _auth_params(api_key, api_secret)
    resp = await client.post(url, data=params)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Bitstamp balance error: {data}")
    return data


async def _get_last_price(client: httpx.AsyncClient, pair: str, api_key: str, api_secret: str) -> float:
    url = f"{BITSTAMP_BASE}/api/v2/transactions/{pair}/"
    params = _auth_params(api_key, api_secret)
    params["time"] = "minute"
    resp = await client.post(url, data=params)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise RuntimeError(f"Bitstamp: no recent transactions for {pair}")
    return float(data[0]["price"])


async def place_order(
    symbol: str,
    side: str,           # "BUY" or "SELL"
    size_mode: str,
    size_value: float,
    api_key: str,
    api_secret: str,
) -> dict:
    pair = _bitstamp_pair(symbol)

    async with httpx.AsyncClient(timeout=15.0) as client:
        if side == "BUY":
            if size_mode == "fixed_usdt":
                amount = size_value
            elif size_mode == "pct_balance":
                balance = await _get_balance(client, pair, api_key, api_secret)
                usdt_avail = float(balance.get("usdt_available", 0))
                amount = usdt_avail * size_value / 100.0
            else:
                raise ValueError(f"Unknown size_mode: {size_mode}")

            url = f"{BITSTAMP_BASE}/api/v2/buy/market/{pair}/"
            params = _auth_params(api_key, api_secret)
            params["amount"] = str(amount)
            resp = await client.post(url, data=params)
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == "error" or "error" in result:
                raise RuntimeError(f"Bitstamp buy error: {result}")
            return result

        elif side == "SELL":
            if size_mode == "pct_balance":
                balance = await _get_balance(client, pair, api_key, api_secret)
                # derive base asset from pair (e.g. "btcusdt" -> "btc")
                base = pair.replace("usdt", "").replace("usdc", "").replace("btc", "", 1) if "usdt" not in pair else pair[:-4]
                base_avail = float(balance.get(f"{base}_available", 0))
                amount = base_avail * size_value / 100.0
            elif size_mode == "fixed_usdt":
                price = await _get_last_price(client, pair, api_key, api_secret)
                if price <= 0:
                    raise RuntimeError("Bitstamp: invalid price for sell calculation")
                amount = size_value / price
            else:
                raise ValueError(f"Unknown size_mode: {size_mode}")

            url = f"{BITSTAMP_BASE}/api/v2/sell/market/{pair}/"
            params = _auth_params(api_key, api_secret)
            params["amount"] = str(amount)
            resp = await client.post(url, data=params)
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == "error" or "error" in result:
                raise RuntimeError(f"Bitstamp sell error: {result}")
            return result

        else:
            raise ValueError(f"Unknown side: {side}")
