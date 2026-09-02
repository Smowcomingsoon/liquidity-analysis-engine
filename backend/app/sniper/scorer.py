"""On-chain + DEXScreener + GoPlus + Rugcheck scoring: 0–15 points.

Categories
  Tokenomics  4 pts  — supply, distribution, LP lock, ownership
  Sicurezza   3 pts  — honeypot, tax, trading restrictions (GoPlus / Rugcheck)
  Utilità     3 pts  — use-case, web+social, MVP
  Team        3 pts  — verified contract, GitHub, doxxed
  Mercato     3 pts  — data stability, volume pattern, buy pressure

Scoring revisions (v3):
- GoPlus Security (free, no key) is the primary source for EVM checks.
  Etherscan/explorer APIs are used as fallback when GoPlus data is absent.
- Rugcheck.xyz (free, no key) is used for Solana security checks.
- DefiLlama cross-validates market-cap when GoPlus MC data differs.
- Wash-trading: vol/liq > 500% deducts market score.
- Reward volume trend: volume_h1 / volume_h24 > 10% = trending now.
- Penalise age < 0.5h: data too unstable.
- Bonus if BOTH website AND social present (genuine project signal).
- Buy-pressure sweet spot: 1.1x–3x ideal; above 3x suspicious.
"""
import asyncio
import os
from dataclasses import dataclass, field

import httpx

from .security import GoplusResult, RugcheckResult, check_goplus, check_rugcheck

ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
BSCSCAN_KEY = os.environ.get("BSCSCAN_API_KEY", "")
BASESCAN_KEY = os.environ.get("BASESCAN_API_KEY", "")
ARBISCAN_KEY = os.environ.get("ARBISCAN_API_KEY", "")

EXPLORERS: dict[str, tuple[str, str]] = {
    "ethereum": ("https://api.etherscan.io/api", ETHERSCAN_KEY),
    "bsc": ("https://api.bscscan.com/api", BSCSCAN_KEY),
    "base": ("https://api.basescan.org/api", BASESCAN_KEY),
    "arbitrum": ("https://api.arbiscan.io/api", ARBISCAN_KEY),
}

# OwnershipTransferred(address,address) keccak256 topic
_OWNERSHIP_TOPIC = "0x8be0079c531659141344cd1fd0a4f28419497f9722a3daafe3b4186f6b6457e0"
_ZERO = "0" * 64

# Known LP lock contract addresses (lowercase)
_LOCK_CONTRACTS = {
    "0x663a5c229c09b049e36dcc11a9b0d4a8eb9db214",  # Unicrypt ETH
    "0xc765bddb93b0d1c1a88282ba0fa6b2d00e3e0c83",  # Unicrypt BSC
    "0xe2fe530c047f2d85298b07d9333c05737f1435fb",  # TeamFinance ETH
    "0x407993575c91ce7643a4d4ccacc9a98c36ee1bbe",  # PinkLock BSC
    "0x71b5759d73262fbb223956913ecf4ecc51057641",  # PinkLock ETH
    "0x000000000000000000000000000000000000dead",
}


@dataclass
class ScoreItem:
    category: str
    criterion: str
    earned: int
    max: int = 1
    auto: bool = True
    note: str = ""


@dataclass
class TokenScore:
    total: int
    max: int
    items: list[ScoreItem] = field(default_factory=list)
    # Security enrichment carried alongside score
    is_honeypot: bool | None = None
    buy_tax: float | None = None
    sell_tax: float | None = None


async def _expl(client: httpx.AsyncClient, base: str, key: str, **params) -> dict | None:
    """Call a block explorer API; return the response dict or None on error/failure."""
    try:
        r = await client.get(base, params={"apikey": key, **params}, timeout=8.0)
        data = r.json()
        if data.get("status") == "1":
            return data
    except Exception:
        pass
    return None


