"""Il filtro di legittimita'. Lorenzo: "voglio evitare rug pull [...] voglio
token legittimi con progetti seri", e poi "fai un controllo anche per github
[...] voglio evitare SHITCOIN e MEMECOIN"."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.trading.onchain import legit


def test_launchpad_and_social_pages_are_not_a_website():
    assert legit.pick_website(["https://pump.fun/coin/X", "https://linktr.ee/x", "https://github.com/x"]) is None
    assert legit.pick_website(["https://x.com/p", "https://www.realproject.io/"]) == "https://www.realproject.io/"


@pytest.mark.parametrize("report, ok", [
    (None, None),
    ({"rugged": True}, False),
    ({"risks": [{"name": "Mint Authority still enabled", "level": "danger"}]}, False),
    ({"risks": [], "lpLockedPct": 40}, False),
    ({"risks": [{"name": "Low holders", "level": "warn"}], "lpLockedPct": 100}, True),
])
def test_rugcheck(report, ok):
    assert legit.judge_rugcheck(report)["ok"] is ok


@pytest.mark.parametrize("info, ok", [
    (None, None),
    ({"is_honeypot": "1"}, False),
    ({"sell_tax": "0.25"}, False),
    ({"is_mintable": "1"}, False),
    ({"can_take_back_ownership": "1"}, False),
    ({"is_open_source": "0"}, False),
    ({"holders": [{"percent": "0.6", "is_contract": 0, "is_locked": 0}]}, False),
    ({"holders": [{"percent": "0.6", "is_contract": 1}], "is_open_source": "1", "sell_tax": "0.01"}, True),
])
def test_goplus(info, ok):
    assert legit.judge_goplus(info)["ok"] is ok


def _commits(n, days):
    return [{"commit": {"author": {"date": f"2026-09-{10 + i % days:02d}T10:00:00Z"}}} for i in range(n)]


@pytest.mark.parametrize("repo, commits, ok", [
    (None, None, None),
    ({}, None, False),
    ({"full_name": "a/b", "fork": True}, _commits(40, 9), False),
    ({"full_name": "a/b"}, _commits(3, 3), False),
    ({"full_name": "a/b"}, _commits(12, 1), False),      # tutto in un giorno
    ({"full_name": "a/b", "stargazers_count": 4}, _commits(12, 4), True),
])
def test_github(repo, commits, ok):
    assert legit.judge_github(repo, commits)["ok"] is ok


def test_github_links_from_anywhere():
    assert legit.github_targets(["https://github.com/orgs/x", "https://github.com/acme/core.git",
                                 "https://github.com/acme", "https://x.com/a"]) == [("acme", "core"), ("acme", None)]


def test_page_text_drops_scripts():
    title, text = legit.page_text("<title>Acme &amp; Co</title><script>evil()</script><p>Real  product</p>")
    assert title == "Acme & Co" and text == "Real product"


def test_site_on_a_private_address_is_never_fetched():
    async def go():
        async with httpx.AsyncClient() as c:
            return await legit.fetch_site("http://127.0.0.1:8000/admin", c)
    assert asyncio.run(go())["error"] == "non-public address"


TOKEN = {"chain": "solana", "symbol": "ACME", "name": "Acme Protocol", "token_address": "Mint111",
         "market_cap": 80_000, "liquidity_usd": 20_000}
LINKS = {"websites": ["https://acme.io"], "socials": [{"type": "twitter", "url": "https://x.com/acme"}]}


GOPLUS_CLEAN = {"mintable": {"status": "0"}, "freezable": {"status": "0"}, "closable": {"status": "0"},
                "balance_mutable_authority": {"status": "0"}, "transfer_fee": {}, "transfer_hook": [],
                "transfer_fee_upgradable": {"status": "0"}, "non_transferable": "0",
                "default_account_state": "1", "metadata_mutable": {"status": "0"}}


def _net(repo_commits=12, lp=100, goplus=GOPLUS_CLEAN):
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "gopluslabs.io/api/v1/solana" in url:
            assert req.url.params["contract_addresses"] == "Mint111"
            return httpx.Response(200, json={"code": 1, "result": {"Mint111": goplus} if goplus else None})
        if "rugcheck" in url:
            return httpx.Response(200, json={"risks": [], "lpLockedPct": lp})
        if url.startswith("https://acme.io"):
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  text="<title>Acme</title><a href='https://github.com/acme/core'>code</a> Mint111")
        if url.endswith("/repos/acme/core"):
            return httpx.Response(200, json={"full_name": "acme/core", "html_url": "https://github.com/acme/core"})
        if url.startswith("https://api.github.com/repos/acme/core/commits"):
            return httpx.Response(200, json=_commits(repo_commits, 5))
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def ok(host):
        return host != "127.0.0.1"
    monkeypatch.setattr(legit, "_public_host", ok)
    monkeypatch.setattr(legit.settings, "anthropic_api_key", "")


def _assess(token=TOKEN, links=LINKS, **net):
    async def go():
        async with httpx.AsyncClient(transport=_net(**net)) as c:
            return await legit.assess(token, links, c)
    return asyncio.run(go())


def test_serious_project_passes_with_site_github_and_contract_on_the_site():
    out = _assess()
    assert out["status"] == "ok" and out["website"] == "https://acme.io"
    assert out["github"] == "https://github.com/acme/core"
    checks = {c["key"]: c for c in out["checks"]}
    assert "cites the contract" in checks["site"]["detail"] and checks["ai"]["detail"] == "not active"


def test_meme_name_fails_before_any_request():
    assert _assess({**TOKEN, "symbol": "PEPE2", "name": "Pepe Two"})["status"] == "fail"


def test_unlocked_liquidity_fails():
    assert _assess(lp=10)["status"] == "fail"


def test_empty_github_fails():
    out = _assess(repo_commits=2)
    assert out["status"] == "fail" and out["reason"].startswith("GitHub")


def test_no_website_yet_waits():
    assert _assess(links={"websites": ["https://pump.fun/coin/x"], "socials": LINKS["socials"]})["status"] == "wait"


def test_claude_verdict_can_only_reject(monkeypatch):
    monkeypatch.setattr(legit.settings, "anthropic_api_key", "k")

    async def review(token, site, links):
        return {"legit": False, "score": 2, "reason": "solo hype, nessun prodotto", "red_flags": ["hype"]}
    monkeypatch.setattr(legit, "ai_review", review)
    out = _assess()
    assert out["status"] == "fail" and "solo hype" in out["reason"]


@pytest.mark.parametrize("change, ok", [
    ({}, True),
    ({"mintable": {"status": "1"}}, False),
    ({"freezable": {"status": "1"}}, False),
    ({"default_account_state": "2"}, False),
    ({"balance_mutable_authority": {"status": "1"}}, False),
    ({"transfer_fee": {"fee_rate": "0.25"}}, False),
    ({"transfer_hook": [{"address": "x"}]}, False),
    ({"metadata_mutable": {"status": "1"}}, True),
])
def test_goplus_solana(change, ok):
    assert legit.judge_goplus_solana({**GOPLUS_CLEAN, **change})["ok"] is ok


def test_goplus_is_a_public_filter_without_any_key():
    out = _assess(goplus={**GOPLUS_CLEAN, "freezable": {"status": "1"}})
    assert out["status"] == "fail" and out["reason"].startswith("GoPlus")
    assert out["scan_url"] == "https://rugcheck.xyz/tokens/Mint111"


def test_goplus_silent_waits():
    out = _assess(goplus=None)
    assert out["status"] == "wait" and "GoPlus" in out["reason"]


def test_report_card_says_how_well_each_filter_was_passed():
    out = _assess()
    by_key = {c["key"]: c for c in out["checks"]}
    assert by_key["security"]["score"] == 100 and by_key["security"]["grade"] == "excellent"
    assert by_key["contract"]["score"] == 100
    gh = by_key["github"]
    assert gh["facts"]["repo"] == "acme/core" and gh["facts"]["active_days"] == 5 and 0 < gh["score"] < 100
    assert out["report"]["counted"] == 5 and out["report"]["grade"] in {g for _, g in legit.GRADES}


def test_grades():
    assert [legit.grade(s) for s in (95, 75, 60, 20)] == ["excellent", "good", "fair", "poor"]


def test_above_100k_only_an_excellent_report_passes():
    """Lorenzo: "uno o due milioni di market cap potrebbe andare bene però
    [...] GitHub, sito, tokenomics tante cose devono essere perfette"."""
    small = _assess({**TOKEN, "market_cap": 90_000})
    assert small["status"] == "ok"
    big = _assess({**TOKEN, "market_cap": 1_500_000})
    gem = next(c for c in big["checks"] if c["key"] == "gem")
    assert big["status"] == "fail" and gem["ok"] is False and "below 85" in big["reason"]


