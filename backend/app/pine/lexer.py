import re
from dataclasses import dataclass

from app.pine.errors import PineParseError

_KEYWORDS = {"if", "else", "var", "and", "or", "not", "true", "false", "na"}
_TYPE_KEYWORDS = {"float", "int", "bool", "string", "color", "line", "label", "box", "table", "array", "matrix"}

_TOKEN_RE = re.compile(
    r"""
      (?P<NUMBER>\d+\.\d+|\.\d+|\d+)
    | (?P<STRING>"[^"]*"|'[^']*')
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<WALRUS>:=)
    | (?P<OP>==|!=|>=|<=|\?|:|,|\[|\]|\(|\)|\.|\+|\-|\*|\/|%|>|<|=)
    | (?P<WS>[ \t]+)
    """,
    re.VERBOSE,
)


@dataclass
class Token:
    kind: str
    value: str
    line_no: int


def tokenize_line(text: str, line_no: int) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise PineParseError(f"unrecognized character: '{text[pos]}'", line_no)
        pos = m.end()
        kind = m.lastgroup
        value = m.group()
        if kind == "WS":
            continue
        if kind == "IDENT" and value in _KEYWORDS:
            tokens.append(Token(value, value, line_no))
        else:
            tokens.append(Token(kind, value, line_no))
    tokens.append(Token("EOF", "", line_no))
    return tokens


def strip_comment(text: str) -> str:
    # crude but sufficient: // is never legal inside a Pine string in the
    # scripts we expect (no escaped quotes), so a naive scan is safe here.
    in_str: str | None = None
    for i, ch in enumerate(text):
        if in_str:
            if ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch == "/" and i + 1 < len(text) and text[i + 1] == "/":
            return text[:i]
    return text


def indent_of(raw_line: str) -> int:
    n = 0
    for ch in raw_line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def preprocess(source: str) -> list[tuple[int, str, int]]:
    """Returns (indent, stripped_text, 1-based line_no) for every non-blank,
    non-comment-only, non-directive line."""
    out: list[tuple[int, str, int]] = []
    for i, raw in enumerate(source.splitlines(), start=1):
        text = strip_comment(raw).rstrip()
        stripped = text.strip()
        if not stripped:
            continue
        if stripped.startswith("//@version"):
            continue
        if stripped.endswith("\\"):
            raise PineParseError("line continuation with '\\' is not supported", i)
        out.append((indent_of(text), stripped, i))
    return out
