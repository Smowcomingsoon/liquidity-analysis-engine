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


async def main():
    items = [{"chain": "robinhood", "token_address": "0xAA5751Ff3Fb61e613993B2EB4EAb1fce017081fc", "symbol": "HYDX",
              "name": "HyperDex", "website": "https://hyperdex.app", "market_cap": 242000, "liquidity_usd": 48000,
              "socials": [{"type": "twitter", "url": "https://x.com/hyperdex_app"}]}]
    items += [json.loads(line) for line in open("../.github/trial/items.jsonl")]
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
        for it in items:
            points = HYDX_POINTS if it["symbol"] == "HYDX" else GENERIC
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