def test_gem_shortfall_names_what_is_missing():
    good = [{"key": "security", "label": "RugCheck", "score": 95, "grade": "eccellente"},
            {"key": "github", "label": "GitHub", "score": 88, "grade": "eccellente"}]
    assert legit.gem_shortfall(good, legit.report_card(good)) == ""
    meh = good + [{"key": "site", "label": "Sito", "score": 60, "grade": "sufficiente"}]
    assert "Sito" in legit.gem_shortfall(meh, {"score": 90})
    thin = [{"key": "contract", "label": "GoPlus contratto", "score": 80, "grade": "buono"},
            {"key": "github", "label": "GitHub", "score": 95, "grade": "eccellente"}]
    assert "GoPlus" in legit.gem_shortfall(thin, {"score": 90})


def _gh(score=90, total=10_203, days=11, pushed_days_ago=4, now=1_800_000_000):
    from datetime import datetime, timezone
    pushed = datetime.fromtimestamp(now - pushed_days_ago * 86400, timezone.utc).isoformat()
    return {"score": score, "facts": {"commits": "100+", "total_commits": total, "active_days": days,
                                      "pushed_at": pushed}}


def test_github_exception_measures_depth_like_dregg():
    """Lorenzo: "progetti in quel modo voglio che siano eccezione in quanto
    tecnicamente incredibili su github". Metro: DREGG, 10 203 commit di un
    solo autore, ultimi 100 su 11 giorni."""
    now = 1_800_000_000
    assert legit.github_shortfall(_gh(), now) == ""
    assert "below 75" in legit.github_shortfall(_gh(score=60), now)
    assert "300 commits in total" in legit.github_shortfall(_gh(total=300), now)
    assert "not read" in legit.github_shortfall(_gh(total=None), now)
    assert "3 days" in legit.github_shortfall(_gh(days=3), now)
    assert "idle for 40" in legit.github_shortfall(_gh(pushed_days_ago=40), now)


