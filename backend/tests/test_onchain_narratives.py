"""Narratives: map, matching, bonus, and how they feed discovery."""
import asyncio

import pytest

from app.trading.onchain import discovery as d, leaderboard as lb, narratives as nv


@pytest.fixture(autouse=True)
def _clean():
    d.reset_for_tests()
    lb.reset_for_tests()
    yield


def test_catalog_is_valid_and_rich():
    cat = nv.catalog()
    assert len(cat) >= 15
    keys = {n["key"] for n in cat}
    assert len(keys) == len(cat)
    for n in cat:
        assert 1 <= n["heat"] <= 3 and n["label"] and n["_re"]


def test_match_finds_core_narratives_and_ignores_plain_text():
    assert nv.match("We build zero-knowledge proofs for private payments")
    assert nv.match("An operating system for autonomous AI agents with x402 payments")
    assert nv.match("The best dog coin on Solana, to the moon") == []


def test_bonus_is_small_and_capped():
    keys = [n["key"] for n in nv.catalog()]
    assert 0 < nv.bonus(keys[:1]) <= nv.BONUS_MAX
    assert nv.bonus(keys) <= nv.BONUS_MAX and nv.bonus([]) == 0


def test_narrative_is_a_label_not_points():
    """07/10: a hot narrative no longer lifts the score (usage does)."""
    pillars = {"tokenomics": {"score": 70}}
    plain = {"chain": "solana", "token_address": "x", "overall": {"score": 70}, "pillars": pillars}
    hot = {**plain, "narratives": nv.match("zero-knowledge prover")}
    assert hot["narratives"] and lb.final_score(hot) == lb.final_score(plain)


def test_chain_index_and_narrative_pages(monkeypatch):
    idx = d.chain_index([{"id": "a", "platforms": {"solana": "M"}}, {"id": "b", "platforms": {"arbitrum": "0x"}},
                         {"id": "c", "platforms": {"robinhood": "0xc"}}])
    assert idx == {"solana": {"a"}, "robinhood": {"c"}, "base": set(), "ethereum": set()}
    calls = []

    async def fake_get(client, path, params=None):
        calls.append((path, (params or {}).get("category")))
        if path == "/coins/list":
            return [{"id": "zkp", "platforms": {"solana": "M1"}}, {"id": "eth-only", "platforms": {"arbitrum": "0x"}}]
        if path == "/coins/markets":
            return [{"id": "zkp", "symbol": "zkp", "name": "ZK Prover", "market_cap": 800_000},
                    {"id": "eth-only", "symbol": "eo", "name": "Eth Only", "market_cap": 800_000}]
        return {}

    monkeypatch.setattr(d, "_cg_get", fake_get)
    monkeypatch.setattr(d, "_save", lambda: None)
    now = 1_790_000_000.0
    for i in range(3):
        asyncio.run(d._cg_step(None, lambda *a: True, now + i * d.CG_GAP_S))
    assert calls[0] == ("/coins/list", None)
    assert calls[1][1] == nv.coingecko_categories()[0]
    assert ("coin", "solana") == d._state["cg_queue"][0][:2] and d._state["cg_queue"][0][2]["id"] == "zkp"
    assert not any(j[0] == "coin" and j[2]["id"] == "eth-only" for j in d._state["cg_queue"])
