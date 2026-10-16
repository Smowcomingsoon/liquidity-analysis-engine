"""La prova giornaliera della scala dei time frame, con avviso su Telegram.

Lorenzo, 01/10, dopo aver lasciato la scala spenta: "ogni giorno rifai un
po' di test e mi mandi dei messaggi su telegram se effettivamente migliorano
i risultati".

Una volta al giorno, sui token della Top con un backtest salvato: il trend
follower con la regola in uso (1d, poi 8h) contro la scala
(strategy_lab.run_daily(rule=True)), e RSI+BB sui 5 minuti di oggi contro la
stessa scala. Legge solo l'archivio delle candele (candle_archive.offline):
nessuna chiamata a GeckoTerminal, nessun risultato del laboratorio toccato.
Lo storico va in data/ladder_daily.json.

"Migliora davvero" e' piu' severo del libro da $10k, perche' il 01/10 il
libro raddoppiava grazie a due token (EQUI +2428%, YOYO +1740%) mentre sul
resto la scala perdeva: servono libro, media senza i 3 migliori e non meno
token migliorati che peggiorati. Solo allora parte il messaggio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from datetime import datetime, timezone


from app import http_client

from app.atomic_write import write_text_atomic
from app.sniper.scanner import GECKO_NETWORKS
from app.trading.onchain import candle_archive, strategy_lab
from app.trading.onchain.storage import data_file

logger = logging.getLogger(__name__)

_FILE = data_file("ladder_daily.json")
LIMIT = 50
RUN_HOUR_UTC = 6          # 8:00 in Italy
POLL_S = 1800
KEEP = 90
TRIM = 3


def _ret(d: dict | None) -> float:
    if not d or d.get("error"):
        return 0.0
    row = next(iter(d.get("rows") or []), {}) or {}
    return float(row.get("return_pct") or 0.0)


def _trades(d: dict | None) -> int:
    row = next(iter((d or {}).get("rows") or []), {}) or {}
    return int(row.get("trades") or 0)


def verdict(pairs: list[tuple[float, float]]) -> dict:
    """(today, ladder) returns per token, in %: does the ladder really do better?"""
    n = len(pairs)
    if not n:
        return {"tokens": 0, "better": False}
    book = lambda i: round(sum(10_000 / n * (1 + p[i] / 100) for p in pairs), 2)  # noqa: E731
    trimmed = lambda i: (statistics.mean(sorted(p[i] for p in pairs)[:-TRIM])  # noqa: E731
                         if n > TRIM else statistics.mean(p[i] for p in pairs))
    up = sum(1 for a, b in pairs if b > a)
    down = sum(1 for a, b in pairs if b < a)
    out = {"tokens": n, "book_old": book(0), "book_new": book(1),
           "trimmed_old": round(trimmed(0), 2), "trimmed_new": round(trimmed(1), 2), "up": up, "down": down}
    out["better"] = (out["book_new"] >= out["book_old"] and out["trimmed_new"] >= out["trimmed_old"]
                     and up >= down and up > 0)
    return out


async def run(now: float | None = None) -> dict:
    now = now or time.time()
    items = sorted(((k, v) for k, v in strategy_lab._results.items()
                    if v.get("pair") and (v.get("daily") or {}).get("rows")),
                   key=lambda kv: -(kv[1].get("at") or 0))[:LIMIT]
    trend, rsi = [], []
    token_off = candle_archive.offline.set(True)
    try:
        async with http_client.pooled(timeout=20.0) as client:
            for key, v in items:
                chain, token = key.split(":", 1)
                net = GECKO_NETWORKS.get(chain)
                if not net:
                    continue
                old = await strategy_lab.run_daily(client, net, v["pair"], token, now, rule=False)
                new = await strategy_lab.run_daily(client, net, v["pair"], token, now, rule=True)
                if old is not None and new is not None:
                    trend.append((v.get("symbol"), _ret(old), _ret(new), _trades(new)))
                rsi_row = next((r for r in (v.get("backtests") or {}).get("rows") or []
                                if r.get("label") == "RSI+BB"), None)
                rsi_new = await strategy_lab.run_daily(client, net, v["pair"], token, now,
                                                       strategies=strategy_lab.RSI_BB, rule=True)
                if rsi_row is not None and rsi_new is not None:
                    rsi.append((v.get("symbol"), float(rsi_row.get("return_pct") or 0.0), _ret(rsi_new),
                                _trades(rsi_new)))
                await asyncio.sleep(0)
    finally:
        candle_archive.offline.reset(token_off)
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    return {"day": day, "at": now,
            "trend": verdict([(a, b) for _, a, b, _ in trend]),
            "rsi_bb": verdict([(a, b) for _, a, b, _ in rsi]),
            "top_trend": sorted(((s, b - a) for s, a, b, _ in trend), key=lambda x: -abs(x[1]))[:3]}


def text_it(res: dict) -> str | None:
    """Italian, only when the ladder really does better on one of the two."""
    parts = []
    for key, name in (("trend", "trend follower"), ("rsi_bb", "RSI+BB")):
        v = res.get(key) or {}
        if v.get("better"):
            parts.append(f"• <b>{name}</b>: portafoglio $10k {v['book_old']:,.0f} → {v['book_new']:,.0f}, "
                         f"media senza i 3 migliori {v['trimmed_old']:+.1f}% → {v['trimmed_new']:+.1f}%, "
                         f"meglio su {v['up']} token, peggio su {v['down']}".replace(",", "."))
    if not parts:
        return None
    return ("📊 <b>Time frame dinamici: la scala migliora i risultati</b>\n"
            + "\n".join(parts)
            + "\nLa regola e' ancora spenta: dimmi tu se accenderla.")


def _history() -> list[dict]:
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def due(now: float, history: list[dict]) -> bool:
    t = datetime.fromtimestamp(now, tz=timezone.utc)
    return t.hour >= RUN_HOUR_UTC and not any(h.get("day") == t.strftime("%Y-%m-%d") for h in history)


async def tick(now: float | None = None) -> dict | None:
    from app.notify import telegram
    now = now or time.time()
    history = _history()
    if not due(now, history):
        return None
    res = await run(now)
    history = (history + [res])[-KEEP:]
    try:
        write_text_atomic(_FILE, json.dumps(history, separators=(",", ":")), encoding="utf-8")
    except OSError:
        logger.exception("ladder watch: storico non scritto")
    text = text_it(res)
    if text:
        await telegram.send_message(text)
    return res


async def watch_loop() -> None:
    await asyncio.sleep(600)       # after the lab has loaded its results
    while True:
        try:
            await tick()
        except Exception:
            logger.exception("ladder watch: giro fallito")
        await asyncio.sleep(POLL_S)
