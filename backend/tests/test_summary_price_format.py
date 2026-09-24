from app.analysis.summary import _fmt_price


def test_fmt_price_large_no_scientific_notation():
    s = _fmt_price(78123.45)
    assert "e+" not in s.lower() and "e-" not in s.lower()
    assert s == "78,123.45"


def test_fmt_price_small_no_scientific_notation():
    s = _fmt_price(0.00001142)
    assert "e+" not in s.lower() and "e-" not in s.lower()
    # keeps a few significant digits instead of rounding to zero
    assert s != "0.00"
    assert s.startswith("0.0000114")
