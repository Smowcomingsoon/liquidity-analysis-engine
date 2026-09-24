"""Preventivi su Robinhood Chain attraverso un aggregatore, non un solo DEX.

Lorenzo, con MYCE: "sto errore non deve MAI SUCCEDERE". MYCE si scambia
solo su Pons e su pool Uniswap v4, e il percorso diretto (Uniswap v3,
quotes.find_pool_and_fee) rispondeva "nessun pool su nessun fee tier".
Tutto quello che si trova su DexScreener deve almeno avere un prezzo:
l'aggregatore conosce tutti i DEX della chain (misurato il 23/09/2026:
ETH -> MYCE passa da Uniswap v4 + Pons v2 su KyberSwap, e da Nordstern
su LI.FI).

Due fonti, provate in ordine: KyberSwap (nessuna commissione, ma a volte
risponde "service temporarily overloaded") e LI.FI (commissione fissa
0,25%). Solo preventivi: firmare la transazione che l'aggregatore
prepara e' un passo a parte, che tocca soldi veri e aspetta il via di
Lorenzo.
"""
from __future__ import annotations

import httpx

CHAIN_ID = 4663
KYBER_ROUTES_URL = "https://aggregator-api.kyberswap.com/robinhood/api/v1/routes"
LIFI_QUOTE_URL = "https://li.quest/v1/quote"
KYBER_CLIENT_ID = "liquidityengine"

# Nel sito il nativo di Robinhood Chain e' rappresentato dall'indirizzo del
# WETH (prices.NATIVE_TOKEN_ADDRESS): agli aggregatori va passato il loro
# segnaposto per l'ETH vero, altrimenti proverebbero a spendere WETH.
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
_KYBER_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
_LIFI_NATIVE = "0x0000000000000000000000000000000000000000"
# LI.FI chiede sempre un mittente, anche solo per un preventivo.
_LIFI_QUOTE_SENDER = "0x1111111111111111111111111111111111111111"


class AggregatorError(RuntimeError):
    """Nessuna fonte ha un percorso per questa coppia, o non risponde."""


def _is_native(address: str) -> bool:
    return address.lower() == ROBINHOOD_WETH.lower()


async def _kyber(token_in: str, token_out: str, amount_in: int) -> int:
    params = {
        "tokenIn": _KYBER_NATIVE if _is_native(token_in) else token_in,
        "tokenOut": _KYBER_NATIVE if _is_native(token_out) else token_out,
        "amountIn": str(amount_in),
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(KYBER_ROUTES_URL, params=params,
                                headers={"x-client-id": KYBER_CLIENT_ID, "accept": "application/json"})
    payload = resp.json()
    summary = (payload.get("data") or {}).get("routeSummary") if isinstance(payload, dict) else None
    if not summary or payload.get("code") != 0:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise AggregatorError(f"KyberSwap: {message or resp.status_code}")
    return int(summary["amountOut"])


async def _lifi(token_in: str, token_out: str, amount_in: int) -> int:
    params = {
        "fromChain": CHAIN_ID, "toChain": CHAIN_ID,
        "fromToken": _LIFI_NATIVE if _is_native(token_in) else token_in,
        "toToken": _LIFI_NATIVE if _is_native(token_out) else token_out,
        "fromAmount": str(amount_in), "fromAddress": _LIFI_QUOTE_SENDER,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(LIFI_QUOTE_URL, params=params, headers={"accept": "application/json"})
    payload = resp.json()
    estimate = payload.get("estimate") if isinstance(payload, dict) else None
    if resp.status_code != 200 or not estimate:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise AggregatorError(f"LI.FI: {message or resp.status_code}")
    return int(estimate["toAmount"])


async def amount_out(token_in: str, token_out: str, amount_in: int) -> tuple[int, str]:
    errors = []
    for name, source in (("KyberSwap", _kyber), ("LI.FI", _lifi)):
        try:
            out = await source(token_in, token_out, amount_in)
        except (httpx.HTTPError, ValueError, KeyError, AggregatorError) as exc:
            errors.append(str(exc))
            continue
        if out > 0:
            return out, name
        errors.append(f"{name}: no output amount")
    raise AggregatorError("; ".join(errors) or "no source")


async def quote(token_in: str, token_out: str, amount_in: int) -> dict:
    """Price impact stimato come per il v3: prezzo eseguito contro quello
    di un importo 1000 volte piu' piccolo (quasi-spot). Mai un impatto
    inventato: se il riferimento non arriva, niente preventivo."""
    out, source = await amount_out(token_in, token_out, amount_in)
    reference_in = max(amount_in // 1000, 1)
    reference_out, _ = await amount_out(token_in, token_out, reference_in)
    spot = reference_out / reference_in
    impact = max(0.0, 1 - (out / amount_in) / spot) * 100
    return {"amount_out": out, "price_impact_pct": impact, "source": source}
