"""The Gem Finder's reader: an analyst's checklist, in arithmetic.

Lorenzo (25/09): no paid model reading projects around the clock, "per ora
concentrati a creare la base matematica e algoritmica di questo processo".
So this module does what a researcher does when opening a project, with
counts instead of intuition, and returns the same dossier Claude would
(summary, stage, substance 0-10, original, verdict, red_flags) plus the
reasons behind each number:

- depth: how many distinct technical concepts the text names, and in how
  many fields (AI, privacy/ZK, infrastructure, DeFi, product). Saturating:
  the sixth concept counts less than the first;
- concreteness: numbers with units, versions, code, install commands, API
  paths. A real product leaks specifics; a pitch does not;
- stage: what the text claims to exist (mainnet, users, integrations)
  against what it promises (roadmap, coming soon), confirmed by an app
  that actually responds;
- token utility: whether the token has a job (pays fees, is staked, votes)
  within a few words of being mentioned;
- noise: buzzwords as a share of all vocabulary, hype, and profit promises;
  the last are the only thing that can make the verdict "scam";
- originality: word 5-gram sketches of every description read. A text that
  is mostly another project's text is a template or a clone;
- coherence: the listing description and the site should talk about the
  same thing. When they do not (a launchpad's token page, a borrowed
  site), the site is not read as the project's.

Every weight is written below and every verdict carries its reasons, so a
wrong verdict can be traced to one number.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Callable

# ------------------------------------------------------------------ lexicon

FIELDS: dict[str, tuple[str, ...]] = {
    "ai": ("inference", "llm", "language model", "agent", "agents", "training", "dataset", "fine-tun",
           "embedding", "gpu", "compute", "neural", "machine learning", "autonomous", "prompt"),
    "privacy": ("encrypt", "zk", "zero-knowledge", "zero knowledge", "proof", "snark", "stark", "tee",
                "trusted execution", "mpc", "fhe", "homomorphic", "attestation", "confidential",
                "private", "privacy", "stealth address", "mixer"),
    "infra": ("rpc", "indexer", "node", "validator", "oracle", "bridge", "rollup", "sdk", "api", "cli",
              "relayer", "sequencer", "consensus", "protocol", "smart contract", "program", "on-chain",
              "onchain", "decentralized network", "depin", "storage", "latency", "throughput"),
    "defi": ("amm", "liquidity", "vault", "lending", "perp", "swap", "routing", "router", "settlement",
             "collateral", "yield", "order book", "orderbook", "stablecoin", "treasury", "x402"),
    "product": ("dashboard", "extension", "bot", "plugin", "wallet", "marketplace", "integration",
                "mobile app", "terminal", "explorer", "open-source", "open source", "audit"),
}

_CONCRETE = [
    re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:ms|tps|gb|mb|tb|k|m|b|x|%|usd|sol|eth|seconds?|minutes?|users|"
               r"transactions|requests|nodes|validators|models)\b", re.I),
    re.compile(r"\bv\d+(?:\.\d+)+\b", re.I),                                   # versions
    re.compile(r"\b(?:npm|pip|cargo|yarn|pnpm|brew|docker)\s+(?:install|run|pull|add)\b", re.I),
    re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+){1,}\(?\b"),                         # camelCase
    re.compile(r"\b[a-z]+_[a-z_]+\b"),                                          # snake_case
    re.compile(r"(?:/v\d/|/api/|\.json\b|\bcurl\b|\bgrpc\b|\bwebsocket\b)", re.I),
]

_LIVE = re.compile(r"\b(mainnet|now live|live on|live now|is live|launched|available (?:now|on)|in production|"
                   r"processed \d|served \d|integrated (?:with|into)|used by|partners? with|downloads?|"
                   r"use it (?:in|with|from|on)|get started|quickstart|sign (?:in|up)|log in|open (?:chat|app)|"
                   r"launch app|try it|npm install|pip install)\b", re.I)
# A live product shows itself, not only in fixed phrases (29/09, PFWA: its
# docs explain how to open a market "live in under a minute", the fee and the
# payout; nowhere "now live on mainnet"). Each class is one piece of evidence;
# promises ("will go live", "coming soon") match none of them.
_LIVE_USE = re.compile(r"\b(markets? (?:are |is )?live|live markets?|live in under|in under a minute|"
                       r"now trading|trading is open)\b", re.I)
_LIVE_CONTRACT = re.compile(r"\b(?:contracts?|program|factory|deployed|address|token)\b[^.]{0,80}?"
                            r"(?:\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b)", re.I)
_LIVE_APP_URL = re.compile(r"\bhttps?://(?:app|trade|dapp|swap|exchange|credence)\.[\w.-]+|"
                           r"\bhttps?://[\w.-]+/(?:app|dapp|trade)\b", re.I)
_LIVE_MECHANICS = re.compile(r"\b(?:\d+(?:\.\d+)?\s?%\s+(?:\w+\s){0,2}fees?|fees?\s+(?:of|is|are|:)\s+\d|"
                             r"payouts? per share|winners? claim|claim (?:their|your) (?:winnings|payouts?))\b", re.I)
_LEGAL = re.compile(r"\b(disclaimer|not affiliated|not endorsed|endorsed by|not financial advice|risks?|"
                    r"terms(?: of (?:use|service))?|legal|liabilit\w*|no warranty)\b", re.I)
_TESTNET = re.compile(r"\b(testnet|devnet|beta|alpha|early access|waitlist|pilot)\b", re.I)
_BUILDING = re.compile(r"\b(roadmap|coming soon|in development|we are building|under development|"
                       r"phase \d|q[1-4]\s?20\d\d|soon|runnable|prototype|proof of concept|"
                       r"reference implementation|implemented|open[- ]source|source code|proof record|"
                       r"machine-checked|formally verified|benchmarks?|development)\b", re.I)
_CANDOR = re.compile(r"\b(named limits?|limitations?|not yet|not ready|no (?:independent )?(?:external )?audit|"
                     r"known (?:issues|limits)|remains? (?:outside|beyond|open)|beyond the present|"
                     r"out of scope|trade-?offs?|caveats?|active research)\b", re.I)

_TOKEN = re.compile(r"(\$[A-Za-z]{2,10}\b|\btokens?\b)")
_UTILITY = {
    "fees": re.compile(r"\b(pay|pays|paid|payments?|fees?|gas|credits?|x402)\b", re.I),
    "staking": re.compile(r"\b(stak\w+|bond\w*|slash\w*|collateral)\b", re.I),
    "governance": re.compile(r"\b(govern\w*|vote|voting|dao)\b", re.I),
    "value": re.compile(r"\b(burn\w*|buy-?backs?|revenue|rewards?|emissions?|yield|dividends?|share of)\b", re.I),
    "access": re.compile(r"\b(access|unlock\w* (?:features|tiers?)|discounts?|priority)\b", re.I),
}
UTILITY_WINDOW = 12   # words either side of a token mention

_BUZZ = re.compile(r"\b(revolutionar\w*|next[- ]gen\w*|cutting[- ]edge|seamless\w*|empower\w*|game[- ]chang\w*|"
                   r"paradigm|disrupt\w*|world'?s first|innovative|limitless|future of|synerg\w*|"
                   r"groundbreaking|state[- ]of[- ]the[- ]art|unleash\w*|supercharg\w*|web3)\b", re.I)
_HYPE = re.compile(r"(\b(100x|1000x|moon\w*|lambo|pump\w*|degen|gem|to the moon|ape in|meme\w*|fair launch|"
                   r"stealth launch|lp burn\w*|renounced|safu|diamond hands|community[- ]driven|next (?:pepe|doge|"
                   r"shib))\b|🚀|💎)", re.I)
_SCAM_STRONG = re.compile(r"\b(guaranteed (?:returns?|profits?|income)|risk[- ]free|daily (?:roi|returns?|profits?)|"
                          r"double your|\d+% (?:daily|per day|weekly)|send (?:sol|eth|usdc) to|"
                          r"no risk)\b", re.I)
_SCAM_WEAK = re.compile(r"\b(passive income|claim (?:your )?airdrop|presale (?:is )?live|whitelist spots?|"
                        r"limited spots|act now|last chance|early investors will)\b", re.I)
_CLONE_SELF = re.compile(r"\b(fork of|forked from|clone of|copy of)\b", re.I)

# Named, checkable technical primitives (study 002): a project that names
# them can be verified against the spec, one that says "AI-powered" cannot.
# A bonus, not a must-have. Short acronyms match case-sensitively so "tee",
# "mcp" or "fhe" inside ordinary words or lowercase prose do not count.
# Wormhole is left out on purpose: it marks bridged copies elsewhere.
_I = re.I
_PRIMITIVES: tuple[tuple[str, re.Pattern], ...] = tuple((label, re.compile(rx, flags)) for label, rx, flags in (
    ("x402", r"\bx402\b", _I),
    ("ERC-8004", r"\berc[- ]?8004\b", _I),
    ("ERC-4337", r"\berc[- ]?4337\b", _I),
    ("EAS", r"\bEAS\b|(?i:\bethereum attestation service\b)", 0),
    ("MCP", r"\bMCP\b|(?i:\bmodel context protocol\b)", 0),
    ("A2A", r"\bA2A\b", 0),
    ("MPC", r"\bMPC\b", 0),
    ("TEE", r"\bTEEs?\b", 0),
    ("FHE", r"\bFHE\b", 0),
    ("LayerZero", r"\blayer ?zero\b", _I),
    ("Turnkey", r"\bTurnkey\b(?! (?:solutions?|platforms?|services?|products?|packages?)\b)", 0),
    ("Umbra", r"\bUmbra\b", 0),
    ("zkVM", r"\bzkvms?\b", _I),
    ("STARK", r"\b(?:zk-?)?STARKs?\b", 0),
    ("SNARK", r"\b(?:zk-?)?SNARKs?\b", 0),
    ("Lean 4", r"\bLean ?4\b", 0),
    ("Polymarket", r"\bpolymarket\b", _I),
    ("Chainlink", r"\bchainlink\b", _I),
    ("EigenLayer", r"\beigen ?layer\b", _I),
    ("Pyth", r"\bPyth\b", 0),
    ("Ondo", r"\bOndo\b", 0),
    ("Hyperlane", r"\bhyperlane\b", _I),
))


def primitives(text: str) -> list[str]:
    """Distinct named primitives in the text, in the table's order."""
    return [label for label, rx in _PRIMITIVES if rx.search(text or "")]


