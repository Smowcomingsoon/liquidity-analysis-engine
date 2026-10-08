"""Lo scanner deve popolare ogni narrativa: categoria -> chiave, hint che
sopravvive fino alla bacheca, merge con l'hint per primo, conteggio dal db."""
from __future__ import annotations

import asyncio
import time

from app.trading.onchain import discovery, leaderboard, narratives, projects_db


def test_keys_for_category_finds_own_key():
    gems = [n for n in narratives.catalog() if n["kind"] == "gem" and n.get("coingecko")]
    assert gems, "nessuna narrativa gem con categoria CoinGecko"
    n = gems[0]
    cat = n["coingecko"][0]
    keys = narratives.keys_for_category(cat)
    assert n["key"] in keys
    assert all(k in {g["key"] for g in gems} for k in keys)


def test_merge_narratives_keeps_hint_first():
    assert leaderboard.merge_narratives(["a"], ["b"], ["c"]) == ["a", "b", "c"]
    assert leaderboard.merge_narratives(["a"], ["a", "b"], []) == ["a", "b"]
    assert leaderboard.merge_narratives(None, None, None) == []


def test_coin_step_carries_hint_to_leaderboard(monkeypatch):
    """Un item che passa dallo step coin con un hint arriva in bacheca con
    narratives_hint, e _analyze lo mette davanti agli altri."""
    leaderboard._entries.clear()
    leaderboard._gone.clear()

    async def fake_get(client, path, params=None):
        return {"id": "x", "symbol": "x", "name": "X", "market_data": {"market_cap": {"usd": 1_000_000}}}

    monkeypatch.setattr(discovery, "_cg_get", fake_get)
    monkeypatch.setattr(discovery, "from_coin_detail",
                        lambda chain, coin, base: ({"chain": chain, "token_address": "tok", "symbol": "X",
                                                    "name": "X", "market_cap": 1_000_000}, ""))
    monkeypatch.setattr(discovery.projects_db, "mark_detail", lambda *a, **k: None)
    monkeypatch.setattr(discovery.projects_db, "reject_cg", lambda *a, **k: None)
    monkeypatch.setattr(discovery.projects_db, "upsert", lambda *a, **k: None)

    discovery._state["cg_queue"] = [("coin", "solana", {"id": "x", "narratives_hint": ["privacy"]})]
    discovery._state["cg_next_at"] = 0.0
    asyncio.run(discovery._cg_step(None, leaderboard.add, time.time()))

    e = leaderboard._entries[leaderboard._key("solana", "tok")]
    assert e["narratives_hint"] == ["privacy"]


def test_narrative_counts_from_db(tmp_path, monkeypatch):
    monkeypatch.setattr(projects_db, "_DB_FILE", tmp_path / "projects.db")
    now = time.time()
    for tok, keys in (("t1", ["privacy", "ai_agents"]), ("t2", ["privacy"])):
        e = {"chain": "solana", "token_address": tok, "symbol": tok.upper(), "name": tok, "narratives": keys}
        projects_db.upsert(e, "test", now)
        projects_db.record(e, "ranked")
    counts = projects_db.narrative_counts()
    assert counts.get("privacy") == 2
    assert counts.get("ai_agents") == 1


def test_shared_category_gives_no_hint():
    """"depin" is swept for Compute and DePIN alike: it cannot say which one."""
    assert set(narratives.keys_for_category("depin")) == {"depin", "decentralized_compute"}
    assert narratives.keys_for_category("depin", exclusive=True) == []
    assert narratives.keys_for_category("bittensor-subnets", exclusive=True) == ["decentralized_compute"]


def test_compute_text_matches():
    for text in ("Rent out your idle GPUs to AI teams", "A decentralized rendering network for 3D artists",
                 "Permissionless cloud for containers", "GPU-as-a-service for inference"):
        assert "decentralized_compute" in narratives.match(text), text
    assert "decentralized_compute" not in narratives.match("Helium hotspot operators earn for coverage")
