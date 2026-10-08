"""Narratives the Gem Finder hunts for: privacy, zero knowledge, AI agents,
agentic operating systems, money managed by AI, and the rest.

Lorenzo (25/09): "dobbiamo inserire nel cacciatore di gemme anche la parte
di narrativa, quelle che vanno piu' forte ora [...] trova quanti piu'
argomenti essenziali per il futuro delle cripto e dei soldi gestiti da AI".

The map lives in narratives.json (key, label, why, heat 1-3, regex terms,
CoinGecko categories, GitHub search phrases). It serves twice:
- as a source: discovery walks the CoinGecko categories and runs the GitHub
  phrases, so projects are searched by theme, not only by chain;
- as a signal: the terms are matched on site text, README topics and the
  project description, and a hot narrative lifts the final score a little.
It never lets a weak project in: qualification stays on the three pillars.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).with_name("narratives.json")
BONUS_MAX = 6.0          # points on the final score, out of 100


@lru_cache(maxsize=1)
def catalog() -> tuple[dict, ...]:
    try:
        raw = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    out = []
    for n in raw if isinstance(raw, list) else []:
        try:
            pats = [re.compile(t, re.I) for t in n.get("terms") or []]
        except re.error:
            continue
        if n.get("key") and pats:
            weak = set(n.get("weak") or [])
            out.append({**n, "kind": n.get("kind") or "gem", "heat": int(n.get("heat") or 1), "_re": pats,
                        "_weak": [p.pattern in weak for p in pats]})
    return tuple(out)


def match(*texts, kind: str = "gem") -> list[str]:
    """Narrative keys of that kind found in the texts (strings or lists)."""
    blob = " ".join(" ".join(t) if isinstance(t, (list, tuple)) else str(t or "") for t in texts)
    if not blob.strip():
        return []
    out = []
    for n in catalog():
        if n["kind"] != kind:
            continue
        # a generic term ("price feeds", "tee") needs a second one to count
        hits = sum(0.5 if weak else 1.0 for p, weak in zip(n["_re"], n["_weak"]) if p.search(blob))
        if hits >= 1:
            out.append(n["key"])
    return out


def labels(keys: list[str]) -> list[dict]:
    by = {n["key"]: n for n in catalog()}
    return [{"key": k, "label": by[k].get("label") or k, "kind": by[k]["kind"], "heat": by[k]["heat"]}
            for k in keys if k in by]


def bonus(keys: list[str]) -> float:
    """Hottest narrative counts most; a second one adds a little."""
    heats = sorted((n["heat"] for n in catalog() if n["key"] in set(keys or [])), reverse=True)
    if not heats:
        return 0.0
    return min(BONUS_MAX, 2.0 * heats[0] + (1.0 if len(heats) > 1 else 0.0))


def keys_for_category(cat: str, exclusive: bool = False) -> list[str]:
    """Gem narrative keys whose CoinGecko category list contains `cat`.
    `exclusive`: only when one narrative owns it. A shared category ("depin",
    "artificial-intelligence") cannot tell Compute from DePIN or AI Agents,
    so it tags nothing and the coin's own text decides."""
    keys = [n["key"] for n in catalog() if n["kind"] == "gem" and cat in (n.get("coingecko") or [])]
    return keys if not exclusive or len(keys) == 1 else []


def coingecko_categories(first: list[str] | None = None) -> list[str]:
    """Gem categories, hottest first. Keys in `first` come before the rest."""
    gems = [n for n in catalog() if n["kind"] == "gem"]
    order = {k: i for i, k in enumerate(first or [])}
    gems.sort(key=lambda n: (order.get(n["key"], len(order)), -n["heat"]))
    seen, out = set(), []
    for n in gems:
        for c in n.get("coingecko") or []:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def github_queries(first: list[str] | None = None) -> list[str]:
    gems = [n for n in catalog() if n["kind"] == "gem"]
    order = {k: i for i, k in enumerate(first or [])}
    gems.sort(key=lambda n: (order.get(n["key"], len(order)), -n["heat"]))
    return [q for n in gems for q in n.get("github") or []]
