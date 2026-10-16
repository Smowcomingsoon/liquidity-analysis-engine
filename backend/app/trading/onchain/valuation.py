"""What an order is worth, decided by the server (audit 29/09, S02).

The per-trade and daily caps, the ledger and the expected fill used to take
notional_usd and price_usd from the browser: a request could declare $10 for
a swap of $10,000. Here the value comes from the raw amounts that are
actually signed, the token's real decimals and a price this server reads
(native: GeckoTerminal via prices.py; stablecoins: 1; others: DexScreener).
No price means no order, never a guess."""
from __future__ import annotations

import math


from app import http_client

from app.trading.onchain import portfolio, prices, token_metadata

STABLES = {
    "solana": {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},              # USDC
    "robinhood": {"0x5fc5360d0400a0fd4f2af552add042d716f1d168"},             # USDG
}
_NATIVE_DECIMALS = {"solana": 9, "robinhood": 18}


def _norm(chain: str, address: str) -> str:
    return address if chain == "solana" else address.lower()


def is_native(chain: str, address: str | None) -> bool:
    return address is None or _norm(chain, address) == _norm(chain, prices.NATIVE_TOKEN_ADDRESS[chain])


def _ok(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


async def token_price_usd(chain: str, address: str | None) -> float | None:
    if is_native(chain, address):
        return _ok(await prices.native_price_usd(chain))
    if _norm(chain, address) in STABLES.get(chain, set()):
        return 1.0
    async with http_client.pooled(timeout=10.0) as client:
        found = await portfolio.dex_prices(chain, [address], client)
    return _ok((found.get(_norm(chain, address)) or {}).get("price"))


async def token_decimals(chain: str, address: str | None) -> int | None:
    if address is None:
        return _NATIVE_DECIMALS[chain]
    return await token_metadata.get_token_decimals(chain, address)


async def swap_value(
    chain: str, token_in: str, amount_in_raw: int, token_out: str, amount_out_raw: int, side: str,
) -> dict | None:
    """{notional_usd, price_usd, qty} for a swap of amount_in_raw token_in
    quoted to amount_out_raw token_out. The traded token is token_out on a
    buy and token_in on a sell; price_usd is its price implied by the swap."""
    d_in, d_out = await token_decimals(chain, token_in), await token_decimals(chain, token_out)
    if d_in is None or d_out is None or amount_in_raw <= 0 or amount_out_raw <= 0:
        return None
    units_in, units_out = amount_in_raw / 10 ** d_in, amount_out_raw / 10 ** d_out
    p_in = await token_price_usd(chain, token_in)
    if p_in is not None:
        notional = units_in * p_in
    else:
        p_out = await token_price_usd(chain, token_out)
        if p_out is None:
            return None
        notional = units_out * p_out
    qty = units_out if side == "buy" else units_in
    return {"notional_usd": notional, "price_usd": notional / qty, "qty": qty}
