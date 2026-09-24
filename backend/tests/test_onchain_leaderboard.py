"""Classifica dei progetti: analisi profonda (GitHub, sito, tokenomics) e
ordine. Tutto senza rete: i giudizi sono funzioni pure."""
import time

import pytest

from app.trading.onchain import depth, leaderboard as lb

NOW = 1_790_000_000.0
DAY = 86400


def _commit(t: float, msg: str = "Fix order routing", login: str = "alice") -> dict:
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    return {"commit": {"author": {"date": iso, "email": f"{login}@x"}, "message": msg}, "author": {"login": login}}


def _repo(**kw) -> dict:
    return {"repo": {"full_name": "acme/core", "html_url": "https://github.com/acme/core", "fork": False,
                     "created_at": "2025-01-01T00:00:00Z", "stargazers_count": 40, **kw.pop("repo", {})},
            "commits": kw.get("commits", []), "contributors": kw.get("contributors", [{"login": "a"}]),
            "languages": kw.get("languages", {"Rust": 400_000, "TypeScript": 120_000}),
            "root": kw.get("root", [{"name": "tests"}, {"name": ".github"}, {"name": "README.md"}]),
            "participation": kw.get("participation", {})}


@pytest.fixture(autouse=True)
def _clean():
    lb.reset_for_tests()
    yield
    lb.reset_for_tests()


# ------------------------------------------------------------------ GitHub

def test_github_real_work_scores_high():
    commits = [_commit(NOW - i * 2 * DAY, login=("alice", "bob", "carol")[i % 3]) for i in range(40)]
    p = depth.judge_github(_repo(commits=commits, contributors=[{}, {}, {}, {}]), now=NOW)
    assert not p["fatal"] and p["score"] >= 70
    assert p["facts"]["tests"] and p["facts"]["ci"] and p["facts"]["authors"] == 3


def test_github_upload_only_repo_is_rejected():
    commits = [_commit(NOW - i * DAY, msg=m) for i, m in
               enumerate(["Add files via upload", "Update README.md", "Create index.html", "Initial commit"])]
    p = depth.judge_github(_repo(commits=commits), now=NOW)
    assert "uploads" in p["fatal"]


def test_github_one_day_and_tiny_code_are_rejected():
    same_day = [_commit(NOW - i * 60) for i in range(20)]
    assert "single day" in depth.judge_github(_repo(commits=same_day), now=NOW)["fatal"]
    spread = [_commit(NOW - i * DAY) for i in range(20)]
    assert "code" in depth.judge_github(_repo(commits=spread, languages={"HTML": 900}), now=NOW)["fatal"]


def test_github_fork_and_missing_and_rate_limited():
    assert "fork" in depth.judge_github(_repo(repo={"fork": True}), now=NOW)["fatal"]
    assert depth.judge_github({}, now=NOW)["fatal"]
    assert depth.judge_github(None)["pending"]


def test_github_born_with_token_is_flagged():
    commits = [_commit(NOW - i * 3600 * 4) for i in range(10)]
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 2 * DAY))
    p = depth.judge_github(_repo(commits=commits, repo={"created_at": created}), launched_at=NOW - DAY, now=NOW)
    assert "repository born with the token" in p["flags"]


def test_github_uses_participation_when_available():
    commits = [_commit(NOW - i * 600) for i in range(100)] + [_commit(NOW - 3 * DAY)]
    p = depth.judge_github(_repo(commits=commits, participation={"all": [0] * 40 + [5] * 12}), now=NOW)
    assert p["facts"]["active_weeks_12"] == 12 and p["facts"]["commits_90d"] == 60


# -------------------------------------------------------------------- sito