_STOP = set("""the and for with that this from your you our are was were will have has not but all can its
into their they them than then there these those about which when where what while also more most such
only over just like each other some many any very using used use based built make makes made one two""".split())

# --------------------------------------------------------------- weights

W_DEPTH, K_DEPTH = 30, 4          # 30 points, e-folding at 4 concepts
W_CONCRETE, K_CONCRETE = 15, 3
W_STAGE = {"live": 20, "testnet": 14, "building": 8, "idea": 2, "unclear": 0}
W_UTILITY = 10                    # full at two roles
W_INFO, K_INFO = 10, 60           # distinct content words
W_COHERENT = 5
W_CANDOR = 8                      # full at two distinct statements of limits
# Full at two distinct named primitives. A bonus on top, left out of
# MAX_POINTS on purpose: in the denominator it would lower every project
# that names none (study 002: not a must-have) and even one that names a
# single primitive (Solrouter, TEE: 75.5 -> 73.6, gem -> promising).
W_PRIMITIVES = 8
MAX_POINTS = W_DEPTH + W_CONCRETE + max(W_STAGE.values()) + W_UTILITY + W_INFO + W_COHERENT + W_CANDOR
BUZZ_PENALTY = 20                 # times the buzzword share
HYPE_PENALTY, HYPE_CAP = 8, 30
CLONE_PENALTY = 25
CLONE_SIMILARITY = 0.5            # estimated Jaccard of 5-gram sets
COHERENCE_MIN = 0.1           # shared words over the shorter of description and site
COHERENCE_WORDS = 5           # shorter descriptions cannot be checked
GEM_MIN, PROMISING_MIN = 75, 55
SKETCH_K = 64
# A shorter text has too few 5-grams to compare: a JavaScript shell ("enable
# JavaScript to run this app") matched another shell at 100% (30/09, IMG
# from Lorenzo: "la cosa del 100% identica e' una cazzata").
SKETCH_MIN_GRAMS = 20


