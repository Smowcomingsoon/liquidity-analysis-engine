"""Guardrail di rischio — tetti confermati da Lorenzo (20 USD/operazione,
100 USD/giorno, 15% di concentrazione), price impact, circuit breaker."""

from __future__ import annotations

from app.config import settings
from app.trading.onchain import ledger, risk


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "_DB_FILE", tmp_path / "ledger.db")
    ledger._init_db()


def test_per_trade_cap_rejects_above_20_usd():
    assert settings.onchain_max_usd_per_trade == 20.0
    ok, reason = risk.check_per_trade_cap(20.01)
    assert ok is False
    assert "20" in reason
    ok, _ = risk.check_per_trade_cap(20.0)
    assert ok is True


def test_daily_cap_accumulates_across_orders(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert settings.onchain_max_usd_per_day == 100.0
    ledger.reserve("k1", "u1", "solana", "TOKEN", "s", "buy", "real", 20.0)
    ledger.mark_confirmed("k1", executed_qty=1.0, executed_price_usd=20.0)
    ok, _ = risk.check_daily_cap("u1", "solana", 79.99)
    assert ok is True
    ok, reason = risk.check_daily_cap("u1", "solana", 80.01)
    assert ok is False
    assert "daily cap" in reason


def test_concentration_cap_blocks_over_15_percent(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    assert settings.onchain_max_token_concentration_pct == 15.0
    # Prima posizione mai aperta: nessun portafoglio da confrontare, si passa.
    ok, _ = risk.check_concentration("u1", "solana", "TOKEN_A", 20.0)
    assert ok is True

    ledger.reserve("k1", "u1", "solana", "TOKEN_A", "s", "buy", "real", 20.0)
    ledger.mark_confirmed("k1", executed_qty=1.0, executed_price_usd=20.0)
    ledger.reserve("k2", "u1", "solana", "TOKEN_B", "s", "buy", "real", 80.0)
    ledger.mark_confirmed("k2", executed_qty=1.0, executed_price_usd=80.0)
    # Portafoglio: 100 USD totali. Aggiungere 20 su TOKEN_A -> 40/120 = 33% -> rifiuta.
    ok, reason = risk.check_concentration("u1", "solana", "TOKEN_A", 20.0)
    assert ok is False
    assert "concentration" in reason
    # Aggiungere 5 su TOKEN_A -> 25/105 = 23.8% -> ancora rifiuta.
    ok, _ = risk.check_concentration("u1", "solana", "TOKEN_A", 5.0)
    assert ok is False
    # Un nuovo TOKEN_C con size piccola resta sotto il 15% del nuovo totale.
    ok, _ = risk.check_concentration("u1", "solana", "TOKEN_C", 1.0)
    assert ok is True


def test_price_impact_rejects_above_5_percent():
    ok, _ = risk.check_price_impact(4.99)
    assert ok is True
    ok, reason = risk.check_price_impact(5.01)
    assert ok is False
    assert "5" in reason


def test_circuit_breaker_blocks_new_orders_once_tripped(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ok, _ = risk.check_circuit_breaker("u1", "solana")
    assert ok is True
    for _ in range(5):
        ledger.record_trade_outcome("u1", "solana", -1.0)
    ok, reason = risk.check_circuit_breaker("u1", "solana")
    assert ok is False
    assert "circuit breaker" in reason


def test_run_all_checks_stops_at_first_failure(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    for _ in range(5):
        ledger.record_trade_outcome("u1", "solana", -1.0)
    # circuit breaker scattato: deve fermarsi li', anche con una size assurda
    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN", 999.0, 0.1)
    assert ok is False
    assert "circuit breaker" in reason


def test_run_all_checks_passes_a_clean_order(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN", 20.0, 0.5)
    assert ok is True
    assert reason == ""


def test_run_all_checks_ignores_pool_info_when_absent(tmp_path, monkeypatch):
    # pool_info=None (default): nessuna chiamata alle funzioni di allowlist,
    # le chiamate esistenti restano intatte.
    _fresh_db(tmp_path, monkeypatch)
    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN", 20.0, 0.5, pool_info=None)
    assert ok is True
    assert reason == ""


def test_run_all_checks_rejects_when_pool_info_unverifiable(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN", 20.0, 0.5, pool_info={})
    assert ok is False
    assert "cannot be verified" in reason


def test_run_all_checks_passes_with_healthy_pool_info(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    import time
    pool_info = {
        "liquidity_usd": 5000.0,
        "created_at": time.time() - 3600,
        "reference_max_liquidity_usd": 5200.0,
    }
    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN", 20.0, 0.5, pool_info=pool_info)
    assert ok is True
    assert reason == ""


def test_clamp_size_to_pool_without_liquidity_keeps_requested_size():
    assert risk.clamp_size_to_pool(20.0, None) == 20.0


def test_clamp_size_to_pool_caps_at_pool_percentage():
    # 1.5% di 1000 USD = 15 USD, sotto sia la richiesta (20) sia il tetto (20).
    assert risk.clamp_size_to_pool(20.0, pool_liquidity_usd=1000.0, safety_cap_usd=20.0) == 15.0


def test_clamp_size_to_pool_caps_at_safety_cap():
    assert risk.clamp_size_to_pool(20.0, pool_liquidity_usd=1_000_000.0, safety_cap_usd=20.0) == 20.0


def test_clamp_size_to_pool_never_exceeds_requested():
    assert risk.clamp_size_to_pool(5.0, pool_liquidity_usd=1_000_000.0, safety_cap_usd=20.0) == 5.0


def test_run_all_checks_skips_concentration_only_for_manual_orders(tmp_path, monkeypatch):
    """Lorenzo: uno swap ETH->USDG legittimo (portare tutto in una
    stablecoin) e' stato respinto perche' USDG sarebbe stato il 100% di un
    portafoglio con solo quella posizione tracciata. Un umano che swappa
    di sua volonta' decide lui quanto diversificare; un'automazione
    (watchlist, ordine limite) resta invece sotto il tetto."""
    _fresh_db(tmp_path, monkeypatch)
    called = {"concentration": False}

    def _spy_concentration(*args):
        called["concentration"] = True
        return False, "non dovrebbe mai arrivare qui per un ordine manuale"

    monkeypatch.setattr(risk, "check_concentration", _spy_concentration)

    ok, _ = risk.run_all_checks("u1", "solana", "TOKEN_A", 5.0, 0.1, strategy_name="manuale")
    assert ok is True
    assert called["concentration"] is False

    ok, reason = risk.run_all_checks("u1", "solana", "TOKEN_A", 5.0, 0.1, strategy_name="ordine limite")
    assert ok is False
    assert called["concentration"] is True
