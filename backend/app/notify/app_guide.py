"""How the app works, for the agent: the code's own documentation, searched.

Lorenzo, 01/10: the voice chat "deve permettere di fare anche delle belle
diagnosi [...] un amico che potrebbe chiedere al bot di spiegargli come
funziona una determinata strategia e deve essere molto preciso [...] deve
avere accesso praticamente a tutto quello che riguarda il funzionamento di
tutta l'app". Every module here opens with a docstring that says what it does
and why, and its constants hold the real numbers (thresholds, windows, caps).
This index reads them straight from the source, so the answer follows the
code as it changes: no separate guide to keep in sync.

Read-only, and only documentation: no function bodies, no data files, no
settings. Modules that handle keys, signatures, logins or real orders are
left out entirely.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]

# Keys, signatures, logins, real execution: never in the index.
SKIP = re.compile(r"(auth|crypto|keys|passkey|reveal|wallet_link|guest|config|diagnostics|_exec|executor|"
                  r"execute|execution|send|twap|token_launch|credentials|futures/)")
CONST_MAX = 160          # characters of a constant's value
ANSWER_MAX = 6000        # characters returned per question

_index: list[dict] | None = None


def _const(node: ast.Assign | ast.AnnAssign) -> tuple[str, str] | None:
    target = node.targets[0] if isinstance(node, ast.Assign) else node.target
    if not isinstance(target, ast.Name) or not target.id.isupper() or node.value is None:
        return None
    try:
        value = ast.literal_eval(node.value)
    except (ValueError, SyntaxError, TypeError):
        return None
    text = repr(value)
    return (target.id, text) if len(text) <= CONST_MAX else None


def _comment_above(lines: list[str], lineno: int) -> str:
    out = []
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        out.append(lines[i].strip().lstrip("#").strip())
        i -= 1
    return " ".join(reversed(out))


def _parse(path: Path) -> list[dict]:
    rel = path.relative_to(APP_DIR).as_posix()
    try:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    lines = src.splitlines()
    out = []
    doc = ast.get_docstring(tree)
    consts = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            c = _const(node)
            if c:
                note = _comment_above(lines, node.lineno)
                consts.append(f"{c[0]} = {c[1]}" + (f"  # {note[:300]}" if note else ""))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            d = ast.get_docstring(node)
            if d and not node.name.startswith("_"):
                out.append({"file": rel, "name": node.name, "text": d})
    if doc or consts:
        out.insert(0, {"file": rel, "name": "", "text": "\n".join(filter(None, [doc, *consts]))})
    return out


def index() -> list[dict]:
    global _index
    if _index is None:
        _index = []
        for path in sorted(APP_DIR.rglob("*.py")):
            rel = path.relative_to(APP_DIR).as_posix()
            if "__pycache__" in rel or "/tests/" in rel or SKIP.search(rel):
                continue
            _index.extend(_parse(path))
    return _index


def overview() -> dict:
    """Every documented module with the first line of what it does."""
    rows, size = [], 0
    for e in index():
        if e["name"]:
            continue
        first = e["text"].strip().splitlines()[0][:140]
        line = f"{e['file']}: {first}"
        if size + len(line) > ANSWER_MAX:
            break
        rows.append(line)
        size += len(line)
    return {"modules": rows}


def search(query: str) -> dict:
    """The documentation passages that best match `query` (any language;
    module and constant names count too)."""
    words = [w for w in re.findall(r"\w+", (query or "").lower()) if len(w) > 2][:8]
    if not words:
        return overview()
    scored = []
    for e in index():
        hay = f"{e['file']} {e['name']} {e['text']}".lower()
        hits = sum(1 for w in words if w in hay)
        if hits:
            scored.append((hits, hay.count(words[0]), e))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out, size = [], 0
    for _hits, _n, e in scored:
        piece = e["text"][:2500]
        if size + len(piece) > ANSWER_MAX:
            break
        out.append({"where": e["file"] + (f" · {e['name']}" if e["name"] else ""), "doc": piece})
        size += len(piece)
    return {"passages": out} if out else {"error": "nothing in the app's documentation matches", "hint": "call with an empty query for the module list"}
