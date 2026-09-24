"""Fonte di verita' per size e stato di ogni ordine on-chain — la parte del
piano "mai sbagliare size, mai duplicare un ordine".

SQLite, non JSON come issues.py/auth.py: qui serve un vincolo UNIQUE vero a
livello database (idempotenza) e scritture atomiche, non un file riscritto
per intero ad ogni salvataggio. Un thread.Lock protegge le scritture perche'
il resto dell'app e' async ma sqlite3 e' sincrona — a questa scala (un
processo, un server) e' l'attrito giusto, non serve un pool di connessioni.

Sequenza obbligata per ogni ordine, sempre in questo ordine:
  1. reserve()       — scrive "pending" PRIMA di firmare qualunque cosa
  2. mark_submitted() — solo dopo che una transazione e' stata davvero inviata
  3. mark_confirmed() o mark_failed() — dopo aver interrogato la chain

reserve() fallisce (ritorna False) se la chiave esiste gia': chi la chiama
non deve firmare niente in quel caso, il tentativo e' gia' in corso o concluso.
"""
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone

from app.trading.onchain.storage import data_file

_DB_FILE = data_file("onchain_ledger.db")
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                idempotency_key TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                strategy_name TEXT NOT NULL,
                side TEXT NOT NULL,
                mode TEXT NOT NULL,
                size_usd REAL NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                tx_signature TEXT,
                price_impact_pct REAL,
                executed_qty REAL,
                executed_price_usd REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                quantity REAL NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (owner_id, chain, token)
            );
            CREATE TABLE IF NOT EXISTS daily_spend (
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                day_key TEXT NOT NULL,
                spent_usd REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (owner_id, chain, day_key)
            );
            CREATE TABLE IF NOT EXISTS circuit_breaker (
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                consecutive_losses INTEGER NOT NULL DEFAULT 0,
                tripped_at REAL,
                reason TEXT,
                PRIMARY KEY (owner_id, chain)
            );
            CREATE TABLE IF NOT EXISTS pool_liquidity_history (
                chain TEXT NOT NULL,
                pool_address TEXT NOT NULL,
                liquidity_usd REAL NOT NULL,
                recorded_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pool_liquidity_history_lookup
                ON pool_liquidity_history (chain, pool_address, recorded_at);
            CREATE TABLE IF NOT EXISTS discrepancies (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                ledger_usd REAL NOT NULL,
                real_balance_usd REAL NOT NULL,
                diff_pct REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at REAL NOT NULL,
                reviewed_at REAL
            );
            CREATE TABLE IF NOT EXISTS limit_orders (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                token_in TEXT NOT NULL,
                token_out TEXT NOT NULL,
                token_in_symbol TEXT NOT NULL,
                token_out_symbol TEXT NOT NULL,
                token_in_decimals INTEGER NOT NULL,
                token_out_decimals INTEGER NOT NULL,
                amount_in REAL NOT NULL,
                direction TEXT NOT NULL,
                target_price_usd REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                filled_idempotency_key TEXT,
                reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_limit_orders_open
                ON limit_orders (status, chain);
        """)


_init_db()


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def reserve(
    idempotency_key: str, owner_id: str, chain: str, token: str,
    strategy_name: str, side: str, mode: str, size_usd: float,
) -> bool:
    """True se la riga e' stata scritta ora (via libera a firmare). False se
    esisteva gia' — un retry sulla stessa chiave, mai una firma doppia."""
    now = time.time()
    with _lock, _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO orders (idempotency_key, owner_id, chain, token, strategy_name, "
                "side, mode, size_usd, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (idempotency_key, owner_id, chain, token, strategy_name, side, mode, size_usd, now, now),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_order(idempotency_key: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        return dict(row) if row else None


def list_orders(owner_id: str, chain: str, limit: int = 100) -> list[dict]:
    """Per la cronologia unificata (piano DEX): tutti gli ordini di un
    wallet, piu' recenti prima — non solo quelli ancora 'pending'."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE owner_id = ? AND chain = ? ORDER BY created_at DESC LIMIT ?",
            (owner_id, chain, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_submitted(idempotency_key: str, tx_signature: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'submitted', tx_signature = ?, updated_at = ? WHERE idempotency_key = ?",
            (tx_signature, time.time(), idempotency_key),
        )


def mark_confirmed(
    idempotency_key: str, executed_qty: float, executed_price_usd: float, price_impact_pct: float | None = None,
) -> None:
    """Chiusura riuscita: aggiorna l'ordine E la posizione E la spesa
    giornaliera nella stessa transazione — sono la stessa fonte di verita',
    non tre scritture indipendenti che possono disallinearsi."""
    executed_usd = executed_qty * executed_price_usd
    with _lock, _connect() as conn:
        order = conn.execute("SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if order is None:
            return
        conn.execute(
            "UPDATE orders SET status = 'confirmed', executed_qty = ?, executed_price_usd = ?, "
            "price_impact_pct = ?, updated_at = ? WHERE idempotency_key = ?",
            (executed_qty, executed_price_usd, price_impact_pct, time.time(), idempotency_key),
        )
        signed_qty = executed_qty if order["side"] == "buy" else -executed_qty
        signed_cost = executed_usd if order["side"] == "buy" else -executed_usd
        conn.execute(
            "INSERT INTO positions (owner_id, chain, token, quantity, cost_usd) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, chain, token) DO UPDATE SET "
            "quantity = quantity + excluded.quantity, cost_usd = cost_usd + excluded.cost_usd",
            (order["owner_id"], order["chain"], order["token"], signed_qty, signed_cost),
        )
        day_key = _today_key()
        conn.execute(
            "INSERT INTO daily_spend (owner_id, chain, day_key, spent_usd) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(owner_id, chain, day_key) DO UPDATE SET spent_usd = spent_usd + excluded.spent_usd",
            (order["owner_id"], order["chain"], day_key, executed_usd),
        )


def mark_failed(idempotency_key: str, reason: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'failed', reason = ?, updated_at = ? WHERE idempotency_key = ?",
            (reason, time.time(), idempotency_key),
        )


def mark_confirmed_signature_only(idempotency_key: str) -> None:
    """Usata SOLO dalla riconciliazione post-crash (reconciliation.py): la
    firma e' confermata on-chain via RPC, ma qui NON tocchiamo posizioni
    né spesa giornaliera come fa mark_confirmed(). Motivo: la size e il
    prezzo davvero eseguiti richiederebbero di leggere i log della
    transazione (variazioni di saldo token), non implementato — vedi
    reconciliation.py. Serve solo a togliere l'ordine da
    pending_unconfirmed() cosi' un nuovo segnale non resti bloccato dietro
    un ordine che in realta' e' gia' chiuso; le posizioni restano quelle
    scritte l'ultima volta che *questo* processo ha visto la conferma."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'confirmed', updated_at = ? WHERE idempotency_key = ?",
            (time.time(), idempotency_key),
        )


def get_position(owner_id: str, chain: str, token: str) -> dict:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT quantity, cost_usd FROM positions WHERE owner_id = ? AND chain = ? AND token = ?",
            (owner_id, chain, token),
        ).fetchone()
        return {"quantity": row["quantity"], "cost_usd": row["cost_usd"]} if row else {"quantity": 0.0, "cost_usd": 0.0}


def get_all_positions(owner_id: str, chain: str) -> list[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT token, quantity, cost_usd FROM positions WHERE owner_id = ? AND chain = ? AND quantity > 0",
            (owner_id, chain),
        ).fetchall()
        return [dict(r) for r in rows]


def get_daily_spent(owner_id: str, chain: str, day_key: str | None = None) -> float:
    day_key = day_key or _today_key()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT spent_usd FROM daily_spend WHERE owner_id = ? AND chain = ? AND day_key = ?",
            (owner_id, chain, day_key),
        ).fetchone()
        return row["spent_usd"] if row else 0.0


