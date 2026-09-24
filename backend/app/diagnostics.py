"""Sezione Diagnosi: test di collegamento e saldo per un exchange, con le
stesse chiavi API che l'utente ha gia' salvato — nessuna nuova credenziale,
nessun nuovo formato di cifratura.

File isolato apposta, come richiesto esplicitamente: se qualcosa qui si
rompe (un exchange cambia la sua API, una firma smette di funzionare), la
riparazione resta dentro questo file, senza toccare esecuzione ordini,
backtest o il resto dell'app. Le uniche dipendenze sono i moduli di
esecuzione gia' esistenti (per riusare le loro firme HMAC, non per
duplicarle) e il registro futures.
"""

from __future__ import annotations

import httpx

from app.trading import binance_exec, bitstamp_exec, coinbase_exec, kraken_exec, kucoin_exec
from app.trading.futures import registry as futures_registry
from app.trading.futures.base import FuturesCreds

# Le stesse sei borse spot gia' collegate altrove nell'app — non un elenco
# nuovo, solo il sottoinsieme di trading.executor.SUPPORTED_EXCHANGES per
# cui esiste (qui sotto) un modo di leggere il saldo.
SUPPORTED_SPOT_EXCHANGES = {"mexc", "binance", "kucoin", "kraken", "coinbase", "bitstamp"}


class DiagnosticsError(RuntimeError):
    """Un test di connessione fallito — il messaggio e' gia' pronto per l'utente."""


async def test_spot_connection(
    exchange: str, api_key: str, api_secret: str, passphrase: str = ""
) -> list[dict]:
    """Prova a leggere il saldo con le chiavi date. Solleva DiagnosticsError
    con un messaggio leggibile se le chiavi non funzionano; altrimenti
    restituisce [{"asset": str, "free": float}, ...] (solo saldi > 0)."""
    if not api_key or not api_secret:
        raise DiagnosticsError("API key or secret missing")
    try:
        if exchange in ("mexc", "binance"):
            return await _mexc_binance_balances(exchange, api_key, api_secret)
        if exchange == "kucoin":
            if not passphrase:
                raise DiagnosticsError("KuCoin also requires the passphrase")
            return await _kucoin_balances(api_key, api_secret, passphrase)
        if exchange == "kraken":
            raw = await kraken_exec.get_all_balances(api_key, api_secret)
            return [{"asset": k, "free": v} for k, v in raw.items()]
        if exchange == "coinbase":
            return await coinbase_exec.get_all_balances(api_key, api_secret)
        if exchange == "bitstamp":
            return await _bitstamp_balance(api_key, api_secret)
    except DiagnosticsError:
        raise
    except Exception as exc:  # noqa: BLE001 — qualunque errore diventa un messaggio per l'utente
        raise DiagnosticsError(str(exc)) from exc
    raise DiagnosticsError(f"Exchange not supported by Diagnostics: {exchange}")


async def _mexc_binance_balances(exchange: str, api_key: str, api_secret: str) -> list[dict]:
    base_url = binance_exec.BINANCE_BASE if exchange == "binance" else binance_exec.MEXC_BASE
    headers = {binance_exec._API_KEY_HEADER[exchange]: api_key}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=10.0) as client:
        qs = binance_exec._sign(api_secret, {})
        resp = await client.get(f"/api/v3/account?{qs}")
    if resp.status_code >= 400:
        raise DiagnosticsError(str(binance_exec._explain(exchange, "account", resp.text)))
    balances = resp.json().get("balances", [])
    out = [{"asset": b["asset"], "free": float(b["free"])} for b in balances if float(b["free"]) > 0]
    out.sort(key=lambda b: -b["free"])
    return out


async def _kucoin_balances(api_key: str, api_secret: str, passphrase: str) -> list[dict]:
    path = "/api/v1/accounts"
    headers = kucoin_exec._kucoin_headers(api_key, api_secret, passphrase, "GET", path, "")
    async with httpx.AsyncClient(base_url=kucoin_exec.KUCOIN_BASE, timeout=10.0) as client:
        resp = await client.get(path, headers=headers)
    body = resp.json()
    if body.get("code") != "200000":
        raise DiagnosticsError(f"KuCoin: {body.get('msg', body)}")
    totals: dict[str, float] = {}
    for acc in body.get("data", []):
        bal = float(acc.get("available", 0) or 0)
        if bal > 0:
            totals[acc["currency"]] = totals.get(acc["currency"], 0.0) + bal
    out = [{"asset": k, "free": v} for k, v in totals.items()]
    out.sort(key=lambda b: -b["free"])
    return out


async def _bitstamp_balance(api_key: str, api_secret: str) -> list[dict]:
    # Bitstamp non ha un endpoint "tutti i saldi" semplice come gli altri:
    # /balance/<pair>/ e' per coppia. BTC/USD e' il riferimento piu' comune
    # per verificare che la chiave funzioni e vedere un numero vero.
    async with httpx.AsyncClient(timeout=10.0) as client:
        data = await bitstamp_exec._get_balance(client, "btcusdt", api_key, api_secret)
    out = []
    for asset in ("usdt", "btc"):
        val = float(data.get(f"{asset}_available", 0) or 0)
        if val > 0:
            out.append({"asset": asset.upper(), "free": val})
    return out


async def test_futures_connection(exchange: str, api_key: str, api_secret: str, passphrase: str = "") -> float:
    """Restituisce il margine USDT disponibile, o solleva DiagnosticsError."""
    if not api_key or not api_secret:
        raise DiagnosticsError("API key or secret missing")
    if exchange not in futures_registry.SUPPORTED_FUTURES_EXCHANGES:
        raise DiagnosticsError(f"{exchange} has no registered futures adapter")
    adapter = futures_registry.get(exchange)
    if adapter.needs_passphrase and not passphrase:
        raise DiagnosticsError(f"{adapter.name} also requires the passphrase")
    creds = FuturesCreds(api_key=api_key, api_secret=api_secret, passphrase=passphrase)
    try:
        return await adapter.available_margin(creds)
    except Exception as exc:  # noqa: BLE001
        raise DiagnosticsError(str(exc)) from exc
