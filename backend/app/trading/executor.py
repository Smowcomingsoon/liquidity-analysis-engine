from app.models import StrategySignal
from app.trading import (
    binance_exec,
    bitstamp_exec,
    coinbase_exec,
    kraken_exec,
    kucoin_exec,
)

SUPPORTED_EXCHANGES = {"binance", "mexc", "kucoin", "kraken", "coinbase", "bitstamp"}


async def place_order_for_signal(
    exchange: str,
    symbol: str,
    signal: StrategySignal,
    size_mode: str,
    size_value: float,
    api_key: str,
    api_secret: str,
    passphrase: str = "",
) -> dict:
    """Map signal type to BUY/SELL and call the right exchange executor."""
    side = _signal_to_side(signal.type)
    if side is None:
        return {"skipped": True, "reason": "no-op signal type"}

    if exchange in ("binance", "mexc"):
        return await binance_exec.place_order(
            exchange, symbol, side, size_mode, size_value, api_key, api_secret
        )
    elif exchange == "kucoin":
        kucoin_symbol = _to_kucoin_symbol(symbol)
        kucoin_side = side.lower()
        return await kucoin_exec.place_order(
            kucoin_symbol, kucoin_side, size_mode, size_value, api_key, api_secret, passphrase
        )
    elif exchange == "kraken":
        return await kraken_exec.place_order(symbol, side, size_mode, size_value, api_key, api_secret)
    elif exchange == "coinbase":
        return await coinbase_exec.place_order(symbol, side, size_mode, size_value, api_key, api_secret)
    elif exchange == "bitstamp":
        return await bitstamp_exec.place_order(symbol, side, size_mode, size_value, api_key, api_secret)
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")


def _signal_to_side(signal_type: str) -> str | None:
    """Map strategy signal type to BUY/SELL. Returns None for noop signals."""
    if signal_type in ("entry_long", "flip_to_long"):
        return "BUY"
    if signal_type in ("entry_short", "flip_to_short", "exit_long"):
        return "SELL"
    # exit_short on spot = already in USDT, noop
    return None


def _to_kucoin_symbol(symbol: str) -> str:
    """BTCUSDT → BTC-USDT, ETHBTC → ETH-BTC"""
    for quote in ("USDT", "BTC", "ETH", "USDC"):
        if symbol.endswith(quote):
            return symbol[: -len(quote)] + "-" + quote
    return symbol