def pending_unconfirmed(owner_id: str | None = None) -> list[dict]:
    """Ordini 'submitted' senza mai una conferma — quello che un riavvio a
    meta' lascia indietro. Chi chiama deve interrogare la chain con la loro
    tx_signature prima di considerare qualunque nuovo segnale."""
    with _lock, _connect() as conn:
        query = "SELECT * FROM orders WHERE status = 'submitted'"
        params: tuple = ()
        if owner_id is not None:
            query += " AND owner_id = ?"
            params = (owner_id,)
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def record_trade_outcome(owner_id: str, chain: str, pnl_usd: float, loss_streak_trip: int = 5) -> bool:
    """Aggiorna il contatore di perdite consecutive; ritorna True se il
    circuit breaker scatta ORA (chi chiama deve fermare nuovi ordini)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT consecutive_losses FROM circuit_breaker WHERE owner_id = ? AND chain = ?",
            (owner_id, chain),
        ).fetchone()
        losses = row["consecutive_losses"] if row else 0
        losses = losses + 1 if pnl_usd < 0 else 0
        tripped = losses >= loss_streak_trip
        conn.execute(
            "INSERT INTO circuit_breaker (owner_id, chain, consecutive_losses, tripped_at, reason) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_id, chain) DO UPDATE SET consecutive_losses = excluded.consecutive_losses, "
            "tripped_at = excluded.tripped_at, reason = excluded.reason",
            (owner_id, chain, losses, time.time() if tripped else None,
             f"{losses} consecutive losing trades" if tripped else None),
        )
        return tripped


def breaker_status(owner_id: str, chain: str) -> dict:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT consecutive_losses, tripped_at, reason FROM circuit_breaker WHERE owner_id = ? AND chain = ?",
            (owner_id, chain),
        ).fetchone()
        if row is None:
            return {"tripped": False, "consecutive_losses": 0, "reason": None}
        return {"tripped": bool(row["tripped_at"]), "consecutive_losses": row["consecutive_losses"], "reason": row["reason"]}


def record_pool_liquidity(chain: str, pool_address: str, liquidity_usd: float, recorded_at: float | None = None) -> None:
    """Una lettura in piu' nello storico del pool — serve solo a
    pool_safety.check_liquidity_drop per trovare il massimo dell'ultima
    ora. Non c'e' un upsert: ogni lettura e' un tentativo di ordine,
    vogliamo la serie intera, non l'ultimo valore."""
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO pool_liquidity_history (chain, pool_address, liquidity_usd, recorded_at) VALUES (?, ?, ?, ?)",
            (chain, pool_address, liquidity_usd, recorded_at if recorded_at is not None else time.time()),
        )


