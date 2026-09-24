"""Scan dei progetti seri: filtri su CoinGecko e DexScreener, senza rete."""
import asyncio

import pytest

from app.trading.onchain import depth, discovery as d


@pytest.fixture(autouse=True)
def _clean():
    d.reset_for_tests()
    d.legit._found.clear()
    yield
    d.reset_for_tests()


def test_market_candidates_keeps_cap_band_and_drops_meme_names():
    rows = [{"id": "big", "symbol": "big", "name": "Big", "market_cap": 9_000_000},
            {"id": "tiny", "symbol": "tiny", "name": "Tiny", "market_cap": 10_000},
            {"id": "dog", "symbol": "doge2", "name": "Doge Inu", "market_cap": 500_000},
            {"id": "grid", "symbol": "grid", "name": "Grid Protocol", "market_cap": 900_000}]
    assert [c["id"] for c in d.market_candidates("robinhood", rows)] == ["grid"]


def _coin(**kw):
    coin = {"symbol": "grid", "name": "Grid Protocol", "platforms": {"robinhood": "0xabc"},
            "categories": ["Robinhood Ecosystem", "Artificial Intelligence (AI)"],
            "links": {"homepage": ["https://grid.xyz"], "repos_url": {"github": ["https://github.com/grid/core"]},
                      "twitter_screen_name": "grid"},
            "description": {"en": "Inference network"}, "genesis_date": "2026-07-01"}
    coin.update(kw)
    return coin


def test_coin_detail_builds_item():
    item, why = d.from_coin_detail("robinhood", _coin(), {"symbol": "GRID", "market_cap": 900_000})
    assert why == "" and item["token_address"] == "0xabc" and item["github"] == "https://github.com/grid/core"
    assert item["website"] == "https://grid.xyz" and item["launched_at"]


def test_coin_detail_rejects_meme_stock_and_missing_contract():
    assert d.from_coin_detail("robinhood", _coin(categories=["Robinhood Chain Meme"]))[1].startswith("category")
    assert d.from_coin_detail("robinhood", _coin(categories=["Tokenized Stocks"]))[1].startswith("category")
    assert d.from_coin_detail("solana", _coin())[1] == "no contract on this chain"


def test_coin_detail_without_github_goes_to_site_analysis():
    item, _ = d.from_coin_detail("robinhood", _coin(links={"homepage": ["https://grid.xyz"]}))
    assert item and item["github"] is None


def test_profiles_keep_own_site_on_our_chains():
    profiles = [
        {"chainId": "solana", "tokenAddress": "M1", "description": "ZK proving network",
         "links": [{"label": "Website", "url": "https://prover.io"}, {"type": "github", "url": "https://github.com/p/r"}]},
        {"chainId": "base", "tokenAddress": "M2", "links": [{"label": "Website", "url": "https://x.io"}]},
        {"chainId": "solana", "tokenAddress": "M3", "description": "the funniest meme on solana",
         "links": [{"label": "Website", "url": "https://lol.io"}]},
        {"chainId": "solana", "tokenAddress": "M4", "links": [{"type": "twitter", "url": "https://x.com/a"}]},
    ]
    out = d.profile_candidates(profiles)
    assert [c["token_address"] for c in out] == ["M1"] and out[0]["github"] == "https://github.com/p/r"


def test_step_queues_pages_then_details(monkeypatch):
    calls = []

    async def fake_get(client, path, params=None):
        calls.append(path)
        if path == "/coins/markets":
            return [{"id": "grid", "symbol": "grid", "name": "Grid Protocol", "market_cap": 900_000}]
        return _coin()

    monkeypatch.setattr(d, "_cg_get", fake_get)
    monkeypatch.setattr(d, "_save", lambda: None)
    added = []
    add = lambda item, source: added.append((item["symbol"], source)) or True  # noqa: E731
    now = 1_790_000_000.0
    asyncio.run(d._cg_step(None, add, now))                 # prepara il giro
    asyncio.run(d._cg_step(None, add, now + 1))             # troppo presto
    assert calls == []
    for i in range(1, 12):
        asyncio.run(d._cg_step(None, add, now + i * d.CG_GAP_S))
    assert ("GRID", "coingecko") in added
    assert d.status()["found"] >= 1


def test_github_of_another_project_is_fatal(monkeypatch):
    async def repo(owner, name, client):
        return {"full_name": "alice/rust-utils", "homepage": "", "description": "misc helpers"}

    monkeypatch.setattr(depth.legit, "github_repo", repo)
    out = asyncio.run(depth.analyze_github(["https://github.com/alice/rust-utils"], None, None,
                                           token={"chain": "solana", "token_address": "M", "symbol": "GRID",
                                                  "name": "Grid Protocol"}, website="https://grid.xyz"))
    assert out["fatal"] == "GitHub does not belong to the project"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Client:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    async def get(self, url, **kw):
        self.urls.append(url)
        return next((r for k, r in self.routes.items() if k in url), _Resp(404, {}))


def test_github_found_by_account_declaring_the_site():
    c = _Client({"/users/gridarena": _Resp(200, {"blog": "gridarena.xyz"})})
    assert asyncio.run(d.legit.github_find("https://www.gridarena.xyz/", c)) == "https://github.com/gridarena"


