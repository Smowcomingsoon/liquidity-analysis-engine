"""Analisi profonda di un progetto, per la classifica.

Lorenzo: "devi essere rigoroso [...] da GitHub E devi controllare i commit
[...] deve essere profonda sta cosa, la tokenomics non deve essere uno scam,
il sito deve essere fatto bene, magari se riesci ad analizzarne l'html e
vedere se e' fatto bene o se e' solo una pagina di presentazione ma dietro
non c'e' niente".

`legit.py` decide se un token si puo' proporre (passa / non passa). Qui si
misura QUANTO e' solido, su tre pilastri, ciascuno 0-100 con i numeri che
lo giustificano e i campanelli d'allarme:

- GitHub: gli ultimi 100 commit (cadenza, settimane attive, autori,
  quanti sono solo caricamenti dal browser), quanto codice c'e', test, CI,
  e se il repository e' nato insieme al token;
- sito: l'HTML, non solo il testo: pagine interne, documentazione e app
  raggiungibili davvero, sezioni (team, roadmap, tokenomics, audit),
  costruttori a modello, "lorem ipsum", eta' del dominio (RDAP);
- tokenomics: chi tiene i token, esclusi pool e locker (primo holder, primi
  10, creatore, insider), autorita' di mint e freeze, liquidita' bloccata,
  tasse.

Ogni pilastro puo' anche bocciare (`fatal`): un progetto con il creatore
che tiene l'80% dell'offerta non entra in classifica, qualunque sia il
resto. Tutte le funzioni `judge_*` sono pure: si provano senza rete.
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

from app.trading.onchain import benchmark, legit, narratives

WEIGHTS = {"github": 0.40, "site": 0.30, "tokenomics": 0.30}

# GitHub
GH_COMMITS_PAGE = 100
GH_MIN_CODE_BYTES = 5_000          # sotto, e' un README con un file
GH_TRIVIAL_MAX = 0.70              # oltre, i commit sono solo caricamenti e ritocchi al README
GH_BORN_WITH_TOKEN_S = 3 * 86400
# I messaggi che GitHub scrive da solo quando si modifica un file dal browser.
_TRIVIAL = re.compile(
    r"^(initial commit|first commit|add files via upload|"
    r"(create|update|delete|rename|add) [\w./ -]+\.\w{1,6})$", re.I)
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "e2e"}

# Sito
SITE_FOLLOW = 3                    # pagine interne che si aprono davvero
SITE_MIN_WORDS_PAGE = 120          # una pagina interna "piena"
_BUILDERS = ("wix", "carrd", "framer", "webflow", "squarespace", "wordpress", "zyro", "hostinger",
             "tilda", "weebly", "godaddy", "site123", "durable", "mobirise")
_FREE_HOSTS = ("carrd.co", "framer.website", "framer.ai", "webflow.io", "vercel.app", "netlify.app",
               "github.io", "wixsite.com", "pages.dev", "web.app", "firebaseapp.com", "gitbook.io",
               "notion.site", "replit.app", "lovable.app", "bolt.new")
_LOREM = re.compile(r"lorem ipsum|dolor sit amet|your (company|project) name|coming soon", re.I)
_SECTIONS = {
    "team": re.compile(r"\b(team|founders?|about us|chi siamo|core contributors)\b", re.I),
    "roadmap": re.compile(r"\broad\s?map\b", re.I),
    "tokenomics": re.compile(r"\btoken\s?omics\b|\btoken distribution\b|\bsupply\b", re.I),
    "audit": re.compile(r"\baudit(ed|s)?\b|certik|hacken|halborn|ottersec|sec3|zellic", re.I),
}
_DOCS_HINT = re.compile(r"(^|[/.])(docs?|documentation|whitepaper|litepaper|gitbook|wiki|developers?|build|"
                        r"learn|guides?|research|laboratory|lab|spec|specification)([/.]|$)", re.I)
_APP_HINT = re.compile(r"(^|[/.])(app|dapp|dashboard|launch|platform|beta)([/.]|$)", re.I)
_APP_TEXT = re.compile(r"\b(launch app|open app|enter app|go to app|use app|try (it|now)|dashboard)\b", re.I)
RDAP = "https://rdap.org/domain/{domain}"

# Tokenomics
TOP1_MAX_PCT = 20.0
TOP10_MAX_PCT = 60.0
CREATOR_MAX_PCT = 15.0
TAX_MAX_PCT = 5.0
_POOLISH = {"AMM", "LOCKER", "BURN"}
_DEAD = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}


def _pillar(key: str, score: float | None, facts: dict, flags: list[str], fatal: str = "") -> dict:
    return {"key": key, "score": None if score is None else int(max(0, min(100, round(score)))),
            "facts": facts, "flags": flags, "fatal": fatal}


def _pending(key: str, why: str) -> dict:
    """Il dato non c'e' ancora (API giu', token troppo nuovo): si riprova."""
    return {"key": key, "score": None, "facts": {}, "flags": [], "fatal": "", "pending": why}


def _ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ GitHub

async def fetch_github(full_name: str, client: httpx.AsyncClient) -> dict | None:
    """Repo, ultimi 100 commit, contributori, linguaggi, cartella principale.
    None = GitHub non risponde (limite di richieste): si riprova."""
    base = f"{legit.GITHUB_API}/repos/{full_name}"
    h = legit._gh_headers()
    try:
        repo = await legit.gh_get(client, base, headers=h, timeout=8.0)
        if repo.status_code == 404:
            return {}
        if repo.status_code != 200:
            return None
        out = {"repo": repo.json()}
        for key, url, params in (("commits", f"{base}/commits", {"per_page": GH_COMMITS_PAGE}),
                                 ("contributors", f"{base}/contributors", {"per_page": 30, "anon": 1}),
                                 ("languages", f"{base}/languages", None),
                                 ("root", f"{base}/contents", None),
                                 ("participation", f"{base}/stats/participation", None)):
            r = await legit.gh_get(client, url, params=params, headers=h, timeout=8.0)
            if key == "participation" and r.status_code != 200:
                out[key] = {}   # 202: GitHub la sta ancora calcolando; si usano i 100 commit
            elif r.status_code in (204, 404, 409):  # vuoto, o niente contributori calcolati
                out[key] = [] if key != "languages" else {}
            elif r.status_code == 200:
                out[key] = r.json()
            else:
                return None
        return out
    except (httpx.HTTPError, ValueError):
        return None


def judge_github(data: dict | None, launched_at: float | None = None, now: float | None = None) -> dict:
    if data is None:
        return _pending("github", "GitHub not responding")
    if not data or not data.get("repo"):
        return _pillar("github", 0, {}, [], "GitHub link without a repository")
    now = now or time.time()
    repo, commits = data["repo"], [c for c in data.get("commits") or [] if isinstance(c, dict)]
    name = repo.get("full_name") or "?"
    if repo.get("fork"):
        return _pillar("github", 0, {"repo": name}, [], f"{name} is a fork")

    dates = [t for t in (_ts(((c.get("commit") or {}).get("author") or {}).get("date")) for c in commits) if t]
    messages = [((c.get("commit") or {}).get("message") or "").strip().splitlines()[0:1] for c in commits]
    trivial = sum(1 for m in messages if not m or _TRIVIAL.match(m[0]))
    authors = {(c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("email")
               for c in commits} - {None, ""}
    people = [x for x in data.get("contributors") or [] if isinstance(x, dict)]
    contributors = len(people)
    # La somma dei contributi dei primi 30: la storia intera, non solo gli ultimi 100 commit.
    total_commits = max(sum(int(x.get("contributions") or 0) for x in people), len(commits))
    languages = data.get("languages") or {}
    code = sum(v for v in languages.values() if isinstance(v, (int, float)))
    names = {(x.get("name") or "").lower() for x in data.get("root") or [] if isinstance(x, dict)}
    has_tests = bool(names & _TEST_DIRS) or any(n.startswith(("test_", "jest.config", "vitest.config",
                                                                "pytest.ini", "foundry.toml", "hardhat.config"))
                                                for n in names)
    has_ci = ".github" in names or ".gitlab-ci.yml" in names or ".circleci" in names
    has_readme = any(n.startswith("readme") for n in names)
    days = {time.strftime("%Y-%m-%d", time.gmtime(t)) for t in dates}
    weekly = [w for w in ((data.get("participation") or {}).get("all") or []) if isinstance(w, int)]
    if len(weekly) >= 13:
        # Le 52 settimane intere: i 100 commit di un repo molto attivo coprono pochi giorni.
        weeks12 = {i for i, n in enumerate(reversed(weekly[-12:])) if n}
        commits90 = sum(weekly[-13:])
    else:
        weeks12 = {int((now - t) // (7 * 86400)) for t in dates if now - t < 84 * 86400}
        commits90 = sum(1 for t in dates if now - t < 90 * 86400)
    last_days = (now - max(dates)) / 86400 if dates else None
    span_days = (max(dates) - min(dates)) / 86400 if dates else 0
    created = _ts(repo.get("created_at"))
    trivial_ratio = trivial / len(commits) if commits else 1.0
    top_langs = [k for k, _ in sorted(languages.items(), key=lambda kv: -kv[1])[:3]]

    facts = {
        "repo": name, "url": repo.get("html_url"), "description": (repo.get("description") or "")[:160],
        "commits": f"{len(commits)}+" if len(commits) >= GH_COMMITS_PAGE else len(commits),
        "commits_90d": commits90, "active_days": len(days), "active_weeks_12": len(weeks12),
        "span_days": round(span_days), "last_commit_days": None if last_days is None else round(last_days, 1),
        "authors": len(authors), "contributors": contributors, "code_kb": round(code / 1024),
        "languages": top_langs, "tests": has_tests, "ci": has_ci, "readme": has_readme,
        "license": ((repo.get("license") or {}).get("spdx_id")) or None,
        "stars": repo.get("stargazers_count") or 0, "forks": repo.get("forks_count") or 0,
        "trivial_pct": round(trivial_ratio * 100), "created_at": repo.get("created_at"),
        # Per il confronto con DREGG (benchmark.py).
        "languages_all": list(languages), "topics": (repo.get("topics") or [])[:12],
        "root_dirs": sorted(x.get("name") for x in data.get("root") or []
                            if isinstance(x, dict) and x.get("type") == "dir" and x.get("name")),
        "total_commits": total_commits,
    }
    flags: list[str] = []
    fatal = ""
    if not commits:
        fatal = "empty repository"
    elif code < GH_MIN_CODE_BYTES:
        fatal = f"almost no code ({code // 1024} KB)"
    elif len(days) < 2:
        fatal = "all commits on a single day"
    elif trivial_ratio > GH_TRIVIAL_MAX:
        fatal = f"{round(trivial_ratio * 100)}% of commits are uploads or README tweaks"
    if launched_at and created and 0 <= launched_at - created < GH_BORN_WITH_TOKEN_S and span_days < 3:
        flags.append("repository born with the token")
    if last_days is not None and last_days > 90:
        flags.append(f"idle for {round(last_days)} days")
    if len(authors) <= 1 and contributors <= 1:
        flags.append("single author")
    if trivial_ratio > 0.4 and not fatal:
        flags.append(f"{round(trivial_ratio * 100)}% trivial commits")
    if fatal:
        return _pillar("github", 0, facts, flags, fatal)

    recency = 0 if last_days is None else 10 if last_days <= 7 else 6 if last_days <= 30 else 3 if last_days <= 90 else 0
    score = (min(commits90, 60) / 60 * 25                       # attivita'
             + min(len(weeks12), 12) / 12 * 20                  # costanza
             + min(max(len(authors), contributors), 5) / 5 * 15 # squadra
             + min(max(math.log10(max(code, 1)) - 4, 0) / 2, 1) * 15  # codice: 10 KB -> 0, 1 MB -> pieno
             + 6 * has_tests + 5 * has_ci + 2 * has_readme + 2 * bool(facts["license"])
             + recency)
    score *= 1 - 0.5 * trivial_ratio
    if "repository born with the token" in flags:
        score *= 0.8
    return _pillar("github", score, facts, flags)


async def analyze_github(urls: list[str], client: httpx.AsyncClient, launched_at: float | None,
                         token: dict | None = None, website: str | None = None) -> dict:
    targets = legit.github_targets(urls)
    if not targets:
        return _pillar("github", 0, {}, [], "no GitHub")
    owner, name = next(((o, r) for o, r in targets if r), targets[0])
    try:
        repo = await legit.github_repo(owner, name, client)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        repo = None
    if repo is None:
        return _pending("github", "GitHub not responding")
    if not repo:
        return judge_github({}, launched_at)
    # Un link a una libreria o a una piattaforma non e' il codice del progetto.
    # github_verified: the README names the token's address, proof enough.
    if token and not token.get("github_verified") \
            and not benchmark.is_benchmark(token.get("chain"), token.get("token_address")) \
            and not legit.github_belongs(repo, token, website):
        return _pillar("github", 0, {"repo": repo.get("full_name")}, [], "GitHub does not belong to the project")
    return judge_github(await fetch_github(repo["full_name"], client), launched_at)


# -------------------------------------------------------------------- sito

class _Anatomy(HTMLParser):
    """Quello che serve dell'HTML, senza dipendenze: link, titoli, meta,
    script, moduli."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []   # (href, testo)
        self.headings = 0
        self.scripts = 0
        self.forms = 0
        self.nav = False
        self.meta: dict[str, str] = {}
        self.icon = False
        self.root_div = False
        self._a: str | None = None
        self._a_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._a, self._a_text = a["href"], []
        elif tag in ("h1", "h2", "h3"):
            self.headings += 1
        elif tag == "script":
            self.scripts += 1
        elif tag == "form":
            self.forms += 1
        elif tag == "nav":
            self.nav = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a.get("content", "")
        elif tag == "link" and "icon" in a.get("rel", "").lower():
            self.icon = True
        elif tag == "div" and a.get("id") in ("root", "app", "__next", "__nuxt", "svelte"):
            self.root_div = True

    def handle_data(self, data):
        if self._a is not None:
            self._a_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._a is not None:
            self.links.append((self._a, " ".join("".join(self._a_text).split())[:80]))
            self._a = None


def _site_root(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def anatomy(html_text: str, base_url: str) -> dict:
    """Cosa c'e' nella pagina: pagine interne, documentazione, app, sezioni."""
    p = _Anatomy()
    try:
        p.feed(html_text)
    except Exception:  # HTML rotto: si tiene quello che si e' letto
        pass
    host = (urlparse(base_url).hostname or "").lower()
    root = _site_root(host.removeprefix("www."))
    internal, docs, app, github = [], [], [], []
    for href, text in p.links:
        url = urljoin(base_url, href.strip())
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            continue
        h = u.hostname.lower()
        target = (h + u.path.rstrip("/")).lower()
        if "github.com" in h:
            github.append(url)
        same = h == host or h.endswith("." + root) or h == root
        if _DOCS_HINT.search(target) and url not in docs:
            docs.append(url)
        elif same and (_APP_HINT.search(target) or _APP_TEXT.search(text)) and url not in app:
            app.append(url)
        if same and (h != host or u.path.rstrip("/") not in ("", urlparse(base_url).path.rstrip("/"))):
            key = h + u.path.rstrip("/")
            if key not in [x[0] for x in internal]:
                internal.append((key, url))
    _, text = legit.page_text(html_text)
    generator = p.meta.get("generator", "").lower()
    builder = next((b for b in _BUILDERS if b in generator or f"{b}." in html_text[:20000].lower()), None)
    return {
        "words": len(text.split()),
        "text": text,
        "internal": [u for _, u in internal],
        "docs": docs, "app": app, "github": github,
        "sections": sorted(k for k, rx in _SECTIONS.items() if rx.search(text)),
        "headings": p.headings, "scripts": p.scripts, "forms": p.forms, "nav": p.nav,
        "meta_description": bool(p.meta.get("description") or p.meta.get("og:description")),
        "og_image": bool(p.meta.get("og:image") or p.meta.get("twitter:image")),
        "icon": p.icon,
        "spa": p.root_div and p.scripts > 0,
        "builder": builder,
        "free_host": next((f for f in _FREE_HOSTS if host == f or host.endswith("." + f)), None),
        "lorem": bool(_LOREM.search(text)),
    }


# Siti che esistono a prescindere dal token: un link li' non dice niente del progetto.
_GENERIC = ("wikipedia.org", "google.com", "apple.com", "amazon.com", "reddit.com", "facebook.com",
            "coingecko.com", "coinmarketcap.com", "solscan.io", "etherscan.io", "pump.fun", "jup.ag",
            "raydium.io", "binance.com", "archive.org", "imgur.com", "ipfs.io")


def _not_own(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    return next((g for g in _GENERIC if host == g or host.endswith("." + g)), None)


def _registration(rdap: dict | None) -> float | None:
    for ev in (rdap or {}).get("events") or []:
        if ev.get("eventAction") == "registration":
            return _ts(ev.get("eventDate"))
    return None


def judge_site(page: dict | None, followed: list[dict], registered_at: float | None,
               token_address: str = "", launched_at: float | None = None, now: float | None = None) -> dict:
    """page: esito di `anatomy` + url/https; followed: le pagine interne
    aperte davvero, ognuna {kind: docs|app|page, ok, words}."""
    if page is None:
        return _pending("site", "website unreachable")
    now = now or time.time()
    docs_ok = any(f["ok"] and f["kind"] == "docs" and f["words"] >= SITE_MIN_WORDS_PAGE for f in followed)
    app_ok = any(f["ok"] and f["kind"] == "app" for f in followed)
    pages_ok = sum(1 for f in followed if f["ok"] and f["words"] >= SITE_MIN_WORDS_PAGE)
    domain_days = (now - registered_at) / 86400 if registered_at else None
    before_launch = (launched_at - registered_at) / 86400 if registered_at and launched_at else None
    cites = bool(token_address) and token_address.lower() in (page.get("text") or "").lower()
    facts = {
        "url": page.get("url"), "words": page["words"], "internal_pages": len(page["internal"]),
        "pages_ok": pages_ok, "docs": bool(page["docs"]), "docs_ok": docs_ok,
        "app": bool(page["app"]), "app_ok": app_ok, "sections": page["sections"],
        "builder": page["builder"], "free_host": page["free_host"], "spa": page["spa"],
        "domain_days": None if domain_days is None else round(domain_days),
        "cites_contract": cites, "https": bool(page.get("https")),
        "tech_terms": benchmark.site_terms(page.get("text") or ""),
        "narratives": narratives.match(page.get("text") or ""),
    }
    flags: list[str] = []
    if page.get("not_own"):
        return _pillar("site", 0, facts, flags, f"not the project's website ({page['not_own']})")
    if page["lorem"]:
        return _pillar("site", 0, facts, flags, "placeholder text (lorem ipsum / coming soon)")
    empty = page["words"] < 40 and not page["internal"] and not page["spa"]
    if empty:
        return _pillar("site", 0, facts, flags, "empty page")
    if not page["internal"] and not page["docs"] and not page["app"]:
        flags.append("single page, nothing behind it")
    if page["builder"]:
        flags.append(f"built with {page['builder']}")
    if page["free_host"]:
        flags.append(f"on a free domain ({page['free_host']})")
    if before_launch is not None and before_launch < 7:
        flags.append("domain registered with the token" if before_launch >= -1 else "domain younger than the token")
    if page["spa"] and page["words"] < 80:
        flags.append("content rendered via JavaScript")

    domain = (0 if domain_days is None else 13 if domain_days >= 180 else 8 if domain_days >= 30
              else 4 if domain_days >= 7 else 0)
    score = (min(page["words"], 1500) / 1500 * 18
             + min(len(page["internal"]), 8) / 8 * 12 + min(pages_ok, SITE_FOLLOW) / SITE_FOLLOW * 8
             + (15 if docs_ok else 7 if page["docs"] else 0)
             + (15 if app_ok else 7 if page["app"] else 0)
             + 3 * len(page["sections"])
             + domain
             + 2 * bool(page.get("https")) + 2 * page["meta_description"] + 2 * page["og_image"]
             + 1 * page["icon"] + 2 * cites)
    if page["free_host"]:
        score -= 10
    if page["builder"]:
        score -= 4
    return _pillar("site", score, facts, flags)


async def analyze_site(url: str | None, client: httpx.AsyncClient, token_address: str = "",
                       launched_at: float | None = None) -> tuple[dict, list[str]]:
    """(pilastro, link a GitHub trovati nell'HTML)."""
    if not url:
        return _pillar("site", 0, {}, [], "no own website"), []
    site = await legit.fetch_site(url, client)
    if not site.get("ok"):
        return _pending("site", site.get("error") or "website unreachable"), []
    page = anatomy(site.get("html") or "", site["final_url"])
    page.update(url=site["final_url"], https=site["final_url"].startswith("https://"),
                not_own=_not_own(site["final_url"]))
    followed: list[dict] = []
    todo = ([("docs", u) for u in page["docs"][:1]] + [("app", u) for u in page["app"][:1]]
            + [("page", u) for u in page["internal"] if u not in page["docs"][:1] + page["app"][:1]])
    for kind, u in todo[:SITE_FOLLOW]:
        sub = await legit.fetch_site(u, client)
        followed.append({"kind": kind, "ok": bool(sub.get("ok")),
                         "words": len((sub.get("text") or "").split())})
    registered = None
    host = (urlparse(site["final_url"]).hostname or "").removeprefix("www.")
    if host and not page["free_host"]:
        try:
            r = await client.get(RDAP.format(domain=_site_root(host)), timeout=8.0, follow_redirects=True)
            registered = _registration(r.json()) if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            registered = None
    return judge_site(page, followed, registered, token_address, launched_at), page["github"]


# -------------------------------------------------------------- tokenomics

async def rugcheck_report(mint: str, client: httpx.AsyncClient) -> dict | None:
    try:
        r = await client.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=12.0)
    except httpx.HTTPError:
        return None
    return r.json() if r.status_code == 200 else None


def judge_tokenomics_solana(report: dict | None, exclude: set[str] | None = None) -> dict:
    """Dal report completo di RugCheck. `exclude`: conti che non sono
    persone (curva di pump.fun, pool) oltre a quelli che RugCheck conosce."""
    if not report or not report.get("topHolders") and not report.get("token"):
        return _pending("tokenomics", "RugCheck does not have the token yet")
    known = report.get("knownAccounts") or {}
    skip = set(exclude or ()) | {a for a, v in known.items() if (v or {}).get("type") in _POOLISH}
    for m in report.get("markets") or []:
        skip |= {v for v in (m.get("pubkey"), m.get("liquidityA"), m.get("liquidityB"),
                             (m.get("liquidityAAccount") or {}).get("owner"),
                             (m.get("liquidityBAccount") or {}).get("owner")) if isinstance(v, str)}
    holders = [h for h in report.get("topHolders") or []
               if h.get("address") not in skip and h.get("owner") not in skip]
    pcts = sorted((float(h.get("pct") or 0) for h in holders), reverse=True)
    supply = float((report.get("token") or {}).get("supply") or 0)
    creator_pct = float(report.get("creatorBalance") or 0) / supply * 100 if supply else 0.0
    insiders = sum(1 for h in holders if h.get("insider")) + int(report.get("graphInsidersDetected") or 0)
    lp_locked = max((float((m.get("lp") or {}).get("lpLockedPct") or 0) for m in report.get("markets") or []),
                    default=None)
    fee = float((report.get("transferFee") or {}).get("pct") or 0)
    return _judge_tokenomics(
        top1=pcts[0] if pcts else 0.0, top10=sum(pcts[:10]), creator=creator_pct,
        holders=int(report.get("totalHolders") or 0), insiders=insiders, lp_locked=lp_locked,
        mintable=bool(report.get("mintAuthority")), freezable=bool(report.get("freezeAuthority")),
        tax=fee, rugged=bool(report.get("rugged")))


def judge_tokenomics_evm(info: dict | None, pair_address: str = "") -> dict:
    """Da GoPlus (Robinhood Chain): holders con percentuale 0-1."""
    if not info:
        return _pending("tokenomics", "GoPlus does not have the token yet")

    def pct(v) -> float:
        try:
            return float(v or 0) * 100
        except (TypeError, ValueError):
            return 0.0

    pair = pair_address.lower()
    holders = [h for h in info.get("holders") or []
               if (h.get("address") or "").lower() not in _DEAD | {pair}
               and not str(h.get("is_locked")) == "1"
               and not re.search(r"pool|pair|lp|lock|burn", h.get("tag") or "", re.I)]
    pcts = sorted((pct(h.get("percent")) for h in holders), reverse=True)
    lps = info.get("lp_holders") or []
    lp_locked = sum(pct(h.get("percent")) for h in lps
                    if str(h.get("is_locked")) == "1" or (h.get("address") or "").lower() in _DEAD) if lps else None
    return _judge_tokenomics(
        top1=pcts[0] if pcts else 0.0, top10=sum(pcts[:10]),
        creator=max(pct(info.get("creator_percent")), pct(info.get("owner_percent"))),
        holders=int(info.get("holder_count") or 0), insiders=0, lp_locked=lp_locked,
        mintable=str(info.get("is_mintable")) == "1", freezable=str(info.get("transfer_pausable")) == "1",
        tax=max(pct(info.get("buy_tax")), pct(info.get("sell_tax"))),
        rugged=str(info.get("is_honeypot")) == "1")


def _judge_tokenomics(*, top1: float, top10: float, creator: float, holders: int, insiders: int,
                      lp_locked: float | None, mintable: bool, freezable: bool, tax: float,
                      rugged: bool) -> dict:
    facts = {"top1_pct": round(top1, 1), "top10_pct": round(top10, 1), "creator_pct": round(creator, 1),
             "holders": holders, "insiders": insiders,
             "lp_locked_pct": None if lp_locked is None else round(min(lp_locked, 100), 1),
             "mint": mintable, "freeze": freezable, "tax_pct": round(tax, 1)}
    fatal = ("already rugged or honeypot" if rugged
             else "new tokens can be minted (mint enabled)" if mintable
             else "accounts can be frozen (freeze enabled)" if freezable
             else f"creator holds {creator:.0f}%" if creator > CREATOR_MAX_PCT
             else f"a single wallet holds {top1:.0f}%" if top1 > TOP1_MAX_PCT
             else f"top 10 hold {top10:.0f}%" if top10 > TOP10_MAX_PCT
             else f"{tax:.0f}% tax" if tax > TAX_MAX_PCT
             else "")
    flags = []
    if insiders:
        flags.append(f"{insiders} wallets linked to each other")
    if lp_locked is not None and lp_locked < 80:
        flags.append(f"only {lp_locked:.0f}% of liquidity locked")
    if creator > 5:
        flags.append(f"creator at {creator:.0f}%")
    if fatal:
        return _pillar("tokenomics", 0, facts, flags, fatal)
    score = (max(0.0, min(1.0, (TOP10_MAX_PCT - top10) / (TOP10_MAX_PCT - 15))) * 40
             + (15 if creator <= 1 else 10 if creator <= 5 else 4)
             + min(max(math.log10(max(holders, 1)) - 1.7, 0) / 2, 1) * 15   # 50 holder -> 0, 5000 -> pieno
             + (15 if lp_locked is None or lp_locked >= 90 else lp_locked / 90 * 15)
             + max(0, 10 - 4 * insiders)
             + 5 * (tax == 0))
    return _pillar("tokenomics", score, facts, flags)


async def analyze_tokenomics(chain: str, token: str, client: httpx.AsyncClient, *, pair_address: str = "",
                             exclude: set[str] | None = None) -> dict:
    if chain == "solana":
        return judge_tokenomics_solana(await rugcheck_report(token, client), exclude)
    if chain in legit.GOPLUS_CHAIN:
        try:
            info = await legit.goplus(chain, token, client)
        except (httpx.HTTPError, ValueError):
            info = None
        return judge_tokenomics_evm(info, pair_address)
    return _pillar("tokenomics", None, {}, [], "chain not covered")


# ----------------------------------------------------------------- insieme

def overall(pillars: dict[str, dict]) -> dict:
    """{score, fatal, pending, weakest}. Un pilastro bocciato boccia tutto;
    uno in attesa lascia il progetto fuori finche' non arriva."""
    fatal = next((f"{k}: {p['fatal']}" for k, p in pillars.items() if p.get("fatal")), "")
    pending = next((f"{k}: {p['pending']}" for k, p in pillars.items() if p.get("pending")), "")
    scored = {k: p["score"] for k, p in pillars.items() if p.get("score") is not None}
    score = sum(WEIGHTS[k] * s for k, s in scored.items()) / sum(WEIGHTS[k] for k in scored) if scored else 0
    weakest = min(scored, key=scored.get) if scored else None
    return {"score": 0 if fatal else round(score, 1), "fatal": fatal, "pending": "" if fatal else pending,
            "weakest": weakest}


async def analyze(item: dict, client: httpx.AsyncClient, *, skip: set[str] = frozenset()) -> dict:
    """item: chain, token_address, pair_address, website, github, links,
    launched_at (+ bonding_curve per pump.fun). `skip`: pilastri ancora
    freschi da non rifare. Ritorna solo i pilastri rifatti, e si ferma al
    primo bocciato: le chiamate costano e il verdetto non cambierebbe."""
    out: dict[str, dict] = {}
    launched = item.get("launched_at")
    gh_links = [item.get("github") or ""] + list(item.get("links") or [])
    if "site" not in skip:
        out["site"], found = await analyze_site(item.get("website"), client, item.get("token_address") or "",
                                                launched)
        gh_links += found
        if out["site"]["fatal"]:
            return out
    if "github" not in skip:
        if not legit.github_targets([u for u in gh_links if u]):
            found_gh = await legit.github_find(item.get("website"), client)
            gh_links += [found_gh] if found_gh else []
        out["github"] = await analyze_github([u for u in gh_links if u], client, launched,
                                             token=item, website=item.get("website"))
        if out["github"]["fatal"]:
            return out
    if "tokenomics" not in skip:
        exclude = {x for x in (item.get("bonding_curve"), item.get("associated_bonding_curve"),
                               item.get("pair_address")) if x}
        out["tokenomics"] = await analyze_tokenomics(item["chain"], item["token_address"], client,
                                                     pair_address=item.get("pair_address") or "",
                                                     exclude=exclude)
    return out
