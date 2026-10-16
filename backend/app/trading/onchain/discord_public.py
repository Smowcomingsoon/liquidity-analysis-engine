"""What a project's own Discord server says, for DeepSeek's dossier.

Lorenzo, 01/10: "possiamo fare lo stesso con discord?" (same as Telegram).
A Discord invite can be read without any account:
https://discord.com/api/v10/invites/<code>?with_counts=true returns the
member count, how many are online, and the server name. No messages: never
log in with a user account.

Only an invite the project itself gives (its socials or links) is read,
never a guess from the name. A 404 means the invite is expired or invalid.

Counts are harder to fake than words, but a few online out of many members
means a quiet or inflated server: weak evidence, not proof of a scam.
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlparse

import httpx

from app.trading.onchain import storage
from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

BASE = "https://discord.com/api/v10"
TTL_S = 12 * 3600
DAILY_MAX = 300           # invite lookups a day, across every project
TIMEOUT_S = 15.0
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_FILE = storage.data_file("onchain_discord.json")

_HOSTS = {"discord.gg", "www.discord.gg", "discord.com", "www.discord.com",
          "discordapp.com", "www.discordapp.com"}
_CODE = re.compile(r"^[A-Za-z0-9-]{2,64}$")

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
        logger.exception("discord: cache non leggibile")


def _save() -> None:
    try:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        calls = {d: n for d, n in _calls.items() if d >= day}
        write_text_atomic(_FILE, json.dumps({"cache": _cache, "calls": calls}))
    except Exception:
        logger.exception("discord: cache non salvata")


def reset_for_tests() -> None:
    _cache.clear()
    _calls.clear()


# --- which invite ------------------------------------------------------------

def _code_of(url: str, typed: bool = False) -> str | None:
    """The invite code in a discord.gg/<code> or discord.com/invite/<code>
    link. Channel and user URLs have none."""
    url = (url or "").strip()
    if not url:
        return None
    if typed and not url.lower().startswith(("http", "discord.")):
        code = url.lstrip("@")
        return code if _CODE.match(code) else None
    if "://" not in url:
        url = "https://" + url
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host not in _HOSTS:
        return None
    parts = [s for s in p.path.split("/") if s]
    if not parts:
        return None
    if host in ("discord.gg", "www.discord.gg"):
        code = parts[0]
    elif len(parts) >= 2 and parts[0] == "invite":
        code = parts[1]
    else:
        return None
    return code if _CODE.match(code) else None


def invite_code(e: dict) -> str | None:
    """The first Discord invite the project gives itself: typed socials
    first, then any discord.gg / discord.com/invite link among its links
    or DexScreener info."""
    socials = list(e.get("socials") or [])
    socials += list(((e.get("info") or {}).get("socials")) or [])
    for s in socials:
        if isinstance(s, dict) and (s.get("type") or "").lower() == "discord":
            code = _code_of(s.get("url") or s.get("handle") or "", typed=True)
            if code:
                return code
    urls = [s.get("url") for s in socials if isinstance(s, dict)]
    urls += [u if isinstance(u, str) else (u or {}).get("url") for u in e.get("links") or []]
    for u in urls:
        code = _code_of(u or "")
        if code:
            return code
    return None


# --- fetch ----------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url, headers={"User-Agent": UA, "Accept": "application/json"})


async def _read(code: str, client: httpx.AsyncClient) -> dict | None:
    r = await _get(client, f"{BASE}/invites/{code}?with_counts=true")
    if r.status_code == 404:
        return {"kind": "missing"}
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    guild = data.get("guild") or {}
    return {"kind": "server", "code": code, "name": guild.get("name"),
            "members": data.get("approximate_member_count"),
            "online": data.get("approximate_presence_count")}


async def lookup(e: dict, now: float | None = None, client: httpx.AsyncClient | None = None) -> dict | None:
    """The project's own server, cached 12 hours. None without an invite,
    over the daily budget, or when Discord cannot be reached."""
    code = invite_code(e)
    if not code:
        return None
    now = now or time.time()
    key = code.lower()
    hit = _cache.get(key)
    if hit and now - hit.get("at", 0) < TTL_S:
        return hit["dc"]
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if _calls.get(day, 0) >= DAILY_MAX:
        return None
    _calls[day] = _calls.get(day, 0) + 1
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as c:
                dc = await _read(code, c)
        else:
            dc = await _read(code, client)
    except httpx.HTTPError as exc:
        logger.info("discord: non raggiungibile: %s", exc)
        return None
    if dc is None:
        return None
    dc["code"] = code
    _cache[key] = {"at": now, "dc": dc}
    _save()
    return dc


def compact(dc: dict | None) -> dict | None:
    """What the judge stores and the page shows: numbers, no texts."""
    if not dc:
        return None
    return {k: dc.get(k) for k in ("code", "kind", "name", "members", "online")
            if dc.get(k) is not None}


_load()
