"""The second look at the Scout's red points (scout_recheck.py)."""
import asyncio
import json

from app.trading.onchain import legit, scout_recheck as sr


def _entry():
    return {"judge": {"problems": ["no public GitHub found", "team is anonymous", "liquidity is tiny"]},
            "pillars": {"github": {"score": None, "flags": ["no public GitHub"]},
                        "site": {"score": 60, "flags": ["content rendered via JavaScript"], "facts": {"url": "https://x.io"}}}}


def test_red_points_take_problems_and_flags_once():
    pts = [p["text"] for p in sr.red_points(_entry())]
    assert pts == ["no public GitHub found", "team is anonymous", "liquidity is tiny", "no public GitHub",
                   "content rendered via JavaScript"]


def test_heuristic_keeps_absences_unverified_and_a_found_github_clears_them():
    pts = sr.red_points(_entry())
    out = {p["text"]: p["status"] for p in sr.heuristic(pts, {"found": [], "pages": []})}
    assert out["no public GitHub"] == "unverified" and out["liquidity is tiny"] == "confirmed"
    out = {p["text"]: p["status"] for p in sr.heuristic(pts, {"found": ["GitHub found (x): https://github.com/a"], "pages": []})}
    assert out["no public GitHub found"] == "refuted"


def test_parse_defaults_unknown_and_missing_statuses_to_unverified():
    pts = sr.red_points(_entry())[:3]
    raw = json.dumps({"items": [{"i": 0, "status": "refuted", "note": "repo a/b found"}, {"i": 1, "status": "maybe"}]})
    out = sr.parse(raw, pts)
    assert [p["status"] for p in out] == ["refuted", "unverified", "unverified"] and out[0]["note"] == "repo a/b found"
    assert sr.parse("not json", pts) is None


def test_other_pages_guesses_docs_next_to_the_site():
    pages = sr.other_pages({"website": "https://www.hyperdex.io"}, {"https://hyperdex.io"})
    assert pages[0] == "https://docs.hyperdex.io" and "https://hyperdex.io/docs" in pages
    assert "https://hyperdex.io/llms.txt" in pages
    assert sr.other_pages({}, set()) == []


def test_recheck_uses_deepseek_on_the_evidence(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "deepseek_api_key", "k")
    monkeypatch.setattr(sr, "_cache", {})

    async def none(*a, **k):
        return None

    async def page(url, client):
        return {"ok": True, "final_url": url, "text": "docs " * 60} if "docs." in url else {"ok": False}

    monkeypatch.setattr(legit, "github_find", none)
    monkeypatch.setattr(sr, "_code_search", none)
    monkeypatch.setattr(legit, "fetch_site", page)
    seen = {}

    async def ask(messages, **kw):
        seen["user"] = messages[1]["content"]
        return json.dumps({"items": [{"i": 0, "status": "unverified"}, {"i": 1, "status": "unverified"},
                                     {"i": 2, "status": "confirmed", "note": "$3k", "evidence": "cap $0, liquidity $0"},
                                     {"i": 3, "status": "unverified"},
                                     {"i": 4, "status": "refuted", "note": "docs read", "evidence": "page read: https://docs.x.io"}]})

    e = _entry()
    rc = asyncio.run(sr.recheck(e, {"chain": "robinhood", "token_address": "0xA", "website": "https://x.io"}, None, ask=ask))
    assert [p["text"] for p in rc["confirmed"]] == ["liquidity is tiny"]
    assert len(rc["unverified"]) == 3 and rc["refuted"][0]["text"] == "content rendered via JavaScript"
    assert "page read: https://docs.x.io" in rc["found"] and "docs docs" in seen["user"]


def test_lock_flag_is_unverified_when_goplus_misses_the_main_pool(monkeypatch):
    async def gp(chain, token, client):
        return {"dex": [{"liquidity": "635.1"}, {"liquidity": "20"}]}

    monkeypatch.setattr(legit, "goplus", gp)
    note = asyncio.run(sr.lp_coverage({"chain": "robinhood", "token_address": "0xA", "liquidity_usd": 58_000}, None))
    assert "$655" in note and "$58,000" in note
    pts = [{"text": "only 0% of liquidity locked", "from": "tokenomics"}]
    assert sr.heuristic(pts, {"found": [note], "pages": []})[0]["status"] == "unverified"
    assert asyncio.run(sr.lp_coverage({"chain": "solana", "token_address": "M"}, None)) == ""