RICH = """<html><head><title>Acme</title><meta name="description" content="x">
<meta property="og:image" content="x.png"><link rel="icon" href="/f.ico"></head><body>
<nav><a href="/about">About</a><a href="/blog">Blog</a><a href="https://docs.acme.io/intro">Docs</a>
<a href="https://app.acme.io">Launch App</a><a href="https://github.com/acme/core">GitHub</a></nav>
<h1>Acme</h1><h2>Roadmap</h2><h2>Team</h2><h2>Tokenomics</h2><p>""" + "real words " * 400 + "</p></body></html>"


def test_anatomy_reads_structure():
    a = depth.anatomy(RICH, "https://acme.io/")
    assert a["docs"] == ["https://docs.acme.io/intro"]
    assert a["app"] == ["https://app.acme.io"]
    assert a["github"] == ["https://github.com/acme/core"]
    assert {"roadmap", "team", "tokenomics"} <= set(a["sections"])
    assert len(a["internal"]) >= 3 and a["meta_description"] and a["og_image"] and a["icon"]


def test_site_with_something_behind_beats_a_landing_page():
    rich = {**depth.anatomy(RICH, "https://acme.io/"), "url": "https://acme.io/", "https": True}
    followed = [{"kind": "docs", "ok": True, "words": 900}, {"kind": "app", "ok": True, "words": 20},
                {"kind": "page", "ok": True, "words": 300}]
    good = depth.judge_site(rich, followed, NOW - 400 * DAY, now=NOW)
    landing_html = "<html><body><h1>MOON</h1><p>" + "buy now " * 60 + "</p><a href='https://x.com/m'>X</a></body></html>"
    landing = {**depth.anatomy(landing_html, "https://moon.xyz/"), "url": "https://moon.xyz/", "https": True}
    bad = depth.judge_site(landing, [], NOW - 2 * DAY, launched_at=NOW - DAY, now=NOW)
    assert good["score"] >= 80 and bad["score"] <= 25
    assert "single page, nothing behind it" in bad["flags"]
    assert "domain registered with the token" in bad["flags"]


def test_site_placeholder_and_empty_and_not_own():
    lorem = {**depth.anatomy("<p>Lorem ipsum dolor sit amet " + "x " * 100 + "</p>", "https://a.io/"), "url": "https://a.io/"}
    assert "lorem" in depth.judge_site(lorem, [], None, now=NOW)["fatal"]
    empty = {**depth.anatomy("<h1>Soon</h1>", "https://b.io/"), "url": "https://b.io/"}
    assert depth.judge_site(empty, [], None, now=NOW)["fatal"] == "empty page"
    wiki = {**depth.anatomy(RICH, "https://en.wikipedia.org/wiki/Frogger"), "not_own": "wikipedia.org"}
    assert "not the project's website" in depth.judge_site(wiki, [], None, now=NOW)["fatal"]


def test_rdap_registration():
    rdap = {"events": [{"eventAction": "expiration", "eventDate": "2027-01-01T00:00:00Z"},
                       {"eventAction": "registration", "eventDate": "2026-09-22T08:02:21.826Z"}]}
    assert depth._registration(rdap) == pytest.approx(1790064141.826)


# -------------------------------------------------------------- tokenomics

def _rug(**kw) -> dict:
    return {"token": {"supply": 1_000_000}, "creatorBalance": kw.get("creator", 0),
            "topHolders": kw.get("holders", [{"address": f"h{i}", "owner": f"o{i}", "pct": 2.0} for i in range(10)]),
            "totalHolders": kw.get("total", 3000), "mintAuthority": kw.get("mint"), "freezeAuthority": None,
            "markets": [{"pubkey": "pool", "liquidityA": "liqA", "liquidityAAccount": {"owner": "poolAuth"},
                         "lp": {"lpLockedPct": 100}}],
            "knownAccounts": {"amm1": {"type": "AMM"}}, "transferFee": {"pct": 0}, "rugged": False}


def test_tokenomics_clean_distribution_scores_high():
    p = depth.judge_tokenomics_solana(_rug())
    assert not p["fatal"] and p["score"] >= 80 and p["facts"]["top10_pct"] == 20.0


