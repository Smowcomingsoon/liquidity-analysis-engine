"""Fase 4: l'ordine vero, non simulato — stessi guardrail di simulate.py,
ma con una chiave, una rete, e (solo se acceso) un invio reale.

Entrambe le chain hanno ora un percorso di firma reale: Solana via
Jupiter (verificato dal vivo fino all'invio), Robinhood Chain via
SwapRouter02.exactInputSingle (router/QuoterV2/Factory tutti verificati
dal vivo il 19/09/2026 — vedi quotes.py/execution.py) — ma quest'ultima
NON e' mai stata provata con un invio vero (nessuna transazione reale
mandata in questa sessione, solo quote e getPool in lettura): il codice
di firma e' reale, l'esperienza dal vivo dell'invio no. Non un motivo per
bloccarla del tutto (il guardrail di settings.onchain_execution_enabled
resta comunque spento di default), ma un motivo per accenderla con
prudenza — size piccole, prima operazione osservata a mano.

settings.onchain_execution_enabled resta la sola porta che decide se
l'invio (mai la firma) parte davvero: qui e' controllato esplicitamente,
non delegato a execution.send_signed_solana_tx/send_signed_evm_tx (che di
proposito non lo controllano da soli).

Ogni chiamata genera un correlation_id (uuid4, diverso dalla idempotency_key
del ledger) loggato su OGNI riga di log di questo ordine — piano SRE punto 2.
Non e' una persistenza a 30 giorni dedicata: questo processo scrive su
stdout/stderr senza una rotazione di log configurata da nessuna parte nel
sito (verificato: nessun logging.basicConfig/RotatingFileHandler in
main.py o altrove) — chi gestisce il deploy deve avere una rotazione a
livello di systemd/docker (es. `journalctl`, `docker logs` con
`max-size`/`max-file`) se vuole i 30 giorni richiesti dal piano. Gap noto,
non silenziato: costruire qui un secondo sistema di log parallelo solo per
onchain sarebbe piu' rischio (due fonti di verita' sui log) che beneficio."""
import logging
import uuid

from app.config import settings
from app.trading.onchain import execution, jito, ledger, pool_safety, prices, risk

logger = logging.getLogger("onchain_execute")


async def _send_solana_with_jito_fallback(
    correlation_id: str, private_key_hex: str, signed_tx_bytes: bytes, notional_usd: float,
) -> str:
    """Piano MEV: instrada via Jito bundle (transazione firmata + tip in un
    bundle atomico, che riduce la finestra di front-run) e ricade
    sull'invio diretto via RPC (send_signed_solana_tx, il percorso di
    prima, mai toccato) se Jito e' spento o non risponde in modo sensato.
    Un bundle Jito non da' MAI una signature vera e propria come una
    sendTransaction — da' un bundle_id — quindi in caso di successo il
    "signature" ritornato qui e' il bundle_id, prefissato per essere
    distinguibile in log e nel ledger da una signature RPC diretta."""
    if not settings.onchain_use_jito:
        return await execution.send_signed_solana_tx(signed_tx_bytes)

    try:
        sol_price_usd = await prices.native_price_usd("solana") or 0.0
        tip_lamports = jito.compute_dynamic_tip_lamports(notional_usd, sol_price_usd)
        tip_account = jito.pick_tip_account()
        tip_tx = await execution.build_signed_solana_tip_tx(private_key_hex, tip_account, tip_lamports)
        bundle_id = await jito.send_bundle_via_jito(signed_tx_bytes, tip_tx)
        logger.info("[%s] inviato via Jito bundle, tip=%d lamports -> %s", correlation_id, tip_lamports, bundle_id,
                     extra={"correlation_id": correlation_id})
        return f"jito:{bundle_id}"
    except Exception as exc:
        logger.warning("[%s] Jito non disponibile (%s), fallback su invio RPC diretto", correlation_id, exc,
                        extra={"correlation_id": correlation_id})
        return await execution.send_signed_solana_tx(signed_tx_bytes)