def _sat(x: float, k: float) -> float:
    return 1 - math.exp(-x / k) if x > 0 else 0.0


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9'-]*", (text or "").lower())


def content_words(text: str) -> set[str]:
    """Distinct content words, lightly stemmed (encrypted ~ encrypts)."""
    return {re.sub(r"(ing|ed|es|s)$", "", w) if len(w) > 5 else w
            for w in words(text) if len(w) >= 4 and w not in _STOP and not w.isdigit()}


def concepts(text: str) -> dict[str, list[str]]:
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    out: dict[str, list[str]] = {}
    for field, terms in FIELDS.items():
        hit = [t for t in terms if re.search(r"(?<![a-z])" + re.escape(t), low)]
        if hit:
            out[field] = hit
    return out


def concreteness(text: str) -> list[str]:
    found: list[str] = []
    for rx in _CONCRETE:
        found += [m.group(0) for m in rx.finditer(text or "")]
    return list(dict.fromkeys(found))


def not_legal(text: str) -> str:
    """The text without its disclaimer/legal sentences: "an independent,
    community-driven project, not affiliated with..." is not hype."""
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return " ".join(p for p in parts if not _LEGAL.search(p))


def stage(text: str, app_ok: bool = False, deployed: bool = False) -> tuple[str, dict]:
    live, test, build = (len(set(m.lower() for m in rx.findall(text or ""))) for rx in (_LIVE, _TESTNET, _BUILDING))
    proof = [k for k, rx in (("usage", _LIVE_USE), ("contract", _LIVE_CONTRACT), ("app_url", _LIVE_APP_URL),
                             ("mechanics", _LIVE_MECHANICS)) if rx.search(text or "")]
    if deployed and "contract" not in proof:
        proof.append("contract")
    live += len(proof)
    ev = {"live": live, "testnet": test, "building": build, "app_responds": app_ok, "proof": proof}
    if len(words(text)) < 25:
        return "unclear", ev
    if live >= 2 or (live and app_ok):
        return "live", ev
    if test or (app_ok and not build):
        return "testnet", ev
    if build or live:
        return "building", ev
    return "idea", ev


