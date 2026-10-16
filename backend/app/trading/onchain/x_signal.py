"""What X says about a project, for DeepSeek's dossier.

Lorenzo, 28/09: "se serve, c'è un modo di poter automatizzare un account su
X, facciamolo [...] in modo tale che lui vada a scansionare tutti quelli che
sono i post [...] filtrati a monte dal nostro database che esclude gli
scam". Only projects already past our filters are searched, each at most
once a day: the X API is paid per read. Without `settings.x_bearer_token`
nothing is called and the dossier says X was not read.

Posts are untrusted text: they go to DeepSeek inside the project-text tags,
with counts (posts, distinct authors, followers) that are harder to fake
than words.
"""
from __future__ import annotations

import logging
import time

import httpx

from app import http_client

from app.config import settings

logger = logging.getLogger(__name__)

SEARCH = "https://api.x.com/2/tweets/search/recent"
TTL_S = 24 * 3600
DAILY_MAX = 150
MAX_RESULTS = 25

_cache: dict[str, dict] = {}
_calls: dict[str, int] = {}


def enabled() -> bool:
    return bool(settings.x_bearer_token)


def query(e: dict) -> str:
    """The cashtag, and the name when it is distinctive enough."""
    parts = []
    sym = (e.get("symbol") or "").strip().lstrip("$")
    if sym.isalnum() and 2 <= len(sym) <= 10:
        parts.append(f"${sym}")
    name = (e.get("name") or "").strip()
    if len(name) >= 5 and name.lower() != sym.lower():
        parts.append(f'"{name}"')
    if e.get("token_address"):
        parts.append(e["token_address"])
    return f"({' OR '.join(parts)}) -is:retweet" if parts else ""


def summarize(data: dict) -> dict:
    tweets = data.get("data") or []
    users = {u["id"]: u for u in (data.get("includes") or {}).get("users") or [] if u.get("id")}
    authors = {t.get("author_id") for t in tweets if t.get("author_id")}
    followers = sorted(((users.get(a) or {}).get("public_metrics") or {}).get("followers_count", 0) for a in authors)
    top = sorted(tweets, key=lambda t: -sum((t.get("public_metrics") or {}).get(k, 0)
                                             for k in ("like_count", "retweet_count", "reply_count")))[:5]
    return {"posts_7d": len(tweets), "authors": len(authors),
            "median_followers": followers[len(followers) // 2] if followers else 0,
            "top_posts": [(t.get("text") or "")[:220] for t in top]}


async def lookup(e: dict, now: float | None = None, client: httpx.AsyncClient | None = None) -> dict | None:
    """Recent posts about the project, cached a day. None when X is off,
    over budget or unreachable."""
    if not enabled():
        return None
    now = now or time.time()
    key = f"{e.get('chain')}:{e.get('token_address')}"
    hit = _cache.get(key)
    if hit and now - hit["at"] < TTL_S:
        return hit["x"]
    q = query(e)
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if not q or _calls.get(day, 0) >= DAILY_MAX:
        return None
    _calls[day] = _calls.get(day, 0) + 1
    params = {"query": q, "max_results": MAX_RESULTS, "tweet.fields": "public_metrics,created_at,author_id",
              "expansions": "author_id", "user.fields": "public_metrics"}
    headers = {"Authorization": f"Bearer {settings.x_bearer_token}"}
    try:
        if client is None:
            async with http_client.pooled(timeout=15.0) as c:
                r = await c.get(SEARCH, params=params, headers=headers)
        else:
            r = await client.get(SEARCH, params=params, headers=headers)
    except httpx.HTTPError as exc:
        logger.info("X: non raggiungibile: %s", exc)
        return None
    if r.status_code != 200:
        logger.info("X: risposta %s", r.status_code)
        return None
    x = summarize(r.json())
    _cache[key] = {"at": now, "x": x}
    return x


def reset_for_tests() -> None:
    _cache.clear()
    _calls.clear()
