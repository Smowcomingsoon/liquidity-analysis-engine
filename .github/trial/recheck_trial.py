"""Temporary: the rigorous re-check on real tokens, with DeepSeek (#464). Removed before merge."""
import asyncio
import json

import httpx

from app.notify import deepseek_desk
from app.trading.onchain import deep_recheck, scout_recheck

HYDX_POINTS = [
    "Only 6 days old with a $242k market cap and $48k liquidity: extremely early and thin",
    "No public GitHub and no contract link on the site (contract_link: false, cites_contract: false), so the 'unified marketplace' claim can't be verified",
    "Site is a SPA with only 3 of 18 internal pages passing checks, so much of the 780-word content may be unreachable or thin",
    "Name 'HyperDex' is generic and the excerpt is pure marketing ('Every Issuer. Every Chain. Best Price.') with no named integrations or partners to back it",
    "No X or Telegram data provided, so there is no evidence of real community traction beyond the 1,291 holders",
]
GENERIC = ["No public GitHub", "No contract link on the site", "No X or Telegram data provided",
           "No verifiable product: the site is marketing only"]


PFWA_POINTS = [
    "No public GitHub or code repository",
    "24h volume $0: no real trading",
    "0% of liquidity locked",
    "Robinhood Chain has no users or activity",
    "No team information and no socials",
]


async def main():
    items = [{"chain": "robinhood", "token_address": "0xAA5751Ff3Fb61e613993B2EB4EAb1fce017081fc", "symbol": "HYDX",
              "name": "HyperDex", "website": "https://hyperdex.app", "market_cap": 242000, "liquidity_usd": 48000,
              "socials": [{"type": "twitter", "url": "https://x.com/hyperdex_app"}]}]
    items += [json.loads(line) for line in open("../.github/trial/items.jsonl") if line.strip()]
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for it in items:
            # The market fields a production item carries (projects_db), from the deepest pair.
            d = (await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{it['token_address']}")).json()
            ps = sorted(d.get("pairs") or [], key=lambda q: -((q.get("liquidity") or {}).get("usd") or 0))
            if ps:
                q = ps[0]
                it.update({"volume_24h": (q.get("volume") or {}).get("h24"), "dex": q.get("dexId"),
                           "liquidity_usd": (q.get("liquidity") or {}).get("usd"), "market_cap": q.get("marketCap"),
                           "created_at": (q.get("pairCreatedAt") or 0) / 1000 or None,
                           "change_24h": (q.get("priceChange") or {}).get("h24")})
            if it["symbol"] == "PFWA":
                it["volume_24h"] = 0  # the $0 a single aggregator once gave
            points = {"HYDX": HYDX_POINTS, "PFWA": PFWA_POINTS}.get(it["symbol"], GENERIC)
            entry = {**it, "pillars": {}, "judge": {"verdict": "avoid", "score": 30, "problems": points},
                     "review": {"keep": False, "reason": "first board review dropped it"}}
            rc = await scout_recheck.recheck(entry, it, c) or {}
            user, claims, facts = deep_recheck.prompt(entry, rc)
            raw = await deepseek_desk.ask([{"role": "system", "content": deep_recheck.SYSTEM},
                                           {"role": "user", "content": user}],
                                          kind="research", max_tokens=2000, json_mode=True, purpose="trial")
            parsed = deep_recheck._parse(raw, facts)
            print(f"\n===== {it['symbol']}")
            for r in rc.get("checked") or []:
                print(f"  checked  {r['source']}: {r['result']} - {r['detail']}")
            for cl in claims:
                print(f"  {cl['status']:10} {cl['text'][:90]}\n             note: {cl['note']}\n             quote: {cl['evidence']}")
            print("  VERDICT", json.dumps(parsed, ensure_ascii=False, indent=1))
            print("  RAW", raw[:1500])


asyncio.run(main())