def token_utility(text: str, symbol: str = "") -> list[str]:
    """Roles named near a mention of the token. Projects name their token by
    its ticker without the dollar sign ("AUREON holders stake..."), so the
    ticker counts as a mention too."""
    ws = (text or "").split()
    own = re.compile(r"(?<![A-Za-z0-9])" + re.escape(symbol) + r"(?![A-Za-z0-9])", re.I) \
        if len(symbol or "") >= 3 else None
    roles: set[str] = set()
    for i, w in enumerate(ws):
        if not (_TOKEN.search(w) or (own and own.search(w))):
            continue
        window = " ".join(ws[max(0, i - UTILITY_WINDOW): i + UTILITY_WINDOW + 1])
        roles |= {role for role, rx in _UTILITY.items() if rx.search(window)}
    return sorted(roles)


def sketch(text: str, k: int = SKETCH_K) -> list[int]:
    """Bottom-k MinHash of word 5-grams: the k smallest 64-bit hashes."""
    ws = words(text)
    grams = {" ".join(ws[i:i + 5]) for i in range(max(0, len(ws) - 4))}
    if len(grams) < SKETCH_MIN_GRAMS:
        return []
    hs = sorted(int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big") for g in grams)
    return hs[:k]


def similarity(a: list[int], b: list[int], k: int = SKETCH_K) -> float:
    """Estimated Jaccard from two bottom-k sketches."""
    if len(a) < SKETCH_MIN_GRAMS or len(b) < SKETCH_MIN_GRAMS:
        return 0.0
    union = sorted(set(a) | set(b))[:k]
    return len(set(union) & set(a) & set(b)) / len(union)


def closest(print_: list[int], corpus: dict[str, list[int]], skip: str = "",
            same: Callable[[str], bool] | None = None) -> tuple[str, float]:
    """The most similar other project. `same(key)` says a match is this very
    project under another key (another chain, another pool): not a copy."""
    best = ("", 0.0)
    for key, other in corpus.items():
        if key.lower() == skip.lower():
            continue
        s = similarity(print_, other)
        if s > best[1] and not (same and s >= CLONE_SIMILARITY and same(key)):
            best = (key, s)
    return best


def summary(description: str, site: str) -> str:
    """The first sentence that names something concrete, 25 words at most."""
    for text in (description, site):
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if len(s.split()) >= 5 and not re.search(r"[{}<>]", s)]
        for s in sents[:4]:
            if concepts(s):
                return " ".join(s.split()[:25])
        if sents:
            return " ".join(sents[0].split()[:25])
    return ""


