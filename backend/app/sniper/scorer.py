"""Anti-scam + market signal scoring — v5 (simplified).

Philosophy: score only what can actually be measured on a new token.
Old criteria (LP lock, GitHub, team, description, supply cap) required
data that early-stage tokens almost never have — producing artificially
low scores that made every legitimate project look suspicious.

New scoring: 10 pts max, two axes only.

  SICUREZZA  (6 pts, measured only when GoPlus / Honeypot.is / Rugcheck reply)
    Non è honeypot                     3 pts  ← highest weight
    Tax ragionevole (buy+sell ≤ 10 %)  2 pts
    No rug flags (ownership, blacklist) 1 pt

  MERCATO  (4 pts, always measured — DexScreener data)
    Età ottimale (30 min – 72 h)        1 pt
    Volume autentico (no wash trading)  1 pt
    Buy pressure positiva (B/S ≥ 1.0)  1 pt
    Liquidità proporzionata (≥ 3 % MC) 1 pt

Score = earned / measured_max (denominator excludes unmeasured criteria).
Without security APIs: max = 4 (market data only).
With GoPlus full data: max = 10.
"""
import asyncio
from dataclasses import dataclass, field

import httpx

from .security import (
    GoplusResult,
    HoneypotIsResult,
    RugcheckResult,
    check_coingecko,
    check_goplus,
    check_honeypot_is,
    check_jupiter_price,
    check_rugcheck,
)


@dataclass
class ScoreItem:
    category: str
    criterion: str
    earned: int
    max: int = 1
    auto: bool = True
    note: str = ""
    measured: bool = True


@dataclass
class TokenScore:
    total: int
    max: int
    items: list[ScoreItem] = field(default_factory=list)
    is_honeypot: bool | None = None
    buy_tax: float | None = None
    sell_tax: float | None = None


