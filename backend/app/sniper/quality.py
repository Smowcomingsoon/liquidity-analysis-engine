"""Tells a project apart from a meme coin.

The sniper was built to find launches: market cap floor $1k, liquidity floor
$500, anything younger than 30 days. That is the meme-coin universe by
construction — a serious project is never six hours old with $500 of
liquidity, and Pump.fun is a meme launchpad, not a discovery feed.

So "no meme coins" is not one more slider on the same scan. It is a second,
much higher bar, applied on top: a token clears it either because a major
exchange already vetted it, or because it looks like something that has been
around and has somewhere to point to.

None of this is a judgement of value. It is a judgement of *evidence*, and it
is deliberately conservative: a real project caught by the name filter is a
nuisance, a meme coin presented as a serious one is a loss.
"""
from __future__ import annotations

# A token whose name or symbol says "meme" is a meme coin whatever its
# numbers look like — including the ones with real liquidity, which is
# exactly the case the numeric floors below would let through.
MEME_TOKENS = {
    "doge", "shib", "shiba", "inu", "pepe", "wojak", "chad", "bonk", "wif",
    "floki", "elon", "musk", "trump", "biden", "moon", "moonshot", "safemoon",
    "baby", "cat", "kitty", "meme", "memecoin", "cum", "ass", "chud", "retard",
    "pump", "rug", "ponzi", "degen", "apu", "brett", "mog", "turbo", "banana",
    "frog", "hamster", "monkey", "ape", "gigachad", "based", "kek", "lambo",
    "tendies", "wagmi", "hodl", "fomo", "gm", "ngmi",
}

# Substrings that are meme-y even glued inside a longer word ("dogwifhat",
# "babydoge"). Kept short and unambiguous on purpose: matching "cat" inside
# "Catalyst" or "ape" inside "Aptos" would throw away real projects, so those
# only ever match as whole tokens above.
MEME_FRAGMENTS = ("doge", "shib", "pepe", "safemoon", "wifhat", "inuinu")

# Bars for a token no exchange has listed yet. All of them must be cleared.
LIQUIDITY_MIN = 250_000.0
MARKET_CAP_MIN = 5_000_000.0
VOLUME_H24_MIN = 100_000.0
AGE_MIN_H = 720.0  # 30 days — younger than this, there is nothing to judge


def _looks_like_meme(symbol: str, name: str) -> bool:
    blob = f"{symbol} {name}".lower()
    words = {w.strip("$#@.,-_()[]") for w in blob.replace("/", " ").split()}
    if words & MEME_TOKENS:
        return True
    squashed = "".join(ch for ch in blob if ch.isalnum())
    return any(frag in squashed for frag in MEME_FRAGMENTS)


def is_serious(
    *,
    symbol: str,
    name: str,
    liquidity_usd: float,
    market_cap: float | None,
    volume_h24: float,
    age_hours: float,
    website: str | None,
    cex_listed: bool | None,
) -> tuple[bool, str]:
    """Return (verdict, reason). The reason is shown to the user, so it names
    the specific bar that was missed rather than saying 'filtered'."""
    if _looks_like_meme(symbol, name):
        return False, "meme coin name or symbol"

    # A major exchange has already done the listing work: age, liquidity and
    # market cap are its problem, not ours.
    if cex_listed is True:
        return True, "listed on a centralized exchange"

    if age_hours < AGE_MIN_H:
        return False, f"too young ({age_hours / 24:.0f} days)"
    if liquidity_usd < LIQUIDITY_MIN:
        return False, f"liquidity ${liquidity_usd:,.0f} below threshold"
    if market_cap is None:
        return False, "capitalizzazione sconosciuta"
    if market_cap < MARKET_CAP_MIN:
        return False, f"market cap ${market_cap:,.0f} below threshold"
    if volume_h24 < VOLUME_H24_MIN:
        return False, f"24h volume ${volume_h24:,.0f} below threshold"
    if not website:
        return False, "no project website"

    return True, "fundamentals above thresholds"