def test_evidence_carries_market_holders_site_and_team(monkeypatch):
    import time
    from app.trading.onchain import traction
    monkeypatch.setattr(traction, "lookup", lambda c, t: {"name": "Hydrex", "tvl": 1_200_000.0})
    monkeypatch.setattr(sr, "twin_line", lambda e, i: "")
    entry = {"pillars": {
        "tokenomics": {"facts": {"holders": 900, "contracts_pct": 33.0, "lp_locked_pct": 0.0,
                                 "top_holders": [{"pct": 20.0, "contract": True, "tag": "UniswapV3"}]}},
        "site": {"facts": {"url": "https://x.io", "words": 30, "rendered": True,
                           "excerpt": "Built by our team. Founder Jane Doe, ex-Aave."}}}}
    item = {"name": "X", "chain": "base", "token_address": "0xa", "created_at": time.time() - 6 * 86400,
            "volume_24h": 50_000, "liquidity_usd": 58_000}
    text = sr.evidence_text(entry, item, {"found": [], "pages": []})
    assert "pool age 6.0 days" in text and "24h volume $50,000" in text
    assert '"tvl": 1200000.0' in text
    assert "20.0% contract (UniswapV3)" in text and '"lp_locked_pct": 0.0' in text
    assert '"rendered": true' in text and "Founder Jane Doe" in text


def test_twin_with_the_same_website_refutes_the_copy_point(monkeypatch):
    from app.trading.onchain import projects_db
    monkeypatch.setattr(projects_db, "get", lambda c, t: {"name": "Hydrex", "symbol": "HYDX", "chain": "ethereum",
                                                           "website": "https://www.hydrex.fi", "description": "d"})
    entry = {"pillars": {"site": {"facts": {"evidence": {"twin": "ethereum:0xb", "twin_similarity": 1.0}}}}}
    item = {"website": "https://hydrex.fi/"}
    line = sr.twin_line(entry, item)
    assert "the same website" in line
    pts = [{"text": "Site text is 100% identical to another project", "from": "judge"}]
    out = sr.heuristic(pts, {"found": [line], "pages": []})
    assert out[0]["status"] == "refuted"


def test_an_anonymous_team_is_never_a_red_point():
    p = {"text": "Anonymous team with no identified builders", "from": "judge", "status": "confirmed", "note": "x"}
    assert sr.minor(p)["status"] == "unverified" and sr.minor(p)["note"].startswith("Minor")
    other = {**p, "text": "Mint enabled"}
    assert sr.minor(other)["status"] == "confirmed"


def test_parse_wants_a_real_quote_for_confirmed_and_refuted():
    pts = sr.red_points(_entry())[:3]
    ev = "Checked sources:\napp code: found — index.js: token address\nliquidity $4,000 against a $900,000 cap"
    raw = json.dumps({"items": [
        {"i": 0, "status": "refuted", "evidence": "app code: found — index.js: token address"},
        {"i": 1, "status": "confirmed", "evidence": "the team is hidden"},
        {"i": 2, "status": "confirmed", "evidence": ""}]})
    out = sr.parse(raw, pts, ev)
    assert [p["status"] for p in out] == ["refuted", "unverified", "unverified"]
    assert out[1]["note"] == "no evidence quoted"