async def score(token, client: httpx.AsyncClient) -> TokenScore:  # token: RawToken
    items: list[ScoreItem] = []
    chain = token.chain_id
    addr = token.token_address

    # ── Fetch security data in parallel ───────────────────────────────────────
    async def _none():
        return None

    gp: GoplusResult | None
    rc: RugcheckResult | None
    hi: HoneypotIsResult | None

    if chain == "solana":
        gp, rc, _cg, _jup = await asyncio.gather(
            check_goplus(chain, addr, client),
            check_rugcheck(addr, client),
            check_coingecko(chain, addr, client),
            check_jupiter_price(addr, client),
        )
        hi = None
    else:
        gp, hi, _cg = await asyncio.gather(
            check_goplus(chain, addr, client),
            check_honeypot_is(chain, addr, client),
            check_coingecko(chain, addr, client),
        )
        rc = None

    has_goplus = gp is not None
    has_honeypot_is = hi is not None and hi.simulation_success

    # ── SICUREZZA ─────────────────────────────────────────────────────────────

    # 1. Non è un honeypot (3 pts — triplo peso)
    not_honeypot = False
    measured_hp = False
    if has_goplus and gp.is_honeypot is not None:  # type: ignore[union-attr]
        not_honeypot = not gp.is_honeypot  # type: ignore[union-attr]
        measured_hp = True
        note_hp = "GoPlus: not a honeypot" if not_honeypot else "⚠️ GoPlus: HONEYPOT"
        if not_honeypot and has_honeypot_is and hi.is_honeypot is True:  # type: ignore[union-attr]
            not_honeypot = False
            note_hp = "⚠️ Honeypot.is: simulation failed"
    elif has_honeypot_is and hi.is_honeypot is not None:  # type: ignore[union-attr]
        not_honeypot = not hi.is_honeypot  # type: ignore[union-attr]
        measured_hp = True
        note_hp = ("Honeypot.is: simulation OK" if not_honeypot
                   else "⚠️ Honeypot.is: HONEYPOT")
    elif rc is not None:
        danger = [r for r in (rc.risks or []) if r.get("level") == "danger"]
        not_honeypot = not rc.rugged and len(danger) == 0
        measured_hp = True
        if rc.rugged:
            note_hp = "Rugcheck: token already rugged"
        elif danger:
            note_hp = f"Rugcheck: {', '.join(r.get('name','?') for r in danger[:2])}"
        else:
            note_hp = f"Rugcheck: no critical flags (score {rc.score})"
    else:
        note_hp = "Manual check — no API available"

    items.append(ScoreItem("Security", "Not a honeypot",
                           3 if not_honeypot else 0, max=3,
                           auto=measured_hp, measured=measured_hp,
                           note=note_hp))

    # 2. Tax ragionevole buy+sell ≤ 10 % (2 pts)
    tax_ok = False
    measured_tax = False
    buy_tax: float | None = None
    sell_tax: float | None = None
    if has_goplus and gp.buy_tax is not None and gp.sell_tax is not None:  # type: ignore[union-attr]
        buy_tax, sell_tax = gp.buy_tax, gp.sell_tax  # type: ignore[union-attr]
        total_tax = (buy_tax or 0) + (sell_tax or 0)
        tax_ok = total_tax <= 10.0
        measured_tax = True
        note_tax = f"Buy {buy_tax:.1f}% + Sell {sell_tax:.1f}% = {total_tax:.1f}%"
    elif has_honeypot_is and hi.buy_tax is not None and hi.sell_tax is not None:  # type: ignore[union-attr]
        buy_tax, sell_tax = hi.buy_tax, hi.sell_tax  # type: ignore[union-attr]
        total_tax = (buy_tax or 0) + (sell_tax or 0)
        tax_ok = total_tax <= 10.0
        measured_tax = True
        note_tax = f"Honeypot.is: {buy_tax:.1f}% + {sell_tax:.1f}% = {total_tax:.1f}%"
    elif rc is not None:
        tax_risks = [r for r in (rc.risks or []) if "tax" in r.get("name", "").lower()]
        tax_ok = len(tax_risks) == 0
        measured_tax = True
        note_tax = "Rugcheck: no unusual tax" if tax_ok else "Rugcheck: tax detected"
    else:
        note_tax = "Manual check"

    items.append(ScoreItem("Security", "Reasonable tax (buy+sell ≤ 10%)",
                           2 if tax_ok else 0, max=2,
                           auto=measured_tax, measured=measured_tax,
                           note=note_tax))

    # 3. No rug flags — ownership OK, no blacklist, no rug (1 pt)
    no_rugflags = False
    measured_rug = False
    if has_goplus:
        renounced = bool(gp.owner_renounced) and not gp.can_take_back_ownership  # type: ignore[union-attr]
        blacklisted = gp.is_blacklisted is True  # type: ignore[union-attr]
        no_rugflags = not blacklisted
        measured_rug = True
        parts = []
        if blacklisted:
            parts.append("blacklist detected")
        if gp.can_take_back_ownership:  # type: ignore[union-attr]
            parts.append("can reclaim ownership")
        if parts:
            note_rug = "GoPlus: " + ", ".join(parts)
        elif renounced:
            note_rug = "GoPlus: ownership renounced, no flags"
        else:
            note_rug = "GoPlus: no critical flags"
    elif rc is not None:
        rug_risks = [r for r in (rc.risks or [])
                     if any(k in r.get("name", "").lower()
                            for k in ("freeze", "blacklist", "rug", "restriction"))]
        no_rugflags = not rc.rugged and len(rug_risks) == 0
        measured_rug = True
        note_rug = ("Rugcheck: no rug flags" if no_rugflags
                    else "Rugcheck: rug flags detected")
    else:
        note_rug = "Manual check"

    items.append(ScoreItem("Security", "No rug/blacklist flags",
                           1 if no_rugflags else 0, max=1,
                           auto=measured_rug, measured=measured_rug,
                           note=note_rug))

    # ── MERCATO (always measured from DexScreener) ────────────────────────────

    # 4. Età ottimale: 30 min – 72 h
    age_h = token.age_hours
    age_ok = 0.5 <= age_h <= 72.0
    if age_h < 0.5:
        note_age = f"Too new ({age_h * 60:.0f} min) — unreliable data"
    elif age_h > 72.0:
        note_age = f"Outside window ({age_h:.0f}h) — early opportunity gone"
    else:
        note_age = f"Age {age_h:.1f}h — in the optimal window"
    items.append(ScoreItem("Market", "Optimal age (30 min – 72 h)",
                           1 if age_ok else 0, auto=True, measured=True,
                           note=note_age))

    # 5. Volume autentico (no wash trading)
    liq = token.liquidity_usd
    wash_ratio = (token.volume_h24 / liq) if liq > 0 else 0
    vol_h1 = token.volume_h1
    vol_m5 = token.volume_m5
    if wash_ratio > 5.0:
        vol_ok = False
        note_vol = f"Suspected wash trading (vol/liq {wash_ratio:.0f}x)"
    elif vol_h1 > 0:
        ratio_m5 = vol_m5 / (vol_h1 / 12)
        vol_ok = ratio_m5 <= 10.0
        note_vol = (f"Spread-out volume ({ratio_m5:.1f}x expected)" if vol_ok
                    else f"Abnormal volume spike ({ratio_m5:.1f}x)")
    else:
        vol_ok = True
        note_vol = "No h1 volume — not penalized"
    items.append(ScoreItem("Market", "Genuine volume (no wash)",
                           1 if vol_ok else 0, auto=True, measured=True,
                           note=note_vol))

    # 6. Buy pressure positiva (B/S ≥ 1.0 nell'ultima ora)
    bsr = token.buy_sell_ratio
    bsr_ok = bsr >= 1.0
    if bsr >= 1.0:
        note_bsr = f"B/S h1: {bsr:.2f} — buy pressure"
    else:
        note_bsr = f"B/S h1: {bsr:.2f} — more sellers than buyers"
    items.append(ScoreItem("Market", "Positive buy pressure (B/S ≥ 1.0)",
                           1 if bsr_ok else 0, auto=True, measured=True,
                           note=note_bsr))

    # 7. Liquidità proporzionata (liq/MC ≥ 3%, o liq > 5k se MC assente)
    mc = token.market_cap
    if mc and mc > 0:
        liq_ratio = liq / mc
        liq_ok = liq_ratio >= 0.03
        note_liq = (f"Liq/MC {liq_ratio * 100:.1f}% — proportionate" if liq_ok
                    else f"Liq/MC {liq_ratio * 100:.1f}% — low relative to MC")
    else:
        liq_ok = liq >= 5_000
        note_liq = (f"Liquidity ${liq:,.0f} — sufficient" if liq_ok
                    else f"Liquidity ${liq:,.0f} — too low")
    items.append(ScoreItem("Market", "Proportionate liquidity (≥ 3% MC)",
                           1 if liq_ok else 0, auto=True, measured=True,
                           note=note_liq))

    # ── Totale ────────────────────────────────────────────────────────────────
    total = sum(i.earned for i in items if i.measured)
    max_pts = sum(i.max for i in items if i.measured)

    return TokenScore(
        total=total,
        max=max_pts,
        items=items,
        is_honeypot=gp.is_honeypot if gp else None,
        buy_tax=buy_tax,
        sell_tax=sell_tax,
    )
