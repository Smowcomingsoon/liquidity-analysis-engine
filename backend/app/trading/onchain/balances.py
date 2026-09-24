"""Saldo e storico transazioni di un wallet on-chain, sola lettura —
nessuna chiave coinvolta qui, solo l'indirizzo (che e' pubblico per
definizione)."""
import httpx

from app.models import OnchainChain
from app.trading.onchain import rpc_client

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
ROBINHOOD_RPC = "https://rpc.mainnet.chain.robinhood.com"
# L'explorer ufficiale (Blockscout) e' dietro una sfida Cloudflare che una
# richiesta da server non supera — verificato dal vivo, non solo letto:
# risponde "Just a moment..." a qualunque User-Agent, anche il path legacy
# /api?module=... in stile Etherscan. Un vero browser la risolve da solo
# (esegue il JS della sfida), un backend no — per questo lo storico
# Robinhood Chain qui sotto e' un link all'explorer, non dati incorporati.
ROBINHOOD_EXPLORER = "https://robinhoodchain.blockscout.com"
LAMPORTS_PER_SOL = 1_000_000_000
WEI_PER_ETH = 10**18
_MAX_TRANSACTIONS = 20


async def native_balance(chain: OnchainChain, address: str) -> float:
    """Saldo nella moneta nativa della chain (SOL, ETH) — non e' un saldo
    in stablecoin: e' quello che serve per pagare il gas, la prima cosa da
    controllare prima che qualunque swap automatico possa mai partire."""
    if chain == "solana":
        return await _solana_balance(address)
    if chain == "robinhood":
        return await _robinhood_balance(address)
    raise ValueError(f"Unsupported chain: {chain}")


async def _solana_balance(address: str) -> float:
    # Passa dal rpc_client (fallback multi-endpoint + circuit breaker): e'
    # la chiamata piu' frequente di tutto il sistema on-chain (saldo,
    # /admin/status, ogni apertura wallet).
    result = await rpc_client.call_rpc("solana", "getBalance", [address])
    return result["value"] / LAMPORTS_PER_SOL


async def _robinhood_balance(address: str) -> float:
    result = await rpc_client.call_rpc("robinhood", "eth_getBalance", [address, "latest"])
    return int(result, 16) / WEI_PER_ETH


SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_BALANCE_OF_SELECTOR = "70a08231"


async def all_token_balances(chain: OnchainChain, address: str, known_evm_tokens: list[str] | None = None) -> list[dict]:
    """Tutti i saldi token del wallet, non solo quelli che il ledger di
    questo sito ha tracciato — Lorenzo: "avere un wallet per capire il
    portafoglio per sapere cosa deteniamo". Su Solana e' un elenco
    completo, la RPC lo da' in un colpo. Su Robinhood Chain NON e' un
    elenco esaustivo: senza un indexer pubblico affidabile (Blockscout e'
    dietro una sfida Cloudflare, vedi sopra), si controllano solo gli
    indirizzi che questo sito conosce (il token di quota, piu' quelli
    comparsi in un ordine limite di questo wallet) — dichiarato, non
    nascosto: un token ricevuto da fuori questa app e non ancora usato
    qui non compare."""
    if chain == "solana":
        return await _solana_token_balances(address)
    if chain == "robinhood":
        return await _robinhood_known_token_balances(address, known_evm_tokens or [])
    raise ValueError(f"Unsupported chain: {chain}")


async def _solana_token_balances(address: str) -> list[dict]:
    result = await rpc_client.call_rpc(
        "solana", "getTokenAccountsByOwner",
        [address, {"programId": SPL_TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed"}],
    )
    out = []
    for item in result.get("value", []):
        info = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
        amount = float(info["uiAmountString"])
        if amount <= 0:
            continue
        out.append({"address": item["account"]["data"]["parsed"]["info"]["mint"],
                    "amount": amount, "decimals": info["decimals"]})
    return out


async def _robinhood_known_token_balances(address: str, token_addresses: list[str]) -> list[dict]:
    out = []
    padded_owner = address[2:].lower().zfill(64)
    for token in {t.lower() for t in token_addresses}:
        try:
            result = await rpc_client.call_rpc(
                "robinhood", "eth_call",
                [{"to": token, "data": f"0x{_BALANCE_OF_SELECTOR}{padded_owner}"}, "latest"],
            )
            raw = int(result, 16)
        except Exception:  # noqa: BLE001 — un token irraggiungibile si salta, non affonda il resto del portafoglio
            continue
        if raw > 0:
            out.append({"address": token, "amount_raw": raw})
    return out


async def recent_transactions(chain: OnchainChain, address: str) -> dict:
    """Le ultime transazioni per un indirizzo. Su Solana e' un dato vero
    (l'RPC pubblico lo offre nativamente); su Robinhood Chain e' un link
    all'explorer con la spiegazione del perche' — vedi il commento sopra
    ROBINHOOD_EXPLORER. Meglio dirlo chiaro che restituire una lista vuota
    che sembra "nessuna transazione" quando in realta' e' "non raggiungibile
    da qui"."""
    if chain == "solana":
        return {"available": True, "transactions": await _solana_transactions(address)}
    if chain == "robinhood":
        return {
            "available": False,
            "transactions": [],
            "explorer_url": f"{ROBINHOOD_EXPLORER}/address/{address}",
            "reason": "The Robinhood Chain explorer sits behind an anti-bot check that server requests "
                      "can't pass — open the link to see the real history.",
        }
    raise ValueError(f"Unsupported chain: {chain}")


async def _solana_transactions(address: str) -> list[dict]:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
        "params": [address, {"limit": _MAX_TRANSACTIONS}],
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(SOLANA_RPC, json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC Solana: {data['error']}")
    return [
        {
            "signature": row["signature"],
            "block_time": row.get("blockTime"),
            "status": "fallita" if row.get("err") else "riuscita",
            "explorer_url": f"https://explorer.solana.com/tx/{row['signature']}",
        }
        for row in (data.get("result") or [])
    ]