def test_tokenomics_pools_and_curve_are_not_holders():
    holders = [{"address": "liqA", "owner": "poolAuth", "pct": 40.0},
               {"address": "x", "owner": "amm1", "pct": 30.0},
               {"address": "curve-ata", "owner": "curve", "pct": 20.0},
               {"address": "h1", "owner": "o1", "pct": 3.0}]
    p = depth.judge_tokenomics_solana(_rug(holders=holders), exclude={"curve"})
    assert p["facts"]["top1_pct"] == 3.0 and not p["fatal"]


@pytest.mark.parametrize("kw,needle", [
    ({"creator": 790_000}, "creator"),
    ({"holders": [{"address": "w", "owner": "w", "pct": 35.0}]}, "a single wallet"),
    ({"holders": [{"address": f"w{i}", "owner": f"w{i}", "pct": 9.0} for i in range(10)]}, "top 10"),
    ({"mint": "someone"}, "mint"),
])
def test_tokenomics_scams_are_rejected(kw, needle):
    assert needle in depth.judge_tokenomics_solana(_rug(**kw))["fatal"]


def test_tokenomics_evm_goplus():
    info = {"holders": [{"address": "0xpair", "percent": "0.5"}, {"address": "0xdead00", "percent": "0.1", "is_locked": 1},
                        {"address": "0xa", "percent": "0.04"}],
            "holder_count": "1200", "creator_percent": "0.01", "lp_holders": [{"address": "0x1", "percent": "0.95", "is_locked": 1}],
            "is_mintable": "0", "buy_tax": "0", "sell_tax": "0"}
    p = depth.judge_tokenomics_evm(info, "0xPAIR")
    assert p["facts"]["top1_pct"] == 4.0 and p["facts"]["lp_locked_pct"] == 95.0 and not p["fatal"]
    assert "tax" in depth.judge_tokenomics_evm({**info, "sell_tax": "0.2"}, "0xpair")["fatal"]


def test_overall_fatal_and_pending():
    ok = {"score": 80, "fatal": "", "flags": []}
    assert depth.overall({"github": ok, "site": ok, "tokenomics": ok})["score"] == 80
    assert depth.overall({"github": {**ok, "fatal": "fork", "score": 0}, "site": ok})["fatal"].startswith("github")
    assert depth.overall({"github": ok, "tokenomics": {"score": None, "pending": "RugCheck"}})["pending"]


# -------------------------------------------------------------- classifica

def _entry(sym: str, gh: int, site: int, tok: int, cap: float = 50_000, age_days: float = 2, source: str = "radar",
           **item) -> dict:
    item = {"chain": "solana", "token_address": sym + "mint", "pair_address": sym + "pool", "symbol": sym,
            "market_cap": cap, "created_at": time.time() - age_days * DAY, **item}
    lb.add(item, source)
    k = lb._key("solana", sym + "mint")
    lb.apply_analysis(k, {n: {"key": n, "score": s, "facts": {}, "flags": [], "fatal": ""}
                          for n, s in (("github", gh), ("site", site), ("tokenomics", tok))})
    return lb._entries[k]


def test_board_orders_by_score_and_tracks_movement():
    _entry("AAA", 60, 60, 60)
    _entry("BBB", 90, 80, 85)
    b = lb.board()
    assert [e["symbol"] for e in b["entries"]] == ["BBB", "AAA"]
    assert [e["rank"] for e in b["entries"]] == [1, 2]
    _entry("CCC", 95, 95, 95)
    b = lb.board()
    assert b["entries"][0]["symbol"] == "CCC" and b["entries"][1]["rank_before"] == 1


