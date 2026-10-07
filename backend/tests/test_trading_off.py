"""Trading and automatic strategies switched off (Lorenzo, 07/10): the Top is
the research ranking alone, and trading endpoints answer 404."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.config import settings


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setattr(settings, "onchain_trading_enabled", False)


def test_default_is_off():
    from app.config import Settings
    assert Settings.model_fields["onchain_trading_enabled"].default is False


def test_top_keeps_rows_without_a_strategy_trade(off, monkeypatch):
    from app.trading.onchain import strategy_lab
    ents = [{"chain": "base", "token_address": t, "symbol": t, "on_board": True} for t in ("A", "B", "C")]
    monkeypatch.setattr(main, "_top_entries", lambda n=50, narrative="": [dict(e) for e in ents][:n])

    def no_lab(*_):
        raise AssertionError("the lab is not read with trading off")
    monkeypatch.setattr(strategy_lab, "get", no_lab)
    monkeypatch.setattr(strategy_lab, "status", no_lab)
    out = asyncio.run(main.onchain_top(n=2, user=SimpleNamespace(id="u")))
    assert [e["symbol"] for e in out["entries"]] == ["A", "B"]
    assert all(e["backtests"] is None for e in out["entries"])
    assert out["backtests"] is None and out["on_board"] == 2


def test_trading_endpoints_answer_404(off):
    with pytest.raises(HTTPException) as err:
        main._require_trading()
    assert err.value.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(main.list_limit_orders("solana", user=SimpleNamespace(id="u")))


def test_trading_on_lets_them_through(monkeypatch):
    monkeypatch.setattr(settings, "onchain_trading_enabled", True)
    main._require_trading()
