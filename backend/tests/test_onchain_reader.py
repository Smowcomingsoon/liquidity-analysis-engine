"""The deterministic reader: an analyst's checklist in arithmetic."""
from app.trading.onchain import reader

from tests.test_onchain_research import SOLROUTER

DOCS = ("Solrouter is private AI chat. Use it in the web chat, Claude Code, Cursor or your own app. "
        "npm install @solrouter/sdk then call client.chat(). Each sealed call writes a receipt to Solana. "
        "Latency under 300 ms on the Nosana GPU network.")
HYPE = ("$PEPEX is the next 100x memecoin! Fair launch, LP burned, renounced. Community-driven degen army, "
        "to the moon, don't miss out!")
BUZZ = ("Nova is a revolutionary next-gen web3 ecosystem that empowers users with cutting-edge AI. Our "
        "innovative platform seamlessly unlocks the future of finance. Roadmap: Q4 2026.")


def _read(desc, site=(), **kw):
    r = reader.read(desc, list(site), **kw)
    r.pop("sketch")
    return r


def test_real_project_reads_as_a_gem():
    r = _read(SOLROUTER, [DOCS], name="Solrouter")
    assert r["verdict"] == "gem" and r["stage"] == "live" and r["original"]
    assert "fees" in r["evidence"]["utility"] and not r["red_flags"]


def test_hype_and_buzzwords_are_thin():
    for text in (HYPE, BUZZ):
        r = _read(text)
        assert r["verdict"] == "thin" and r["points"] < 20 and r["red_flags"]


def test_profit_promises_are_a_scam():
    r = _read("Stake $YLD and earn guaranteed returns of 3% daily. Risk-free, presale is live.")
    assert r["verdict"] == "scam" and "promises returns" in r["red_flags"][0]


def test_copied_text_is_not_original():
    corpus = {"solana:A": reader.sketch(SOLROUTER)}
    r = _read(SOLROUTER.replace("Solrouter", "Privroute"), corpus=corpus, key="solana:B")
    assert not r["original"] and r["verdict"] == "thin" and r["evidence"]["twin"] == "solana:A"
    assert reader.similarity(reader.sketch(SOLROUTER), reader.sketch(HYPE)) < 0.1


def test_a_site_about_something_else_is_not_read():
    """A launchpad's token page: rich text, none of it about the project."""
    r = _read("Pixel frogs that jump across the chain for fun and friendship forever.", [DOCS])
    assert r["verdict"] == "thin" and any("not read" in f for f in r["red_flags"])
    r = _read(SOLROUTER, [DOCS], name="Frogcoin", symbol="FROG")
    assert "site never names the project: not read" in r["red_flags"]


def test_stage_needs_evidence():
    assert reader.stage("We are building it. Roadmap: testnet in Q3 2026 and more words " * 3)[0] == "testnet"
    assert reader.stage("Now live on mainnet, used by 4000 traders, get started in the app. " * 3)[0] == "live"
    assert reader.stage("short")[0] == "unclear"


def test_token_role_is_read_near_the_token():
    assert reader.token_utility("$ROUTER is the token used to pay for inference and staked by nodes") == \
        ["fees", "staking"]
    assert reader.token_utility("The token. " + "filler " * 30 + "fees are low") == []
    # The ticker without "$" is a mention too (29/09: every project read "no stated role").
    assert reader.token_utility("AUREON holders stake to run agents", "AUREON") == ["staking"]
    assert reader.token_utility("AUREON holders stake to run agents") == []

RESEARCH = ("Dregg is a deep research project with a runnable decision engine and a public proof record. "
            "The core is machine-checked and Lean-authored. Named limit: no independent external audit yet. "
            "Some components remain outside the present scope and are active research.")


def test_research_stage_code_reads_as_building():
    r = _read("We publish a runnable prototype and a public proof record for the decision engine. "
              "The reference implementation is machine-checked and open-source. " * 2)
    assert r["stage"] == "building"


def test_stated_limits_are_candor():
    r = _read(RESEARCH)
    assert r["parts"]["candor"] == 8
    assert "states its own limits" in r["why"]


def test_marketing_text_has_no_candor():
    r = _read(HYPE)
    assert r["parts"]["candor"] == 0