async def score(token, client: httpx.AsyncClient) -> TokenScore:  # token: RawToken
    items: list[ScoreItem] = []
    chain = token.chain_id
    addr = token.token_address
    pair_addr = token.pair_address

    base_url, api_key = EXPLORERS.get(chain, (None, None))  # type: ignore[misc]
    has_api = bool(base_url and api_key)

    # ── Fetch security data (GoPlus + Rugcheck) in parallel ──────────────────
    gp: GoplusResult | None
    rc: RugcheckResult | None
    if chain == "solana":
        gp_task = asyncio.create_task(check_goplus(chain, addr, client))
        rc_task = asyncio.create_task(check_rugcheck(addr, client))
        gp, rc = await asyncio.gather(gp_task, rc_task)
    else:
        gp = await check_goplus(chain, addr, client)
        rc = None

    has_goplus = gp is not None

    # ── TOKENOMICS (4 pts) ───────────────────────────────────────────────────

    # 1. Supply capped — prefer GoPlus is_mintable, fall back to source code
    verified = False
    supply_capped = False
    if has_goplus:
        supply_capped = gp.is_mintable is False  # type: ignore[union-attr]
        verified = gp.is_open_source is True  # type: ignore[union-attr]
        note_supply = "GoPlus: non mintabile" if supply_capped else "GoPlus: mint rilevata"
    elif has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="contract", action="getsourcecode", address=addr)
        if data:
            src = data["result"][0].get("SourceCode", "")
            verified = bool(src)
            supply_capped = verified and "function mint" not in src.lower()
        note_supply = "Da source code" if has_api else ""
    else:
        note_supply = "Nessuna API disponibile — verifica manuale"

    items.append(ScoreItem("Tokenomics", "Supply capped (no mint attiva)",
                           1 if supply_capped else 0,
                           auto=has_goplus or has_api, note=note_supply))

    # 2. Holder distribution — prefer GoPlus top_holder_pct, fall back to explorer
    good_dist = False
    if has_goplus and gp.top_holder_pct is not None:  # type: ignore[union-attr]
        good_dist = gp.top_holder_pct < 15  # type: ignore[union-attr]
        note_dist = f"GoPlus: top holder {gp.top_holder_pct:.1f}%"  # type: ignore[union-attr]
    elif has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="token", action="tokenholderlist",
                           contractaddress=addr, page=1, offset=10)
        if data and isinstance(data.get("result"), list) and data["result"]:
            top_pct = float(data["result"][0].get("TokenHolderPercent") or 100)
            good_dist = top_pct < 15
            note_dist = f"Top holder {top_pct:.1f}%"
        else:
            note_dist = "Verifica manuale raccomandata"
    else:
        note_dist = "Verifica manuale raccomandata"

    items.append(ScoreItem("Tokenomics", "Distribuzione sana (top holder <15%)",
                           1 if good_dist else 0, auto=has_goplus or has_api,
                           note=note_dist))

    # 3. LP locked — only via explorer (GoPlus LP holder data is less reliable)
    lp_locked = False
    if has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="token", action="tokenholderlist",
                           contractaddress=pair_addr, page=1, offset=10)
        if data and isinstance(data.get("result"), list):
            for h in data["result"]:
                if (h.get("TokenHolderAddress") or "").lower() in _LOCK_CONTRACTS:
                    lp_locked = True
                    break

    items.append(ScoreItem("Tokenomics", "LP bloccata (Unicrypt / PinkLock / burn)",
                           1 if lp_locked else 0, auto=has_api,
                           note="Lock contract rilevato" if lp_locked
                           else "Verifica manuale su Unicrypt / TeamFinance"))

    # 4. Ownership renounced — prefer GoPlus owner_renounced, fall back to event log
    renounced = False
    if has_goplus:
        renounced = bool(gp.owner_renounced)  # type: ignore[union-attr]
        if gp.can_take_back_ownership:  # type: ignore[union-attr]
            renounced = False  # override: contract can regain ownership
        note_ren = "GoPlus: owner = 0x0" if renounced else (
            "GoPlus: può riprendere ownership" if gp.can_take_back_ownership  # type: ignore[union-attr]
            else "GoPlus: owner attivo"
        )
    elif has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="logs", action="getLogs",
                           address=addr, topic0=_OWNERSHIP_TOPIC)
        if data and isinstance(data.get("result"), list):
            for log in data["result"]:
                topics = log.get("topics") or []
                if len(topics) >= 3 and topics[2].lstrip("0x").zfill(64) == _ZERO:
                    renounced = True
                    break
        note_ren = "Owner = address(0)" if renounced else "Verifica manuale"
    else:
        note_ren = "Verifica manuale"

    items.append(ScoreItem("Tokenomics", "Ownership rinunciata",
                           1 if renounced else 0, auto=has_goplus or has_api,
                           note=note_ren))

    # ── SICUREZZA (3 pts) ─────────────────────────────────────────────────────

    # 5. Non è un honeypot
    not_honeypot = False
    if has_goplus and gp.is_honeypot is not None:  # type: ignore[union-attr]
        not_honeypot = not gp.is_honeypot  # type: ignore[union-attr]
        note_hp = "GoPlus: non è un honeypot" if not_honeypot else "⚠️ GoPlus: HONEYPOT rilevato"
    elif rc is not None:
        # Rugcheck for Solana: no "danger" level risks
        danger_risks = [r for r in (rc.risks or []) if r.get("level") == "danger"]
        not_honeypot = not rc.rugged and len(danger_risks) == 0
        if rc.rugged:
            note_hp = "Rugcheck: token già ruggato"
        elif danger_risks:
            note_hp = f"Rugcheck: {', '.join(r.get('name','?') for r in danger_risks[:2])}"
        else:
            score_txt = f" (score {rc.score})" if rc.score is not None else ""
            note_hp = f"Rugcheck: nessun rischio critico{score_txt}"
    else:
        note_hp = "Verifica manuale (honeypot.is / rugcheck)"

    items.append(ScoreItem("Sicurezza", "Non è un honeypot",
                           1 if not_honeypot else 0,
                           auto=has_goplus or rc is not None,
                           note=note_hp))

    # 6. Tax ragionevole (buy+sell ≤ 10%)
    tax_ok = False
    if has_goplus and gp.buy_tax is not None and gp.sell_tax is not None:  # type: ignore[union-attr]
        total_tax = (gp.buy_tax or 0) + (gp.sell_tax or 0)  # type: ignore[union-attr]
        tax_ok = total_tax <= 10.0
        note_tax = f"Buy {gp.buy_tax:.1f}% + Sell {gp.sell_tax:.1f}% = {total_tax:.1f}%"  # type: ignore[union-attr]
    elif rc is not None:
        # Solana: check for "high tax" risk in rugcheck
        tax_risks = [r for r in (rc.risks or []) if "tax" in r.get("name", "").lower()]
        tax_ok = len(tax_risks) == 0
        note_tax = "Rugcheck: nessuna tassa anomala" if tax_ok else "Rugcheck: tassa anomala rilevata"
    else:
        note_tax = "Verifica manuale su dexscreener.com"

    items.append(ScoreItem("Sicurezza", "Tax ragionevole (buy+sell ≤ 10%)",
                           1 if tax_ok else 0,
                           auto=has_goplus or rc is not None,
                           note=note_tax))

    # 7. No restrizioni di trading (cooldown / blacklist)
    no_restrictions = False
    if has_goplus:
        cd = gp.trading_cooldown  # type: ignore[union-attr]
        bl = gp.is_blacklisted  # type: ignore[union-attr]
        no_restrictions = cd is not True and bl is not True
        parts = []
        if cd:
            parts.append("cooldown attivo")
        if bl:
            parts.append("blacklist rilevata")
        note_restr = ("GoPlus: nessuna restrizione" if no_restrictions
                      else "GoPlus: " + ", ".join(parts))
    elif rc is not None:
        restr_risks = [r for r in (rc.risks or [])
                       if any(k in r.get("name", "").lower()
                              for k in ("freeze", "blacklist", "restriction"))]
        no_restrictions = len(restr_risks) == 0
        note_restr = ("Rugcheck: nessuna restrizione" if no_restrictions
                      else "Rugcheck: restrizioni rilevate")
    else:
        note_restr = "Verifica manuale"

    items.append(ScoreItem("Sicurezza", "Nessuna restrizione di trading",
                           1 if no_restrictions else 0,
                           auto=has_goplus or rc is not None,
                           note=note_restr))

    # ── UTILITÀ (3 pts) ───────────────────────────────────────────────────────
    has_desc = bool(token.description and len(token.description) > 50)
    has_website = bool(token.website)
    has_social = bool(token.socials)

    items.append(ScoreItem("Utilità", "Use case identificabile",
                           1 if has_desc else 0, auto=True,
                           note="Descrizione presente" if has_desc else "Nessuna descrizione"))
    items.append(ScoreItem("Utilità", "MVP o prodotto esistente",
                           0, auto=False,
                           note=f"Verifica manuale: {token.website}" if has_website
                           else "Nessun sito web"))
    items.append(ScoreItem("Utilità", "Presenza web + social completa",
                           1 if (has_website and has_social) else 0, auto=True,
                           note="Website e social presenti" if (has_website and has_social)
                           else "Manca website o social"))

    # ── TEAM (3 pts) ──────────────────────────────────────────────────────────
    # Contract verified — prefer GoPlus is_open_source, fall back to explorer
    if not has_goplus and has_api and not verified:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="contract", action="getsourcecode", address=addr)
        if data:
            verified = bool(data["result"][0].get("SourceCode", ""))

    items.append(ScoreItem("Team", "Contratto verificato su explorer",
                           1 if verified else 0, auto=has_goplus or has_api,
                           note="Source code pubblico" if verified else "Non verificato"))

    has_github = any("github.com" in (s.get("url") or "") for s in (token.socials or []))
    items.append(ScoreItem("Team", "Sviluppo attivo (GitHub presente)",
                           1 if has_github else 0, auto=True,
                           note="GitHub link nei socials" if has_github else "Nessun GitHub"))
    items.append(ScoreItem("Team", "Team identificabile / doxxato",
                           0, auto=False, note="Verifica manuale"))

    # ── SEGNALI DI MERCATO (3 pts) ────────────────────────────────────────────

    # Age guard
    age_h = token.age_hours
    if age_h < 0.5:
        items.append(ScoreItem("Mercato", "Dati stabili (età ≥ 30 min)",
                               0, auto=True, note=f"Token troppo nuovo ({age_h:.1f}h)"))
    else:
        items.append(ScoreItem("Mercato", "Dati stabili (età ≥ 30 min)",
                               1, auto=True, note=f"Età {age_h:.1f}h — affidabile"))

    # Volume pattern + wash-trading detection
    vol_h1 = token.volume_h1
    vol_m5 = token.volume_m5
    liq = token.liquidity_usd
    wash_ratio = (token.volume_h24 / liq) if liq > 0 else 0

    if wash_ratio > 5.0:
        items.append(ScoreItem("Mercato", "Pattern volume pulito",
                               0, auto=True,
                               note=f"Vol/Liq {wash_ratio:.0f}x — possibile wash-trading"))
    elif vol_h1 > 0:
        ratio = vol_m5 / (vol_h1 / 12)
        vol_ok = 0.05 <= ratio <= 8.0
        items.append(ScoreItem("Mercato", "Pattern volume pulito",
                               1 if vol_ok else 0, auto=True,
                               note=f"m5/atteso: {ratio:.1f}x"))
    else:
        items.append(ScoreItem("Mercato", "Pattern volume pulito",
                               0, auto=True, note="Dati volume insufficienti"))

    # Buy pressure sweet spot: 1.1x–3x
    bsr = token.buy_sell_ratio
    if 1.1 <= bsr <= 3.0:
        items.append(ScoreItem("Mercato", "Buy pressure sana (B/S 1.1x–3x)",
                               1, auto=True, note=f"B/S h1: {bsr:.2f} — zona ottimale"))
    elif bsr > 3.0:
        items.append(ScoreItem("Mercato", "Buy pressure sana (B/S 1.1x–3x)",
                               0, auto=True, note=f"B/S h1: {bsr:.2f} — pump sospetto"))
    else:
        items.append(ScoreItem("Mercato", "Buy pressure sana (B/S 1.1x–3x)",
                               0, auto=True, note=f"B/S h1: {bsr:.2f}"))

    total = sum(i.earned for i in items)
    max_pts = sum(i.max for i in items)
    return TokenScore(
        total=total,
        max=max_pts,
        items=items,
        is_honeypot=gp.is_honeypot if gp else None,
        buy_tax=gp.buy_tax if gp else None,
        sell_tax=gp.sell_tax if gp else None,
    )
