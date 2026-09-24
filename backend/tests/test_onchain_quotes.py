"""Preventivi pre-trade. jupiter_quote() e' verificato anche con una vera
chiamata di rete (Jupiter e' pubblico, nessuna chiave) — non solo un
trasporto finto: e' il tipo di errore che un mock non avrebbe mai trovato
(un campo rinominato dall'API reale, per esempio)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.trading.onchain import quotes

_RealAsyncClient = httpx.AsyncClient
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _client_factory(handler):
    def factory(*args, **kwargs):
        transport = httpx.MockTransport(handler)
        kwargs.pop("proxies", None)
        return _RealAsyncClient(*args, transport=transport, **kwargs)
    return factory


def _run(coro):
    return asyncio.run(coro)


def test_jupiter_quote_real_network_call():
    result = _run(quotes.jupiter_quote(SOL_MINT, USDC_MINT, 10_000_000, slippage_bps=50))
    assert result["in_amount"] == 10_000_000
    assert result["out_amount"] > 0
    assert 0 <= result["price_impact_pct"] < 5  # SOL/USDC e' profondissimo, l'impatto deve essere minuscolo


def test_jupiter_quote_parses_mocked_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "inAmount": "10000000", "outAmount": "1133425", "priceImpactPct": "0.0004738",
        })

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    result = _run(quotes.jupiter_quote(SOL_MINT, USDC_MINT, 10_000_000))
    assert result["in_amount"] == 10_000_000
    assert result["out_amount"] == 1133425
    assert result["price_impact_pct"] == pytest.approx(0.04738)


def test_jupiter_quote_raises_on_error_payload(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "no route found"})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    with pytest.raises(RuntimeError, match="no route found"):
        _run(quotes.jupiter_quote(SOL_MINT, USDC_MINT, 10_000_000))


def _reserves_response(reserve0: int, reserve1: int) -> str:
    return "0x" + format(reserve0, "064x") + format(reserve1, "064x") + format(0, "016x")


def test_uniswap_v2_price_impact_buy_token1_with_token0(monkeypatch):
    pool = "0xPool"
    token0 = "0x000000000000000000000000000000000000AAAA"
    reserve0, reserve1 = 1_000_000, 500_000  # spot price = 0.5 token1 per token0

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "0x0902f1ac" in body:  # getReserves()
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _reserves_response(reserve0, reserve1)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 24 + token0[2:]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    result = _run(quotes.uniswap_v2_price_impact(pool, token0, 10_000))
    # amount_out = 10000*500000 / (1000000+10000) = 4950.49...
    assert result["amount_out"] == 4950
    assert result["price_impact_pct"] > 0
    assert result["price_impact_pct"] < 5  # 1% del pool, impatto piccolo


def test_uniswap_v2_price_impact_scales_with_trade_size(monkeypatch):
    pool = "0xPool"
    token0 = "0x000000000000000000000000000000000000AAAA"
    reserve0, reserve1 = 1_000_000, 500_000

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "0x0902f1ac" in body:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _reserves_response(reserve0, reserve1)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 24 + token0[2:]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    small = _run(quotes.uniswap_v2_price_impact(pool, token0, 1_000))
    large = _run(quotes.uniswap_v2_price_impact(pool, token0, 500_000))
    assert large["price_impact_pct"] > small["price_impact_pct"]


def test_uniswap_v2_price_impact_raises_on_empty_pool(monkeypatch):
    pool = "0xPool"
    token0 = "0x000000000000000000000000000000000000AAAA"

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "0x0902f1ac" in body:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _reserves_response(0, 0)})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + "0" * 24 + token0[2:]})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    with pytest.raises(RuntimeError, match="no liquidity"):
        _run(quotes.uniswap_v2_price_impact(pool, token0, 1_000))


# WETH e USDG reali su Robinhood Chain (docs.robinhood.com/chain/contracts),
# usati per una vera chiamata di rete: lo stesso spirito di
# test_jupiter_quote_real_network_call, non solo un RPC finto.
ROBINHOOD_WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
ROBINHOOD_USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"


def test_get_pool_real_network_call_finds_the_weth_usdg_pool():
    pool = _run(quotes.get_pool(ROBINHOOD_WETH, ROBINHOOD_USDG, 3000))
    assert pool is not None
    assert pool.startswith("0x") and len(pool) == 42


def test_get_pool_real_network_call_returns_none_for_a_fee_tier_without_a_pool():
    # Un token/fee inventato non ha mai un pool: getPool ritorna l'indirizzo zero.
    fake_token = "0x000000000000000000000000000000000000dEaD"
    pool = _run(quotes.get_pool(ROBINHOOD_WETH, fake_token, 3000))
    assert pool is None


def test_uniswap_v3_quote_real_network_call_on_the_weth_usdg_pool():
    result = _run(quotes.uniswap_v3_quote(ROBINHOOD_WETH, ROBINHOOD_USDG, 3000, 10**16))  # 0.01 WETH
    assert result["amount_out"] > 0
    assert result["price_impact_pct"] >= 0


def test_uniswap_v3_quote_mocked_price_impact_scales_with_trade_size(monkeypatch):
    """quoteExactInputSingle non-lineare per costruzione (x*y=k dentro le
    tick range): un ordine 1000 volte piu' grande della size di riferimento
    deve dare un price impact piu' alto, mai piu' basso."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert quotes._SELECTOR_QUOTE_EXACT_INPUT_SINGLE.hex() in body
        # decodifica l'ultimo argomento (amount_in) dalla calldata per
        # rispondere con un amountOut coerente con una leggera curva.
        from eth_abi import decode as abi_decode
        raw = bytes.fromhex(body.split('"data":"0x')[1].split('"')[0][8:])
        _, _, amount_in, _, _ = abi_decode(["address", "address", "uint256", "uint24", "uint160"], raw)
        amount_out = amount_in * 2 - (amount_in * amount_in) // 10**19  # curva concava finta
        from eth_abi import encode as abi_encode
        result_hex = "0x" + abi_encode(["uint256", "uint160", "uint32", "uint256"], [amount_out, 0, 1, 21000]).hex()
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result_hex})

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory(handler))
    small = _run(quotes.uniswap_v3_quote(ROBINHOOD_WETH, ROBINHOOD_USDG, 3000, 10**15))
    large = _run(quotes.uniswap_v3_quote(ROBINHOOD_WETH, ROBINHOOD_USDG, 3000, 10**17))
    assert large["price_impact_pct"] > small["price_impact_pct"]