PFWA_DOCS = ("PFWA is a permissionless prediction market on Robinhood Chain. Anyone can open a market on a token's "
             "price, market cap or volume, no approvals, live in under a minute. Every outcome is its own token on "
             "a bonding curve, so the odds move with every trade. Trading closed for good. Winners claim at the "
             "fixed payout per share, free. Disclaimer: PFWA is an independent, community-driven project and is "
             "not affiliated with, endorsed by, or associated with Robinhood.")
PROMISE = ("Zenith will be the future of decentralized prediction markets. Our protocol is coming soon: markets "
           "will go live after launch. Roadmap: testnet design in Q1 2027, token sale later. Join the community "
           "and stay tuned for more on our vision and the team behind it.")


def test_live_product_is_read_from_evidence_not_phrases():
    st, ev = reader.stage(PFWA_DOCS)
    assert st == "live" and {"usage", "mechanics"} <= set(ev["proof"])
    assert reader.stage(PROMISE)[0] in ("idea", "building", "testnet")
    assert reader.stage(PROMISE)[1]["proof"] == []
    # A deployed contract on an explorer is evidence; a promise alone never is.
    assert "contract" in reader.stage(PROMISE, deployed=True)[1]["proof"]
    addr = "The factory contract is deployed at 0x7B38e940Cd649bc0C701c42eE1A6ceff75584afa on Robinhood Chain. "
    assert "contract" in reader.stage(addr * 3)[1]["proof"]


def test_disclaimer_sentences_are_not_hype():
    r = _read(PFWA_DOCS)
    assert r["parts"]["-hype"] == 0 and not any("hype" in f for f in r["red_flags"])
    assert _read(HYPE)["parts"]["-hype"] > 0          # hype outside a disclaimer still counts


def test_named_primitives_are_a_bonus():
    text = ("Agents pay per call over x402 and register identity with ERC-8004; results are attested "
            "through the Ethereum Attestation Service. The team will guarantee a steep climb.")
    assert reader.primitives(text) == ["x402", "ERC-8004", "EAS"]
    r = _read(text)
    assert r["parts"]["primitives"] == reader.W_PRIMITIVES
    assert r["evidence"]["primitives"] == ["x402", "ERC-8004", "EAS"]
    assert "named primitives: x402, ERC-8004, EAS" in r["why"]
    # Short acronyms only in capitals, as words: ordinary prose does not count.
    assert reader.primitives("a guarantee to the mcp team, three eas, fhe-ish tees") == []
    assert _read(BUZZ)["parts"]["primitives"] == 0


def test_short_texts_are_never_called_copies():
    shell = "You need to enable JavaScript to run this app."
    assert reader.sketch(shell) == []
    r = reader.read(shell, [], corpus={"base:0xb": reader.sketch(SOLROUTER)}, key="base:0xa")
    assert r["original"] and r["evidence"]["twin"] is None


def test_the_same_project_under_another_key_is_not_a_copy():
    corpus = {"ethereum:0xB": reader.sketch(SOLROUTER), "base:0XA": reader.sketch(SOLROUTER)}
    r = reader.read(SOLROUTER, [], corpus=corpus, key="base:0xa", same=lambda k: k == "ethereum:0xB")
    assert r["original"] and r["evidence"]["twin"] is None


def test_same_project_matches_the_site_or_name_and_ticker(monkeypatch):
    from app.trading.onchain import projects_db, research
    rows = {"ethereum:0xb": {"website": "https://www.hydrex.fi", "name": "Hydrex", "symbol": "HYDX"},
            "base:0xc": {"website": "https://other.io", "name": "Other", "symbol": "OTH"}}
    monkeypatch.setattr(projects_db, "get", lambda c, t: rows.get(f"{c}:{t}"))
    assert research.same_project("ethereum:0xb", {"website": "https://hydrex.fi/", "name": "X", "symbol": "Y"})
    assert research.same_project("ethereum:0xb", {"name": "hydrex", "symbol": "hydx"})
    assert not research.same_project("base:0xc", {"website": "https://hydrex.fi", "name": "Hydrex", "symbol": "HYDX"})
