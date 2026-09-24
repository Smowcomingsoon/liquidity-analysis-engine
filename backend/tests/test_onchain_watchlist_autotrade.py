"""Fase 4 dalla watchlist: il ponte segnale -> ordine on-chain, e il pezzo
che lo sblocca (l'indirizzo del token salvato dalla ricerca DexScreener).

Firma/invio sono mockati qui (verificati con rete vera altrove, vedi
test_onchain_execute.py/test_onchain_quotes.py) — questo file verifica solo
che main._run_onchain_auto_trade prenda le decisioni giuste: saltare senza
indirizzo, saltare le vendite (decimali del token non salvati), comprare
davvero quando i pezzi ci sono tutti.
"""
from __future__ import annotations

import asyncio

import pytest

from app import auth, main, trading_log
from app.models import OnchainWallet, StrategySignal, User, WatchlistEntry
from app.trading.onchain import execute as onchain_execute
from app.trading.onchain import ledger as onchain_ledger
from app.trading.onchain import quotes as onchain_quotes
from app.trading.onchain import token_metadata as onchain_token_metadata


def _run(coro):
    return asyncio.run(coro)


def _signal(sig_type: str, price: float = 2.0) -> StrategySignal:
    return StrategySignal(time=0, type=sig_type, price=price, label="t")


def _dex_entry(**overrides) -> WatchlistEntry:
    base = dict(
        id="e1", owner_id="u1", symbol="PEPE@solana:Pxxx1111111111111111111111", asset_type="dex",
        size_cap_usdt={"lorenzo": 10.0},
    )
    base.update(overrides)
    return WatchlistEntry(**base)


def _owner(pilot: tuple[str, ...] = ("solana",), **wallets) -> User:
    # Pilota acceso di default: e' la condizione in cui questi test sono
    # nati. Il caso spento ha il suo test sotto.
    return User(id="u1", email="u1@example.com", password_hash="x", onchain_wallets=wallets,
                sniper_autotrade_chains=list(pilot))


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setattr(trading_log, "_STORE_FILE", tmp_path / "journal.json")
    trading_log._entries.clear()
    yield
    trading_log._entries.clear()


def test_skipped_without_base_token_address():
    entry = _dex_entry(base_token_address=None)
    owner = _owner()
    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("entry_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "saltato"
    assert "token address is missing" in logged[0]["detail"]


def test_skipped_without_wallet_for_chain():
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner()  # nessun wallet solana
    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("entry_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "saltato"
    assert "on-chain wallet" in logged[0]["detail"]


def test_sell_signal_skipped_for_missing_decimals(monkeypatch):
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner(solana=OnchainWallet(address="Wallet1", private_key_enc="x", enabled=True))
    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("exit_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "saltato"
    assert "decimals" in logged[0]["detail"]


def test_buy_signal_executes_real_order_when_address_and_wallet_present(monkeypatch):
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner(solana=OnchainWallet(address="Wallet1", private_key_enc="x", enabled=True))

    async def _fake_quote(input_mint, output_mint, amount_lamports, slippage_bps=50):
        assert output_mint == "TokenMintAddr222"
        return {"in_amount": amount_lamports, "out_amount": 1_000, "price_impact_pct": 0.4, "raw": {}}

    monkeypatch.setattr(onchain_quotes, "jupiter_quote", _fake_quote)
    monkeypatch.setattr(auth, "get_decrypted_onchain_private_key", lambda owner, chain: "deadbeef")

    async def _fake_execute_order(owner_id, chain, token, strategy_name, side, notional_usd,
                                   price_impact_pct, price_usd, private_key_hex, quote):
        assert side == "buy"
        assert token == "TokenMintAddr222"
        return {"accepted": True, "reason": "", "idempotency_key": "k1", "tx_signature": "sig1"}

    monkeypatch.setattr(onchain_execute, "execute_order", _fake_execute_order)

    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("entry_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "eseguito"


def test_sell_signal_skipped_without_position_even_with_decimals(monkeypatch):
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner(solana=OnchainWallet(address="Wallet1", private_key_enc="x", enabled=True))

    async def _fake_decimals(chain, token_address):
        return 6

    monkeypatch.setattr(onchain_token_metadata, "get_token_decimals", _fake_decimals)
    monkeypatch.setattr(onchain_ledger, "get_position", lambda owner_id, chain, token: {"quantity": 0.0, "cost_usd": 0.0})

    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("exit_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "saltato"
    assert "no recorded position" in logged[0]["detail"]


def test_sell_signal_executes_real_order_when_decimals_and_position_present(monkeypatch):
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner(solana=OnchainWallet(address="Wallet1", private_key_enc="x", enabled=True))

    async def _fake_decimals(chain, token_address):
        assert token_address == "TokenMintAddr222"
        return 6

    monkeypatch.setattr(onchain_token_metadata, "get_token_decimals", _fake_decimals)
    monkeypatch.setattr(
        onchain_ledger, "get_position",
        lambda owner_id, chain, token: {"quantity": 123.0, "cost_usd": 200.0},
    )

    async def _fake_quote(input_mint, output_mint, amount_lamports, slippage_bps=50):
        assert input_mint == "TokenMintAddr222"  # si vende il token base, non lo stabile
        assert amount_lamports == 123 * 10**6  # convertito nei decimali veri, non indovinati
        return {"in_amount": amount_lamports, "out_amount": 240, "price_impact_pct": 0.3, "raw": {}}

    monkeypatch.setattr(onchain_quotes, "jupiter_quote", _fake_quote)
    monkeypatch.setattr(auth, "get_decrypted_onchain_private_key", lambda owner, chain: "deadbeef")

    async def _fake_execute_order(owner_id, chain, token, strategy_name, side, notional_usd,
                                   price_impact_pct, price_usd, private_key_hex, quote):
        assert side == "sell"
        assert token == "TokenMintAddr222"
        return {"accepted": True, "reason": "", "idempotency_key": "k2", "tx_signature": "sig2"}

    monkeypatch.setattr(onchain_execute, "execute_order", _fake_execute_order)

    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("exit_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "eseguito"


def test_onchain_globally_disabled_is_skipped(monkeypatch):
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner(solana=OnchainWallet(address="Wallet1", private_key_enc="x", enabled=True))
    monkeypatch.setattr(main.settings, "onchain_enabled", False)
    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("entry_long")]))
    logged = trading_log.list_for("u1")
    assert logged[0]["outcome"] == "saltato"
    assert "spento" in logged[0]["detail"]


def test_pilot_off_skips_buys_but_keeps_sells(monkeypatch):
    entry = _dex_entry(base_token_address="TokenMintAddr222")
    owner = _owner(pilot=(), solana=OnchainWallet(address="Wallet1", private_key_enc="x", enabled=True))
    _run(main._run_onchain_auto_trade(owner, entry, "lorenzo", [_signal("entry_long"), _signal("exit_long")]))
    logged = trading_log.list_for("u1")
    by_type = {e["signal_type"]: e for e in logged}
    assert by_type["entry_long"]["outcome"] == "saltato"
    assert "pilot off" in by_type["entry_long"]["detail"]
    # La vendita prosegue per la sua strada (qui si ferma sui decimali, non sul pilota).
    assert "pilota spento" not in by_type["exit_long"]["detail"]
