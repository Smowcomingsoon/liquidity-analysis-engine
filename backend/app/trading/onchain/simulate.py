"""Fase 2 del piano: paper trading. Stessi controlli di rischio e stesso
ledger della Fase 3, ma mode='simulated' — mai una chiave, mai una firma,
mai una transazione. Serve a vedere se il sistema (guardrail, size,
concentrazione) si comporta bene su dati di mercato reali prima di
rischiare fondi veri.

correlation_id (uuid4, distinto dalla idempotency_key) su ogni riga di log,
stesso pattern di execute.py — vedi il suo docstring per il gap noto sulla
rotazione dei log."""
import logging
import uuid

from app.trading.onchain import ledger, pool_safety, risk

logger = logging.getLogger("onchain_simulate")


async def simulate_order(
    owner_id: str, chain: str, token: str, strategy_name: str, side: str,
    notional_usd: float, price_impact_pct: float, price_usd: float,
    pool_address: str | None = None,
) -> dict:
    """Ritorna sempre un esito (mai un'eccezione per un rifiuto normale):
    {'accepted': bool, 'reason': str, 'idempotency_key': str|None, 'correlation_id': str}.

    pool_address e' opzionale: quando c'e', si legge un preventivo reale di
    liquidita'/eta' del pool (DexScreener) e lo si passa sia alla size
    dinamica sia all'allowlist di run_all_checks — ricalcolato ad ogni
    chiamata, non solo alla prima. Quando manca (chain non ancora coperta,
    o il chiamante non lo passa) il comportamento resta quello di prima:
    nessuna size dinamica, nessun controllo di allowlist pool."""
    correlation_id = uuid.uuid4().hex
    logger.info(
        "[%s] simulate_order owner=%s chain=%s token=%s side=%s notional_usd=%.2f",
        correlation_id, owner_id, chain, token, side, notional_usd,
        extra={"correlation_id": correlation_id},
    )

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
        return {"accepted": False, "reason": reason, "idempotency_key": None, "correlation_id": correlation_id}

    # Chiave diversa ad ogni chiamata apposta: una simulazione non ha un
    # segnale reale da cui derivare una chiave deterministica come Fase 3
    # avra' (qui e' l'utente/API a chiedere "provalo ora", non uno
    # scheduler che potrebbe ripetersi).
    idempotency_key = f"sim-{uuid.uuid4().hex}"
    reserved = ledger.reserve(idempotency_key, owner_id, chain, token, strategy_name, side, "simulated", notional_usd)
    if not reserved:
        return {"accepted": False, "reason": "duplicate key, try again", "idempotency_key": None,
                "correlation_id": correlation_id}

    executed_qty = notional_usd / price_usd if price_usd > 0 else 0.0
    ledger.mark_confirmed(idempotency_key, executed_qty=executed_qty, executed_price_usd=price_usd, price_impact_pct=price_impact_pct)
    logger.info("[%s] simulazione confermata idempotency_key=%s", correlation_id, idempotency_key,
                extra={"correlation_id": correlation_id})
    return {"accepted": True, "reason": "", "idempotency_key": idempotency_key, "correlation_id": correlation_id}
