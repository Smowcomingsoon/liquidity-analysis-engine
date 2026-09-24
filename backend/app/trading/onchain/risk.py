"""Guardrail confermati da Lorenzo il 19/09/2026 (config.py):
tetto per operazione, tetto giornaliero, concentrazione per token.

Ogni funzione ritorna (ok: bool, reason: str) — mai un'eccezione per un
rifiuto normale: un ordine rifiutato per slippage o tetto e' un esito
previsto del sistema, non un errore. reason e' sempre in italiano, e'
quello che finisce nel log e nell'eventuale notifica."""
from app.config import settings
from app.trading.onchain import ledger, pool_safety


def check_per_trade_cap(size_usd: float) -> tuple[bool, str]:
    if size_usd > settings.onchain_max_usd_per_trade:
        return False, (
            f"size {size_usd:.2f} USD above the per-trade cap "
            f"({settings.onchain_max_usd_per_trade:.2f} USD)"
        )
    return True, ""


def check_daily_cap(owner_id: str, chain: str, size_usd: float) -> tuple[bool, str]:
    spent = ledger.get_daily_spent(owner_id, chain)
    if spent + size_usd > settings.onchain_max_usd_per_day:
        return False, (
            f"would spend {spent + size_usd:.2f} USD today on {chain}, "
            f"above the daily cap ({settings.onchain_max_usd_per_day:.2f} USD)"
        )
    return True, ""


def check_concentration(owner_id: str, chain: str, token: str, additional_usd: float) -> tuple[bool, str]:
    """Concentrazione relativa DENTRO il portafoglio on-chain di questa
    chain (somma del costo di tutte le posizioni), non rispetto al saldo
    nativo del wallet — quello richiederebbe un prezzo SOL/ETH in USD che
    qui non abbiamo ancora. Semplificazione dichiarata, non nascosta: il
    vero backstop resta il controllo di slippage/price-impact sul quote,
    questo e' una difesa in piu', non l'unica."""
    positions = ledger.get_all_positions(owner_id, chain)
    existing_token_cost = next((p["cost_usd"] for p in positions if p["token"] == token), 0.0)
    total_portfolio_cost = sum(p["cost_usd"] for p in positions)
    if total_portfolio_cost <= 0:
        # Nessuna posizione ancora aperta: la prima operazione e' per forza
        # il 100% di un portafoglio vuoto — il tetto ha senso solo dal
        # momento in cui esiste un portafoglio con cui confrontarsi.
        return True, ""
    new_token_cost = existing_token_cost + additional_usd
    new_total = total_portfolio_cost + additional_usd
    pct = (new_token_cost / new_total) * 100
    if pct > settings.onchain_max_token_concentration_pct:
        return False, (
            f"{token} would be {pct:.1f}% of the on-chain portfolio on {chain}, "
            f"above the concentration cap ({settings.onchain_max_token_concentration_pct:.0f}%)"
        )
    return True, ""


def check_circuit_breaker(owner_id: str, chain: str) -> tuple[bool, str]:
    status = ledger.breaker_status(owner_id, chain)
    if status["tripped"]:
        return False, f"circuit breaker active on {chain}: {status['reason']} — manual re-arm required"
    return True, ""


def check_discrepancy(owner_id: str, chain: str, token: str) -> tuple[bool, str]:
    """Blocca nuovi ordini su token+wallet con una discrepanza saldo-vero
    aperta (vedi reconciliation.check_position_discrepancy) — nessuna
    correzione automatica, serve revisione a mano (ledger.review_discrepancy)."""
    if ledger.has_open_discrepancy(owner_id, chain, token):
        return False, (
            f"{token} on {chain} has an open, unreviewed balance discrepancy — "
            "new orders blocked until it is reviewed manually"
        )
    return True, ""


# Soglie del price impact, dal piano approvato (revisione risk management).
PRICE_IMPACT_WARN_PCT = 1.0
PRICE_IMPACT_REJECT_PCT = 5.0


def check_price_impact(price_impact_pct: float) -> tuple[bool, str]:
    """Il vero backstop contro lo slippage assurdo: sotto l'1% passa senza
    fiatare, fra 1-5% passa ma andrebbe loggato come WARNING da chi chiama,
    sopra il 5% e' un rifiuto secco — nessuna transazione va inviata."""
    if price_impact_pct > PRICE_IMPACT_REJECT_PCT:
        return False, f"price impact {price_impact_pct:.2f}% above the hard cap ({PRICE_IMPACT_REJECT_PCT:.0f}%)"
    return True, ""


