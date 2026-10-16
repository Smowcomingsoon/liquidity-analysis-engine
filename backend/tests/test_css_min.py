import re
from pathlib import Path

from app.css_min import minify

STATIC = Path(__file__).resolve().parents[2] / "frontend" / "static"


def test_comments_go_and_meaningful_spaces_stay():
    css = "/* note */\n.a  .b {\n  margin: 0   auto; /* x */\n}\n"
    assert minify(css) == ".a .b { margin: 0 auto; }"


def test_strings_and_urls_are_untouched():
    css = '.a::before { content: "/* not a comment */  two  spaces"; }\n.b { background: url(/x/y.png?v=1  ); }'
    out = minify(css)
    assert '"/* not a comment */  two  spaces"' in out
    assert "url(/x/y.png?v=1  )" in out


def test_escaped_quote_inside_string():
    assert minify(r'.a{content:"a\"/*b*/"}') == r'.a{content:"a\"/*b*/"}'


def test_real_files_keep_every_token():
    # Same tokens in the same order, only whitespace and comments differ.
    for path in list(STATIC.glob("*.css")) + list((STATIC / "onchain").glob("*.css")):
        src = path.read_text(encoding="utf-8")
        out = minify(src)
        strip = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
        assert out.split() == strip.split(), path.name
        assert out.count("{") == src.count("{") - sum(c.count("{") for c in re.findall(r"/\*.*?\*/", src, re.S))
