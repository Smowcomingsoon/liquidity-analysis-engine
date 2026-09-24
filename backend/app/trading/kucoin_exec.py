import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx

KUCOIN_BASE = "https://api.kucoin.com"


def _base_asset(symbol: str) -> str:
    """BTC-USDT → BTC"""
    return symbol.split("-")[0]


def _kucoin_headers(
    api_key: str,
    api_secret: str,
    passphrase: str,
    method: str,
    path: str,
    body: str,
) -> dict:
    timestamp = str(int(time.time() * 1000))
    str_to_sign = timestamp + method.upper() + path + body
    sig = base64.b64encode(
        hmac.new(api_secret.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    enc_passphrase = base64.b64encode(
        hmac.new(api_secret.encode(), passphrase.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY": api_key,
        "KC-API-SIGN": sig,
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": enc_passphrase,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }


async def place_order(
    symbol: str,
    side: str,
    size_mode: str,
    size_value: float,
    api_key: str,
    api_secret: str,
    passphrase: str,
    max_usdt: float | None = None,
) -> dict:
    base = _base_asset(symbol)

    async with httpx.AsyncClient(base_url=KUCOIN_BASE) as client:
        if size_mode == "pct_balance":
            if side == "buy":
                path = "/api/v1/accounts?type=trade&currency=USDT"
                headers = _kucoin_headers(api_key, api_secret, passphrase, "GET", path, "")
                resp = await client.get(path, headers=headers)
                if resp.status_code >= 400:
                    raise RuntimeError(f"kucoin account error: {resp.text}")
                accounts = resp.json().get("data", [])
                free = float(accounts[0]["available"]) if accounts else 0.0
                funds_or_size = free * size_value / 100
            else:
                path = f"/api/v1/accounts?type=trade&currency={base}"
                headers = _kucoin_headers(api_key, api_secret, passphrase, "GET", path, "")
                resp = await client.get(path, headers=headers)
                if resp.status_code >= 400:
                    raise RuntimeError(f"kucoin account error: {resp.text}")
                accounts = resp.json().get("data", [])
                free = float(accounts[0]["available"]) if accounts else 0.0
                funds_or_size = free * size_value / 100
        else:
            funds_or_size = size_value

        # Tetto per singolo ingresso — solo lato buy (funds, in USDT), mai
        # lato sell (size, in moneta base): vedi place_order_for_signal.
        if side == "buy" and max_usdt:
            funds_or_size = min(funds_or_size, max_usdt)

        if funds_or_size <= 0:
            raise RuntimeError("Computed quantity is zero or negative — insufficient balance")

        order_path = "/api/v1/orders"
        payload: dict = {
            "clientOid": str(uuid.uuid4()),
            "side": side,
            "symbol": symbol,
            "type": "market",
        }
        if side == "buy":
            payload["funds"] = str(funds_or_size)
        else:
            payload["size"] = str(funds_or_size)

        body = json.dumps(payload)
        headers = _kucoin_headers(api_key, api_secret, passphrase, "POST", order_path, body)
        order_resp = await client.post(order_path, content=body, headers=headers)
        if order_resp.status_code >= 400:
            raise RuntimeError(f"kucoin order error: {order_resp.text}")
        return order_resp.json()
