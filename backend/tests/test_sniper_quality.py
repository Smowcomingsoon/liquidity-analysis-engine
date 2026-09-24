"""Lo sniper non deve piu' proporre meme coin.

Il caso che conta e' il terzo: una meme coin con liquidita', capitalizzazione,
eta' e volume da progetto vero. Le soglie numeriche da sole la lascerebbero
passare — e' il nome a tradirla.
"""
from app.sniper.quality import is_serious

SERIOUS = dict(
    symbol="LINK", name="Chainlink",
    liquidity_usd=4_000_000.0, market_cap=9_000_000_000.0,
    volume_h24=50_000_000.0, age_hours=40_000.0,
    website="https://chain.link", cex_listed=None,
)


def test_un_progetto_con_i_fondamentali_passa():
    ok, _ = is_serious(**SERIOUS)
    assert ok


def test_il_lancio_di_ieri_non_passa():
    ok, why = is_serious(**{**SERIOUS, "age_hours": 20.0, "cex_listed": None})
    assert not ok
    assert "young" in why


def test_una_meme_coin_ricca_resta_fuori():
    ok, why = is_serious(**{**SERIOUS, "symbol": "PEPE", "name": "Pepe"})
    assert not ok
    assert "meme" in why


def test_il_nome_batte_anche_la_quotazione_in_borsa():
    # DOGE e' quotato ovunque: senza il controllo sul nome entrerebbe.
    ok, _ = is_serious(**{**SERIOUS, "symbol": "DOGE", "name": "Dogecoin", "cex_listed": True})
    assert not ok


def test_la_quotazione_sostituisce_i_fondamentali():
    ok, why = is_serious(**{
        **SERIOUS, "age_hours": 50.0, "liquidity_usd": 1_000.0,
        "market_cap": None, "website": None, "cex_listed": True,
    })
    assert ok
    assert "exchange" in why


def test_senza_sito_e_senza_quotazione_non_passa():
    ok, why = is_serious(**{**SERIOUS, "website": None})
    assert not ok
    assert "website" in why


def test_dogwifhat_non_sfugge_attaccato():
    ok, _ = is_serious(**{**SERIOUS, "symbol": "WIF", "name": "dogwifhat"})
    assert not ok


def test_un_nome_serio_che_contiene_una_parola_meme_non_viene_buttato():
    # "Aptos" contiene "ape", "Catalyst" contiene "cat": non devono cadere.
    for sym, nm in (("APT", "Aptos"), ("CTA", "Catalyst"), ("MOO", "Moonbeam Network")):
        ok, why = is_serious(**{**SERIOUS, "symbol": sym, "name": nm})
        assert ok, f"{nm} scartato: {why}"