def test_a_long_solo_history_counts_like_a_team():
    commits = [{"commit": {"author": {"date": f"2026-08-{d:02d}T00:00:00Z"}}, "author": {"login": "emberian"}}
               for d in range(10, 21) for _ in range(9)]
    repo = {"full_name": "emberian/dregg", "stargazers_count": 35}
    short = legit.judge_github(repo, commits)["score"]
    deep = legit.judge_github(repo, commits, 10_203)
    assert deep["score"] - short > 10 and deep["score"] >= 85 and deep["facts"]["total_commits"] == 10_203


def test_exception_token_fails_without_an_exceptional_github():
    out = _assess({**TOKEN, "market_cap": 2_500_000, "exception": True})
    gem = next(c for c in out["checks"] if c["key"] == "gem")
    assert out["status"] == "fail" and gem["label"] == "GitHub exception" and gem["ok"] is False


def test_exception_without_github_fails_at_once():
    links = {**LINKS}
    async def go():
        def handler(req):
            url = str(req.url)
            if "gopluslabs.io" in url:
                return httpx.Response(200, json={"code": 1, "result": {"Mint111": GOPLUS_CLEAN}})
            if "rugcheck" in url:
                return httpx.Response(200, json={"risks": [], "lpLockedPct": 100})
            if url.startswith("https://acme.io"):
                return httpx.Response(200, headers={"content-type": "text/html"}, text="<title>Acme</title> hi")
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await legit.assess({**TOKEN, "exception": True}, links, c)
    out = asyncio.run(go())
    assert out["status"] == "fail" and "no GitHub" in out["reason"]


def test_exception_token_passes_with_an_exceptional_report(monkeypatch):
    monkeypatch.setattr(legit, "gem_shortfall", lambda checks, report: "")
    monkeypatch.setattr(legit, "github_shortfall", lambda gh, now=None: "")
    out = _assess({**TOKEN, "market_cap": 2_500_000, "exception": True})
    gem = next(c for c in out["checks"] if c["key"] == "gem")
    assert out["status"] == "ok" and gem["ok"] is True


def test_platform_repos_are_never_the_project_github():
    """VISE (rug del 24/09, -93%): il sito linkava pump-fun/pump-public-docs,
    e il filtro l'aveva preso per il GitHub del progetto."""
    urls = ["https://github.com/pump-fun/pump-public-docs", "https://github.com/acme/core"]
    assert legit.github_targets(urls) == [("acme", "core")]
    vise = {"symbol": "VISE", "name": "Vise"}
    docs = {"full_name": "pump-fun/pump-public-docs", "description": "Pump.fun public docs"}
    assert not legit.github_belongs(docs, vise, "https://vise.fun/")
    assert legit.github_belongs({"full_name": "emberian/dregg"}, {"symbol": "DREGG", "name": "dregg"},
                                "https://www.dregg.net/")
    assert legit.github_belongs({"full_name": "x/core", "homepage": "https://acme.io"}, {"symbol": "Q"},
                                "https://acme.io")


def test_someone_elses_repo_fails(monkeypatch):
    async def foreign(owner, repo, client):
        return {"full_name": "other/docs", "html_url": "https://github.com/other/docs"}
    monkeypatch.setattr(legit, "github_repo", foreign)
    out = _assess({**TOKEN, "symbol": "VISE", "name": "Vise"})
    assert out["status"] == "fail" and "does not belong to the project" in out["reason"]


def test_holder_concentration_ignores_pools_and_fails_whales():
    report = {"risks": [], "lpLockedPct": 100, "knownAccounts": {"Pool1": {"type": "AMM"}},
              "markets": [{"pubkey": "Pool1"}],
              "topHolders": [{"owner": "Pool1", "pct": 74}] + [{"owner": f"w{i}", "pct": 2} for i in range(12)]}
    assert legit.holder_concentration(report) == (2, 20)
    assert legit.judge_rugcheck(report)["ok"] is True
    whale = {**report, "topHolders": report["topHolders"] + [{"owner": "whale", "pct": 22}]}
    assert "22%" in legit.judge_rugcheck(whale)["detail"]
    spread = {**report, "topHolders": [{"owner": f"w{i}", "pct": 5} for i in range(10)]}
    assert "top 10" in legit.judge_rugcheck(spread)["detail"]