def test_fatal_pillar_excludes_for_good():
    _entry("BAD", 80, 80, 80)
    k = lb._key("solana", "BADmint")
    lb.apply_analysis(k, {"tokenomics": {"key": "tokenomics", "score": 0, "facts": {}, "flags": [],
                                         "fatal": "il creatore tiene il 79%"}})
    assert k not in lb._entries and lb.board()["excluded"][0]["reason"].endswith("79%")
    assert lb.add({"chain": "solana", "token_address": "BADmint"}, "radar") is False


def test_weak_pillar_and_gem_rule():
    assert lb.qualifies(_entry("WEAK", 90, 20, 90)).startswith("site")
    assert "100k" in lb.qualifies(_entry("MID", 84, 90, 90, cap=500_000))
    assert lb.qualifies(_entry("GEM", 88, 90, 90, cap=500_000)) == ""


def test_exception_uses_the_radar_rule():
    """Lorenzo: DREGG va bene oltre 21 giorni e $2M, se tecnicamente
    incredibile su GitHub. La regola e' quella del radar."""
    pushed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - DAY))
    strong = {"key": "github", "label": "GitHub", "score": 90, "grade": "eccellente",
              "facts": {"commits": "100+", "total_commits": 5_000, "active_days": 40, "pushed_at": pushed}}
    checks = [strong, {"key": "security", "label": "Sicurezza", "score": 95}]
    report = {"score": 92}
    weak = {**strong, "facts": {**strong["facts"], "active_days": 3}}
    assert "GitHub" in lb.qualifies(_entry("OLD", 85, 90, 90, cap=2_500_000, age_days=88,
                                           checks=[weak, checks[1]], report=report))
    assert lb.qualifies(_entry("DREGG", 93, 75, 80, cap=2_500_000, age_days=88, checks=checks,
                               report=report, exception=True)) == ""
    assert "limits" in lb.qualifies(_entry("HUGE", 99, 99, 99, cap=50_000_000, age_days=88, checks=checks,
                                           report=report))
    assert lb.board()["entries"][0]["exception"] is True


def test_due_refreshes_by_pillar():
    e = _entry("R", 80, 80, 80)
    now = time.time()
    assert lb.due(e, now) == set()
    assert lb.due(e, now + 25 * 60) == {"tokenomics"}
    assert lb.due(e, now + 13 * 3600) == {"tokenomics", "github", "site"}


def test_market_update_keeps_the_most_liquid_pool_and_launch():
    _entry("M", 80, 80, 80)
    lb.apply_market([
        {"chainId": "solana", "baseToken": {"address": "Mmint"}, "liquidity": {"usd": 5000}, "marketCap": 70000,
         "pairCreatedAt": 1_000_000_000_000, "pairAddress": "p1", "dexId": "pumpfun"},
        {"chainId": "solana", "baseToken": {"address": "Mmint"}, "liquidity": {"usd": 20000}, "marketCap": 90000,
         "pairCreatedAt": 1_100_000_000_000, "pairAddress": "p2", "dexId": "pumpswap", "priceChange": {"h24": 12}},
    ])
    e = lb._entries[lb._key("solana", "Mmint")]
    assert e["market_cap"] == 90000 and e["liquidity_usd"] == 20000 and e["change_24h"] == 12
    assert e["launched_at"] == 1_000_000_000 and not e["bonding"]


# ------------------------------------------------------------- riferimento

from app.trading.onchain import benchmark  # noqa: E402

DREGG_GITHUB = {"description": "Formally verified distributed object-capability OS",
                "topics": ["lean4", "stark", "zero-knowledge"], "languages_all": ["Rust", "Lean", "TypeScript"],
                "root_dirs": ["metatheory", "circuit", "node", "blocklace", "sdk", "cli", "docs"],
                "total_commits": 10_203}


def test_dregg_is_the_top_of_its_own_benchmark():
    sim = benchmark.similarity(benchmark.signals(DREGG_GITHUB, benchmark.site_terms(
        "machine-checked kernel, zkvm, consensus, wasm playground, no independent external audit, research software")))
    assert sim["score"] >= 90


