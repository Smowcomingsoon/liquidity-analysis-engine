"""Top 15/25/50: the best projects we know, by score (main.onchain_top)."""
import asyncio
import time

from app import main


def _board():
    return [{"chain": "solana", "token_address": f"B{i}", "symbol": f"B{i}", "score": s} for i, s in enumerate([70, 90, 80])]


def _row(token, score, **kw):
    now = time.time()
    return {"chain": "solana", "token": token, "symbol": token, "name": token, "score": score,
            "analyzed_at": now, "overall": {}, "status": "studied", **kw}


def _search(rows):
    """Like projects_db.search: only the rows with the status asked for."""
    return lambda *a, **k: {"items": [r for r in rows if not k.get("status") or r["status"] == k["status"]]}


def test_best_by_score_board_and_studied(monkeypatch):
    monkeypatch.setattr(main.settings, "onchain_enabled", True)
    monkeypatch.setattr(main.onchain_leaderboard, "board", lambda: {"entries": _board()})
    monkeypatch.setattr(main.onchain_projects_db, "search",
                        lambda *a, **k: {"items": [_row("D0", 99)]})
    monkeypatch.setattr(main.onchain_judge, "get", lambda *a, **k: None)
    monkeypatch.setattr(main.onchain_leaderboard, "final_score", lambda e: 99)   # rescored with today's formula
    out = asyncio.run(main.onchain_top(n=15, user=None))
    assert [e["symbol"] for e in out["entries"]] == ["D0", "B1", "B2", "B0"]
    assert out["on_board"] == 3
    assert [e["on_board"] for e in out["entries"]] == [False, True, True, True]
    assert len(asyncio.run(main.onchain_top(n=2, user=None))["entries"]) == 2


def test_vetoed_rejected_and_stale_are_absent(monkeypatch):
    monkeypatch.setattr(main.settings, "onchain_enabled", True)
    monkeypatch.setattr(main.onchain_leaderboard, "board", lambda: {"entries": _board()})
    old = time.time() - 40 * 86400
    monkeypatch.setattr(main.onchain_projects_db, "search", _search([
        _row("V0", 99), _row("R0", 98, status="rejected"), _row("S0", 97, analyzed_at=old)]))
    monkeypatch.setattr(main.onchain_judge, "get",
                        lambda chain, token: {"verdict": "avoid", "confidence": 0.9} if token == "V0" else None)
    out = asyncio.run(main.onchain_top(n=15, user=None))
    assert [e["symbol"] for e in out["entries"]] == ["B1", "B2", "B0"]
    assert out["on_board"] == 3
