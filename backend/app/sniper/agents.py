"""Multi-agent token discovery system.

Three parallel agents feed the sniper candidate archive continuously:

  QuickScanAgent   — DEXScreener profiles + Gecko new-pools every 2 min
  MarketHarvestAgent — full market scan + auto-import every 45 min
  PumpFunAgent     — Pump.fun trending Solana meme coins every 5 min

All agents share the sniper SSE stream so the console shows live activity.
Agent status (name, last_run, last_count, error) is exposed via get_status().
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from .scanner import _passes_primary, fetch_pumpfun_trending

logger = logging.getLogger(__name__)

# ── shared status registry ────────────────────────────────────────────────────

@dataclass
class AgentStatus:
    name: str
    label: str
    interval_s: int
    last_run: float = 0.0
    last_count: int = 0
    running: bool = False
    error: Optional[str] = None

_registry: dict[str, AgentStatus] = {}


def get_status() -> list[dict]:
    return [
        {
            "name": s.name,
            "label": s.label,
            "interval_s": s.interval_s,
            "last_run": s.last_run,
            "last_count": s.last_count,
            "running": s.running,
            "error": s.error,
        }
        for s in _registry.values()
    ]


# ── helpers ───────────────────────────────────────────────────────────────────

def _add_to_sniper(token, score_items=None) -> bool:
    """Add a RawToken directly to the sniper candidate archive. Returns True if new."""
    from .cache import _store as sniper_store, Candidate

    existing = sniper_store.get(token.token_address)
    c = Candidate(
        chain_id=token.chain_id,
        token_address=token.token_address,
        pair_address=token.pair_address,
        symbol=token.symbol,
        name=token.name,
        dex_id=token.dex_id,
        pair_url=token.pair_url,
        price_usd=token.price_usd,
        market_cap=token.market_cap,
        liquidity_usd=token.liquidity_usd,
        volume_h24=token.volume_h24,
        volume_h1=token.volume_h1,
        age_hours=token.age_hours,
        buy_sell_ratio=token.buy_sell_ratio,
        website=getattr(token, "website", None),
        socials=getattr(token, "socials", []),
        score_total=0,
        score_max=13,
        score_items=score_items or [],
        first_seen=existing.first_seen if existing else time.time(),
        last_updated=time.time(),
    )
    sniper_store[token.token_address] = c
    return existing is None


def _broadcast_sniper(event: dict) -> None:
    from .cache import _broadcast
    _broadcast(event)


# ── PumpFunAgent ──────────────────────────────────────────────────────────────

async def _pump_fun_loop() -> None:
    """Fetch Pump.fun trending Solana meme coins every 5 min and add those
    passing primary filter to the sniper archive."""
    name = "pump_fun"
    status = AgentStatus(name=name, label="Pump.fun Monitor", interval_s=300)
    _registry[name] = status

    while True:
        await asyncio.sleep(status.interval_s)
        status.running = True
        status.error = None
        _broadcast_sniper({"type": "agent_start", "agent": name, "label": status.label})
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                tokens = await fetch_pumpfun_trending(client)

            from .cache import _save_candidates
            added = 0
            for token in tokens:
                ok, reason = _passes_primary(token)
                if not ok:
                    continue
                if _add_to_sniper(token):
                    added += 1
                    _broadcast_sniper({
                        "type": "result",
                        "symbol": token.symbol,
                        "chain": token.chain_id,
                        "address": token.token_address,
                        "score": 0,
                        "score_max": 13,
                        "passed": True,
                        "reason": None,
                        "agent": name,
                    })

            if added:
                _save_candidates()

            status.last_run = time.time()
            status.last_count = added
            _broadcast_sniper({
                "type": "agent_done",
                "agent": name,
                "label": status.label,
                "fetched": len(tokens),
                "added": added,
            })
            logger.info("PumpFunAgent: %d/%d tokens added", added, len(tokens))

        except Exception as exc:
            status.error = str(exc)
            logger.warning("PumpFunAgent error: %s", exc)
            _broadcast_sniper({"type": "agent_error", "agent": name, "message": str(exc)})
        finally:
            status.running = False


# ── MarketHarvestAgent ────────────────────────────────────────────────────────

async def _market_harvest_loop() -> None:
    """Run a full market scan + auto-import every 45 min."""
    name = "market_harvest"
    status = AgentStatus(name=name, label="Market Harvest", interval_s=2700)
    _registry[name] = status

    while True:
        await asyncio.sleep(status.interval_s)
        status.running = True
        status.error = None
        _broadcast_sniper({"type": "agent_start", "agent": name, "label": status.label})
        try:
            from .market import run_market_scan, run_import
            await run_market_scan()
            added = await run_import()
            status.last_run = time.time()
            status.last_count = added
            _broadcast_sniper({
                "type": "agent_done",
                "agent": name,
                "label": status.label,
                "added": added,
            })
            logger.info("MarketHarvestAgent: %d new candidates", added)
        except Exception as exc:
            status.error = str(exc)
            logger.warning("MarketHarvestAgent error: %s", exc)
            _broadcast_sniper({"type": "agent_error", "agent": name, "message": str(exc)})
        finally:
            status.running = False


# ── coordinator ───────────────────────────────────────────────────────────────

async def start_all() -> list[asyncio.Task]:
    """Start all agents as background tasks. Call from app lifespan."""
    tasks = [
        asyncio.create_task(_pump_fun_loop(), name="agent:pump_fun"),
        asyncio.create_task(_market_harvest_loop(), name="agent:market_harvest"),
    ]
    logger.info("Started %d discovery agents", len(tasks))
    return tasks
