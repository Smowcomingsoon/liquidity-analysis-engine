"""Allowlist pool — Fase 3 del piano: prima di ogni ordine, non solo al primo
ingresso in allowlist. Tre controlli: liquidita' minima, eta' minima del
pool, nessun crollo di liquidita' oltre una soglia nell'ultima ora.

sniper/security.py (GoPlus/Rugcheck/Honeypot.is) lavora su un TOKEN — tassa,
mintabilita', punteggio di rug. Qui serve invece dato sul POOL — liquidita'
in USD e data di creazione — che quelle API non espongono. Fonte reale
usata: DexScreener (pubblico, nessuna chiave), che da' liquidita' USD e
`pairCreatedAt` per indirizzo di pool su molte chain. Robinhood Chain e'
troppo nuova per essere indicizzata (stesso limite gia' documentato in
quotes.py per l'aggregator Uniswap): per quella chain fetch_pool_snapshot
ritorna sempre None, dichiarato qui, non un valore inventato.

Stile identico a risk.py: (ok: bool, reason: str), mai un'eccezione per un
rifiuto normale."""
from __future__ import annotations

import time

import httpx

DEXSCREENER_BASE = "https://api.dexscreener.com"

# Solo le chain che DexScreener indicizza davvero. "robinhood" e' assente di
# proposito — vedi il commento sopra.
DEXSCREENER_CHAIN_SLUGS: dict[str, str] = {
    "solana": "solana",
}

DEFAULT_MIN_LIQUIDITY_USD = 3000.0
DEFAULT_MIN_POOL_AGE_MINUTES = 30.0
DEFAULT_MAX_LIQUIDITY_DROP_PCT = 50.0


async def fetch_pool_snapshot(chain: str, pool_address: str) -> dict | None:
    """Un'unica chiamata reale a DexScreener per liquidita' USD e timestamp
    di creazione del pool. None se la chain non e' indicizzata o la
    chiamata/parsing fallisce — mai un valore inventato per riempire il
    buco. Chi chiama deve trattare None come "non verificabile", non come
    "pool sicuro"."""
    slug = DEXSCREENER_CHAIN_SLUGS.get(chain)
    if slug is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{DEXSCREENER_BASE}/latest/dex/pairs/{slug}/{pool_address}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])
        if not pairs:
            return None
        pair = pairs[0]
        liquidity_usd = (pair.get("liquidity") or {}).get("usd")
        if liquidity_usd is None:
            return None
        created_at_ms = pair.get("pairCreatedAt")
        return {
            "liquidity_usd": float(liquidity_usd),
            "created_at": (created_at_ms / 1000.0) if created_at_ms else None,
        }
    except Exception:
        return None


def check_liquidity_minimum(
    liquidity_usd: float | None, minimum_usd: float = DEFAULT_MIN_LIQUIDITY_USD,
) -> tuple[bool, str]:
    """Fallisce chiuso: se non sappiamo la liquidita' (nessun dato da
    DexScreener) il pool non e' verificabile e l'ordine si rifiuta — questo
    e' un guardrail di sicurezza (allowlist), non una feature opzionale come
    la size dinamica: qui un dato mancante non deve mai valere "via libera"."""
    if liquidity_usd is None:
        return False, "pool liquidity cannot be verified (no DexScreener data) — rejected for safety"
    if liquidity_usd < minimum_usd:
        return False, f"pool liquidity {liquidity_usd:.0f} USD below the required minimum ({minimum_usd:.0f} USD)"
    return True, ""


def check_pool_age(
    created_at: float | None, minimum_minutes: float = DEFAULT_MIN_POOL_AGE_MINUTES,
) -> tuple[bool, str]:
    """Stesso fail-closed di check_liquidity_minimum: niente snipe su pool
    la cui eta' non si riesce a verificare."""
    if created_at is None:
        return False, "pool age cannot be verified (no DexScreener data) — rejected for safety"
    age_minutes = (time.time() - created_at) / 60.0
    if age_minutes < minimum_minutes:
        return False, f"pool created {age_minutes:.0f} minutes ago, below the required minimum age ({minimum_minutes:.0f} minutes)"
    return True, ""


def check_liquidity_drop(
    current_liquidity_usd: float | None,
    reference_max_usd: float | None,
    max_drop_pct: float = DEFAULT_MAX_LIQUIDITY_DROP_PCT,
) -> tuple[bool, str]:
    """reference_max_usd e' il massimo storico dell'ultima ora (da
    ledger.max_pool_liquidity_since). Se manca uno dei due dati non c'e'
    niente con cui confrontare — la primissima lettura di un pool, o un
    pool per cui la liquidita' non e' verificabile: si passa, lo stesso
    principio di check_concentration per un portafoglio vuoto. Il rifiuto
    per "non verificabile" lo fa gia' check_liquidity_minimum a monte."""
    if current_liquidity_usd is None or reference_max_usd is None or reference_max_usd <= 0:
        return True, ""
    drop_pct = (1 - current_liquidity_usd / reference_max_usd) * 100
    if drop_pct > max_drop_pct:
        return False, (
            f"pool liquidity down {drop_pct:.0f}% in the last hour "
            f"(from {reference_max_usd:.0f} to {current_liquidity_usd:.0f} USD)"
        )
    return True, ""
