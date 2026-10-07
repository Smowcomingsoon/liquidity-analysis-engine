import pytest

from app.providers import dexscreener


@pytest.fixture(autouse=True)
def _fresh_gecko_gate():
    """La cache e il tetto di GeckoTerminal sono globali: un test non deve
    vedere le risposte finte di un altro, ne' aspettare il suo minuto."""
    dexscreener._gecko_seen.clear()
    dexscreener._gecko_calls.clear()
    yield
    dexscreener._gecko_seen.clear()
    dexscreener._gecko_calls.clear()


@pytest.fixture(autouse=True)
def _private_projects_db(monkeypatch, tmp_path):
    """The Gem Finder's project database is a file on the volume: every test
    gets its own, empty."""
    from app.trading.onchain import projects_db
    monkeypatch.setattr(projects_db, "_DB_FILE", tmp_path / "projects.db")
    yield


@pytest.fixture(autouse=True)
def _fresh_github_cache():
    """Stessa cosa per la cache e la quota di GitHub."""
    from app.trading.onchain import legit
    legit._gh_cache.clear()
    legit._gh_blocked.clear()
    yield
    legit._gh_cache.clear()
    legit._gh_blocked.clear()


@pytest.fixture(autouse=True)
def _onchain_wallets_on(monkeypatch):
    """Execution tests use made-up owner ids with no user behind them: treat
    their wallets as switched on. Tests of the switch itself re-patch this."""
    from app.trading.onchain import risk
    monkeypatch.setattr(risk, "_wallet_enabled", lambda owner_id, chain: True)
    yield


@pytest.fixture(autouse=True)
def _trading_on(monkeypatch):
    """The trading and strategy tests predate the switch that turned them off
    (Lorenzo, 07/10): they run with it on. Tests of the switch re-patch it."""
    from app.config import settings
    monkeypatch.setattr(settings, "onchain_trading_enabled", True)
    yield


@pytest.fixture(autouse=True)
def _private_deepseek_journal(monkeypatch, tmp_path):
    """The DeepSeek call journal is a file on the volume: every test gets its own."""
    from app.notify import deepseek_calls
    monkeypatch.setattr(deepseek_calls, "_FILE", tmp_path / "deepseek_calls.jsonl")
    deepseek_calls.reset_for_tests()
    yield



@pytest.fixture(autouse=True)
def _private_quant_memory(monkeypatch, tmp_path):
    from app.trading.onchain import quant_memory
    monkeypatch.setattr(quant_memory, "_path", tmp_path / "quant.sqlite3")


@pytest.fixture(autouse=True)
def _private_history(monkeypatch, tmp_path):
    """The append-only evaluation history is a file on the volume: every test gets its own."""
    from app.trading.onchain import history
    monkeypatch.setattr(history, "_FILE", tmp_path / "onchain_history.jsonl")
    yield