async def execute_order(
    owner_id: str, chain: str, token: str, strategy_name: str, side: str,
    notional_usd: float, price_impact_pct: float, price_usd: float,
    private_key_hex: str, quote: dict,
    pool_address: str | None = None,
) -> dict:
    correlation_id = uuid.uuid4().hex
    logger.info(
        "[%s] execute_order owner=%s chain=%s token=%s side=%s notional_usd=%.2f",
        correlation_id, owner_id, chain, token, side, notional_usd,
        extra={"correlation_id": correlation_id},
    )

    # Allowlist pool + size dinamica, ricalcolate ad ogni tentativo — non
    # solo alla prima. pool_address opzionale: se manca (chain non ancora
    # coperta da DexScreener, o il chiamante non lo passa) il comportamento
    # e' quello di prima, nessuna riduzione e nessun controllo di allowlist.
    pool_info = None
    pool_liquidity_usd = None
    if pool_address is not None:
        snapshot = await pool_safety.fetch_pool_snapshot(chain, pool_address)
        pool_liquidity_usd = snapshot["liquidity_usd"] if snapshot else None
        if pool_liquidity_usd is not None:
            ledger.record_pool_liquidity(chain, pool_address, pool_liquidity_usd)
        reference_max = ledger.max_pool_liquidity_since(chain, pool_address)
        pool_info = {
            "liquidity_usd": pool_liquidity_usd,
            "created_at": snapshot["created_at"] if snapshot else None,
            "reference_max_liquidity_usd": reference_max,
        }

    notional_usd = risk.clamp_size_to_pool(notional_usd, pool_liquidity_usd)

    ok, reason = risk.run_all_checks(
        owner_id, chain, token, notional_usd, price_impact_pct, pool_info=pool_info, strategy_name=strategy_name,
    )
    if not ok:
        logger.warning("[%s] rifiutato dal risk guardrail: %s", correlation_id, reason,
                        extra={"correlation_id": correlation_id})
        return {"accepted": False, "reason": reason, "idempotency_key": None, "tx_signature": None,
                "correlation_id": correlation_id}

    if chain not in ("solana", "robinhood"):
        return {"accepted": False, "reason": f"unsupported chain: {chain}",
                "idempotency_key": None, "tx_signature": None, "correlation_id": correlation_id}

    idempotency_key = f"live-{uuid.uuid4().hex}"
    if not ledger.reserve(idempotency_key, owner_id, chain, token, strategy_name, side, "live", notional_usd):
        logger.warning("[%s] idempotency_key duplicata: %s", correlation_id, idempotency_key,
                        extra={"correlation_id": correlation_id})
        return {"accepted": False, "reason": "order already in progress (duplicate idempotency key)",
                "idempotency_key": None, "tx_signature": None, "correlation_id": correlation_id}

    try:
        if chain == "solana":
            signed = await execution.build_signed_solana_swap(private_key_hex, quote)
        else:
            signed = await execution.build_signed_robinhood_swap(
                private_key_hex, quote["token_in"], quote["token_out"], quote["fee"],
                quote["amount_in"], quote["amount_out_minimum"],
            )
    except Exception as exc:
        logger.error("[%s] firma fallita: %s", correlation_id, exc, extra={"correlation_id": correlation_id})
        ledger.mark_failed(idempotency_key, f"signing failed: {exc}")
        return {"accepted": False, "reason": f"signing failed: {exc}",
                "idempotency_key": idempotency_key, "tx_signature": None, "correlation_id": correlation_id}

    if not settings.onchain_execution_enabled:
        logger.info("[%s] esecuzione reale disattivata, ordine non inviato", correlation_id,
                     extra={"correlation_id": correlation_id})
        ledger.mark_failed(idempotency_key, "esecuzione reale disattivata (onchain_execution_enabled=False)")
        return {"accepted": False, "reason": "live execution disabled on this server",
                "idempotency_key": idempotency_key, "tx_signature": None, "correlation_id": correlation_id}

    try:
        if chain == "solana":
            signature = await _send_solana_with_jito_fallback(correlation_id, private_key_hex, signed, notional_usd)
        else:
            signature = await execution.send_signed_evm_tx(signed)
    except Exception as exc:
        logger.error("[%s] invio fallito: %s", correlation_id, exc, extra={"correlation_id": correlation_id})
        ledger.mark_failed(idempotency_key, f"send failed: {exc}")
        return {"accepted": False, "reason": f"send failed: {exc}",
                "idempotency_key": idempotency_key, "tx_signature": None, "correlation_id": correlation_id}

    ledger.mark_submitted(idempotency_key, signature)
    ledger.mark_confirmed(
        idempotency_key,
        executed_qty=notional_usd / price_usd,
        executed_price_usd=price_usd,
        price_impact_pct=price_impact_pct,
    )
    logger.info("[%s] confermato tx=%s", correlation_id, signature, extra={"correlation_id": correlation_id})
    return {"accepted": True, "reason": "", "idempotency_key": idempotency_key, "tx_signature": signature,
            "correlation_id": correlation_id}