def max_pool_liquidity_since(chain: str, pool_address: str, since: float | None = None) -> float | None:
    """Massimo storico di liquidita' del pool dopo `since` (default: l'ora
    scorsa). None se non c'e' ancora nessuna lettura — la primissima volta
    che si vede questo pool."""
    since = since if since is not None else time.time() - 3600
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT MAX(liquidity_usd) AS m FROM pool_liquidity_history "
            "WHERE chain = ? AND pool_address = ? AND recorded_at >= ?",
            (chain, pool_address, since),
        ).fetchone()
        return row["m"] if row and row["m"] is not None else None


def open_discrepancy(
    owner_id: str, chain: str, token: str, ledger_usd: float, real_balance_usd: float, diff_pct: float,
) -> str:
    """Registra una discrepanza saldo-vero-vs-ledger da rivedere a mano —
    mai corretta in automatico (vedi reconciliation.check_position_discrepancy)."""
    discrepancy_id = uuid.uuid4().hex
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO discrepancies (id, owner_id, chain, token, ledger_usd, real_balance_usd, "
            "diff_pct, status, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (discrepancy_id, owner_id, chain, token, ledger_usd, real_balance_usd, diff_pct, time.time()),
        )
    return discrepancy_id


def has_open_discrepancy(owner_id: str, chain: str, token: str) -> bool:
    """risk.run_all_checks la puo' chiamare per bloccare nuovi ordini su
    quel token+wallet finche' la discrepanza non e' rivista a mano."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM discrepancies WHERE owner_id = ? AND chain = ? AND token = ? AND status = 'open' LIMIT 1",
            (owner_id, chain, token),
        ).fetchone()
        return row is not None


def list_discrepancies(owner_id: str | None = None, status: str | None = None) -> list[dict]:
    with _lock, _connect() as conn:
        query = "SELECT * FROM discrepancies WHERE 1=1"
        params: list = []
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(owner_id)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def review_discrepancy(discrepancy_id: str) -> None:
    """Chiusura MANUALE apposta, stesso principio di reset_breaker: nessuna
    revisione automatica di una discrepanza di saldo."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE discrepancies SET status = 'reviewed', reviewed_at = ? WHERE id = ?",
            (time.time(), discrepancy_id),
        )