def clamp_size_to_pool(
    requested_usd: float,
    pool_liquidity_usd: float | None,
    max_pool_pct: float = 1.5,
    safety_cap_usd: float | None = None,
) -> float:
    """Size mai fissa in USD: min(size richiesta, max_pool_pct% della
    liquidita' del pool, tetto di sicurezza). safety_cap_usd default al
    tetto per operazione — che run_all_checks controlla comunque dopo, ma
    applicarlo anche qui evita di calcolare una size "clampata" piu' alta
    del tetto e poi scoprirlo un passo dopo.

    pool_liquidity_usd non e' sempre disponibile (dipende da chain/quote —
    vedi pool_safety.fetch_pool_snapshot): quando e' None o zero non si
    riduce niente, la richiesta torna invariata e resta check_per_trade_cap
    (chiamato subito dopo da run_all_checks) a rifiutarla se sopra tetto —
    lo stesso comportamento di sempre. Il tetto di sicurezza qui dentro
    conta solo insieme a una liquidita' del pool nota: senza quella non
    c'e' "size dinamica" da applicare, solo una richiesta da lasciare al
    controllo esistente. Mai bloccare un ordine per una feature nuova che
    manca di un dato."""
    if pool_liquidity_usd is None or pool_liquidity_usd <= 0:
        return requested_usd
    cap = safety_cap_usd if safety_cap_usd is not None else settings.onchain_max_usd_per_trade
    return min(requested_usd, pool_liquidity_usd * max_pool_pct / 100.0, cap)


def run_all_checks(
    owner_id: str, chain: str, token: str, size_usd: float, price_impact_pct: float,
    pool_info: dict | None = None, strategy_name: str | None = None,
) -> tuple[bool, str]:
    """Tutti i controlli in ordine, ferma al primo che rifiuta — l'ordine
    di questa lista e' quello suggerito dal piano: prima il circuit
    breaker (nessun senso controllare altro se e' gia' scattato), poi i
    tetti in USD (piu' cheap da calcolare), poi price impact (richiede un
    quote gia' in mano) e concentrazione.

    pool_info e' opzionale apposta, per non rompere le chiamate esistenti
    (e i test) che non hanno ancora un pool da controllare: {"liquidity_usd",
    "created_at", "reference_max_liquidity_usd", opzionali "min_liquidity_usd"/
    "min_age_minutes"/"max_drop_pct"}. Quando e' presente, l'allowlist pool
    (liquidita' minima, eta' minima, crollo di liquidita') viene controllata
    ad OGNI tentativo, non solo una volta all'ingresso — cosi' chiede il
    piano.

    strategy_name == "manuale" salta SOLO la concentrazione, non gli altri
    controlli: e' l'unico che decide "quanto diversificare", una scelta che
    un umano che swappa di sua volonta' puo' prendere lui — Lorenzo l'ha
    chiesto dopo che uno swap ETH->USDG legittimo (portare tutto in una
    stablecoin) e' stato respinto perche' USDG sarebbe stato il 100% di un
    portafoglio on-chain che, di fatto, aveva solo quella posizione tracciata.
    Un'automazione (watchlist, ordine limite) resta invece sotto il tetto:
    e' quella che puo' costruire una concentrazione senza che nessuno la
    stia guardando."""
    checks: list[tuple] = [
        (check_circuit_breaker, (owner_id, chain)),
        (check_discrepancy, (owner_id, chain, token)),
        (check_per_trade_cap, (size_usd,)),
        (check_daily_cap, (owner_id, chain, size_usd)),
        (check_price_impact, (price_impact_pct,)),
    ]
    if strategy_name != "manuale":
        checks.append((check_concentration, (owner_id, chain, token, size_usd)))
    if pool_info is not None:
        checks.append((
            pool_safety.check_liquidity_minimum,
            (pool_info.get("liquidity_usd"), pool_info.get("min_liquidity_usd", pool_safety.DEFAULT_MIN_LIQUIDITY_USD)),
        ))
        checks.append((
            pool_safety.check_pool_age,
            (pool_info.get("created_at"), pool_info.get("min_age_minutes", pool_safety.DEFAULT_MIN_POOL_AGE_MINUTES)),
        ))
        checks.append((
            pool_safety.check_liquidity_drop,
            (pool_info.get("liquidity_usd"), pool_info.get("reference_max_liquidity_usd"),
             pool_info.get("max_drop_pct", pool_safety.DEFAULT_MAX_LIQUIDITY_DROP_PCT)),
        ))
    for check, args in checks:
        ok, reason = check(*args)
        if not ok:
            return False, reason
    return True, ""