def _client(routes):
    import httpx

    def handler(req):
        body = routes.get(str(req.url))
        if body is None:
            return httpx.Response(404, text="")
        if isinstance(body, dict):
            return httpx.Response(200, json=body)
        return httpx.Response(200, text=body, headers={"content-type": "application/javascript"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


ADDR = "0xAA5751Ff3Fb61e613993B2EB4EAb1fce017081fc"


def test_app_probe_reads_the_bundle_and_the_live_api():
    bundle = (f'const t="{ADDR.lower()}";fetch("/api/assets");'
              'const c="Trade tokenized stocks from Ondo and xStocks";')
    routes = {"https://dex.app/": '<html><script type="module" src="/assets/index-a1.js"></script></html>',
              "https://dex.app/assets/index-a1.js": bundle,
              "https://dex.app/api/assets": {"assets": ["x" * 2000]}}

    async def go():
        async with _client(routes) as c:
            return await sr.app_probe({"website": "https://dex.app/", "token_address": ADDR}, c)
    out = asyncio.run(go())
    assert "token contract address in the app code (index-a1.js)" in out["found"]
    assert any(f.startswith("live API /api/assets: 200") for f in out["found"])
    assert "Trade tokenized stocks from Ondo and xStocks" in out["snippets"]


def test_app_probe_never_raises_on_a_dead_bundle():
    routes = {"https://dex.app/": '<script src="/gone.js"></script>'}

    async def go():
        async with _client(routes) as c:
            return await sr.app_probe({"website": "https://dex.app/", "token_address": ADDR}, c)
    out = asyncio.run(go())
    assert out["found"] == [] and any(r["source"] == "app code" and r["result"] == "unreadable" for r in out["checked"])


def test_x_profile_finds_the_address_in_the_bio():
    routes = {"https://api.fxtwitter.com/hyperdex_app": {"user": {
        "followers": 1015, "tweets": 68, "joined": "Wed Sep 16 2026", "description": f"Marketplace {ADDR}"}}}

    async def go():
        async with _client(routes) as c:
            return await sr.x_profile({"token_address": ADDR,
                                       "socials": [{"type": "twitter", "url": "https://x.com/hyperdex_app"}]}, c)
    out = asyncio.run(go())
    assert "token address in the X bio" in out["found"] and out["checked"][0]["result"] == "found"


def test_measured_contract_and_x_beat_the_model():
    pts = [{"text": "No public GitHub and no contract link on the site (contract_link: false)", "from": "judge",
            "status": "confirmed", "note": ""},
           {"text": "No X or Telegram data provided", "from": "judge", "status": "unverified", "note": ""},
           {"text": "liquidity is tiny", "from": "judge", "status": "confirmed", "note": ""}]
    ev = {"found": ["token contract address in the app code (hyperToken.js)", "X: 1017 followers, 68 posts, joined x"]}
    out = sr.facts_win(pts, ev)
    assert [p["status"] for p in out] == ["refuted", "refuted", "confirmed"]
    assert out[0]["evidence"].startswith("token contract address")


def test_a_healthy_pool_is_not_thin():
    pts = [{"text": "Only 6 days old with $48k liquidity: extremely early and thin", "from": "judge",
            "status": "confirmed", "note": ""}]
    out = sr.facts_win(pts, {"found": []}, {"liquidity_usd": 49_600, "market_cap": 262_000})
    assert out[0]["status"] == "unverified" and "19%" in out[0]["note"]
    out = sr.facts_win(pts, {"found": []}, {"liquidity_usd": 5_000, "market_cap": 262_000})
    assert out[0]["status"] == "confirmed"


def test_an_address_found_does_not_clear_an_audit_point():
    pts = [{"text": "No contract audit published", "from": "judge", "status": "confirmed", "note": ""}]
    out = sr.facts_win(pts, {"found": ["token contract address in the app code (a.js)"]})
    assert out[0]["status"] == "confirmed"


def test_github_by_name_wants_the_readme_to_name_the_site():
    routes = {
        "https://api.github.com/search/repositories?q=pfwa&per_page=5": {"items": [
            {"full_name": "a/pfwa", "html_url": "https://github.com/a/pfwa", "fork": False},
            {"full_name": "b/pfwa", "html_url": "https://github.com/b/pfwa", "fork": False}]},
        "https://api.github.com/repos/a/pfwa/readme": "nothing here",
        "https://api.github.com/repos/b/pfwa/readme": "The contracts behind pfwa.fun",
    }

    async def go():
        async with _client(routes) as c:
            return await sr.github_by_name({"symbol": "PFWA", "website": "https://www.pfwa.fun"}, c)
    out = asyncio.run(go())
    assert out == ("https://github.com/b/pfwa", "repo whose README names the site")


def test_github_by_name_returns_none_on_a_403():
    routes = {"https://api.github.com/search/repositories?q=pfwa&per_page=5": None}

    async def go():
        async with _client(routes) as c:
            return await sr.github_by_name({"symbol": "PFWA", "website": "https://www.pfwa.fun"}, c)
    assert asyncio.run(go()) is None


def test_facts_win_refutes_missing_github_and_zero_volume():
    pts = [{"text": "No public GitHub", "from": "judge", "status": "confirmed", "note": ""},
           {"text": "24h volume $0", "from": "judge", "status": "confirmed", "note": ""}]
    ev = {"found": ["GitHub found (repo whose README names the site): https://github.com/a/b",
                    "Cross-check (GeckoTerminal): 24h volume $30,000, pools reserve $50,000"],
          "gecko": {"volume_24h": 30000.0, "reserve_usd": 50000.0}}
    out = sr.facts_win(pts, ev)
    assert [p["status"] for p in out] == ["refuted", "refuted"]
    assert out[0]["evidence"].startswith("GitHub found")
    assert out[1]["evidence"].startswith("Cross-check (GeckoTerminal)")


def test_facts_win_no_team_with_site_code_and_x_is_unverified():
    pts = [{"text": "No team information", "from": "judge", "status": "confirmed", "note": ""},
           {"text": "No socials", "from": "judge", "status": "confirmed", "note": ""}]
    ev = {"found": ["GitHub found (repo whose README names the site): https://github.com/a/b",
                    "X: @pfwafun, 900 followers"]}
    out = sr.facts_win(pts, ev)
    assert [p["status"] for p in out] == ["unverified", "refuted"]


def test_zero_volume_stays_when_geckoterminal_agrees():
    pts = [{"text": "24h volume $0", "from": "judge", "status": "confirmed", "note": ""}]
    out = sr.facts_win(pts, {"found": [], "gecko": {"volume_24h": 0.0, "reserve_usd": 900.0}})
    assert out[0]["status"] == "confirmed"


def test_rules_reach_every_review_and_name_each_chain_lockers():
    from app.trading.onchain import deep_recheck, judge
    for system in (judge.REVIEW_SYSTEM, deep_recheck.SYSTEM, sr.SYSTEM):
        assert "never call a chain empty" in system and "HoodLock" in system and "Streamflow" in system
    lines = sr.market_lines({}, {"chain": "solana", "token_address": "x"})
    assert any(line.startswith("LP lock data (GoPlus): does not cover solana; lockers used there: Streamflow") for line in lines)