def reset_breaker(owner_id: str, chain: str) -> None:
    """Riattivazione MANUALE apposta — vedi il piano: un breaker che si
    riarma da solo non e' un freno, e' un ritardo."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE circuit_breaker SET consecutive_losses = 0, tripped_at = NULL, reason = NULL "
            "WHERE owner_id = ? AND chain = ?",
            (owner_id, chain),
        )


# ─────────────────────────────────────────── ordini limite (piano DEX) ────
# Un ordine limite qui e' size + prezzo obiettivo + direzione, in attesa che
# un ciclo esterno (limit_order_loop in main.py) lo chiuda chiamando
# execute.execute_order con gli stessi guardrail di un ordine manuale — mai
# un secondo motore che firma da solo scavalcando risk.run_all_checks().

def create_limit_order(
    owner_id: str, chain: str,
    token_in: str, token_out: str, token_in_symbol: str, token_out_symbol: str,
    token_in_decimals: int, token_out_decimals: int,
    amount_in: float, direction: str, target_price_usd: float,
) -> dict:
    order_id = uuid.uuid4().hex
    now = time.time()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO limit_orders (id, owner_id, chain, token_in, token_out, "
            "token_in_symbol, token_out_symbol, token_in_decimals, token_out_decimals, "
            "amount_in, direction, target_price_usd, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (order_id, owner_id, chain, token_in, token_out, token_in_symbol, token_out_symbol,
             token_in_decimals, token_out_decimals, amount_in, direction, target_price_usd, now, now),
        )
    return get_limit_order(order_id)


def get_limit_order(order_id: str) -> dict | None:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM limit_orders WHERE id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def list_limit_orders(owner_id: str, chain: str | None = None, status: str | None = None) -> list[dict]:
    with _lock, _connect() as conn:
        query = "SELECT * FROM limit_orders WHERE owner_id = ?"
        params: list = [owner_id]
        if chain is not None:
            query += " AND chain = ?"
            params.append(chain)
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def list_open_limit_orders(chain: str | None = None) -> list[dict]:
    """Letto dal ciclo di esecuzione, non da un utente: nessun owner_id qui,
    scandisce tutti gli ordini aperti di tutti i wallet su questa chain."""
    with _lock, _connect() as conn:
        query = "SELECT * FROM limit_orders WHERE status = 'open'"
        params: list = []
        if chain is not None:
            query += " AND chain = ?"
            params.append(chain)
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def cancel_limit_order(order_id: str, owner_id: str) -> bool:
    """Mai una DELETE vera: un ordine cancellato resta nella cronologia,
    stesso principio delle discrepanze — non si butta via lo storico."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            "UPDATE limit_orders SET status = 'cancelled', updated_at = ? "
            "WHERE id = ? AND owner_id = ? AND status = 'open'",
            (time.time(), order_id, owner_id),
        )
        return cur.rowcount > 0


def mark_limit_order_filled(order_id: str, idempotency_key: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE limit_orders SET status = 'filled', filled_idempotency_key = ?, updated_at = ? WHERE id = ?",
            (idempotency_key, time.time(), order_id),
        )


def mark_limit_order_failed(order_id: str, reason: str) -> None:
    """Non 'filled' e non piu' 'open': un fallimento del check (non del
    guardrail — quello lo riprova al giro dopo) chiude l'ordine invece di
    ritentarlo all'infinito con lo stesso errore."""
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE limit_orders SET status = 'failed', reason = ?, updated_at = ? WHERE id = ?",
            (reason, time.time(), order_id),
        )
