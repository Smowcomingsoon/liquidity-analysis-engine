"""
Read-only exchange account balances — MEXC and KuCoin.

IMPORTANT: this module must only ever call the read-only account/balance
endpoint of each exchange (GET /api/v3/account on MEXC, GET
/api/v1/accounts on KuCoin). No order, trade, transfer, or withdrawal
endpoint is implemented or should ever be added here — this app does not
and will not execute trades. See PROMPT_SPEC.md / project non-negotiables.
"""

import base64
import hashlib
import hmac
import time


from app import http_client

from app.config import settings

MEXC_BASE_URL = "https://api.mexc.com"
KUCOIN_BASE_URL = "https://api.kucoin.com"


class BalanceFetchError(Exception):
    pass


async def fetch_mexc_balances() -> list[dict]:
    if not settings.mexc_api_key or not settings.mexc_api_secret:
        return []
    ts = int(time.time() * 1000)
    query = f"timestamp={ts}&recvWindow=10000"
    signature = hmac.new(
        settings.mexc_api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    url = f"{MEXC_BASE_URL}/api/v3/account?{query}&signature={signature}"
    async with http_client.pooled(timeout=15.0) as client:
        resp = await client.get(url, headers={"X-MEXC-APIKEY": settings.mexc_api_key})
    try:
        data = resp.json()
    except ValueError as exc:
        raise BalanceFetchError("Invalid response from MEXC") from exc
    if resp.status_code != 200:
        raise BalanceFetchError(data.get("msg") or f"MEXC error ({resp.status_code})")

    out = []
    for b in data.get("balances", []):
        free = float(b.get("free", 0))
        locked = float(b.get("locked", 0))
        if free + locked > 0:
            out.append({"asset": b["asset"], "free": free, "locked": locked, "total": free + locked})
    out.sort(key=lambda b: b["total"], reverse=True)
    return out


async def fetch_kucoin_balances() -> list[dict]:
    if not settings.kucoin_api_key or not settings.kucoin_api_secret or not settings.kucoin_api_passphrase:
        return []
    ts = str(int(time.time() * 1000))
    method = "GET"
    endpoint = "/api/v1/accounts"
    str_to_sign = f"{ts}{method}{endpoint}"
    secret_bytes = settings.kucoin_api_secret.encode("utf-8")
    signature = base64.b64encode(
        hmac.new(secret_bytes, str_to_sign.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    passphrase_signed = base64.b64encode(
        hmac.new(secret_bytes, settings.kucoin_api_passphrase.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    headers = {
        "KC-API-KEY": settings.kucoin_api_key,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": passphrase_signed,
        "KC-API-KEY-VERSION": "2",
    }
    async with http_client.pooled(timeout=15.0) as client:
        resp = await client.get(f"{KUCOIN_BASE_URL}{endpoint}", headers=headers)
    try:
        data = resp.json()
    except ValueError as exc:
        raise BalanceFetchError("Invalid response from KuCoin") from exc
    if data.get("code") != "200000":
        raise BalanceFetchError(data.get("msg") or "KuCoin error")

    totals: dict[str, float] = {}
    for a in data.get("data", []):
        balance = float(a.get("balance", 0))
        if balance > 0:
            totals[a["currency"]] = totals.get(a["currency"], 0) + balance
    out = [{"asset": asset, "free": total, "locked": 0.0, "total": total} for asset, total in totals.items()]
    out.sort(key=lambda b: b["total"], reverse=True)
    return out