def test_a_plain_web_project_is_far_from_dregg():
    web = {"description": "Community token with a website", "languages_all": ["JavaScript", "HTML", "CSS"],
           "root_dirs": ["public", "src"], "total_commits": 40}
    assert benchmark.similarity(benchmark.signals(web, benchmark.site_terms("buy, stake, earn")))["score"] <= 10


def test_site_terms_are_small():
    terms = benchmark.site_terms("We use STARK proofs, Plonky3 and a BFT consensus. Research software.")
    assert terms["zk"] == ["plonky3", "stark"] and "consensus" in terms["protocol"] and terms["candor"]


def test_dregg_is_pinned_first_even_with_lower_score():
    _entry("BEST", 99, 99, 99)
    lb.add(benchmark.seed_item(), "riferimento")
    k = lb._key(benchmark.CHAIN, benchmark.TOKEN)
    lb.apply_analysis(k, {"github": {"key": "github", "score": 70, "facts": DREGG_GITHUB, "flags": [], "fatal": ""},
                          "tokenomics": {"key": "tokenomics", "score": None, "facts": {}, "flags": [], "fatal": "",
                                         "pending": "RugCheck"}})
    b = lb.board()
    assert b["entries"][0]["symbol"] == "DREGG" and b["entries"][0]["benchmark"] and b["entries"][0]["honor"]
    assert b["entries"][1]["symbol"] == "BEST"


def test_similarity_lifts_a_dregg_like_project():
    plain = _entry("PLAIN", 80, 80, 80)
    like = _entry("LIKE", 78, 78, 78)
    lb.apply_analysis(lb._key("solana", "LIKEmint"),
                      {"github": {"key": "github", "score": 78, "facts": DREGG_GITHUB, "flags": [], "fatal": ""}})
    assert lb.final_score(like) > lb.final_score(plain)
    assert [e["symbol"] for e in lb.board()["entries"]][:2] == ["LIKE", "PLAIN"]


def test_benchmark_is_never_excluded():
    lb.add(benchmark.seed_item(), "riferimento")
    k = lb._key(benchmark.CHAIN, benchmark.TOKEN)
    lb.apply_analysis(k, {"site": {"key": "site", "score": 0, "facts": {}, "flags": [], "fatal": "pagina vuota"}})
    assert k in lb._entries


def test_serious_project_enters_up_to_three_million_and_a_year():
    e = _entry("SER", 80, 70, 65, cap=2_500_000, age_days=90, source="coingecko")
    assert lb.qualifies(e) == ""
    e["market_cap"] = 3_500_000
    assert "over" in lb.qualifies(e)
    old = _entry("OLD", 80, 70, 65, cap=900_000, age_days=400, source="coingecko")
    assert "days" in lb.qualifies(old)


def test_serious_project_needs_real_code():
    assert "GitHub" in lb.qualifies(_entry("THIN", 60, 90, 90, cap=500_000, source="dexscreener"))
    assert "site" in lb.qualifies(_entry("WEAK", 85, 50, 90, cap=500_000, source="coingecko"))


def test_serious_project_survives_prune_past_radar_age():
    _entry("MID", 80, 70, 65, cap=800_000, age_days=60, source="coingecko")
    lb._prune(time.time())
    assert lb._key("solana", "MIDmint") in lb._entries


def test_github_channel_counts_as_serious_source():
    e = _entry("GHX", 80, 65, 65, cap=1_500_000, age_days=100, source="github")
    assert lb.qualifies(e) == ""


def test_data_that_never_arrives_does_not_ban_the_project():
    e = _entry("SLOW", 80, 80, 80)
    k = lb._key("solana", "SLOWmint")
    e["overall"]["pending"] = "GitHub not answering"
    e["found_at"] = time.time() - lb.PENDING_GIVE_UP_S - 60
    lb._prune(time.time())
    assert k not in lb._entries and k not in lb._gone
