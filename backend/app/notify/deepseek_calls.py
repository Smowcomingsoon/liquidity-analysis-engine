"""Journal of every DeepSeek call: why it was made and whether it was new work.

Issue #358, I00: "prima misuriamo dove DeepSeek sbaglia o ripete lavoro".
The ledger in deepseek_desk counts tokens per day and kind; it cannot say
which calls repeated an identical question, which answers did not parse,
or how much of the prompt DeepSeek already had in cache. This does, one
line per call, without the prompt text (only hashes and sizes):

- `purpose` and `subject`: what the call was for (judge, code, site...)
  and about what (chain:token, repo, url). Callers pass them.
- `in_hash`: the whole request (messages + model + max_tokens). The same
  hash seen again inside DUP_WINDOW_S is a repeat: the caller asked the
  same question with the same data. `same_out` says whether the answer
  came back the same too, i.e. the call bought nothing.
- `json_ok`: for JSON-mode calls, whether the answer parsed.
- `cache_hit`: prompt tokens DeepSeek billed as cache hits.

stats() is what /api/agent/calls shows.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from app.trading.onchain.storage import data_file

_FILE = data_file("deepseek_calls.jsonl")
KEEP = 20_000                        # lines kept on disk
DUP_WINDOW_S = 14 * 86400

_lock = threading.Lock()
_rows: list[dict] = []
_last: dict[str, tuple[float, str]] = {}   # in_hash -> (at, out_hash)
_loaded = False


def _h(obj) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        lines = _FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for ln in lines[-KEEP:]:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            _rows.append(r)
            if r.get("in"):
                _last[r["in"]] = (r.get("t", 0), r.get("out", ""))


def request_hash(body: dict) -> str:
    return _h({k: body.get(k) for k in ("model", "messages", "max_tokens", "tools", "response_format")})


def log(*, kind: str, purpose: str | None, subject: str | None, body: dict, content: str | None,
        usage: dict | None, ms: float, error: str = "", now: float | None = None) -> dict:
    """One call. `content` None means the call failed before an answer."""
    now = now or time.time()
    usage = usage or {}
    in_hash = request_hash(body)
    out_hash = _h(content) if content is not None else ""
    row = {"t": round(now, 1), "kind": kind, "purpose": purpose or kind, "subject": (subject or "")[:120],
           "in": in_hash, "sys": _h((body.get("messages") or [{}])[0].get("content") or ""),
           "chars": sum(len(str(m.get("content") or "")) for m in body.get("messages") or []),
           "tin": int(usage.get("prompt_tokens") or 0), "tout": int(usage.get("completion_tokens") or 0),
           "hit": int(usage.get("prompt_cache_hit_tokens") or 0), "ms": int(ms), "out": out_hash}
    if error:
        row["err"] = error[:120]
    if content is not None and (body.get("response_format") or {}).get("type") == "json_object":
        try:
            json.loads(content)
            row["json_ok"] = True
        except ValueError:
            row["json_ok"] = False
    with _lock:
        _load()
        prev = _last.get(in_hash)
        if prev and now - prev[0] < DUP_WINDOW_S:
            row["dup_age"] = int(now - prev[0])
            if content is not None:
                row["same_out"] = prev[1] == out_hash
        if content is not None:
            _last[in_hash] = (now, out_hash)
        _rows.append(row)
        try:
            with _FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            if len(_rows) > 2 * KEEP:
                del _rows[:-KEEP]
                _FILE.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in _rows),
                                 encoding="utf-8")
        except OSError:
            pass
    return row


def stats(days: float = 7, now: float | None = None) -> dict:
    """Per purpose: calls, tokens, repeats and what they cost, failures."""
    now = now or time.time()
    with _lock:
        _load()
        rows = [r for r in _rows if r.get("t", 0) >= now - days * 86400]
    out: dict[str, dict] = {}
    for r in rows:
        p = out.setdefault(r.get("purpose") or "?", {
            "calls": 0, "tokens_in": 0, "tokens_out": 0, "cache_hit": 0, "errors": 0, "json_bad": 0,
            "repeats": 0, "repeats_same_answer": 0, "repeat_tokens": 0, "subjects": set(), "ms": 0})
        p["calls"] += 1
        p["tokens_in"] += r.get("tin", 0)
        p["tokens_out"] += r.get("tout", 0)
        p["cache_hit"] += r.get("hit", 0)
        p["ms"] += r.get("ms", 0)
        p["errors"] += 1 if r.get("err") else 0
        p["json_bad"] += 1 if r.get("json_ok") is False else 0
        if r.get("subject"):
            p["subjects"].add(r["subject"])
        if "dup_age" in r:
            p["repeats"] += 1
            p["repeat_tokens"] += r.get("tin", 0) + r.get("tout", 0)
            p["repeats_same_answer"] += 1 if r.get("same_out") else 0
    for p in out.values():
        n = p["calls"]
        p["subjects"] = len(p["subjects"])
        p["avg_ms"] = round(p.pop("ms") / n) if n else 0
        p["cache_hit_share"] = round(p["cache_hit"] / p["tokens_in"], 3) if p["tokens_in"] else 0.0
        p["calls_per_subject"] = round(n / p["subjects"], 2) if p["subjects"] else None
    total = {k: sum(p[k] for p in out.values()) for k in
             ("calls", "tokens_in", "tokens_out", "repeats", "repeats_same_answer", "repeat_tokens", "errors", "json_bad")}
    since = min((r.get("t", now) for r in rows), default=None)
    return {"days": days, "since": since, "total": total,
            "by_purpose": dict(sorted(out.items(), key=lambda kv: -kv[1]["tokens_in"]))}


def count_between(start: float, end: float | None = None) -> int:
    """Calls made in [start, end]: what the nightly run spent."""
    end = end if end is not None else time.time()
    with _lock:
        _load()
        return sum(1 for r in _rows if start <= r.get("t", 0) <= end)


def recent(limit: int = 100) -> list[dict]:
    with _lock:
        _load()
        return list(reversed(_rows[-limit:]))


def reset_for_tests() -> None:
    global _loaded
    with _lock:
        _rows.clear()
        _last.clear()
        _loaded = True
