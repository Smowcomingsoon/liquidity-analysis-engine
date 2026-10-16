"""What a project's own Telegram channel says, for DeepSeek's dossier.

Lorenzo, 29/09: X can't be read for free, but a public Telegram channel
can: https://t.me/s/<name> is a plain HTML page, no account needed, with
the subscriber count and the latest ~20 posts (date, views, text). That is
how often the team posts, how many people read it, and what they say.

Only a handle the project itself gives (its socials or links) is read,
never a guess from the name. When there is no public preview, t.me/s/
redirects to t.me/<name>: that page still says how many members a group
has, or that the name does not exist.

Post texts are untrusted: they go to DeepSeek inside the project-text tags.
Counts and views are harder to fake than words.
"""
from __future__ import annotations

import html
import json
import logging
import re
import statistics
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from app.trading.onchain import storage
from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

BASE = "https://t.me"
TTL_S = 12 * 3600
DAILY_MAX = 300           # page fetches a day, across every project
TIMEOUT_S = 15.0
RECENT_POSTS = 5
POST_CHARS = 300
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FILE = storage.data_file("onchain_telegram.json")

_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me", "telegram.dog"}
_HANDLE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_RESERVED = {"joinchat", "addstickers", "share", "proxy", "socks", "iv", "login", "setlanguage", "addlist"}

_cache: dict[str, dict] = {}
_calls: dict[str, int] = {}


def _load() -> None:
    try:
        with open(_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        _cache.update(data.get("cache") or {})
        _calls.update(data.get("calls") or {})
    except FileNotFoundError:
        pass
    except Exception:
        logger.exception("telegram: cache non leggibile")


def _save() -> None:
    try:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        calls = {d: n for d, n in _calls.items() if d >= day}
        write_text_atomic(_FILE, json.dumps({"cache": _cache, "calls": calls}))
    except Exception:
        logger.exception("telegram: cache non salvata")


def reset_for_tests() -> None:
    _cache.clear()
    _calls.clear()


# --- which channel -----------------------------------------------------------

def _handle_of(url: str, typed: bool = False) -> str | None:
    """The public name in a t.me link; invite links (+x, joinchat) have none.
    A bare "@name" counts only when the social is typed telegram."""
    url = (url or "").strip()
    if not url:
        return None
    if typed and not url.lower().startswith(("http", "t.me", "www.", "telegram.")):
        name = url.lstrip("@")
        return name if _HANDLE.match(name) else None
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlparse(url)
    except ValueError:
        return None
    if (p.hostname or "").lower() not in _HOSTS:
        return None
    parts = [s for s in p.path.split("/") if s]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts:
        return None
    name = parts[0].lstrip("@")
    if name.lower() in _RESERVED or not _HANDLE.match(name):
        return None
    return name


def handle(e: dict) -> str | None:
    """The first Telegram name the project gives itself: typed socials
    first, then any t.me link among its links or DexScreener info."""
    socials = list(e.get("socials") or [])
    socials += list(((e.get("info") or {}).get("socials")) or [])
    for s in socials:
        if isinstance(s, dict) and (s.get("type") or "").lower() == "telegram":
            name = _handle_of(s.get("url") or s.get("handle") or "", typed=True)
            if name:
                return name
    urls = [s.get("url") for s in socials if isinstance(s, dict)]
    urls += [u if isinstance(u, str) else (u or {}).get("url") for u in e.get("links") or []]
    for u in urls:
        name = _handle_of(u or "")
        if name:
            return name
    return None


# --- the page ----------------------------------------------------------------

def _num(text: str) -> int | None:
    """"9.44M", "4.2K", "2 073" (thin or no-break spaces), "1,234"."""
    t = (text or "").strip().replace("\xa0", "").replace(" ", "").replace(" ", "").replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([KMB]?)$", t, re.I)
    if not m:
        return None
    mult = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"[ \t]+", " ", html.unescape(fragment)).strip()


def _ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _posts(page: str) -> list[dict]:
    """The message blocks of a t.me/s page, oldest first: id, time, views, text."""
    posts = []
    for block in page.split('class="tgme_widget_message_wrap')[1:]:
        when = re.search(r'<time datetime="([^"]+)"', block)
        at = _ts(when.group(1)) if when else None
        if at is None:
            continue
        pid = re.search(r'data-post="[^/"]+/(\d+)"', block)
        views = re.search(r'class="tgme_widget_message_views">([^<]*)<', block)
        body = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        posts.append({"id": int(pid.group(1)) if pid else None, "at": at,
                      "views": _num(views.group(1)) if views else None,
                      "text": _text(body.group(1)) if body else ""})
    posts.sort(key=lambda p: p["at"])
    return posts


