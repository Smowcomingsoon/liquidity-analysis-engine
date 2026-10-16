"""Riconciliazione periodica + recovery da crash — dal piano: "ogni riga
submitted senza conferma si verifica via RPC con la sua firma (mai un
nuovo tentativo sulla stessa chiave) prima che qualunque nuovo segnale
possa eseguire" e "un job confronta il saldo vero via RPC con
onchain_positions; se non corrispondono, non corregge in silenzio —
blocca nuovi ordini [...] e apre una discrepanza per revisione."

Questo modulo NON firma e NON invia mai nulla: solo lettura via RPC.

LIMITE NOTO: reconcile_pending_orders() books the fill expected at signing
time (quote out amount), once the chain confirms the transaction — non
ricostruisce size/prezzo davvero eseguiti. Farlo richiederebbe leggere i log della
transazione (per Solana le innerInstructions/variazioni di saldo token
via getTransaction, per EVM i topic Transfer nella receipt): un parser
per-token che questo repo non ha ancora. Rows saved without an expected
fill (older code) only leave the in-flight list (see
ledger.confirm_from_chain).
"""
import time

import httpx  # noqa: F401  (tests patch this module's httpx.AsyncClient)

from app import http_client

from app.trading.onchain import execution, ledger

SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

# Soglia di scostamento saldo-vero-vs-ledger sopra la quale si apre una
# discrepanza da rivedere a mano (dal piano: "se non corrispondono").
DISCREPANCY_THRESHOLD_PCT = 5.0

# Un ordine 'submitted' la cui firma l'RPC non trova ancora dopo cosi'
# tanto tempo si considera fallita (nodo indietro vs. transazione mai
# arrivata sono indistinguibili da qui, ma aspettare all'infinito
# bloccherebbe pending_unconfirmed() per sempre).
STALE_SUBMITTED_S = 600.0


async def _solana_signature_status(signature: str) -> str:
    """'confirmed' | 'failed' | 'pending', via getSignatureStatuses —
    nessuna nuova transazione, solo lettura dello stato di una firma gia'
    inviata in una sessione precedente."""
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
        "params": [[signature], {"searchTransactionHistory": True}],
    }
    async with http_client.pooled(timeout=15.0) as client:
        resp = await client.post(SOLANA_RPC_URL, json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC Solana (getSignatureStatuses): {data['error']}")
    values = data.get("result", {}).get("value", [None])
    result = values[0] if values else None
    if result is None:
        return "pending"
    if result.get("err") is not None:
        return "failed"
    # "processed" can still be dropped by a fork: only confirmed/finalized count.
    return "confirmed" if result.get("confirmationStatus") in ("confirmed", "finalized") else "pending"


async def _evm_receipt_status(tx_hash: str) -> str:
    """'confirmed' | 'failed' | 'pending', via eth_getTransactionReceipt —
    stessa idea per Robinhood Chain (EVM): status 0x1 = successo, 0x0 =
    reverted, nessun risultato = non ancora minata (o mai arrivata)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]}
    async with http_client.pooled(timeout=15.0) as client:
        resp = await client.post(execution.ROBINHOOD_RPC, json=payload)
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC Robinhood Chain (eth_getTransactionReceipt): {data['error']}")
    receipt = data.get("result")
    if receipt is None:
        return "pending"
    return "confirmed" if receipt.get("status") == "0x1" else "failed"


async def reconcile_pending_orders(owner_id: str, chain: str) -> dict:
    """Da chiamare periodicamente E al riavvio del processo, PRIMA di
    lasciare che un nuovo segnale esegua su questo owner_id+chain — vedi
    ledger.pending_unconfirmed(), che questa funzione consuma."""
    pending = [o for o in ledger.pending_unconfirmed(owner_id) if o["chain"] == chain]
    confirmed, failed, still_pending = [], [], []
    for order in pending:
        key = order["idempotency_key"]
        signature = order["tx_signature"]
        if not signature:
            # No signature is saved before signing ends, and the signature is
            # saved before any send: an old row without one never left.
            if time.time() - order["updated_at"] > STALE_SUBMITTED_S:
                ledger.mark_failed(key, "never signed (no signature saved)")
                failed.append(key)
            else:
                still_pending.append(key)
            continue
        if signature.startswith("jito:"):
            # A bundle id from older rows, not a transaction signature:
            # nothing to look up, it only leaves the in-flight list.
            ledger.mark_confirmed_signature_only(key)
            confirmed.append(key)
            continue
        try:
            status = await (
                _solana_signature_status(signature) if chain == "solana" else _evm_receipt_status(signature)
            )
        except Exception:  # noqa: BLE001 - RPC irraggiungibile: riprova al prossimo giro, non e' un fallimento dell'ordine
            still_pending.append(key)
            continue
        if status == "confirmed":
            ledger.confirm_from_chain(key)
            confirmed.append(key)
        elif status == "failed":
            ledger.mark_failed(key, "transaction failed on-chain (verified via RPC)")
            failed.append(key)
        else:
            age_s = time.time() - order["updated_at"]
            if age_s > STALE_SUBMITTED_S:
                ledger.mark_failed(key, f"no on-chain trace after {age_s:.0f}s — treated as failed")
                failed.append(key)
            else:
                still_pending.append(key)
    return {"confirmed": confirmed, "failed": failed, "still_pending": still_pending}


async def reconcile_all() -> dict:
    """Every owner and chain with an order in flight — run by a loop in
    main.py and at startup (audit 29/09, S04: nothing called the reconciler)."""
    pairs = {(o["owner_id"], o["chain"]) for o in ledger.pending_unconfirmed()}
    out = {}
    for owner_id, chain in pairs:
        if chain in ("solana", "robinhood"):
            out[f"{owner_id}:{chain}"] = await reconcile_pending_orders(owner_id, chain)
    return out


def check_position_discrepancy(owner_id: str, chain: str, token: str, real_balance_usd: float) -> dict:
    """Confronta cio' che il ledger pensa di aver investito
    (get_position()['cost_usd']) con real_balance_usd — il saldo vero in
    USD, che CHI CHIAMA deve aver gia' calcolato via RPC.

    DIPENDENZA MANCANTE, dichiarata non indovinata: calcolare
    real_balance_usd per un token qualunque richiede un price feed
    per-token che questo repo non ha ancora (onchain_prices.py copre solo
    il gas nativo SOL/ETH, non token SPL/ERC20 generici). Per questo la
    funzione lo accetta come parametro invece di fingere di saperlo
    calcolare da sola.

    Se lo scostamento supera DISCREPANCY_THRESHOLD_PCT, apre una riga in
    ledger.discrepancies e da quel momento risk.check_discrepancy() blocca
    nuovi ordini su owner_id+chain+token finche' qualcuno non revisiona a
    mano con ledger.review_discrepancy()."""
    position = ledger.get_position(owner_id, chain, token)
    ledger_usd = position["cost_usd"]
    if ledger_usd <= 0 and real_balance_usd <= 0:
        diff_pct = 0.0
    elif ledger_usd <= 0:
        diff_pct = 100.0
    else:
        diff_pct = abs(real_balance_usd - ledger_usd) / ledger_usd * 100
    discrepancy_id = None
    if diff_pct > DISCREPANCY_THRESHOLD_PCT:
        discrepancy_id = ledger.open_discrepancy(owner_id, chain, token, ledger_usd, real_balance_usd, diff_pct)
    return {
        "ledger_usd": ledger_usd, "real_balance_usd": real_balance_usd, "diff_pct": diff_pct,
        "discrepancy_id": discrepancy_id, "blocked": discrepancy_id is not None,
    }