def test_github_found_by_repo_homepage_only_on_exact_domain():
    items = [{"html_url": "https://github.com/x/other", "homepage": "https://gridarena.xyz.evil.io"},
             {"html_url": "https://github.com/y/arena", "homepage": "https://gridarena.xyz"}]
    c = _Client({"/search/repositories": _Resp(200, {"items": items})})
    assert asyncio.run(d.legit.github_find("https://gridarena.xyz", c)) == "https://github.com/y/arena"
    d.legit._found.clear()
    c = _Client({"/search/repositories": _Resp(200, {"items": items[:1]})})
    assert asyncio.run(d.legit.github_find("https://gridarena.xyz", c)) is None


def test_github_lookup_is_cached_but_not_when_github_fails():
    c = _Client({"/users/gridarena": _Resp(403, {})})
    assert asyncio.run(d.legit.github_find("https://gridarena.xyz", c)) is None
    c = _Client({"/users/gridarena": _Resp(200, {"blog": "https://gridarena.xyz"})})
    assert asyncio.run(d.legit.github_find("https://gridarena.xyz", c)) == "https://github.com/gridarena"
    c = _Client({})
    assert asyncio.run(d.legit.github_find("https://gridarena.xyz", c)) == "https://github.com/gridarena"
    assert c.urls == []


MINT = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
EVM = "0xDEd852De9fe9bA9b6f27f39e8e81cF851a5c79cC"


def test_readme_addresses_skips_words_and_known_programs():
    text = (f"## Token\nCA: `{MINT}`\nUses So11111111111111111111111111111111111111112 and "
            f"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA. Robinhood: {EVM}\n"
            "abcdefghijkmnopqrstuvwxyzabcdefghijk")
    out = d.readme_addresses(text)
    assert out == {"solana": [MINT], "robinhood": [EVM]}


def test_search_candidates_keeps_fresh_own_repos():
    now = 1_790_000_000.0
    items = [{"full_name": "a/live", "size": 900, "pushed_at": "2026-09-20T10:00:00Z"},
             {"full_name": "a/fork", "size": 900, "fork": True, "pushed_at": "2026-09-20T10:00:00Z"},
             {"full_name": "a/stale", "size": 900, "pushed_at": "2025-01-01T10:00:00Z"},
             {"full_name": "a/empty", "size": 0, "pushed_at": "2026-09-20T10:00:00Z"}]
    assert [c["full_name"] for c in d.search_candidates(items, now)] == ["a/live"]


def _pair(addr, cap, liq=20_000, chain="solana", symbol="PRVR", name="Prover Network"):
    return {"chainId": chain, "baseToken": {"address": addr, "symbol": symbol, "name": name},
            "marketCap": cap, "liquidity": {"usd": liq}, "pairAddress": "pool", "pairCreatedAt": 1_785_000_000_000,
            "info": {"websites": [{"url": "https://prover.io"}]}}


def test_token_from_pairs_picks_liquid_pool_in_band():
    repo = {"html_url": "https://github.com/p/r", "homepage": ""}
    item, why = d.token_from_pairs("solana", [_pair(MINT, 800_000, 5_000), _pair(MINT, 900_000, 50_000)], repo)
    assert why == "" and item["market_cap"] == 900_000 and item["github_verified"]
    assert item["website"] == "https://prover.io" and item["launched_at"] == 1_785_000_000
    assert d.token_from_pairs("solana", [_pair(MINT, 9_000_000)], repo)[1].startswith("cap")
    assert d.token_from_pairs("solana", [_pair(MINT, 500_000, symbol="PEPE", name="Pepe Inu")], repo)[1] == "meme name"


def test_repo_readme_to_leaderboard():
    d._state["gh_queue"].append({"full_name": "p/r", "html_url": "https://github.com/p/r", "homepage": "",
                                 "default_branch": "main"})
    c = _Client({"raw.githubusercontent.com/p/r/main/README.md": _Resp(200, {}),
                 "tokens/v1/solana/": _Resp(200, [_pair(MINT, 700_000)])})
    c.routes["raw.githubusercontent.com/p/r/main/README.md"].text = f"Our token: {MINT}"
    added = []
    asyncio.run(d._gh_repo(c, lambda item, src: added.append((item["token_address"], src)) or True, 1_790_000_000.0))
    assert added == [(MINT, "github")] and d._seen["gh:p/r"]["ok"]


def test_verified_readme_skips_ownership_check(monkeypatch):
    async def repo(owner, name, client):
        return {"full_name": "alice/rust-utils", "homepage": "", "description": "misc"}

    async def fetch(full, client):
        return {}

    monkeypatch.setattr(depth.legit, "github_repo", repo)
    monkeypatch.setattr(depth, "fetch_github", fetch)
    out = asyncio.run(depth.analyze_github(["https://github.com/alice/rust-utils"], None, None,
                                           token={"chain": "solana", "token_address": "M", "symbol": "GRID",
                                                  "github_verified": True}, website="https://grid.xyz"))
    assert out["fatal"] != "GitHub does not belong to the project"


def test_github_rate_limit_is_recorded():
    d.legit.github_rate.clear()
    r = _Resp(200, {})
    r.headers = {"x-ratelimit-limit": "60", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1790000000",
                 "x-ratelimit-resource": "core"}
    asyncio.run(d.legit.gh_get(_Client({"/x": r}), "https://api.github.com/x"))
    assert d.legit.github_rate["core"]["limit"] == 60 and d.legit.github_rate["core"]["remaining"] == 0
