"""Wiring reale del TWAP — dal piano: "un segnale della strategia non
genera un ordine unico ma un job con N fette [...]. Non e' alta frequenza:
3-8 fette, 15-90 secondi di intervallo. Sotto un notional minimo si
esegue in un colpo unico. Un segnale opposto ha sempre precedenza: ferma
le fette non ancora eseguite (mai quelle gia' confermate). Lo stato del
job vive persistito fuori dal processo — un riavvio lo rilegge e riprende,
non duplica."

twap.py calcola solo N e i ritardi (matematica pura, nessun I/O); questo
modulo e' quello che li usa per davvero: persiste il job PRIMA di
eseguire la prima fetta, chiama execute.execute_order() una volta per
fetta, aspetta i ritardi tra una fetta e la successiva, e si ferma al
primo fallimento (mai un errore silenzioso).

Stesso stile di ledger.py: SQLite, thread.Lock, WAL, stesso file DB (una
tabella in piu', non un database separato — un job e' comunque figlio
degli stessi ordini che finiscono in `orders`).

RESUME AUTOMATICO: resume_running_twap_jobs() e' cablata nel lifespan di
main.py (stesso pattern degli altri loop in background del sito) e, ad
ogni avvio del processo, riprende ogni job 'running' dalla fetta
slices_done — mai da zero, mai una fetta gia' confermata due volte. Per
farlo, execute_twap_job() persiste ora anche price_usd e quote_meta (gli
stessi input specifici della chain che main.py usava solo in memoria per
costruire quote_for_slice: input/output mint per Solana, token_in/out/fee
per Robinhood — MAI la chiave privata, che alla ripresa viene ridecriptata
dall'account dell'owner, non letta dal ledger). Un job creato prima di
questo campo (o passato senza quote_meta) non e' ricostruibile: resta
'running' e va richiamato a mano — non viene ignorato in silenzio, vedi
il log di resume_running_twap_jobs().
"""
import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from app.trading.onchain import execute, ledger, twap

logger = logging.getLogger("onchain_twap_jobs")

# Sotto questo notional, TWAP escluso di proposito (dal piano): il gas per
# fetta costerebbe piu' del risparmio di slippage su un ordine cosi'
# piccolo. plan_slices() gia' collassa a 1 fetta sotto DEFAULT_MIN_SLICE_USD
# per fetta, ma qui e' la soglia sul TOTALE dell'ordine, prima ancora di
# calcolare quante fette servirebbero.
DEFAULT_MIN_TWAP_NOTIONAL_USD = 15.0

# Firma del callback che chi chiama passa per costruire il quote di UNA
# fetta: Solana (amount_lamports) e Robinhood (amount_in) scalano con la
# size della fetta, non con quella totale dell'ordine — costruirlo e'
# responsabilita' di chi conosce la chain, non di questo modulo generico.
QuoteForSlice = Callable[[float], Awaitable[tuple[float, dict]]]


def _connect():
    return ledger._connect()


