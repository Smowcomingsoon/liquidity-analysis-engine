"""Swaps the user signs with their own wallet (wallet_link.py): we prepare,
they sign and send, we never hold a key.

Second step of the plan agreed with Lorenzo for "far collegare il wallet a
qualsiasi persona": after linking, buying and selling from that wallet.

- Robinhood Chain (MetaMask, Rabby): pay with USDG or USDC through a
  Uniswap v3 pool; the answer is the list of transactions to send in order
  (an ERC-20 approve of SwapRouter02 when the allowance is short, then
  exactInputSingle), each {to, data, value}. Sells pay out in USDG.
- Solana (Phantom): Jupiter builds the transaction for the user's own
  public key; the answer is that unsigned transaction, base64.

Every answer carries the live quote and the minimum out, so the page shows
what the wallet is about to sign before it asks.
"""
from __future__ import annotations

import base64


from app import http_client

from app.trading.onchain import execution, quotes, token_metadata

ROBINHOOD_CHAIN_ID = 4663
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_SOLANA = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DEFAULT_SLIPPAGE_BPS = 300          # memes move; the page lets the user see the floor
MAX_SLIPPAGE_BPS = 2000


class SwapError(ValueError):
    pass


async def _rpc(method: str, params: list) -> str:
    return await execution._robinhood_rpc(method, params)


async def _erc20_balance(token: str, owner: str) -> int:
    data = "0x70a08231" + owner[2:].lower().zfill(64)
    return int(await _rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


async def _allowance(token: str, owner: str, spender: str) -> int:
    data = "0xdd62ed3e" + owner[2:].lower().zfill(64) + spender[2:].lower().zfill(64)
    return int(await _rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def approve_data(spender: str, amount: int) -> str:
    return "0x095ea7b3" + spender[2:].lower().zfill(64) + format(amount, "064x")


async def build_evm(owner: str, side: str, token: str, amount: float | None, pct: int | None, pay: str,
                    slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """Transactions for the user's EVM wallet on Robinhood Chain."""
    if side == "buy":
        token_in, token_out = pay, token
        dec_in = await token_metadata.get_token_decimals("robinhood", token_in)
        if dec_in is None or not amount or amount <= 0:
            raise SwapError("Enter an amount to spend.")
        amount_in = int(amount * 10 ** dec_in)
        have = await _erc20_balance(token_in, owner)
        if amount_in > have:
            raise SwapError(f"Your wallet holds {have / 10 ** dec_in:g} of it.")
    else:
        token_in, token_out = token, USDG
        have = await _erc20_balance(token_in, owner)
        amount_in = have * max(1, min(100, pct or 100)) // 100
        if amount_in <= 0:
            raise SwapError("Your wallet holds none of this token.")
    found = await quotes.find_pool_and_fee(token_in, token_out)
    if found is None:
        raise SwapError("No direct Uniswap v3 pool for this pair: try the other currency.")
    _pool, fee = found
    q = await quotes.uniswap_v3_quote(token_in, token_out, fee, amount_in)
    min_out = int(q["amount_out"] * (10_000 - slippage_bps) // 10_000)
    router = execution.ROBINHOOD_SWAP_ROUTER02
    txs = []
    if await _allowance(token_in, owner, router) < amount_in:
        txs.append({"to": token_in, "data": approve_data(router, amount_in), "value": "0x0", "label": "Allow the swap"})
    calldata = execution.build_swap_router02_calldata(token_in, token_out, fee, owner, amount_in, min_out)
    txs.append({"to": router, "data": "0x" + calldata.hex(), "value": "0x0", "label": "Swap"})
    dec_out = await token_metadata.get_token_decimals("robinhood", token_out)
    return {"kind": "evm", "chain_id": ROBINHOOD_CHAIN_ID, "txs": txs, "token_in": token_in, "token_out": token_out,
            "amount_in": amount_in, "out": q["amount_out"], "min_out": min_out, "decimals_out": dec_out,
            "impact": q.get("price_impact_pct"), "fee": fee}


async def build_solana(owner: str, side: str, token: str, amount: float | None, pct: int | None, pay: str,
                       slippage_bps: int = DEFAULT_SLIPPAGE_BPS) -> dict:
    """An unsigned Jupiter transaction for the user's Phantom wallet."""
    from app.trading.onchain import balances
    if side == "buy":
        mint_in, mint_out = pay, token
        dec_in = 9 if pay == SOL_MINT else await token_metadata.get_token_decimals("solana", pay)
        if dec_in is None or not amount or amount <= 0:
            raise SwapError("Enter an amount to spend.")
        amount_in = int(amount * 10 ** dec_in)
    else:
        mint_in, mint_out = token, USDC_SOLANA
        rows = await balances.all_token_balances("solana", owner)
        held = next((r for r in rows if r.get("address") == token), None)
        dec = await token_metadata.get_token_decimals("solana", token)
        if not held or dec is None:
            raise SwapError("Your wallet holds none of this token.")
        raw = held.get("amount_raw") or int(float(held.get("amount") or 0) * 10 ** dec)
        amount_in = raw * max(1, min(100, pct or 100)) // 100
    q = await quotes.jupiter_quote(mint_in, mint_out, amount_in, slippage_bps)
    async with http_client.pooled(timeout=15.0) as client:
        resp = await client.post(execution.JUPITER_SWAP_URL, json={
            "quoteResponse": q["raw"], "userPublicKey": owner, "dynamicComputeUnitLimit": True,
        })
    if resp.status_code != 200 or "swapTransaction" not in (resp.json() or {}):
        raise SwapError(f"Jupiter could not build the swap ({resp.status_code}).")
    tx = resp.json()["swapTransaction"]
    base64.b64decode(tx)          # a malformed answer fails here, not in the wallet
    dec_out = 9 if mint_out == SOL_MINT else await token_metadata.get_token_decimals("solana", mint_out)
    return {"kind": "solana", "tx_base64": tx, "token_in": mint_in, "token_out": mint_out, "amount_in": amount_in,
            "decimals_out": dec_out,
            "out": q["out_amount"], "min_out": int(q["raw"].get("otherAmountThreshold") or 0),
            "impact": q.get("price_impact_pct")}
