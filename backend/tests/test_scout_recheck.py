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


def test_parse_defaults_unknown_statuses_to_confirmed():
    pts = sr.red_points(_entry())[:2]
    raw = json.dumps({"items": [{"i": 0, "status": "refuted", "note": "repo a/b found"}, {"i": 1, "status": "maybe"}]})
    out = sr.parse(raw, pts)
    assert [p["status"] for p in out] == ["refuted", "confirmed"] and out[0]["note"] == "repo a/b found"
    assert sr.parse("not json", pts) is None


def test_other_pages_guesses_docs_next_to_the_site():
    pages = sr.other_pages({"website": "https://www.hyperdex.io"}, {"https://hyperdex.io"})
    assert pages[0] == "https://docs.hyperdex.io" and "https://hyperdex.io/docs" in pages
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
                                     {"i": 2, "status": "confirmed", "note": "$3k"}, {"i": 3, "status": "unverified"},
                                     {"i": 4, "status": "refuted", "note": "docs read"}]})

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