def _init_db() -> None:
    with ledger._lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS twap_jobs (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                side TEXT NOT NULL,
                total_usd REAL NOT NULL,
                slices_planned TEXT NOT NULL,
                slices_done INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        # Migrazione additiva per DB creati prima di queste colonne: senza
        # price_usd/quote_meta un job vecchio non e' ricostruibile al
        # riavvio (vedi resume_running_twap_jobs), ma non deve rompere
        # l'avvio del processo su un DB esistente.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(twap_jobs)").fetchall()}
        if "price_usd" not in existing_cols:
            conn.execute("ALTER TABLE twap_jobs ADD COLUMN price_usd REAL")
        if "quote_meta" not in existing_cols:
            conn.execute("ALTER TABLE twap_jobs ADD COLUMN quote_meta TEXT")


_init_db()


def _create_job(
    owner_id: str, chain: str, token: str, strategy_name: str, side: str,
    total_usd: float, slices: list[float], price_usd: float,
    quote_meta: dict | None,
) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    with ledger._lock, _connect() as conn:
        conn.execute(
            "INSERT INTO twap_jobs (id, owner_id, chain, token, strategy_name, side, total_usd, "
            "slices_planned, slices_done, status, price_usd, quote_meta, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'running', ?, ?, ?, ?)",
            (
                job_id, owner_id, chain, token, strategy_name, side, total_usd, json.dumps(slices),
                price_usd, json.dumps(quote_meta) if quote_meta is not None else None, now, now,
            ),
        )
    return job_id


def get_job(job_id: str) -> dict | None:
    with ledger._lock, _connect() as conn:
        row = conn.execute("SELECT * FROM twap_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        job["slices_planned"] = json.loads(job["slices_planned"])
        job["quote_meta"] = json.loads(job["quote_meta"]) if job.get("quote_meta") else None
        return job


def list_running_jobs(owner_id: str | None = None) -> list[dict]:
    """Job lasciati a status='running' — quello che un riavvio a meta'
    lascia indietro. Vedi il limite noto in testa al modulo: e' chi
    riprende (oggi nessuno automaticamente) a doverli richiamare."""
    with ledger._lock, _connect() as conn:
        query = "SELECT * FROM twap_jobs WHERE status = 'running'"
        params: tuple = ()
        if owner_id is not None:
            query += " AND owner_id = ?"
            params = (owner_id,)
        rows = conn.execute(query, params).fetchall()
    jobs = [dict(r) for r in rows]
    for job in jobs:
        job["slices_planned"] = json.loads(job["slices_planned"])
        job["quote_meta"] = json.loads(job["quote_meta"]) if job.get("quote_meta") else None
    return jobs


def _update_progress(job_id: str, slices_done: int) -> None:
    with ledger._lock, _connect() as conn:
        conn.execute(
            "UPDATE twap_jobs SET slices_done = ?, updated_at = ? WHERE id = ?",
            (slices_done, time.time(), job_id),
        )


def _set_status(job_id: str, status: str, reason: str | None = None) -> None:
    with ledger._lock, _connect() as conn:
        conn.execute(
            "UPDATE twap_jobs SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
            (status, reason, time.time(), job_id),
        )


def request_cancel(job_id: str) -> bool:
    """Un segnale opposto ha sempre precedenza (dal piano): ferma le fette
    non ancora eseguite, mai quelle gia' confermate — il loop di
    execute_twap_job() controlla questo flag prima di partire con OGNI
    fetta successiva alla prima, mai a meta' di una fetta in corso (che a
    questo punto e' comunque gia' stata firmata/inviata). Ritorna False se
    il job non e' (piu') in stato 'running'."""
    with ledger._lock, _connect() as conn:
        row = conn.execute("SELECT status FROM twap_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or row["status"] != "running":
            return False
        conn.execute(
            "UPDATE twap_jobs SET status = 'cancel_requested', updated_at = ? WHERE id = ?",
            (time.time(), job_id),
        )
    return True


async def _run_slices(
    job_id: str, owner_id: str, chain: str, token: str, strategy_name: str, side: str,
    price_usd: float, private_key_hex: str, slices: list[float], delays: list[float],
    quote_for_slice: QuoteForSlice, *, start_index: int = 0,
) -> dict:
    """Corpo del loop TWAP, condiviso fra un job nuovo (start_index=0) e la
    ripresa di un job dopo un riavvio (start_index=job['slices_done']): la
    fetta i e' sempre slices[i], mai rinumerata, cosi' un job ripreso non
    firma due volte una fetta gia' confermata.

    delays e' sempre la lista completa (indicizzata sulla posizione nella
    fetta, non sul punto di ripresa): delays[i-1] e' l'attesa PRIMA della
    fetta i. Alla ripresa non si attende quella della prima fetta rimasta
    (start_index stesso trattamento di i==0 nel job originale) — il
    processo e' appena tornato su, aspettare di nuovo non serve a niente e
    ritarderebbe solo la ripresa."""
    for i in range(start_index, len(slices)):
        slice_usd = slices[i]
        if i > start_index:
            job = get_job(job_id)
            if job is not None and job["status"] == "cancel_requested":
                _set_status(job_id, "cancelled", f"stopped after {i} of {len(slices)} slices (opposite signal)")
                return {
                    "job_id": job_id, "status": "cancelled",
                    "slices_done": i, "slices_total": len(slices),
                }
            await asyncio.sleep(delays[i - 1])

        try:
            price_impact_pct, quote = await quote_for_slice(slice_usd)
        except Exception as exc:  # noqa: BLE001 - un quote fallito ferma il job, non e' silenzioso
            _set_status(job_id, "failed", f"quote failed on slice {i + 1}/{len(slices)}: {exc}")
            return {
                "job_id": job_id, "status": "failed",
                "slices_done": i, "slices_total": len(slices),
                "reason": f"quote failed on slice {i + 1}/{len(slices)}: {exc}",
            }

        result = await execute.execute_order(
            owner_id, chain, token, strategy_name, side, slice_usd,
            price_impact_pct, price_usd, private_key_hex, quote,
        )
        if not result["accepted"]:
            _set_status(job_id, "failed", result["reason"])
            return {
                "job_id": job_id, "status": "failed",
                "slices_done": i, "slices_total": len(slices),
                "reason": result["reason"],
            }
        _update_progress(job_id, i + 1)

    _set_status(job_id, "completed")
    return {"job_id": job_id, "status": "completed", "slices_done": len(slices), "slices_total": len(slices)}


async def execute_twap_job(
    owner_id: str, chain: str, token: str, strategy_name: str, side: str,
    total_usd: float, price_usd: float, private_key_hex: str,
    pool_liquidity_usd: float, quote_for_slice: QuoteForSlice,
    *, min_twap_notional_usd: float = DEFAULT_MIN_TWAP_NOTIONAL_USD,
    rng=None, quote_meta: dict | None = None,
) -> dict:
    """Esegue un segnale come una sola fetta (sotto min_twap_notional_usd,
    o su un pool profondo dove twap.plan_slices() collassa a 1) o come un
    vero TWAP scaglionato. Scrive il job nel ledger PRIMA di firmare la
    prima fetta — e' quello che rende un riavvio ricostruibile (vedi
    list_running_jobs()), non un dict Python che si perde al riavvio.

    Ogni fetta e' semplicemente una execute.execute_order() con
    notional_usd = quella fetta: il quote (che dipende dalla size della
    fetta, non del totale) lo costruisce quote_for_slice(slice_usd), che
    chi chiama fornisce perche' solo lui conosce gli input specifici della
    chain (input/output mint per Solana, token_in/out/fee per Robinhood).

    quote_meta e' la versione persistibile (JSON, nessuna chiave privata)
    di quegli stessi input specifici della chain: chi chiama lo passa
    quando vuole che resume_running_twap_jobs() possa ricostruire da solo
    quote_for_slice dopo un riavvio (vedi _build_quote_for_slice). Senza,
    il job resta comunque persistito e list_running_jobs() lo ritrova, ma
    la ripresa automatica lo salta (non ha di che ricostruire il quote).

    Al primo fallimento (risk check, firma o invio) si ferma: nessun
    tentativo sulle fette successive, il job va a 'failed' con la ragione,
    e il risultato riporta esattamente quante fette sono state eseguite —
    mai un fallimento silenzioso."""
    if total_usd < min_twap_notional_usd:
        slices = [round(total_usd, 2)]
    else:
        slices = twap.plan_slices(total_usd, pool_liquidity_usd)

    job_id = _create_job(owner_id, chain, token, strategy_name, side, total_usd, slices, price_usd, quote_meta)
    delays = twap.plan_delays_s(len(slices), rng=rng) if len(slices) > 1 else []

    return await _run_slices(
        job_id, owner_id, chain, token, strategy_name, side, price_usd, private_key_hex,
        slices, delays, quote_for_slice,
    )


def _build_quote_for_slice(chain: str, total_usd: float, quote_meta: dict) -> QuoteForSlice:
    """L'inverso di quanto main.py costruisce a mano in
    /api/onchain/execute/twap: dato il quote_meta persistito alla
    creazione del job, ricostruisce lo stesso quote_for_slice(slice_usd)
    che avrebbe usato il chiamante originale — stessa formula di scala
    (proporzionale a slice_usd/total_usd), stesse chiamate a quotes.py."""
    from app.trading.onchain import quotes as onchain_quotes

    if chain == "solana":
        input_mint = quote_meta["input_mint"]
        output_mint = quote_meta["output_mint"]
        total_amount_lamports = quote_meta["total_amount_lamports"]
        slippage_bps = quote_meta.get("slippage_bps", 50)

        async def _quote_for_slice(slice_usd: float) -> tuple[float, dict]:
            amount_lamports = max(1, round(total_amount_lamports * (slice_usd / total_usd)))
            quote = await onchain_quotes.jupiter_quote(input_mint, output_mint, amount_lamports, slippage_bps)
            return quote["price_impact_pct"], quote

        return _quote_for_slice

    token_in = quote_meta["token_in"]
    token_out = quote_meta["token_out"]
    fee = quote_meta["fee"]
    total_amount_in = quote_meta["total_amount_in"]
    max_slippage_pct = quote_meta.get("max_slippage_pct", 1.0)

    async def _quote_for_slice(slice_usd: float) -> tuple[float, dict]:
        amount_in = max(1, round(total_amount_in * (slice_usd / total_usd)))
        quote = await onchain_quotes.uniswap_v3_quote(token_in, token_out, fee, amount_in)
        amount_out_minimum = int(quote["amount_out"] * (1 - max_slippage_pct / 100))
        return quote["price_impact_pct"], {
            "token_in": token_in, "token_out": token_out, "fee": fee,
            "amount_in": amount_in, "amount_out_minimum": amount_out_minimum,
        }

    return _quote_for_slice


async def resume_running_twap_jobs() -> list[dict]:
    """Chiamata all'avvio del processo (vedi lifespan in main.py): ogni job
    lasciato 'running' da un riavvio a meta' viene ripreso dalla fetta
    slices_done, mai da zero (list_running_jobs() e' la stessa funzione
    che i test di persistenza gia' usano, non duplicata qui).

    Un solo processo scrive su questo SQLite (WAL + thread.Lock, come
    ledger.py; deploy/README.md descrive un singolo container Docker su
    Hetzner, non piu' istanze in parallelo) — se questo dovesse cambiare,
    due processi che chiamano resume sullo stesso job potrebbero firmare
    la stessa fetta due volte: qui non c'e' un lock distribuito, solo
    quello in-process.

    La chiave privata NON e' mai persistita nel ledger: viene ridecriptata
    al volo dall'account dell'owner, esattamente come fa main.py per una
    richiesta normale. Un job senza quote_meta (creato prima di questo
    campo, o passato senza — vedi execute_twap_job) non e' ricostruibile:
    resta 'running' e va richiamato a mano, non silenziosamente ignorato."""
    from app import auth

    results = []
    for job in list_running_jobs():
        job_id = job["id"]
        if not job.get("quote_meta"):
            logger.warning(
                "[twap-resume] job %s (%s/%s) senza quote_meta: non ricostruibile, resta 'running'.",
                job_id, job["chain"], job["token"],
            )
            continue

        user = auth.get_user(job["owner_id"])
        if user is None:
            _set_status(job_id, "failed", "owner not found when resuming after restart")
            results.append({"job_id": job_id, "status": "failed", "reason": "owner not found"})
            continue

        try:
            private_key_hex = auth.get_decrypted_onchain_private_key(user, job["chain"])
        except Exception as exc:  # noqa: BLE001 - un job non ripristinabile va a failed, non muto
            _set_status(job_id, "failed", f"on-chain key could not be decrypted on resume: {exc}")
            results.append({"job_id": job_id, "status": "failed", "reason": str(exc)})
            continue

        quote_for_slice = _build_quote_for_slice(job["chain"], job["total_usd"], job["quote_meta"])
        slices = job["slices_planned"]
        delays = twap.plan_delays_s(len(slices)) if len(slices) > 1 else []
        logger.info(
            "[twap-resume] riprendo job %s (%s/%s) dalla fetta %d/%d",
            job_id, job["chain"], job["token"], job["slices_done"] + 1, len(slices),
        )
        result = await _run_slices(
            job_id, job["owner_id"], job["chain"], job["token"], job["strategy_name"], job["side"],
            job["price_usd"], private_key_hex, slices, delays, quote_for_slice,
            start_index=job["slices_done"],
        )
        results.append(result)
    return results
