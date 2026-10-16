"""Laboratorio di backtest: ogni token passato dal diario degli esiti viene
ricontrollato nei momenti morti, con tutte le strategie, e il risultato resta
in un database SQLite. GeckoTerminal e' condiviso (piano gratuito, il fondo
usa fino a 14 chiamate al minuto): il laboratorio prende solo le chiamate
rimaste libere."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import zlib


from app import http_client

from app.providers import dexscreener
from app.sniper.scanner import GECKO_NETWORKS
from app.trading.onchain import alert_track, outcomes, priority, system
from app.trading.onchain.storage import data_file

logger = logging.getLogger(__name__)

_DB_FILE = data_file("backtest_lab.sqlite")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_conn_path = None

VERSION = 3
MATURE_S = 48 * 3600
RUG_SETTLED_S = 3600
LIMIT = 1000
PER_TICK = 3
TICK_S = 60
SPARE_BELOW = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab (
    key TEXT PRIMARY KEY,
    chain TEXT, token TEXT, pair TEXT, symbol TEXT, decision TEXT,
    at REAL, fetched_at REAL, bars BLOB, result TEXT, version INTEGER
);
"""


def _db() -> sqlite3.Connection:
    global _conn, _conn_path
    if _conn is None or _conn_path != _DB_FILE:
        _conn = sqlite3.connect(_DB_FILE, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _conn_path = _DB_FILE
    return _conn


def done_count() -> int:
    """Quanti token sono gia' stati analizzati con la versione corrente."""
    return _db().execute("SELECT COUNT(*) FROM lab WHERE version = ?", (VERSION,)).fetchone()[0]


def queue(now: float | None = None) -> list[tuple[str, dict]]:
    """(key, entry) dei token maturi, con rete nota e non ancora analizzati
    con questa versione. Prima gli alert, poi i piu' recenti."""
    now = time.time() if now is None else now
    done = {r[0] for r in _db().execute("SELECT key FROM lab WHERE version = ?", (VERSION,))}
    items: list[tuple[str, dict]] = []
    for key, entry in outcomes.entries().items():
        if key in done:
            continue
        at = entry.get("at")
        # a rug's outcome is known: its candles are complete an hour after it
        rug_at = entry.get("rug_at") if entry.get("rug") else None
        if not at or (now - rug_at < RUG_SETTLED_S if rug_at else now - at < MATURE_S):
            continue
        if not GECKO_NETWORKS.get(entry.get("chain")):
            continue
        items.append((key, entry))
    # alerts, then rugs (Lorenzo, 29/09: "documentare tutti i rug [...] anche
    # quelli morti"), then the most recent
    items.sort(key=lambda kv: ("alert" not in (kv[1].get("source") or ""), not kv[1].get("rug"),
                               -kv[1].get("at", 0)))
    return items


def path(df) -> dict | None:
    """Il percorso del prezzo: quanto ha moltiplicato al massimo e al minimo,
    quando e' arrivato il massimo, dove e' finito."""
    if df is None or len(df) < 2:
        return None
    first = float(df["close"].iloc[0])
    if not first:
        return None
    hi = df["high"].astype(float)
    lo = df["low"].astype(float)
    peak = int(hi.to_numpy().argmax())
    try:
        peak_min = int((df.index[peak] - df.index[0]).total_seconds() // 60)
    except Exception:
        peak_min = peak
    return {
        "max_mult": float(hi.max()) / first,
        "min_mult": float(lo.min()) / first,
        "peak_min": peak_min,
        "last_mult": float(df["close"].iloc[-1]) / first,
        "bars": int(len(df)),
    }


def analyze(bars: list, at: float, known: dict | None = None) -> dict:
    """Backtest di tutte le strategie sulle barre dalla proposta in poi, piu'
    il percorso del prezzo. `known` e' il rug gia' visto on-chain, se c'e'."""
    after = [b for b in bars if b[0] >= at]
    if len(after) < 2:
        return {"backtests": None, "path": None}
    return {"backtests": system.backtest_all(after, known), "path": path(system.frame(after))}


def _store(key: str, entry: dict, bars: bytes, result: dict) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO lab (key, chain, token, pair, symbol, decision, at, fetched_at, bars, result, version)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, entry.get("chain"), entry.get("token_address"), entry.get("pair_address"),
             entry.get("symbol"), entry.get("decision"), entry.get("at"), time.time(),
             bars, json.dumps(result), VERSION),
        )
        _db().commit()


