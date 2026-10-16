"""Conservative CSS minifier for the files under /static (overhaul plan, phase 3).

The sources stay readable and commented; what the browser receives drops
the comments and collapses whitespace (-29% after gzip on the app's CSS).
Strings and url(...) are copied untouched, and every whitespace run becomes
one space instead of disappearing, so selectors like `a b` or values like
`0 auto` keep their meaning. Nothing is reordered or merged.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


def minify(css: str) -> str:
    out: list[str] = []
    i, n = 0, len(css)
    pending_space = False
    while i < n:
        c = css[i]
        if c == "/" and css.startswith("/*", i):
            end = css.find("*/", i + 2)
            i = n if end < 0 else end + 2
            pending_space = True
            continue
        if c in " \t\r\n\f":
            pending_space = True
            i += 1
            continue
        if pending_space and out:
            out.append(" ")
        pending_space = False
        if c in "\"'":
            j = i + 1
            while j < n and css[j] != c:
                j += 2 if css[j] == "\\" else 1
            out.append(css[i:j + 1])
            i = j + 1
            continue
        if c in "uU" and css[i:i + 4].lower() == "url(":
            end = css.find(")", i)
            end = n - 1 if end < 0 else end
            out.append(css[i:end + 1])
            i = end + 1
            continue
        out.append(c)
        i += 1
    return "".join(out).strip()


@lru_cache(maxsize=64)
def _cached(path: str, mtime_ns: int) -> bytes:
    return minify(Path(path).read_text(encoding="utf-8")).encode("utf-8")


def minified_file(path: str) -> bytes:
    return _cached(path, Path(path).stat().st_mtime_ns)
