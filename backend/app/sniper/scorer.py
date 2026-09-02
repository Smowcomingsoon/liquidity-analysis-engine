"""On-chain + DEXScreener scoring: 0–13 points across four categories."""
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx

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
    auto: bool = True  # True = verified automatically; False = needs manual check
    note: str = ""


@dataclass
class TokenScore:
    total: int
    max: int
    items: list[ScoreItem] = field(default_factory=list)


async def _expl(client: httpx.AsyncClient, base: str, key: str, **params) -> Optional[dict]:
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

    # ── TOKENOMICS (4 pts) ──

    # 1. Supply capped — infer from verified source code
    verified = False
    supply_capped = False
    if has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="contract", action="getsourcecode", address=addr)
        if data:
            src = data["result"][0].get("SourceCode", "")
            verified = bool(src)
            supply_capped = verified and "function mint" not in src.lower()

    items.append(ScoreItem("Tokenomics", "Supply capped (no mint attiva)",
                           1 if supply_capped else 0, auto=has_api,
                           note="Da source code" if has_api else "Nessuna API explorer — verifica manuale"))

    # 2. Holder distribution — top holder < 15% (Etherscan TokenHolderPercent field)
    good_dist = False
    if has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="token", action="tokenholderlist",
                           contractaddress=addr, page=1, offset=10)
        if data and isinstance(data.get("result"), list) and data["result"]:
            top_pct = float(data["result"][0].get("TokenHolderPercent") or 100)
            good_dist = top_pct < 15

    items.append(ScoreItem("Tokenomics", "Distribuzione sana (top holder <15%)",
                           1 if good_dist else 0, auto=has_api,
                           note="Top holder verificato" if good_dist else "Verifica manuale raccomandata"))

    # 3. LP locked — check if LP token (pair_address) is held by a known lock contract
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
                           note="Lock contract rilevato tra top LP holder" if lp_locked
                           else "Verifica manuale su Unicrypt / TeamFinance"))

    # 4. Ownership renounced — OwnershipTransferred to address(0)
    renounced = False
    if has_api:
        data = await _expl(client, base_url, api_key,  # type: ignore[arg-type]
                           module="logs", action="getLogs",
                           address=addr, topic0=_OWNERSHIP_TOPIC)
        if data and isinstance(data.get("result"), list):
            for log in data["result"]:
                topics = log.get("topics") or []
                if len(topics) >= 3 and topics[2].lstrip("0x").zfill(64) == _ZERO:
                    renounced = True
                    break

    items.append(ScoreItem("Tokenomics", "Ownership rinunciata",
                           1 if renounced else 0, auto=has_api,
                           note="Owner = address(0)" if renounced else "Verifica manuale"))

    # ── UTILITY (3 pts) ──
    has_desc = bool(token.description and len(token.description) > 50)
    has_website = bool(token.website)

    items.append(ScoreItem("Utilità", "Use case identificabile",
                           1 if has_desc else 0, auto=True,
                           note="Descrizione DEXScreener presente" if has_desc else "Nessuna descrizione rilevata"))
    items.append(ScoreItem("Utilità", "MVP o prodotto esistente",
                           0, auto=False,
                           note=f"Verifica manuale: {token.website}" if has_website else "Nessun sito web rilevato"))
    items.append(ScoreItem("Utilità", "Integrazione chain-native",
                           0, auto=False, note="Verifica manuale whitepaper / GitHub"))

    # ── TEAM (3 pts) ──
    items.append(ScoreItem("Team", "Contratto verificato su explorer",
                           1 if verified else 0, auto=has_api,
                           note="Source code pubblico" if verified else "Non verificato"))

    has_github = any("github.com" in (s.get("url") or "") for s in (token.socials or []))
    items.append(ScoreItem("Team", "Sviluppo attivo (GitHub presente)",
                           1 if has_github else 0, auto=True,
                           note="GitHub link nei socials" if has_github else "Nessun GitHub rilevato"))
    items.append(ScoreItem("Team", "Team identificabile / doxxato",
                           0, auto=False, note="Verifica manuale"))

    # ── SEGNALI DI MERCATO (3 pts) ──
    items.append(ScoreItem("Mercato", "Crescita organica holder",
                           0, auto=False, note="Richiede storico holder (API premium)"))

    vol_h1 = token.volume_h1
    vol_m5 = token.volume_m5
    # m5 expected ~ vol_h1/12; allow 0.1x–8x variance
    if vol_h1 > 0:
        ratio = vol_m5 / (vol_h1 / 12)
        vol_ok = 0.05 <= ratio <= 8.0
        vol_note = f"m5/atteso: {ratio:.1f}x"
    else:
        vol_ok = False
        vol_note = "Dati volume insufficienti"

    items.append(ScoreItem("Mercato", "Pattern volume pulito",
                           1 if vol_ok else 0, auto=True, note=vol_note))

    bsr = token.buy_sell_ratio
    items.append(ScoreItem("Mercato", "Buy pressure sostenuta (B/S ≥1.1)",
                           1 if bsr >= 1.1 else 0, auto=True, note=f"B/S h1: {bsr:.2f}"))

    total = sum(i.earned for i in items)
    max_pts = sum(i.max for i in items)
    return TokenScore(total=total, max=max_pts, items=items)