async def fill(now: float | None = None, per_tick: int = PER_TICK) -> int:
    """Analizza fino a per_tick token del diario, se restano chiamate libere
    a GeckoTerminal. Ritorna quante righe ha scritto."""
    if priority.backtests_first():
        return 0
    if dexscreener.gecko_load() >= SPARE_BELOW:
        return 0
    items = queue(now)
    if not items:
        return 0
    stored = 0
    async with http_client.pooled(timeout=15) as client:
        for key, entry in items[:per_tick]:
            net = GECKO_NETWORKS.get(entry.get("chain"))
            if not net:
                continue
            got = await alert_track._candles(
                client, net, entry.get("pair_address") or "", "minute", 5,
                (entry.get("at") or 0) + LIMIT * 300, LIMIT,
                entry.get("token_address") or "",
            )
            if got is None:
                continue
            if not got:
                _store(key, entry, zlib.compress(b"[]"), {"gone": True})
                stored += 1
                continue
            j = outcomes.entries().get(f"{entry.get('chain')}:{entry.get('token_address')}")
            known = ({"at": j["rug_at"], "why": j.get("rug_why") or ""}
                     if j and j.get("rug") and j.get("rug_at") else None)
            result = await asyncio.to_thread(analyze, got, entry.get("at") or 0, known)
            _store(key, entry, zlib.compress(json.dumps(got).encode()), result)
            stored += 1
    return stored


def get(key: str) -> dict | None:
    row = _db().execute("SELECT * FROM lab WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["result"] = json.loads(out["result"]) if out["result"] else None
    except Exception:
        out["result"] = None
    return out


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def summary() -> dict:
    """Cosa dice il laboratorio: per ogni decisione, il ritorno mediano del
    miglior backtest, il buy-and-hold mediano, quanti hanno raddoppiato e
    quanti si sono rivelati rug."""
    rows = [dict(r) for r in _db().execute("SELECT * FROM lab")]
    known = outcomes.entries()
    gone = 0
    with_trades = 0
    by_decision: dict[str, dict] = {}
    for row in rows:
        try:
            result = json.loads(row["result"]) if row["result"] else {}
        except Exception:
            result = {}
        if result.get("gone"):
            gone += 1
        bt = result.get("backtests") or {}
        trades = [t for t in bt.get("rows") or [] if t.get("trades") and t.get("return_pct") is not None]
        if trades:
            with_trades += 1
        decision = row.get("decision") or "?"
        bucket = by_decision.setdefault(decision, {"n": 0, "best": [], "hold": [], "doubled": 0, "rugs": 0, "known": 0})
        bucket["n"] += 1
        if trades:
            best = max(t["return_pct"] for t in trades)
            bucket["best"].append(best)
        if bt.get("hold_pct") is not None:
            bucket["hold"].append(bt["hold_pct"])
        p = result.get("path") or {}
        if p.get("max_mult") is not None and p["max_mult"] >= 2:
            bucket["doubled"] += 1
        entry = known.get(row["key"])
        if entry is not None:
            bucket["known"] += 1
            if outcomes.outcome(entry) == "rug":
                bucket["rugs"] += 1
    out: dict[str, dict] = {}
    for decision, b in by_decision.items():
        n = b["n"] or 1
        out[decision] = {
            "n": b["n"],
            "best_median": _median(b["best"]),
            "hold_median": _median(b["hold"]),
            "doubled": b["doubled"] / n,
            "rugs": (b["rugs"] / b["known"]) if b["known"] else None,
        }
    return {"rows": len(rows), "gone": gone, "with_trades": with_trades, "by_decision": out}


async def lab_loop() -> None:
    await asyncio.sleep(120)
    while True:
        try:
            await fill()
        except Exception:
            logger.exception("backtest lab: fill fallito")
        await asyncio.sleep(TICK_S)