def test_find_pool_and_fee_tries_preferred_fee_first(monkeypatch):
    """Lorenzo, con $MYCEL: 'nessun pool per questa coppia su questo fee
    tier' quando il pool c'era, solo su un tier diverso da 3000 — gli
    endpoint manuali provavano solo 3000, l'auto-trade della watchlist
    scorreva gia' tutti i tier. find_pool_and_fee() e' l'unico posto ora."""
    tried = []

    async def _fake_get_pool(token_a, token_b, fee):
        tried.append(fee)
        return "0xPool" if fee == 10000 else None

    monkeypatch.setattr(quotes, "get_pool", _fake_get_pool)
    result = _run(quotes.find_pool_and_fee("A", "B", preferred_fee=3000))
    assert result == ("0xPool", 10000)
    assert tried[0] == 3000  # il preferito, provato per primo
    assert 10000 in tried  # e si ferma non appena trova il pool, non scorre il resto


def test_find_pool_and_fee_without_preference_tries_all_tiers(monkeypatch):
    async def _fake_get_pool(token_a, token_b, fee):
        return None

    monkeypatch.setattr(quotes, "get_pool", _fake_get_pool)
    result = _run(quotes.find_pool_and_fee("A", "B"))
    assert result is None


def test_find_pool_and_fee_real_network_call_finds_the_weth_usdg_pool():
    result = _run(quotes.find_pool_and_fee(ROBINHOOD_WETH, ROBINHOOD_USDG))
    assert result is not None
    pool, fee = result
    # Non un fee tier fisso: su Robinhood Chain il pool WETH/USDG a 3000 e'
    # stato affiancato da uno a 500 (2026-09-23), e find_pool_and_fee
    # restituisce il primo tier che esiste. Il contratto e' "un pool vero su
    # un tier noto", non quale tier il mercato usi oggi.
    assert fee in quotes.ROBINHOOD_FEE_TIERS
    assert pool == _run(quotes.get_pool(ROBINHOOD_WETH, ROBINHOOD_USDG, fee))
