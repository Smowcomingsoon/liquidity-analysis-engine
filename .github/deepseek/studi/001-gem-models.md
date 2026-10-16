# Study 001: Model gems: what makes AUREON, STEALF, PFWA, GRID and BV7X worth ranking

Asked by Claude on 29/09 (request `requests/001-gem-models.md`), answered by DeepSeek on the server at 2026-09-29 07:23 UTC.
Lorenzo: "stagli addosso a DeepSeek [...] non darlo per buono [...] deve essere un lavoro perfetto perché da quello poi deriva la logica di tutto il sito."

## Claude's review (read this before using anything below)

**Verdict: not usable as rules yet.** The answer is careful about separating measured from claimed, but three of the five dossiers it reasoned on were stale, so several conclusions rest on data we already know is wrong:

1. **Stale pillars.** PFWA, GRID and BV7X were analysed before today's fixes and the dossier only re-fetched market data. So DeepSeek saw PFWA's site as "empty page" (it now reads ~1,750 words), GRID's "single wallet 29%" and BV7X's "41%" (on Robinhood these top holders are contracts such as the Uniswap pool manager, not wallets) and BV7X's "GitHub link without a repository" (its docs link public repos). Its calls "GRID fatal is right", "BV7X GitHub fatal is right" and "top10 > 50% = hard cap" are built on those artefacts. Fix: the dossier must re-run every pillar fresh and list the top holders with contract/wallet labels.
2. **Domain older than token read as a red flag.** It proposes "domain/token age ratio > 10x = aged domain, fresh token" as a warning (STEALF 27x, BV7X 20x). For builders it is the opposite: the project existed before its token (BV7X ran on Base since February 2026, its Robinhood pool is 12 days old). Rejected.
3. **Thresholds fitted to nothing.** "holders ≥ 1,500 must-have", "lp_locked ≥ 90%" are not derived from the five examples (they fail them) and ignore that LP locking on Robinhood's Uniswap v4 is measured differently. Not adopted until measured on fresh data.
4. **Missing data it did not ask for.** AUREON's public SDK (github.com/buildaureon/aureon-sdk) was listed among its sites but our GitHub pillar never read it; its site is JS-only and was unreadable. Both are our pipeline's faults, not the project's.
5. **Truncated.** The answer hit the 8,000-token limit at the end of section 10.

**Accepted (to verify and implement):**
- "token has no stated role" fires for every project, DREGG included: it measures the reader, not the token. Fix the reader.
- STEALF's "weakest pillar: site" label is wrong when GitHub is missing.
- Missing public code should lower confidence, not exclude; a dead code link is different from no link.
- Naming specific, falsifiable third-party primitives (Turnkey, Umbra, LayerZero, ERC-8004, x402, EAS, Lean/STARK) is a strong signal of a real build, and a good search pattern.
- STEALF's DeepSeek judge verdict "avoid" (38) is too harsh for a live docs site with 4,300 holders, 100% LP lock and 0% creator.

Next: fix the dossier and the pipeline bugs above, then ask again as study 002 (`follow_up: 001-gem-models`) with fresh data and these corrections.

---

## Request

The site owner hand-picked five small-cap projects as examples of the "serious gems" our Gem Finder must find by itself: AUREON (Robinhood Chain, aureonlabs.network, public SDK on GitHub), STEALF (Solana), PFWA (Robinhood Chain), GRID by Virtuals (Robinhood Chain), BV7X (Robinhood Chain). The sixth dossier, DREGG, is our existing benchmark. Until today our automatic rules kept all of them out, so the rules are wrong somewhere, and your study will become the logic of the whole selection. Be exhaustive, concrete and critical: an unverified claim is worse than no claim.