def parse_channel(page: str, now: float) -> dict:
    """The preview page of a public channel: subscribers and recent posts."""
    subs = None
    for value, kind in re.findall(r'class="counter_value">([^<]*)</span>\s*<span class="counter_type">([^<]*)', page):
        if kind.strip().lower() in ("subscribers", "subscriber", "members", "member"):
            subs = _num(value)
            break
    posts = _posts(page)
    ages = [(now - p["at"]) / 86400 for p in posts]
    views = [p["views"] for p in posts if p["views"] is not None]
    oldest_in_30 = bool(posts) and ages[0] <= 30
    return {
        "kind": "channel", "subs": subs,
        "posts_7d": sum(1 for a in ages if a <= 7),
        "posts_30d": sum(1 for a in ages if a <= 30),
        # The page holds ~20 posts: when all of them are inside 30 days the
        # count is a floor, not the real number.
        "posts_30d_capped": oldest_in_30,
        "last_post_days": round(max(ages[-1], 0), 1) if posts else None,
        "median_views": int(statistics.median(views)) if views else None,
        "recent_posts": [p["text"][:POST_CHARS] for p in reversed(posts) if p["text"]][:RECENT_POSTS],
    }


def parse_landing(page: str) -> dict:
    """t.me/<name> when there is no preview: a group says its members, a
    channel with the preview off its subscribers, a free name nothing."""
    extra = re.search(r'class="tgme_page_extra">([^<]*)<', page)
    if not extra:
        return {"kind": "missing"}
    text = html.unescape(extra.group(1))
    m = re.match(r"\s*([\d\s\xa0 .,KMB]+?)\s*(members?|subscribers?)", text, re.I)
    if not m:
        return {"kind": "missing"}
    count = _num(m.group(1))
    if m.group(2).lower().startswith("member"):
        online = re.search(r"([\d\s\xa0 .,]+)\s*online", text)
        return {"kind": "group", "subs": count, "online": _num(online.group(1)) if online else None}
    return {"kind": "no_preview", "subs": count}


# --- fetch ----------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url, headers={"User-Agent": UA, "Accept-Language": "en"}, follow_redirects=False)


async def _read(name: str, now: float, client: httpx.AsyncClient, day: str) -> dict | None:
    r = await _get(client, f"{BASE}/s/{name}")
    if r.status_code == 200 and ("tgme_widget_message_wrap" in r.text or "tgme_channel_info" in r.text):
        return parse_channel(r.text, now)
    if r.status_code == 404:
        return {"kind": "missing"}
    if r.status_code not in (301, 302, 200) or _calls.get(day, 0) >= DAILY_MAX:
        return None
    _calls[day] = _calls.get(day, 0) + 1
    r = await _get(client, f"{BASE}/{name}")
    if r.status_code == 404:
        return {"kind": "missing"}
    if r.status_code != 200:
        return None
    return parse_landing(r.text)


async def lookup(e: dict, now: float | None = None, client: httpx.AsyncClient | None = None) -> dict | None:
    """The project's own channel, cached 12 hours. None without a handle,
    over the daily budget, or when Telegram cannot be reached."""
    name = handle(e)
    if not name:
        return None
    now = now or time.time()
    key = name.lower()
    hit = _cache.get(key)
    if hit and now - hit.get("at", 0) < TTL_S:
        return hit["tg"]
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if _calls.get(day, 0) >= DAILY_MAX:
        return None
    _calls[day] = _calls.get(day, 0) + 1
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
                tg = await _read(name, now, c, day)
        else:
            tg = await _read(name, now, client, day)
    except httpx.HTTPError as exc:
        logger.info("telegram: non raggiungibile: %s", exc)
        return None
    if tg is None:
        return None
    tg["handle"] = name
    _cache[key] = {"at": now, "tg": tg}
    _save()
    return tg


def compact(tg: dict | None) -> dict | None:
    """What the judge stores and the page shows: numbers, no texts."""
    if not tg:
        return None
    return {k: tg.get(k) for k in ("handle", "kind", "subs", "online", "posts_7d", "posts_30d", "last_post_days", "median_views")
            if tg.get(k) is not None}


# --- history -----------------------------------------------------------------

HISTORY_PAGES = 5
HISTORY_DAYS = 90
HISTORY_CHARS = 1500


async def history(name: str, now: float, client: httpx.AsyncClient,
                  max_pages: int = HISTORY_PAGES, days: int = HISTORY_DAYS,
                  after_id: int | None = None) -> list[dict]:
    """Page back through a public channel's posts, up to `max_pages` pages,
    stopping at posts older than `days`, at `after_id` (already read), or
    when a page adds nothing new. Each page counts in the same daily budget
    as `lookup`."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    cutoff = now - days * 86400
    seen: dict[int, dict] = {}
    before: int | None = None
    for _ in range(max_pages):
        if _calls.get(day, 0) >= DAILY_MAX:
            break
        _calls[day] = _calls.get(day, 0) + 1
        url = f"{BASE}/s/{name}" + (f"?before={before}" if before else "")
        r = await _get(client, url)
        if r.status_code != 200 or "tgme_widget_message_wrap" not in r.text:
            break
        page = _posts(r.text)
        if not page:
            break
        added = 0
        for p in page:
            if p["id"] is None or p["id"] in seen:
                continue
            seen[p["id"]] = p
            added += 1
        ids = [p["id"] for p in page if p["id"] is not None]
        if not ids:
            break
        oldest = min(ids)
        if added == 0:
            break
        before = oldest
        if page[0]["at"] < cutoff or (after_id is not None and oldest <= after_id):
            break
    out = [p for p in seen.values() if p["at"] >= cutoff]
    out.sort(key=lambda p: p["at"])
    for p in out:
        p["text"] = p["text"][:HISTORY_CHARS]
    return out


_load()