def read(description: str, site_texts: list[str], *, name: str = "", symbol: str = "", app_ok: bool = False,
         deployed: bool = False, corpus: dict[str, list[int]] | None = None, key: str = "",
         same: Callable[[str], bool] | None = None) -> dict:
    """The dossier: same keys as Claude's, plus `points` (0-100) and `why`."""
    site = "\n".join(t for t in site_texts if t)
    d_words, s_words = content_words(description), content_words(site)
    coherence = (len(d_words & s_words) / min(len(d_words), len(s_words))
                 if len(d_words) >= COHERENCE_WORDS and s_words else None)
    names = [n.lower() for n in (name, symbol) if n and len(n) >= 3]
    unnamed = bool(names and site) and not any(n in site.lower() for n in names)
    borrowed = unnamed or (coherence is not None and coherence < COHERENCE_MIN)
    if borrowed:
        site = ""
    text = f"{description}\n{site}"
    found = concepts(text)
    n_concepts = sum(len(v) for v in found.values())
    concrete = concreteness(text)
    st, st_ev = stage(text, app_ok, deployed)
    roles = token_utility(text, symbol)
    candor = len(set(m.lower() for m in _CANDOR.findall(text)))
    prims = primitives(text)
    info = len(content_words(text))
    plain = not_legal(text)
    buzz = len(_BUZZ.findall(plain))
    hype = len(_HYPE.findall(plain))
    strong = sorted({m.lower() for m in _SCAM_STRONG.findall(text)})
    weak = sorted({m.lower() for m in _SCAM_WEAK.findall(text)})
    print_ = sketch(description or site)
    twin, twin_sim = closest(print_, corpus or {}, skip=key, same=same) if print_ else ("", 0.0)
    clone = twin_sim >= CLONE_SIMILARITY or bool(_CLONE_SELF.search(text))

    parts = {
        "depth": W_DEPTH * _sat(n_concepts, K_DEPTH),
        "concrete": W_CONCRETE * _sat(len(concrete), K_CONCRETE),
        "stage": W_STAGE[st],
        "utility": W_UTILITY * min(len(roles), 2) / 2,
        "info": W_INFO * _sat(info, K_INFO),
        "coherence": W_COHERENT if coherence is not None and coherence >= COHERENCE_MIN else 0,
        "candor": W_CANDOR * min(candor, 2) / 2,
        "primitives": W_PRIMITIVES * min(len(prims), 2) / 2,
    }
    buzz_share = buzz / (buzz + n_concepts) if buzz + n_concepts else 0.0
    penalty = {"buzz": BUZZ_PENALTY * buzz_share, "hype": min(HYPE_CAP, HYPE_PENALTY * hype),
               "clone": CLONE_PENALTY if clone else 0}
    top = MAX_POINTS - (W_COHERENT if coherence is None else 0)   # no site: coherence is not asked
    points = max(0.0, min(100.0, sum(parts.values()) / top * 100 - sum(penalty.values())))

    flags: list[str] = []
    if strong:
        flags.append("promises returns: " + ", ".join(strong))
    if weak:
        flags.append("pressure/airdrop bait: " + ", ".join(weak))
    if hype >= 2:
        flags.append(f"hype language ({hype})")
    if buzz_share >= 0.5 and buzz >= 3:
        flags.append("mostly buzzwords")
    if clone:
        flags.append(f"text {round(twin_sim * 100)}% identical to another project" if twin_sim >= CLONE_SIMILARITY
                     else "declares itself a fork/clone")
    if borrowed:
        flags.append("site never names the project: not read" if unnamed
                     else "site does not talk about this project: not read")
    if st in ("idea", "building") and len(roles) == 0 and n_concepts < 3:
        flags.append("nothing built, token has no role")

    if strong or len(weak) >= 2:
        verdict = "scam"
    elif points >= GEM_MIN and not clone and st in ("live", "testnet") and roles:
        verdict = "gem"
    elif points >= PROMISING_MIN and not clone:
        verdict = "promising"
    else:
        verdict = "thin"

    why = [f"{n_concepts} concepts in {len(found)} fields ({', '.join(found)})" if found else "no technical concept",
           f"{len(concrete)} concrete details" if concrete else "no concrete detail",
           f"stage {st}", f"token: {', '.join(roles)}" if roles else "token has no stated role"]
    if candor:
        why.append("states its own limits")
    if prims:
        why.append("named primitives: " + ", ".join(prims))
    return {
        "summary": summary(description, site), "stage": st, "substance": round(points / 10),
        "original": not clone, "verdict": verdict, "red_flags": flags[:5],
        "points": round(points, 1), "why": why,
        "parts": {k: round(v, 1) for k, v in {**parts, **{f"-{k}": v for k, v in penalty.items()}}.items()},
        "evidence": {"concepts": found, "concrete": concrete[:8], "stage": st_ev, "utility": roles,
                     "primitives": prims,
                     "coherence": None if coherence is None else round(coherence, 3),
                     "twin": twin if twin_sim >= CLONE_SIMILARITY else None, "twin_similarity": round(twin_sim, 2)},
        "sketch": print_,
    }