For EACH of the five projects write:
1. What it actually builds, in two sentences, citing only facts from its dossier (site/docs text, GitHub facts, pools, holders). Mark anything the project claims but the data does not show as "claimed, unverified".
2. Evidence of real work: product live or not (app URL, contracts, docs depth), code (public or not; if public: activity, size, originality), team signals, age and track record across chains.
3. Tokenomics and market: cap, FDV, liquidity/cap ratio, holder concentration (separate wallets from contracts such as pools, vesting, launchpads), volume, pool age vs project age.
4. Narrative: which of our catalogue narratives it fits and why it is a strong or weak narrative right now.
5. Red flags you can find in the data, even small ones. Be sceptical.
6. Which of our current rule verdicts (given in each dossier under the rules' verdict) is right and which is wrong, and why.

Then, across all five (and compared with DREGG):
7. The common profile of a "serious gem": the traits they share, stated as measurable signals our code can check (thresholds, ratios, presence of X, text patterns in docs), each with the data field it would read from the dossier. Distinguish must-have signals from bonuses.
8. Which of our current checks are too strict or measure the wrong thing, and exactly how you would change them (numbers).
9. What must still exclude a project, so that loosening the rules does not let memecoins, copies or rugs in.
10. Search strategy: which narratives, keywords, launchpads, chains, sources and patterns would find more projects like these; be specific enough to turn into search queries.

Answer in English markdown with one section per numbered point. Where the dossier lacks data, say exactly what is missing instead of guessing.

## Dossiers sent

```
## AUREON (AUREON) — robinhood:0xd293291060334D42E5DBEa6fb854C231AF527777
Status: not known to the gem finder. Fetched now: market, github, site, tokenomics.
Market: cap $105,893, liquidity $57,125, 24h volume $15,137, 24h change 60.97%, age 73 days
Pools: 1, main on uniswap (https://dexscreener.com/robinhood/0xf75fbce8d8096cbe8b2b057ed8190d124ea7a5bf), FDV $105,893, 24h txns {"buys": 114, "sells": 326}
Links: website https://www.aureonlabs.network/, other sites ["https://www.aureonlabs.network/", "https://github.com/buildaureon/aureon-sdk"], github none, socials ["https://x.com/buildaureon"]
Rules verdict (qualifies): passes
Researcher's floor: passes
Too big: passes
Final score: 61.0
Early gem: False
Overall: {"score": 71.0, "fatal": "", "pending": "", "weakest": "site"}
Pillar github: score None, fatal -, flags ["no public GitHub"]
  facts: {"missing": true}
Pillar site: score 50, fatal -, flags ["site built in JavaScript, not readable"]
  facts: {"sites_read": 1, "site_score": null, "description": {"words": 0, "mechanisms": [], "hype": 0}, "description_score": 0, "narratives": [], "stage": "unclear", "ai_substance": 0, "verdict": "thin", "original": true, "red_flags": [], "reader": true, "why": ["no technical concept", "no concrete detail", "stage unclear", "token has no stated role"], "parts": {"depth": 0.0, "concrete": 0.0, "stage": 0, "utility": 0.0, "info": 1.1, "coherence": 0, "candor": 0.0, "-buzz": 0.0, "-hype": 0, "-clone": 0}, "evidence": {"concepts": {}, "concrete": [], "stage": {"live": 0, "testnet": 0, "building": 0, "app_responds": false, "proof": []}, "utility": [], "coherence": null, "twin": null, "twin_similarity": 0.0}, "unreadable": "site built in JavaScript, not readable", "holders": null, "gt_score": null}
Pillar tokenomics: score 92, fatal -, flags []
  facts: {"top1_pct": 2.4, "top10_pct": 8.1, "creator_pct": 1.8, "holders": 2216, "insiders": 0, "lp_locked_pct": 99.6, "mint": false, "freeze": false, "tax_pct": 0.0, "contracts_pct": 13.6}
DeepSeek judge (cached): none
Code review (cached): none
<project_text>
[site summary] AUREON · the financial intelligence layer for Financial Compass on Robinhood Chain.
</project_text>
```

```
## STEALF (Stealf) — solana:G5W6LwkLeoj6rZqBP1y3KT8k6Cz6rXGJmMNU3TArXtXw
Status: on the gem finder's watch list. Fetched now: market, github.
Market: cap $656,866, liquidity $200,297, 24h volume $82,857, 24h change 18.37%, age 14 days
Pools: 1, main on meteora (https://dexscreener.com/solana/gqbkay1ajzivdjk8jhddmqapzn6ogefd12ltjyyxqc4c), FDV $656,866, 24h txns {"buys": 195, "sells": 189}
Links: website https://www.stealf.xyz/, other sites ["https://www.stealf.xyz/"], github none, socials ["https://x.com/STEALFxyz", "https://discord.gg/zYS2e2gyy"]
Database: score 51.2, rank None, why DeepSeek: No public GitHub and no code activity measured, so the 'privacy layer' and 'FHE/MPC/TEE' narratives are unverifiable marketing, categories [], sources ["jupiter", "database", "reference"]
Narratives: ["privacy", "fhe_mpc_tee", "stablecoin_payments"]
Rules verdict (qualifies): DeepSeek: No public GitHub and no code activity measured, so the 'privacy layer' and 'FHE/MPC/TEE' narratives are unverifiable marketing
Researcher's floor: passes
Too big: passes
Final score: 51.2
Early gem: False
Overall: {"score": 68.5, "fatal": "", "pending": "", "weakest": "site"}
Pillar github: score None, fatal -, flags ["no public GitHub"]
  facts: {"missing": true}
Pillar site: score 62, fatal -, flags []
  facts: {"url": "https://www.stealf.xyz/", "words": 231, "internal_pages": 3, "pages_ok": 3, "docs": true, "docs_ok": true, "app": false, "app_ok": false, "sections": ["team", "tokenomics"], "builder": null, "free_host": null, "spa": false, "domain_days": 378, "cites_contract": false, "https": true, "tech_terms": {}, "narratives": ["privacy", "fhe_mpc_tee", "stablecoin_payments"], "sites_read": 1, "site_score": 56, "description": {"words": 0, "mechanisms": [], "hype": 0}, "description_score": 0, "stage": "live", "ai_substance": 7, "verdict": "promising", "original": true, "red_flags": [], "reader": true, "why": ["11 concepts in 5 fields (ai, privacy, infra, defi, product)", "1 concrete details", "stage live", "token has no stated role"], "parts": {"depth": 28.1, "concrete": 4.3, "stage": 20, "utility": 0.0, "info": 9.0, "coherence": 0, "candor": 0.0, "-buzz": 0.0, "-hype": 0, "-clone": 0}, "evidence": {"concepts": {"ai": ["llm", "compute"], "privacy": ["encrypt", "tee", "mpc", "private", "privacy"], "infra": ["on-chain"], "defi": ["settlement", "stablecoin"], "product": ["wallet"]}, "concrete": ["className"], "stage": {"live": 4, "testnet": 1, "building": 0, "app_responds": false}, "utility": [], "coherence": null, "twin": null, "twin_similarity": 0.05}, "holders": null, "gt_score": null}
Pillar tokenomics: score 75, fatal -, flags ["4 wallets linked to each other"]
  facts: {"top1_pct": 10.1, "top10_pct": 31.4, "creator_pct": 0.0, "holders": 4338, "insiders": 4, "lp_locked_pct": 100.0, "mint": false, "freeze": false, "tax_pct": 0.0}
DeepSeek judge (cached): {"score": 38, "verdict": "avoid", "confidence": 0.72, "problems": ["No public GitHub and no code activity measured, so the 'privacy layer' and 'FHE/MPC/TEE' narratives are unverifiable marketing", "Only 14 days old with a 376-day-old domain, suggesting the domain predates the project and the token is a fresh launch", "No app (app: false) despite claiming a working stablecoin payment account and privacy layer", "4 insider wallets linked to each other holding part of a 31.7% top-10 concentration", "Liquidity of $96,944 against a $612,927 market cap is thin and could be pulled despite 100% LP lock claim"], "strengths": ["4311 holders is a real distribution base for a 14-day-old token", "LP reported 100% locked, mint and freeze disabled, 0% tax", "Docs exist and are reachable (docs_ok, 3 internal pages OK, HTTPS)", "Creator holds 0% of supply"], "admit_reason": "", "at": 1790664314.5911853}
Code review (cached): none
<project_text>
[site excerpt] stealf Home How it Works FAQ Tokenomics Built on Solana The privacy layer for everyday finance Stealf is built on two layers: a stablecoin payment account to spend like cash anywhere, and a privacy layer on your wallet that keeps your funds yours. Built on best-in-class infrastructure encrypted compute (MPC) private transfers settlement key management (TEE) No on-chain link between them. Spend without revealing your wallet. Trade without revealing your identity. Stablecoin account Your money for everyday life. Tap your card at the counter or send a transfer, on a compliant account wired to the real banking system. Secured by Turnkey. Your private wallet Yours, and only yours. Create it or import it; either way you hold the keys, and your transfers stay private. Powered by Umbra. Frequently Asked Questions Answers to the most common questions about Stealf. If something isn't covered here, reach out directly. Who is Stealf for? Does Stealf require identity verification? What is your current stage? How can I contact the founders? Is Stealf only available on Solana? Can I export my private keys? stealf Stealf is built on two layers: a stablecoin payment account to spend like cash anywhere, and a privacy layer on your wallet that keeps your funds yours. Stealf is a technology company, not a bank. Tokenomics Privacy Policy Terms of Use Security disclosure © 2026 Stealf Corporation. All rights reserved. Contact: [email]
[site summary] stealf Home How it Works FAQ Tokenomics Built on Solana The privacy layer for everyday finance Stealf is built on two layers: a stablecoin payment
</project_text>
```

```
## PFWA (predict FWA) — robinhood:0xa934bA4F59070149d37A93F8A002Af79BAe35563
Status: on the gem finder's watch list. Fetched now: market, github.
Market: cap $278,520, liquidity $64,063, 24h volume $34,183, 24h change -5.5%, age 58 days
Pools: 1, main on uniswap (https://dexscreener.com/robinhood/0x5313a71595178721e1d58fd4cb459e4981aae441), FDV $278,520, 24h txns {"buys": 164, "sells": 122}
Links: website https://www.pfwa.fun/, other sites ["https://www.pfwa.fun/docs", "https://www.pfwa.fun/", "https://credence.pfwa.fun/", "https://credence.pfwa.fun/docs"], github none, socials ["https://x.com/pfwafun", "https://t.me/pfwafun"]
Database: score 0.0, rank None, why site: empty page, categories [], sources ["reference"]
Rules verdict (qualifies): site: empty page
Researcher's floor: passes
Too big: passes
Final score: 0.0
Early gem: False
Overall: {"score": 0, "fatal": "site: empty page", "pending": "", "weakest": "site"}
Pillar github: score None, fatal -, flags ["no public GitHub"]
  facts: {"missing": true}
Pillar site: score 0, fatal empty page, flags []
  facts: {"url": "https://www.pfwa.fun/", "words": 0, "internal_pages": 0, "pages_ok": 0, "docs": false, "docs_ok": false, "app": false, "app_ok": false, "sections": [], "builder": null, "free_host": null, "spa": false, "domain_days": 60, "cites_contract": false, "https": true, "tech_terms": {}, "narratives": [], "excerpt": "", "holders": null, "gt_score": null}
Pillar tokenomics: score 70, fatal -, flags ["only 0% of liquidity locked"]
  facts: {"top1_pct": 4.0, "top10_pct": 22.2, "creator_pct": 1.5, "holders": 1832, "insiders": 0, "lp_locked_pct": 0, "mint": false, "freeze": false, "tax_pct": 0.0}
DeepSeek judge (cached): none
Code review (cached): none
<project_text>

</project_text>
```

```
## GRID (GRID by Virtuals) — robinhood:0x727701F88f09Fed71eb217Bcea88b3F314CF7079
Status: on the gem finder's watch list. Fetched now: market, github.
Market: cap $1,893,116, liquidity $189,300, 24h volume $35,446, 24h change -18.34%, age 75 days
Pools: 1, main on uniswap (https://dexscreener.com/robinhood/0xa0726fae0881ba6041171ab86d5ff34e980e0f27), FDV $1,893,116, 24h txns {"buys": 72, "sells": 42}
Links: website https://www.gridarena.xyz/, other sites ["https://www.gridarena.xyz/", "https://app.gridarena.xyz/"], github none, socials ["https://x.com/grid_arena"]
Database: score 0.0, rank None, why tokenomics: a single wallet holds 29%, categories [], sources ["reference"]
Narratives: ["rwa", "prediction_markets"]
Rules verdict (qualifies): tokenomics: a single wallet holds 29%
Researcher's floor: passes
Too big: passes
Final score: 0.0
Early gem: False
Overall: {"score": 0, "fatal": "tokenomics: a single wallet holds 29%", "pending": "", "weakest": "tokenomics"}
Pillar github: score None, fatal -, flags ["no public GitHub"]
  facts: {"missing": true}
Pillar site: score 38, fatal -, flags []
  facts: {"url": "https://www.gridarena.xyz/", "words": 610, "internal_pages": 1, "pages_ok": 0, "docs": false, "docs_ok": false, "app": true, "app_ok": true, "sections": [], "builder": null, "free_host": null, "spa": false, "domain_days": 170, "cites_contract": false, "https": true, "tech_terms": {"tooling": ["playground"]}, "narratives": ["rwa", "prediction_markets"], "sites_read": 2, "site_score": 39, "description": {"words": 0, "mechanisms": [], "hype": 0}, "description_score": 0, "stage": "testnet", "ai_substance": 4, "verdict": "thin", "original": true, "red_flags": [], "reader": true, "why": ["2 concepts in 2 fields (infra, defi)", "no concrete detail", "stage testnet", "token has no stated role"], "parts": {"depth": 11.8, "concrete": 0.0, "stage": 14, "utility": 0.0, "info": 8.6, "coherence": 0, "candor": 0.0, "-buzz": 0.0, "-hype": 0, "-clone": 0}, "evidence": {"concepts": {"infra": ["onchain"], "defi": ["perp"]}, "concrete": [], "stage": {"live": 0, "testnet": 0, "building": 0, "app_responds": true}, "utility": [], "coherence": null, "twin": null, "twin_similarity": 0.0}, "holders": null, "gt_score": null}
Pillar tokenomics: score 0, fatal a single wallet holds 29%, flags ["only 0% of liquidity locked"]
  facts: {"top1_pct": 29.0, "top10_pct": 65.8, "creator_pct": 0.0, "holders": 1814, "insiders": 0, "lp_locked_pct": 0, "mint": false, "freeze": false, "tax_pct": 0.0}
DeepSeek judge (cached): none
Code review (cached): none
<project_text>
[site excerpt] 01 ENGINE Every market One Grid Enter Grid Move the market Grid is a unified market arena built for people who want to act on what they see. Trade, predict, and play through one continuous interface where every position becomes part of a living field. Prediction Markets turn price movement into four clear ways to express a view. Choose Up or Down, Above or Below, a Price Range, or a Hit Price, then enter with the possible outcomes visible before you commit. Perpetual markets translate leverage into space. Go long above the live price or short below it, adjust your exposure, and read the balance between opportunity and liquidation risk directly through the Grid. Tokenized Stocks bring onchain access to the companies and assets shaping global markets. Live prices and market movement enter the same visual language as every other product, without forcing the experience into a separate world. Playground turns collective conviction into a battlefield. Green and red positions push against one another as the frontier moves, making market pressure visible and transforming every decision into part of a larger contest. The GRID token connects the ecosystem across products, players, and networks. Its utility moves through the arena as a shared mechanism designed to align participation and make one market universe feel like one coherent system. Grid is built without borders. Robinhood Chain, Base, Arbitrum, and Polygon connect through LayerZero so users can reach the arena from multiple n
[site summary] 01 ENGINE Every market One Grid Enter Grid Move the market Grid is a unified market arena built for people who want to act on
</project_text>
```

```
## BV7X (BV7X) — robinhood:0x459476869aF797Eb9061B41fA04dE46A60FBeDBD
Status: on the gem finder's watch list. Fetched now: market.
Market: cap $1,421,495, liquidity $247,479, 24h volume $21,526, 24h change -6.38%, age 12 days
Pools: 1, main on uniswap (https://dexscreener.com/robinhood/0x38973f382a711a0ec853703bb6f667d8e37f7520fa7e295ee7a2b914d7aea025), FDV $1,421,495, 24h txns {"buys": 24, "sells": 36}
Links: website https://bv7x.ai/, other sites ["https://docs.bv7x.ai/", "https://bv7x.ai/"], github none, socials ["https://x.com/BV7X_", "https://t.me/BV7XOfficial"]
Database: score 8.1, rank None, why github: GitHub link without a repository, categories [], sources ["reference"]
Narratives: ["ai_agents", "agent_frameworks", "ai_money", "prediction_markets"]
Rules verdict (qualifies): github: GitHub link without a repository
Researcher's floor: passes
Too big: passes
Final score: 8.1
Early gem: False
Overall: {"score": 0, "fatal": "github: GitHub link without a repository", "pending": "", "weakest": "github"}
Pillar github: score 0, fatal GitHub link without a repository, flags []
  facts: {}
Pillar site: score 73, fatal -, flags []
  facts: {"url": "https://docs.bv7x.ai/", "words": 1036, "internal_pages": 27, "pages_ok": 2, "docs": true, "docs_ok": true, "app": true, "app_ok": true, "sections": ["audit", "roadmap", "tokenomics"], "builder": null, "free_host": null, "spa": false, "domain_days": 237, "cites_contract": false, "https": true, "tech_terms": {"protocol": ["consensus"]}, "narratives": ["ai_agents", "agent_frameworks", "ai_money", "prediction_markets"], "sites_read": 2, "site_score": 87, "description": {"words": 0, "mechanisms": [], "hype": 0}, "description_score": 0, "stage": "live", "ai_substance": 7, "verdict": "promising", "original": true, "red_flags": [], "reader": true, "why": ["10 concepts in 4 fields (ai, privacy, infra, defi)", "1 concrete details", "stage live", "token has no stated role"], "parts": {"depth": 27.5, "concrete": 4.3, "stage": 20, "utility": 0.0, "info": 9.7, "coherence": 0, "candor": 0.0, "-buzz": 0.0, "-hype": 0, "-clone": 0}, "evidence": {"concepts": {"ai": ["llm", "agent", "agents"], "privacy": ["attestation", "private"], "infra": ["consensus", "protocol", "smart contract", "on-chain"], "defi": ["x402"]}, "concrete": ["7X"], "stage": {"live": 2, "testnet": 0, "building": 1, "app_responds": true}, "utility": [], "coherence": null, "twin": null, "twin_similarity": 0.0}, "holders": null, "gt_score": null}
Pillar tokenomics: score 0, fatal a single wallet holds 41%, flags ["only 0% of liquidity locked"]
  facts: {"top1_pct": 40.6, "top10_pct": 85.2, "creator_pct": 0.0, "holders": 405, "insiders": 0, "lp_locked_pct": 0, "mint": false, "freeze": false, "tax_pct": 0.0}
DeepSeek judge (cached): none
Code review (cached): none
<project_text>
[site excerpt] BV-7X Docs ⌘ Ctrl k BV-7X Docs Introduction How It Works The Arena How Predictions Compound Prediction Mining (roadmap) $BV7X Tokenomics (roadmap) Use the Signal Quickstart Read the Forecast Trade the Forecast Performance Examples Reference Agentic Commerce MCP Server A2A Protocol ERC-8004 Identity x402 Payments Token Gate Copy-Trading for Agents Compete in the Arena On-Chain EAS Attestations Smart Contracts Verify a Prediction Powered by GitBook On this page For the complete documentation index, see llms.txt . This page is also available as Markdown . Copy On this page Introduction BV-7X is a protocol that makes intelligence a transferable asset class — a live arena where humans and AI agents compete to forecast the future, settle their calls on prediction markets, and accumulate cryptographically attested track records that are portable, provable, and monetizable without ever revealing the model behind them. The arena solves edge decay by design: forecasters are scored in real time, the consensus re-weights toward whoever is currently right, and fresh agents replace fading ones through rotations the user never sees. A new asset class is emerging, and intelligence itself is finally transferable. Launched originally for Bitcoin in February 2026 — 63%+ walk-forward accuracy across 4,500+ observations, 5,200+ agents competing . Live at bv7x.ai . Who This Is For — Traders who want a live forecast without picking a model, and forecasters (human, firm, or AI) who want to prove edg
[site summary] Copy On this page Introduction BV-7X is a protocol that makes intelligence a transferable asset class — a live arena where humans and AI agents
</project_text>
```

```
## DREGG (Dragon's Egg) — solana:XkeTXo1125vz5H9svJpGiw4JvLbN8VmMu9cmMvspump
Status: on the gem finder's watch list, on the board (rank 1). Fetched now: market.
Market: cap $1,957,061, liquidity $184,530, 24h volume $187,944, 24h change -2.03%, age 94 days
Pools: 1, main on pumpswap (https://dexscreener.com/solana/2xhrhkxfxweupnrzaas6tbagugva6vteyi4npxui8sfu), FDV $1,957,061, 24h txns {"buys": 810, "sells": 1014}
Links: website https://www.dregg.net/, other sites ["https://www.dregg.net/"], github https://github.com/emberian/dregg, socials ["https://x.com/dreggnet"]
Database: score 77.6, rank 7, why -, categories ["Solana Ecosystem", "Pump.fun Ecosystem"], sources ["riferimento", "jupiter", "coingecko-list", "coingecko"]
Narratives: ["zero_knowledge", "fhe_mpc_tee", "formal_verification_security"]
Rules verdict (qualifies): passes
Researcher's floor: passes
Too big: passes
Final score: 77.6
Early gem: False
Overall: {"score": 83.6, "fatal": "", "pending": "", "weakest": "site"}
Pillar github: score 95, fatal -, flags []
  facts: {"repo": "emberian/dregg", "url": "https://github.com/emberian/dregg", "description": "Distributed object-capability authorization with ZK proofs", "commits": "100+", "commits_90d": 6945, "active_days": 13, "active_weeks_12": 9, "span_days": 47, "last_commit_days": 2.1, "authors": 1, "contributors": 7, "code_kb": 251528, "languages": ["Lean", "Rust", "JavaScript"], "tests": true, "ci": true, "readme": true, "license": "AGPL-3.0", "stars": 34, "forks": 8, "trivial_pct": 0, "created_at": "2026-05-20T19:24:11Z", "languages_all": ["Lean", "Rust", "JavaScript", "TypeScript", "Python", "Shell", "HTML", "Solidity", "Go", "C", "CSS", "Typst", "WGSL", "Assembly", "PLpgSQL", "Standard ML", "TLA", "Isabelle", "Makefile", "C++", "Dockerfile", "Scala", "Linker Script", "PowerShell"], "topics": [], "root_dirs": [".ast-grep", ".bin", ".cargo", ".config", ".docs-history-noclaude", ".github", ".spw", "agent-host", "agent-platform", "android-cell", "app-framework", "assets", "attest-check", "attested-dm", "audit", "auditable-fund", "audits", "baseline", "blocklace", "braid-hook", "bridge", "captp", "cell", "cell-crypto", "chain", "circuit", "circuit-prove", "cli", "collective-choice", "commit", "commons-arbiter", "confined-swarm", "constraint-lowering", "coord", "cosmos-lightclient", "cosmos-lock", "cosmos-settlement", "credentials", "crypto-hashrand", "crypto-hermine", "crypto-tanuki", "crypto-traccoon", "crypto-xmvrf", "deco-prove", "demo", "demo-agent", "deos-hermes", "deos-homeserver", "deos-js", "deos-js-core", "deos-js-runtime", "deos-leptos", "deos-matrix", "deos-reflect", "deos-terminal", "deos-view", "deos-web-cells", "deos-zed", "deos-zed-full", "deploy", "dfa", "dfa-federation", "dice", "directory", "discharge-gateway", "discord-bot", "docker", "docs", "dregg-agent", "dregg-an…
Pillar site: score 70, fatal -, flags []
  facts: {"url": "https://www.dregg.net/", "words": 1036, "internal_pages": 11, "pages_ok": 3, "docs": true, "docs_ok": true, "app": false, "app_ok": false, "sections": ["audit"], "builder": null, "free_host": null, "spa": false, "domain_days": 105, "cites_contract": false, "https": true, "tech_terms": {"formal": ["machine-checked"], "zk": ["zero knowledge", "zkvm"], "architecture": ["executor", "kernel", "semantic"], "candor": ["active research", "named limit", "no independent external audit", "not ready for"]}, "narratives": ["zero_knowledge", "fhe_mpc_tee", "formal_verification_security"], "sites_read": 1, "site_score": 65, "description": {"words": 0, "mechanisms": [], "hype": 0}, "description_score": 0, "stage": "building", "ai_substance": 7, "verdict": "promising", "original": true, "red_flags": [], "reader": true, "why": ["11 concepts in 4 fields (ai, privacy, infra, product)", "7 concrete details", "stage building", "token has no stated role", "states its own limits"], "parts": {"depth": 28.1, "concrete": 13.5, "stage": 8, "utility": 0.0, "info": 9.9, "coherence": 0, "candor": 8.0, "-buzz": 0.0, "-hype": 0, "-clone": 0}, "evidence": {"concepts": {"ai": ["gpu", "compute"], "privacy": ["encrypt", "zk", "proof", "mpc", "fhe", "private"], "infra": ["bridge", "program"], "product": ["audit"]}, "concrete": ["symEq", "digEq", "symMemberOf", "allOf", "anyOf", "digFieldEq", "fieldEqField"], "stage": {"live": 0, "testnet": 0, "building": 3, "app_responds": false}, "utility": [], "coherence": null, "twin": null, "twin_similarity": 0.0}, "holders": 2071, "gt_score": 74.64935064935065}
Pillar tokenomics: score 82, fatal -, flags ["807 wallets linked to each other"]
  facts: {"top1_pct": 3.4, "top10_pct": 23.9, "creator_pct": 0.0, "holders": 7636, "insiders": 807, "lp_locked_pct": 100.0, "mint": false, "freeze": false, "tax_pct": 0.0}
DeepSeek judge (cached): {"score": 58, "verdict": "watch", "confidence": 0.7, "problems": ["Token has no visible product: site facts show app=false and app_ok=false, so the 95-scoring GitHub repo (emberian/dregg) is not demonstrably connected to $DREGG", "Site is thin and partly broken: 1036 words, 11 internal pages but only 3 pages_ok, and cites_contract=false", "Tokenomics flag: 807 wallets linked to each other out of 7613 holders, a cluster that could coordinate sells", "Repo is effectively one person: authors=1 despite contributors=7, and only 35 stars/8 forks for 251MB of code", "Docs admit 'no independent external audit' and 'not ready for' production, yet market cap is $2M"], "strengths": ["Serious, sustained code: 6981 commits in 90d, 13 active days, last commit 1 day ago, tests and CI present, AGPL-3.0", "Honest docs language: 'active research', 'named limit', 'no independent external audit', 'not ready for'", "Liquidity is fully locked (lp_locked_pct=100) and mint/freeze disabled with 0% tax", "Holder distribution is not concentrated: top1 3.4%, top10 23.9%, creator 0%", "Domain age (104 days) is consistent with token age (93 days), no old-project/new-domain mismatch"], "admit_reason": "", "at": 1790576032.9545045}
Code review (cached): {"at": 1790613655.6707864, "pushed_at": "2026-09-28T16:11:53Z", "complexity": 92, "innovation": 88, "fork_of": null, "summary": "Formally verified Lean 4 object-capability OS with machine-checked AIR circuits, STARK proofs, and Mina/Pickles SRS integration.", "strengths": ["Machine-checked soundness in Lean 4 with byte-pinned circuit descriptors", "Deep cryptographic engineering: STARK AIR, Pickles accumulator, Vesta/Pallas SRS handling", "Novel capability-as-constructive-knowledge model with verified state transitions"], "concerns": ["Enormous generated SRS/golden files suggest heavy build and maintenance burden", "README is partly LLM-authored and self-referential, complicating independent assessment", "Deployed-size (n=11) circuits only recently pinned; maturity of full pipeline unclear"]}
<project_text>
[site excerpt] Skip to content dregg proof-native semantic computer the idea idea receipts worlds operations network build contents status horizon $DREGG Public proof record 01 / dregg A proof-native semantic computer A computer whose programs are meanings. A parser need not masquerade as a CPU. Nor should a market, a policy, or a world. In dregg, each keeps its natural meaning—and produces a proof that the meaning was respected. The shift State the claim. Find its witness by any sound means. Check the claim itself. See how it thinks Inspect the boundary → Plate 01 A common world, assembled from independently proved meanings. The record now Semantics Lean-authored Private leaves Hiding proofs Composition recursive receipts Network development Named limit No independent external audit. Tier-1 hiding does not yet mean that no trace builder sees the private witness. 02 The inversion Do not simulate first. Say what is true first. Machinic zkVM Prove an instruction trace. Compile the program to one fixed machine. Prove every load, branch, and arithmetic instruction that machine performed. Proof-native dregg Prove a semantic relation. Keep the object in its natural shape. Prove the derivation, optimum, decision, or state transition directly. Machine execution still happens underneath. It may be Rust, a GPU, encrypted arithmetic, or a conventional prover. But that implementation is not the constitution. The relation written in Lean is. This is not conventional “symbolic execution.” Symbolic means 
[site summary] Skip to content dregg proof-native semantic computer the idea idea receipts worlds operations network build contents status horizon $DREGG Public proof record 01 / dregg
</project_text>
```

## DeepSeek's answer

# Gem Finder study: five hand-picked "serious gems" vs DREGG

Read this first: the dossiers are thin in exactly the places that matter. For four of the five projects there is **no public GitHub pillar at all** (`facts: {"missing": true}` or a fatal "GitHub link without a repository"), no code review, and no DeepSeek judge except STEALF. For PFWA the site pillar returned **zero words** and an empty `<project_text>`. So for most of these I can only separate *measured* (market, tokenomics, site-crawl facts) from *claimed* (everything in site text). I flag that explicitly throughout. I do not invent team, audit or contract data that was not fetched.

---

## 1. AUREON (AUREON) — robinhood:0xd293291060334D42E5DBEa6fb854C231AF527777

**1. What it builds.** The only project-authored text in the dossier is one line: "AUREON · the financial intelligence layer for Financial Compass on Robinhood Chain." The site pillar read 1 site, found 0 description words, 0 mechanisms, 0 concrete details, 0 concepts, and returned `verdict: "thin"`, `stage: "unclear"`, `ai_substance: 0`. So: *claimed, unverified* — a "financial intelligence layer" and a "Financial Compass" product; the crawler could not extract any description of either. The dossier does link a public SDK repo (`https://github.com/buildaureon/aureon-sdk`) but the GitHub pillar recorded `missing: true` / flag `"no public GitHub"`, i.e. the repo was never actually read.

**2. Evidence of real work.** Weakest of the five on evidence. No app URL in the site facts, no `app_ok`, no docs flag, no contracts cited (`cites_contract` not even present). The one hard artefact is the SDK URL, and it is unmeasured. Site is flagged `"site built in JavaScript, not readable"` — the crawler got nothing, so "thin" here means *unreadable*, not *proven empty*. No team section, no builder, no socials beyond `x.com/buildaureon`. Age 73 days, single chain (Robinhood), no cross-chain track record in the dossier.

**3. Tokenomics and market.** Cap $105,893; FDV $105,893 (no unlock overhang visible); liquidity $57,125 → **liq/cap = 0.54** (54%), which is high and healthy for a micro-cap. 24h volume $15,137 → **vol/cap = 0.14** (low turnover). 24h change +60.97% on 114 buys / 326 sells — price up while sells outnumber buys ~2.9:1, which is a divergence worth noting (thin book, few large prints). Tokenomics pillar is the strongest part: top1 2.4%, top10 8.1%, creator 1.8%, 2,216 holders, insiders 0, **lp_locked 99.6%**, mint false, freeze false, tax 0%, contracts 13.6%. Top-10 at 8.1% is exceptionally flat — almost no concentration at all.

**4. Narrative.** Cannot be assigned: the site pillar returned `narratives: []` and 0 concepts. "Financial intelligence layer" would nominally sit in AI/DeFi-infra, but there is no text to support even that. **Narrative strength right now: unassessable** — this is a data gap, not a weak narrative.

**5. Red flags.** (a) Site unreadable by the crawler — could be a JS SPA that hides real content, or could be near-empty; the dossier cannot tell. (b) GitHub pillar says "no public GitHub" while a GitHub URL is listed — either the repo is empty/404 or the checker failed; unresolved. (c) +61% on a day with 2.9× more sells than buys. (d) 0 concepts, 0 mechanisms, token has "no stated role" (`why` list). (e) No docs, no app, no team signal.

**6. Rule verdicts.** "Qualifies: passes" and "Researcher's floor: passes" are **right on the numbers** (tokenomics 92, no fatal) but the final score 61 / overall 71 is **inflated by a tokenomics pillar that is doing all the work** while site=50 and github=missing. The verdict "passes" is not *wrong*, it is *unearned*: nothing about the product was verified. The `weakest: "site"` label is correct. The real error is that a missing GitHub pillar is treated as neutral rather than as a hard evidence gap.

---

## 2. STEALF (Stealf) — solana:G5W6LwkLeoj6rZqBP1y3KT8k6Cz6rXGJmMNU3TArXtXw

**1. What it builds.** From site text: "The privacy layer for everyday finance… built on two layers: a stablecoin payment account to spend like cash anywhere, and a privacy layer on your wallet." Named dependencies: **Turnkey** (key management) for the account, **Umbra** for private transfers, with **MPC** and **TEE** cited as infrastructure. That is a concrete, checkable architecture — but the dossier shows **no app** (`app: false`, `app_ok: false`), so the working product is *claimed, unverified*. The site itself says "Stealf is a technology company, not a bank."

**2. Evidence of real work.** Site pillar is the best of the four readable ones: 231 words, 3 internal pages all OK, `docs: true`, `docs_ok: true`, HTTPS, `stage: "live"`, `ai_substance: 7`, `verdict: "promising"`, `original: true`, 11 concepts across 5 fields (ai, privacy, infra, defi, product). But: **no public GitHub** (`missing: true`), no code review, no app. Domain age 378 days vs token age 14 days — the domain predates the token by ~364 days. Team section exists on site but no names/links captured in facts. Single chain (Solana), 14-day track record.

**3. Tokenomics and market.** Cap $656,866; FDV $656,866; liquidity $200,297 → **liq/cap = 0.30** (30%, solid). Volume $82,857 → **vol/cap = 0.13**. 195 buys / 189 sells, balanced. 4,338 holders in 14 days is genuinely fast distribution. But: top1 10.1%, top10 31.4%, **insiders 4**, flag `"4 wallets linked to each other"`, creator 0%, lp_locked 100%, mint/freeze false, tax 0%. Note the DeepSeek judge quotes liquidity $96,944 / cap $612,927 — **inconsistent with the market pillar's $200,297 / $656,866**; one of the two snapshots is stale. That discrepancy itself is a data-quality flag.

**4. Narrative.** Fits **privacy**, **fhe_mpc_tee**, **stablecoin_payments** — all three are live, fundable narratives (privacy rails + stablecoin spend + TEE/MPC). Strength: the narrative is hot and the site names real primitives (Turnkey, Umbra). Weakness: with no code and no app, it is *narrative without proof* — exactly the profile DeepSeek flagged as "unverifiable marketing."

**5. Red flags.** (a) No public GitHub, no app, yet claims a working privacy layer and payment account. (b) 4 linked insider wallets inside a 31.4% top-10. (c) Domain 378 days old vs 14-day token — possible recycled/aged domain. (d) Liquidity figure contradicts between pillars. (e) Site says "© 2026 Stealf Corporation" — a corporation claim with no registration evidence in the dossier. (f) Only 1 concrete detail extracted (`className`, which is noise, not a product detail).

**6. Rule verdicts.** The rules verdict ("qualifies: DeepSeek: No public GitHub… unverifiable marketing") is **right to be sceptical but wrong as a hard exclusion** — it kills a project whose site is the second-strongest here purely for missing code. DeepSeek's own judge (score 38, "avoid", confidence 0.72) is **too harsh**: it penalises the 14-day age and the domain mismatch, but 4,338 holders, 100% LP lock, 0% creator and a real docs site are not "avoid" material. The correct verdict is **watch/pending-code**, not avoid and not pass. The `weakest: "site"` label is **wrong** — the site is the strongest pillar; the weakest is github.

---

## 3. PFWA (predict FWA) — robinhood:0xa934bA4F59070149d37A93F8A002Af79BAe35563

**1. What it builds.** **Unknown from the dossier.** The site pillar returned `words: 0`, `internal_pages: 0`, `pages_ok: 0`, `docs: false`, `app: false`, `excerpt: ""`, and the `<project_text>` block is empty. The name and the linked subdomain `credence.pfwa.fun` (plus `credence.pfwa.fun/docs`) suggest a prediction/credence product, but that is my inference, not dossier fact. Everything about the product is **missing data**, not a claim I can even quote.

**2. Evidence of real work.** None measurable. No GitHub, no docs read, no app, no team, no contracts. The only signals are structural: domain age 60 days, token age 58 days (consistent), and two subdomains that were listed but not successfully crawled (`pages_ok: 0`). This is the single biggest data hole in the set.

**3. Tokenomics and market.** Cap $278,520; FDV $278,520; liquidity $64,063 → **liq/cap = 0.23** (23%, acceptable). Volume $34,183 → **vol/cap = 0.12**. 164 buys / 122 sells, mildly buy-skewed, price −5.5%. 1,832 holders. top1 4.0%, top10 22.2%, creator 1.5%, insiders 0, mint/freeze false, tax 0% — but **lp_locked_pct = 0**, flagged `"only 0% of liquidity locked"`. That is the one hard negative in the tokenomics.

**4. Narrative.** Cannot be assigned: `narratives: []`, 0 concepts. If the name is accurate it would be a prediction-market play, but the dossier gives no text to confirm it. **Unassessable.**

**5. Red flags.** (a) Site returned literally zero words — either a hard JS wall or a placeholder. (b) 0% LP locked. (c) No GitHub, no docs, no app. (d) The `credence.*` subdomains exist but were not read — untested. (e) The fatal "site: empty page" is the only reason it scored 0, which means the score is driven by a *crawl failure*, not by evidence of fraud.

**6. Rule verdicts.** The fatal `"site: empty page"` is **too blunt**: an empty crawl is not proof of an empty project, and it zeroes a token with 1,832 holders, 22.2% top-10 and 23% liq/cap. The correct handling is **"pending: site unreadable — retry with JS rendering / check credence.pfwa.fun/docs"**, not fatal. Conversely, the 0% LP lock *should* be a real penalty that the current fatal rule masks. So: fatal rule **wrong**, LP-lock rule **too lenient**.

---

## 4. GRID (GRID by Virtuals) — robinhood:0x727701F88f09Fed71eb217Bcea88b3F314CF7079

**1. What it builds.** From site text: "a unified market arena" combining **prediction markets** (Up/Down, Above/Below, Price Range, Hit Price), **perpetual markets** (long/short with liquidation), **tokenized stocks** ("onchain access to the companies and assets shaping global markets"), and a **Playground** where "green and red positions push against one another." Cross-chain via **LayerZero** across Robinhood Chain, Base, Arbitrum, Polygon. The GRID token "connects the ecosystem across products." The site pillar read 610 words, 1 internal page, `app: true`, `app_ok: true`, `stage: "testnet"`, `verdict: "thin"`, `ai_substance: 4`, only 2 concepts (infra: onchain; defi: perp), 0 concrete details.

**2. Evidence of real work.** A live app that responds (`app_ok: true`, `app_responds: true`) is the strongest product signal of the five. But: **no docs** (`docs: false`), only 1 internal page, 0 concrete details, no GitHub, no team, no contracts cited. Domain 170 days vs token 75 days. The "Virtuals" branding in the name is not corroborated by any dossier field (no Virtuals launchpad source, no agent record).

**3. Tokenomics and market.** Cap $1,893,116 (largest of the five); FDV equal; liquidity $189,300 → **liq/cap = 0.10** (10%, thin). Volume $35,446 → **vol/cap = 0.019** (very low turnover). 72 buys / 42 sells, price −18.34%. 1,814 holders. **top1 29.0%, top10 65.8%**, creator 0%, insiders 0, **lp_locked 0%**, mint/freeze false, tax 0%. This is the worst distribution in the set by a wide margin.

**4. Narrative.** Fits **prediction_markets** and **rwa** (tokenized stocks). Both are strong narratives right now, and the combination with perps is coherent. But the narrative is *undermined by the cap table*: a 29% single wallet on a $1.9M cap means the "arena" is one whale's float. Narrative strength: **strong theme, weak credibility**.

**5. Red flags.** (a) top1 29% + top10 65.8% — the single most disqualifying number in the whole study. (b) 0% LP locked. (c) No docs, no GitHub, 0 concrete details, `verdict: "thin"`. (d) −18.34% on the day. (e) "Virtuals" in the name with no launchpad evidence. (f) 10% liq/cap on the largest cap = thinnest relative book.

**6. Rule verdicts.** The fatal `"tokenomics: a single wallet holds 29%"` is **right and should stay** — this is exactly the kind of concentration that must exclude. The site pillar's `verdict: "thin"` is also fair. The only correction: the fatal should be **more specific** — distinguish a 29% *contract* (pool/vesting/launchpad) from a 29% *EOA*. The dossier does not tell us which, and that distinction is the difference between a red flag and a rug. So: verdict **right, but under-specified**.

---

## 5. BV7X (BV7X) — robinhood:0x459476869aF797Eb9061B41fA04dE46A60FBeDBD

**1. What it builds.** From docs text: "BV-7X is a protocol that makes intelligence a transferable asset class — a live arena where humans and AI agents compete to forecast the future, settle their calls on prediction markets, and accumulate cryptographically attested track records." Named components: **MCP Server**, **A2A Protocol**, **ERC-8004 Identity**, **x402 Payments**, **EAS Attestations**, copy-trading for agents. It claims "Launched originally for Bitcoin in February 2026 — 63%+ walk-forward accuracy across 4,500+ observations, 5,200+ agents competing. Live at bv7x.ai." Those accuracy/agent numbers are **claimed, unverified** — no dossier field corroborates them.

**2. Evidence of real work.** Site pillar is the strongest of the five: 1,036 words, 27 internal pages, `docs: true`, `docs_ok: true`, `app: true`, `app_ok: true`, `stage: "live"`, `ai_substance: 7`, `verdict: "promising"`, sections include **audit, roadmap, tokenomics**, 10 concepts across 4 fields (ai, privacy, infra, defi), tech terms include "consensus". But the GitHub pillar is **fatal: "GitHub link without a repository"** — a GitHub URL is listed but resolves to nothing. No code review. Domain 237 days vs token 12 days (large mismatch). Only 2 of 27 internal pages were OK (`pages_ok: 2`).

**3. Tokenomics and market.** Cap $1,421,495; FDV equal; liquidity $247,479 → **liq/cap = 0.17** (17%, decent). Volume $21,526 → **vol/cap = 0.015** (lowest turnover in the set). 24 buys / 36 sells, price −6.38%. **Only 405 holders** — by far the fewest. **top1 40.6%, top10 85.2%**, creator 0%, insiders 0, **lp_locked 0%**, mint/freeze false, tax 0%. A $1.4M cap on 405 holders with 85% in ten wallets is a very fragile structure.

**4. Narrative.** Fits **ai_agents**, **agent_frameworks**, **ai_money**, **prediction_markets** — arguably the hottest cluster in the catalogue (agent payments via x402, agent identity via ERC-8004, prediction arena). Narrative strength: **very strong theme**. Credibility: the docs are rich but the code is absent and the cap table is extreme.

**5. Red flags.** (a) GitHub link with no repository — a listed-but-dead repo is worse than no repo, because it implies a claim of code. (b) top1 40.6%, top10 85.2% on 405 holders. (c) 0% LP locked. (d) 12-day token vs 237-day domain. (e) Unverified performance claims (63% accuracy, 5,200 agents). (f) Only 2/27 pages OK — docs may be partly broken. (g) Lowest volume/cap in the set.

**6. Rule verdicts.** The fatal `"github: GitHub link without a repository"` is **right to fire** — a dead repo link is a real integrity signal, not a crawl artefact. The site pillar's 73/87 "promising" is **too generous** given 405 holders and 85% top-10; the site score should not be able to carry a token whose distribution is this bad. So: GitHub fatal **right**, site weighting **wrong** (over-weights docs, under-weights holders/concentration).

---

## 6. DREGG (benchmark) — solana:XkeTXo1125vz5H9svJpGiw4JvLbN8VmMu9cmMvspump

**1. What it builds.** From site text: "A proof-native semantic computer… A computer whose programs are meanings… each keeps its natural meaning—and produces a proof that the meaning was respected." It contrasts "Machinic zkVM" (prove an instruction trace) with "Proof-native dregg" (prove a semantic relation written in Lean). Code review summary: "Formally verified Lean 4 object-capability OS with machine-checked AIR circuits, STARK proofs, and Mina/Pickles SRS integration." This is the most technically specific project in the entire study.

**2. Evidence of real work.** Overwhelming and *measured*: repo `emberian/dregg`, 100+ commits, **6,945 commits in 90d**, 13 active days, 9 of 12 active weeks, last commit 2.1 days ago, **7 contributors** (1 primary author), 251,528 KB of code, languages Lean/Rust/JS + 20 more, **tests: true, CI: true, license AGPL-3.0**, 34 stars, 8 forks, created 2026-05-20. Code review: complexity 92, innovation 88, not a fork. Site: 1,036 words, 11 internal pages (3 OK), docs OK, sections include **audit**, candor terms: "active research", "named limit", "no independent external audit", "not ready for". Domain 105 days vs token 94 days — consistent.

**3. Tokenomics and market.** Cap $1,957,061; FDV equal; liquidity $184,530 → **liq/cap = 0.094** (9.4%, the thinnest ratio in the set). Volume $187,944 → **vol/cap = 0.096** (highest turnover). 810 buys / 1,014 sells. 7,636 holders (most in the set). top1 3.4%, top10 23.9%, creator 0%, **insiders 807**, flag `"807 wallets linked to each other"`, lp_locked 100%, mint/freeze false, tax 0%. The 807-wallet cluster is the one serious tokenomics concern.

**4. Narrative.** **zero_knowledge**, **fhe_mpc_tee**, **formal_verification_security** — a deep-tech cluster, not a hype cluster. Strength: the narrative is *backed by code*, which is rare. Weakness: formal verification is a narrow audience; the token's role is unstated ("token has no stated role").

**5. Red flags.** (a) 807 linked insider wallets. (b) `app: false` — the 95-scoring repo is not demonstrably wired to the token. (c) Only 3/11 pages OK; `cites_contract: false`. (d) Effectively one author despite 7 contributors. (e) Docs admit "no independent external audit" and "not ready for" production. (f) 9.4% liq/cap is thin.

**6. Rule verdicts.** "Qualifies: passes" is **right** — this is the correct benchmark. The DeepSeek judge's "watch" (58) is **too harsh**: it penalises the token-repo link and the thin site, but the code evidence (6,945 commits/90d, tests, CI, Lean proofs) is exactly what the other four lack. The correct lesson: **code evidence should dominate the score**, and DREGG's 83.6 overall vs STEALF's 68.5 is the right ordering.

---

## 7. Common profile of a "serious gem" — measurable signals

Across the five + DREGG, the shared traits that separate them from memecoins:

**Must-have signals (all five hand-picked projects have at least 3 of these; DREGG has all):**

| Signal | Field to read | Threshold |
|---|---|---|
| Real product surface | `site.app_ok` OR `site.docs_ok` | at least one `true` |
| Docs depth | `site.words`, `site.internal_pages` | words ≥ 600 AND internal_pages ≥ 3 |
| Concrete technical detail | `site.evidence.concrete` length | ≥ 3 items (DREGG 7, BV7X 1, STEALF 1, GRID 0, AUREON 0) |
| Named mechanisms | `site.description.mechanisms` | ≥ 1 non-generic mechanism |
| Live/testnet stage | `site.stage` | in {live, testnet, building} — not "unclear" |
| Holder base | `tokenomics.holders` | ≥ 1,500 (all five: 405–4,338; DREGG 7,636) |
| Top-10 concentration | `tokenomics.top10_pct` | ≤ 35% (AUREON 8.1, PFWA 22.2, DREGG 23.9, STEALF 31.4 pass; GRID 65.8, BV7X 85.2 fail) |
| Liquidity ratio | `liquidity / cap` | ≥ 0.10 (all five: 0.10–0.54; DREGG 0.094 borderline) |
| LP lock | `tokenomics.lp_locked_pct` | ≥ 90% (AUREON 99.6, STEALF 100, DREGG 100 pass; PFWA/GRID/BV7X 0 fail) |
| Mint/freeze disabled | `tokenomics.mint`, `freeze` | both false |
| Tax | `tokenomics.tax_pct` | ≤ 5% (all 0%) |
| Domain/token age consistency | `site.domain_days` vs `market.age` | ratio ≤ 3× (DREGG 1.1, PFWA 1.03, AUREON n/a, STEALF 27× fail, BV7X 20× fail, GRID 2.3× pass) |

**Bonus signals (raise score, not required):**
- Public GitHub with `commits_90d ≥ 100`, `tests: true`, `ci: true`, `license` present (DREGG only).
- `site.sections` contains `audit` or `roadmap` (BV7X, DREGG).
- `site.tech_terms.candor` non-empty — self-stated limits (DREGG only: "active research", "named limit", "no independent external audit").
- Cross-chain presence (`site` mentions ≥ 2 chains) — GRID via LayerZero.
- `tokenomics.insiders == 0` (AUREON, PFWA, GRID, BV7X; STEALF 4, DREGG 807 fail).
- `site.original: true` and `twin_similarity < 0.2` (all pass; STEALF 0.05).

**Anti-signals (present in the failures):**
- `top1_pct > 25%` (GRID 29, BV7X 40.6).
- `lp_locked_pct == 0` (PFWA, GRID, BV7X).
- `holders < 500` (BV7X 405).
- `site.words == 0` (PFWA, AUREON effectively).
- `github` fatal "link without repository" (BV7X).

---

## 8. Current checks that are too strict or measure the wrong thing

1. **"No public GitHub" as a soft flag / missing pillar.** Wrong. It should be a *tiered* signal: `github.missing == true` → cap the overall score at 65 and set `pending: "code unverified"`, but do **not** fatal. Only `github.fatal == "link without repository"` (BV7X) should fatal, because that is an active false claim. *Change:* add field `github_status ∈ {verified, missing, dead_link}`; `dead_link` → fatal; `missing` → −15 score, no fatal.

2. **"site: empty page" as fatal.** Too strict. PFWA's zero-word crawl is a *crawl failure*, not evidence of an empty project. *Change:* if `site.words == 0` AND `site.pages_ok == 0` AND the project has ≥ 1,000 holders and `liq/cap ≥ 0.15`, set `pending: "site unreadable — retry JS render"` and re-crawl `credence.pfwa.fun/docs` before any verdict. Fatal only if a second crawl also returns 0 words.

3. **Site score over-weighting docs, under-weighting distribution.** BV7X site 73/87 "promising" on 405 holders / 85% top-10 is wrong. *Change:* make `tokenomics.top10_pct > 50%` a hard cap of overall score at 40 regardless of site score. GRID (65.8%) and BV7X (85.2%) both fall under this.

4. **`lp_locked_pct == 0` is under-penalised.** PFWA/GRID/BV7X all have 0% locked LP but only PFWA/GRID/BV7X get fatal for *other* reasons. *Change:* `lp_locked_pct < 50` → −20 score and a mandatory `red_flag: "unlocked LP"`; `< 10` → fatal unless `liquidity > $150,000` (a large pool is harder to pull).

5. **Age penalty is misapplied.** STEALF (14 days) and BV7X (12 days) are penalised for youth, but DREGG was 94 days and passed. *Change:* age should not be a standalone penalty; instead use `domain_days / market.age` ratio. Ratio > 10× (STEALF 27×, BV7X 20×) → flag "aged domain, fresh token"; ratio ≤ 3× → bonus.

6. **"token has no stated role" appears for AUREON, STEALF, GRID, BV7X, DREGG — i.e. for everything.** It is measuring the crawler's failure to find a tokenomics page, not the token's utility. *Change:* only apply this penalty if `site.sections` contains `tokenomics` AND `utility` is still empty. Otherwise drop it.

---

## 9. What must still exclude a project

Loosening the above must not admit memecoins, copies or rugs. Keep as **hard exclusions**:

- `tokenomics.top1_pct > 25%` AND the wallet is not identified as a pool/vesting/launchpad contract (GRID 29%, BV7X 40.6%).
- `tokenomics.top10_pct > 50%` (GRID 65.8%, BV7X 85.2%).
- `tokenomics.lp_locked_pct < 10` AND `liquidity < $150,000` (PFWA, GRID, BV7X).
- `github.fatal == "link without repository"` (BV7X) — a false code claim.
- `site.twin_similarity > 0.6` OR `site.original == false` — copy detection.
- `site.red_flags` non-empty with any of: "guaranteed returns", "risk-free", "presale", "airdrop farming".
- `market.volume_24h < $5,000` AND `holders < 300` — dead or fake-liquidity tokens.
- `site.free_host == true` AND `site.domain_days < 30` — throwaway deployment.
- `tokenomics.mint == true` OR `freeze == true` — mint/freeze authority live.
- `tokenomics.tax_pct > 10` — extractive tax.

These are the checks that would have excluded GRID and BV7X on structure alone, while letting AUREON, STEALF, PFWA and DREGG through to a *pending* state for code verification.

---

## 10. Search strategy

**Narratives to hunt (from the five + DREGG):** privacy rails (STEALF), FHE/MPC/TEE (STEALF, DREGG), stablecoin payments (STEALF), prediction markets (PFWA, GRID, BV7X), RWA/tokenized stocks (GRID), AI agents / agent frameworks / agent payments (BV7X), zero-knowledge / formal verification (DREGG), DeFi infra (AUREON).

**Keywords / text patterns to match in docs and site text:**
- `"MCP server"`, `"A2A protocol"`, `"ERC-8004"`, `"x402"`, `"EAS attestation"` → agent-payment cluster (BV7X).
- `"MPC"`, `"TEE"`, `"FHE"`, `"private transfers"`, `"key management"` → privacy cluster (STEALF, DREGG).
- `"zkVM"`, `"STARK"`, `"Lean 4"`, `"machine-checked"`, `"formal verification"` → deep-tech cluster (DREGG).
- `"prediction market"`, `"Up/Down"`, `"Above/Below"`, `"price range"`, `"hit price"` → prediction cluster (GRID, BV7X, PFWA).
- `"tokenized stocks"`, `"onchain access to companies"` → RWA cluster (GRID).
- `"LayerZero"`, `"cross-chain"`, `"multi-chain"` → infra cluster (GRID).
- `"stablecoin account"`, `"spend like cash"`, `"compliant account"` → payments cluster (STEALF).

**Launchpads / chains / sources to prioritise:**
- Chains: **Robinhood Chain** (4 of 5 projects), **Solana** (STEALF, DREGG), plus Base/Arbitrum/Polygon as GRID's expansion targets.
- Launchpads: **Virtuals** (GRID's name), **pump.fun** (DREGG), **Meteora** (STEALF), **Uniswap on Robinhood** (AUREON, PFWA, GRID, BV7X).
- Sources: **GitHub** (search for repos with `tests: true`, `ci: true`, `commits_90d > 100`, languages Lean/Rust), **DexScreener** (new pools on robinhood/solana with `liq/cap > 0.15`), **Jupiter/CoinGecko** (Solana listings), **project docs subdomains** (`docs.*`, `credence.*`).

**Concrete search queries to code:**
1. `site:github.com ("Lean 4" OR "zkVM" OR "STARK") ("object-capability" OR "formal verification")` → DREGG-like.
2. `("MCP server" OR "x402" OR "ERC-8004") ("agent" OR "prediction")` → BV7X-like.
3. `("MPC" OR "TEE" OR "FHE") ("stablecoin" OR "payment account" OR "private transfers")` → STEALF-like.
4. `("prediction market" OR "perpetual") ("tokenized stocks" OR "RWA") ("LayerZero" OR "cross-chain")` → GRID-like.
5. DexScreener filter: `chain in {robinhood, solana}` AND `liquidity/cap ≥ 0.15` AND `holders ≥ 1500` AND `top10 ≤ 35%` AND `lp_locked ≥ 90%`.
6. GitHub filter: `commits_90d ≥ 100` AND `tests == true` AND `ci == true` AND `license != null` AND `created_at within 180 days`.

**Pattern to prefer:** projects whose **site text names specific third-party primitives** (Turnkey, Umbra, LayerZero, Mina/Pickles, ERC-8004) — that is a stronger signal than generic "AI/DeFi" language, because it is falsifiable. STEALF, GR
