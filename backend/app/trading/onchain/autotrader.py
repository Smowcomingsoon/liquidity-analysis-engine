"""The system, trading the Robinhood Chain wallet on its own.

Lorenzo: "voglio semplicemente che con un tasto io attivo la strategia e che
faccio operazioni sul wallet disponibile [...] ho depositato una cifra sul
wallet di Robinhood, quindi voglio che automaticamente la strategia faccia i
suoi ingressi, le sue uscite e che utilizzi sempre la totalita' del saldo
[...] uno seleziona il 100% del saldo, il 50, il 25 [...] devi considerare
sempre le fee, in modo tale che le operazioni vadano sempre in porto".
His answers when asked: the balance is USDG; one token at a time; no limit
other than the chosen share; simulated first.

- mode: "off", "sim" (every decision and a real quote, nothing sent, a
  virtual USDG balance) or "real";
- pct: the share of the USDG balance each entry spends (10/25/50/100);
- the signals are the system's (system.py) on the Meme Sniper's Robinhood
  alerts: a buy the system made within FRESH_S, while flat, opens the
  position; the system leaving that token (sell, 48h, rug) closes it.
  Turning the switch off stops new entries only: an open position is still
  closed by its exit, never left behind with no one watching it;
- sizing: pct of the USDG balance, one base unit less so rounding never
  asks for more than there is. The pool fee is inside the amount; gas is
  paid in ETH, so an entry is skipped (and says why) if the wallet has not
  enough ETH for approve + swap; min out is the live quote minus SLIPPAGE;
- real orders go through execute.execute_order (ledger, idempotency, the
  server's onchain_execution_enabled switch) with the risk guards off, as
  chosen; only Uniswap v3 routes can be signed today, so a token reachable
  only through an aggregator is skipped in real mode and says so.
"""
from __future__ import annotations

import json
import math
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.trading.onchain import alert_track, gem_track, quant_runtime
from app.trading.onchain import slippage as slip_est
from app.trading.onchain.storage import data_file
from app.atomic_write import write_text_atomic

logger = logging.getLogger(__name__)

_STORE_FILE = data_file("onchain_autotrader.json")
CHAIN = "robinhood"
USDG = "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168"
USDG_DECIMALS = 6
MODES = ("off", "sim", "real")
PCTS = (10, 25, 50, 100)
FRESH_S = 15 * 60          # a buy older than this is not chased
SLIPPAGE = 0.05            # memes move: tight limits make orders fail
GAS_UNITS = 2 * (300_000 + 80_000)   # cash swap + trade swap, each with a possible approve
CASH_SLIPPAGE = 0.005      # USDC <-> USDG: two dollars, a tight floor is enough
MIN_CASH_POOL_USD = 20_000
_meta: dict = {"usdc": None, "usdc_at": 0.0}
KEEP_TRADES = 60
SIM_START_USDG = 10_000.0  # virtual balance when the wallet holds no USDG (Lorenzo, 30/09: 10.000)
SIM_CASH_MAX = 10_000_000.0  # a typo guard on the simulated balance, not a limit on real money

_state: dict[str, dict] = {}


def _load() -> None:
    try:
        raw = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if isinstance(raw, dict):
        _state.update({k: v for k, v in raw.items() if isinstance(v, dict)})


def _save() -> None:
    try:
        write_text_atomic(_STORE_FILE, json.dumps(_state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def reset_all_sim(start: float = SIM_START_USDG) -> int:
    """Every account's simulated book back to `start` in cash, no open trade
    and no simulated trade in the log (Lorenzo, 30/09: "reset completo di
    tutti gli account e far si' che l'importo sia 10.000"). Real is untouched."""
    n = 0
    for u in _state.values():
        u.update(sim_usdg=round(start, 6), sim_start=round(start, 6), paper=[], trend=[])
        if u.get("position") and u["position"].get("mode") == "sim":
            u["position"] = None
        u["trades"] = [t for t in u.get("trades") or [] if t.get("mode") != "sim"]
        n += 1
    _save()
    return n


def reset_for_tests() -> None:
    _state.clear()
    _meta.update(usdc=None, usdc_at=0.0)


def _user(uid: str) -> dict:
    u = _state.setdefault(uid, {"mode": "off", "pct": 100, "position": None, "sim_usdg": None,
                                "sim_start": None, "trades": [], "note": ""})
    u.setdefault("paper", [])
    u.setdefault("trend", [])
    return u


def settings(uid: str) -> dict:
    u = _user(uid)
    return {"mode": u["mode"], "pct": u["pct"]}


def configure(uid: str, mode: str | None = None, pct: int | None = None, usdg_now: float | None = None,
              sim_cash: float | None = None) -> dict:
    """Switch mode or share. Entering "sim" starts the virtual balance from
    the wallet's real USDG (or SIM_START_USDG if it has none).

    sim_cash sets the simulated free cash (Lorenzo, 30/09: "voglio poter
    modificare il saldo a mio piacimento"). Open paper trades stay; the
    start moves by the same amount, like a deposit or a withdrawal, so the
    return since start is not faked by the edit."""
    u = _user(uid)
    if pct is not None:
        if pct not in PCTS:
            raise ValueError(f"pct must be one of {PCTS}")
        u["pct"] = pct
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if mode == "sim" and u["mode"] != "sim":
            start = usdg_now if usdg_now and usdg_now > 0 else SIM_START_USDG
            u.update(sim_usdg=round(start, 6), sim_start=round(start, 6), paper=[])
            if u["position"] and u["position"].get("mode") == "sim":
                u["position"] = None
        u["mode"] = mode
        u["note"] = ""
    if sim_cash is not None:
        if not math.isfinite(sim_cash) or not 0 <= sim_cash <= SIM_CASH_MAX:
            raise ValueError(f"balance must be between 0 and {SIM_CASH_MAX:,.0f}")
        cash = round(sim_cash, 6)
        if u["sim_start"]:
            start = u["sim_start"] + cash - (u["sim_usdg"] or 0)
            open_value = sum(p["stake"] * (1 + p.get("ret", 0.0)) for p in u["paper"]) + _trend_open(u)
            u["sim_start"] = round(start if start > 0 else cash + open_value, 6) or None
        else:
            u["sim_start"] = cash or None
        u["sim_usdg"] = cash
    _save()
    return view(uid)


def _price(alert: dict | None) -> float | None:
    last = (alert or {}).get("last") or {}
    return last.get("price") or (alert or {}).get("entry")


def view(uid: str) -> dict:
    u = _user(uid)
    reviewed = quant_runtime.approved()
    pos = u["position"]
    pos_view = None
    if pos:
        a = alert_track._alerts.get(pos["alert"])
        price = _price(a)
        qty = pos["qty_raw"] / 10 ** pos["decimals"]
        value = qty * price if price else None
        pos_view = {**{k: pos[k] for k in ("symbol", "token", "pair", "at", "cost_usdg", "mode", "route")},
                    "value_usdg": round(value, 4) if value is not None else None,
                    "qty": qty, "price": price,
                    "ret": round(value / pos["cost_usdg"] - 1, 4) if value and pos["cost_usdg"] else None,
                    "image": (a or {}).get("image")}
    label = alert_track.system_board("meme")["chosen"] if u["paper"] else None
    papers = ([_paper_view(p, label) for p in u["paper"]] + [_trend_view(p) for p in u["trend"]])[::-1]
    sim = None
    if u["sim_start"]:
        equity = (u["sim_usdg"] or 0) + ((pos_view or {}).get("value_usdg") or 0 if pos and pos["mode"] == "sim" else 0)
        equity += sum(p["value_usdg"] for p in papers)
        sim = {"usdg": u["sim_usdg"], "start": u["sim_start"], "equity": round(equity, 4),
               "ret": round(equity / u["sim_start"] - 1, 4) if u["sim_start"] else None}
    closed = [t for t in u["trades"] if t.get("side") == "sell" and t.get("ok") and t.get("ret") is not None]
    return {"mode": u["mode"], "pct": u["pct"], "pcts": list(PCTS), "chain": CHAIN,
            "entry_policy": "reviewed_gem_trend" if reviewed else "blocked_no_reviewed_policy",
            "position": pos_view or (papers[0] if papers else None), "positions": papers,
            "sim": sim, "trades": u["trades"][-20:][::-1], "note": u["note"],
            "stats": {"closed": len(closed), "wins": sum(1 for t in closed if t["ret"] > 0),
                      "sum_ret": round(sum(t["ret"] for t in closed), 4) if closed else None},
            "system": alert_track.system_board("meme")["chosen"], "usdc": _meta["usdc"]}


# --------------------------------------------------------------- decisions

def _system_of(a: dict, label: str) -> dict | None:
    """The system entered on the alert itself and closed by the trend
    follower's exit (Lorenzo, 28/09: "la strategia deve entrare nel momento
    in cui arriva l'alert [...] Poi deve gestire esclusivamente la
    chiusura"): alert_bt, system.follow(on_alert=True)."""
    return (a.get("alert_bt") or {}).get(label)


def _signal_key(a: dict) -> str:
    s = a.get("quant_system") or {}
    return json.dumps([a["chain"], a["token_address"], s.get("report_hash"), s.get("policy_id")])


def _unseen(u: dict, a: dict) -> bool:
    signal = (a.get("quant_system") or {}).get("in_at", 0)
    return signal > u.get("quant_seen", {}).get(_signal_key(a), -1)


def _claim(u: dict, a: dict) -> None:
    # Durable high-water marks outlive the truncated display log.
    u.setdefault("quant_seen", {})[_signal_key(a)] = a["quant_system"]["in_at"]


def entry_candidate(label: str, now: float, user_state: dict | None = None) -> dict | None:
    """An alert observes; only a reviewed, fresh trend signal can buy a gem."""
    rows = [a for a in alert_track._alerts.values()
            if a["chain"] == CHAIN and quant_runtime.can_enter(a, now)
            and (user_state is None or _unseen(user_state, a))]
    return max(rows, key=lambda a: a["quant_system"]["in_at"], default=None)


def should_exit(pos: dict, label: str, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    a = alert_track._alerts.get(pos["alert"])
    if a is None:
        return "alert no longer tracked"
    if a.get("gone"):
        return "pool gone"
    s = a.get("quant_system") if pos.get("quant_policy") else _system_of(a, label)
    if pos.get("quant_policy") and (not s or not s.get("eligible") or now - s.get("evaluated_at", 0) > 600):
        return "quant signal data unavailable"
    if not s:
        return None
    if pos.get("quant_policy") and any(e.get("side") in ("sell", "loss") and e.get("t", 0) >= pos.get("at", float("inf")) for e in s.get("events", [])):
        return "system sold"
    if s.get("state") != "in":
        last = (s.get("events") or [{}])[-1]
        return {"sell": "system sold", "rug": "pool gone"}.get(last.get("side"), "system out") \
            if last.get("why") != "48h" else "48h window over"
    return None


def buy_amount(balance_raw: int, pct: int) -> int:
    """pct of the balance, one base unit less: never more than there is."""
    return max(0, balance_raw * pct // 100 - 1)


# ----------------------------------------------------------------- market

@dataclass
class Market:
    """What the autotrader needs from the chain; the defaults are real, tests
    pass fakes."""
    usdg_balance: Callable[[str], Awaitable[int]]
    token_balance: Callable[[str, str], Awaitable[int]]
    eth_balance_wei: Callable[[str], Awaitable[int]]
    gas_price_wei: Callable[[], Awaitable[int]]
    decimals: Callable[[str], Awaitable[int | None]]
    quote: Callable[[str, str, int], Awaitable[dict]]
    swap: Callable[..., Awaitable[dict]]
    find_usdc: Callable[[], Awaitable[str | None]] | None = None


async def _default_market() -> Market:
    from app.trading.onchain import aggregator, balances, execution, quotes, token_metadata

    async def token_balance(owner: str, token: str) -> int:
        rows = await balances._robinhood_known_token_balances(owner, [token])
        return rows[0]["amount_raw"] if rows else 0

    async def usdg_balance(owner: str) -> int:
        return await token_balance(owner, USDG)

    async def eth_balance_wei(owner: str) -> int:
        return int(await execution._robinhood_rpc("eth_getBalance", [owner, "latest"]), 16)

    async def gas_price_wei() -> int:
        return int(await execution._robinhood_rpc("eth_gasPrice", []), 16)

    async def decimals(token: str) -> int | None:
        return await token_metadata.get_token_decimals(CHAIN, token)

    async def quote(token_in: str, token_out: str, amount_in: int) -> dict:
        try:
            found = await quotes.find_pool_and_fee(token_in, token_out)
        except Exception:
            found = None
        if found is not None:
            _pool, fee = found
            q = await quotes.uniswap_v3_quote(token_in, token_out, fee, amount_in)
            return {"out": int(q["amount_out"]), "route": "uniswap-v3", "fee": fee,
                    "impact": float(q.get("price_impact_pct") or 0)}
        q = await aggregator.quote(token_in, token_out, amount_in)
        return {"out": int(q["amount_out"]), "route": "aggregator", "fee": None,
                "impact": float(q.get("price_impact_pct") or 0)}

    async def find_usdc() -> str | None:
        """Lorenzo: cash is USDC, converted to USDG to trade on Robinhood.
        The USDC contract there is not hard-coded: it is the USDC side of
        the most liquid USDC/USDG pool on DexScreener (the pool the
        conversions go through anyway), checked on chain to be a 6-decimal
        token called USDC. robinhood_usdc_address in the settings wins."""
        from app.config import settings as app_settings
        if app_settings.robinhood_usdc_address:
            return app_settings.robinhood_usdc_address
        from app import http_client
        async with http_client.pooled(timeout=10.0) as c:
            r = await c.get("https://api.dexscreener.com/latest/dex/search", params={"q": "USDC USDG"})
        best = None
        for p in (r.json() or {}).get("pairs") or []:
            if p.get("chainId") != CHAIN:
                continue
            sides = [p.get("baseToken") or {}, p.get("quoteToken") or {}]
            usdc = next((t for t in sides if (t.get("symbol") or "").upper() == "USDC"), None)
            usdg = next((t for t in sides if (t.get("address") or "").lower() == USDG.lower()), None)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if usdc and usdg and liq >= MIN_CASH_POOL_USD and (best is None or liq > best[1]):
                best = (usdc["address"], liq)
        if not best or await token_metadata.get_token_decimals(CHAIN, best[0]) != USDC_DECIMALS:
            return None
        return best[0]

    return Market(usdg_balance, token_balance, eth_balance_wei, gas_price_wei, decimals, quote, real_swap, find_usdc)


USDC_DECIMALS = 6


async def usdc_address(market: Market, now: float | None = None) -> str | None:
    now = now or time.time()
    if _meta["usdc"] or market.find_usdc is None or now - _meta["usdc_at"] < 600:
        return _meta["usdc"]
    _meta["usdc_at"] = now
    try:
        _meta["usdc"] = await market.find_usdc()
    except Exception:
        _meta["usdc"] = None
    return _meta["usdc"]


async def _ensure_allowance(private_key_hex: str, owner: str, token: str, amount: int) -> str | None:
    """ERC-20 approve of SwapRouter02 when the allowance is short. Returns
    the approve tx hash, or None when none was needed. The swap that follows
    has the next nonce, so it can only land after it."""
    from eth_utils import to_checksum_address

    from app.trading.onchain import execution
    router = execution.ROBINHOOD_SWAP_ROUTER02
    data = "0xdd62ed3e" + owner[2:].lower().zfill(64) + router[2:].lower().zfill(64)
    allowance = int(await execution._robinhood_rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)
    if allowance >= amount:
        return None
    nonce = int(await execution._robinhood_rpc("eth_getTransactionCount", [owner, "pending"]), 16)
    gas_price = int(await execution._robinhood_rpc("eth_gasPrice", []), 16) * 12 // 10
    calldata = "0x095ea7b3" + router[2:].lower().zfill(64) + "f" * 64      # approve(router, max)
    tx = {"nonce": nonce, "to": to_checksum_address(token), "value": 0, "gas": 80_000, "gasPrice": gas_price,
          "chainId": 4663, "data": calldata}
    return await execution.send_signed_evm_tx(execution.sign_evm_transaction(private_key_hex, tx))


async def real_swap(*, user, token_in: str, token_out: str, amount_in: int, min_out: int, fee: int,
                    notional_usd: float, impact: float, price_usd: float, side: str, token: str) -> dict:
    from app import auth
    from app.config import settings as app_settings
    from app.trading.onchain import execute, risk
    if not app_settings.onchain_execution_enabled:
        return {"ok": False, "reason": "real execution is switched off on this server"}
    ok, reason = risk.run_safety_checks(user.id, CHAIN, token)
    if not ok:
        return {"ok": False, "reason": reason}
    owner = user.onchain_wallets[CHAIN].address
    private_key_hex = auth.get_decrypted_onchain_private_key(user, CHAIN)
    try:
        approve_tx = await _ensure_allowance(private_key_hex, owner, token_in, amount_in)
    except Exception as exc:
        return {"ok": False, "reason": f"approve failed: {exc}"}
    result = await execute.execute_order(
        user.id, CHAIN, token, "System", side, notional_usd, impact, price_usd, private_key_hex,
        {"token_in": token_in, "token_out": token_out, "fee": fee, "amount_in": amount_in,
         "amount_out_minimum": min_out},
        guards=False,
    )
    return {"ok": bool(result.get("accepted")), "reason": result.get("reason") or "",
            "tx": result.get("tx_signature"), "approve_tx": approve_tx}


# ------------------------------------------------------------------- tick

def _log(u: dict, row: dict) -> dict:
    last = u["trades"][-1] if u["trades"] else None
    if not row.get("ok") and last and not last.get("ok") and last.get("reason") == row.get("reason") \
            and last.get("symbol") == row.get("symbol") and last.get("side") == row.get("side"):
        last["t"] = row["t"]           # the same failure again: one row, not one a minute
        return last
    u["trades"].append(row)
    del u["trades"][:-KEEP_TRADES]
    return row


# --------------------------------------------------------------- paper book
#
# Simulated follows every alert, on every chain, with the trade the
# Strategies tab shows: bought at the alert, sold on the trend follower's
# exit (Lorenzo, 28/09: "il wallet simulato non sembra replicare gli
# ingressi [...] voglio che li segua"). Priced from alert_bt, so the
# simulated wallet and the backtest are the same trades. Real stays on
# Robinhood quotes, one token at a time.

def _paper_ret(p: dict, a: dict | None, label: str) -> tuple[float, bool, str]:
    """(return so far, closed, why) of one paper trade."""
    if a is None:
        return p.get("ret", 0.0), True, "alert no longer tracked"
    s = (a.get("quant_system") if p.get("quant_policy") else _system_of(a, label)) or {}
    if p.get("quant_policy"):
        # The paper stake starts at this entry, not at the start of a multi-trade backtest.
        price = s.get("mark") or p.get("entry_price")
        exits = [ev for ev in s.get("events", []) if ev["t"] >= p["signal_at"] and ev["side"] in ("sell", "loss")]
        if exits:
            price = exits[0]["price"]
        costs = p["costs"]
        ret = price / p["entry_price"] * (1 - costs["commission"]) ** 2 * (1 - costs["slippage"]) / (1 + costs["slippage"]) - 1
        if a.get("gone"):
            return -1.0, True, "pool gone"
        return ret, bool(exits), "trend exit" if exits else ""
    ret = s.get("ret")
    ret = p.get("ret", 0.0) if ret is None else float(ret)
    if s.get("state") == "out" or s.get("done"):
        last = (s.get("events") or [{}])[-1]
        return ret, True, {"rug": "pool gone", "48h": "48h window over"}.get(
            last.get("why") or last.get("side"), "system sold")
    if a.get("gone"):
        return -1.0, True, "pool gone"
    return ret, False, ""


def _paper_view(p: dict, label: str | None = None) -> dict:
    a = alert_track._alerts.get(p["alert"])
    ret = p.get("ret", 0.0)
    if a is not None and label:
        ret, _closed, _why = _paper_ret(p, a, label)   # same reading as the mark below
    value = p["stake"] * (1 + ret)
    # The price the value was marked at: the system's own candle close while
    # it holds (Lorenzo, 29/09: "il saldo totale non corrisponde al saldo
    # delle posizioni aperte" -- valuing at the tracker's newer price made a
    # live re-mark drift from the total). Out, or held without a stored mark
    # yet: no price, so nothing re-marks a value the system is not holding
    # at a known price. No backtest yet: the tracker's price.
    s = (a.get("quant_system") if p.get("quant_policy") else _system_of(a, label)) if a is not None and label else None
    price = (s.get("mark") if s.get("state") == "in" else None) if s else _price(a)
    shown = price or _price(a)
    return {"symbol": p["symbol"], "token": p["token"], "chain": p["chain"], "at": p["at"], "mode": "sim",
            "cost_usdg": p["stake"], "value_usdg": round(value, 4), "ret": round(ret, 4),
            "price": price, "qty": value / shown if shown else None,
            "image": (a or {}).get("image"), "route": "paper", "pair": (a or {}).get("pair_address")}


def paper_tick(u: dict, label: str, now: float, open_new: bool = True) -> list[dict]:
    """Close the paper trades the system left, then open one on every fresh
    alert not followed yet, each with the share of the simulated equity.
    Switched away from Simulated (open_new False): open trades still close
    on the system's exit, nothing new opens."""
    rows: list[dict] = []
    for p in list(u["paper"]):
        a = alert_track._alerts.get(p["alert"])
        ret, closed, why = _paper_ret(p, a, label)
        p["ret"] = ret
        if closed:
            cash = p["stake"] * (1 + ret)
            u["sim_usdg"] = round((u["sim_usdg"] or 0) + cash, 6)
            u["paper"].remove(p)
            rows.append(_log(u, {"t": now, "side": "sell", "symbol": p["symbol"], "token": p["token"],
                                 "alert": p["alert"], "chain": p["chain"], "mode": "sim", "why": why, "ok": True,
                                 "usdg": round(cash, 6), "ret": round(ret, 4)}))
    if not open_new:
        return rows
    followed = {p["alert"] for p in u["paper"]}
    fresh = sorted((a for a in alert_track._alerts.values()
                    if quant_runtime.can_enter(a, now, mode="paper") and _unseen(u, a)
                    and alert_track.key_of(a) not in followed), key=lambda a: a["at"])
    for a in fresh:
        # A delayed poll cannot fill at an earlier backtest open.
        paper_entry = _price(a)
        if not paper_entry or not math.isfinite(paper_entry) or paper_entry <= 0:
            continue
        equity = (u["sim_usdg"] or 0) + sum(p["stake"] * (1 + p.get("ret", 0.0)) for p in u["paper"]) + _trend_open(u)
        stake = round(min(u["sim_usdg"] or 0, equity * u["pct"] / 100), 6)
        base = {"t": now, "side": "buy", "symbol": a["symbol"], "token": a["token_address"],
                "alert": alert_track.key_of(a), "chain": a["chain"], "mode": "sim", "why": "validated trend signal"}
        if stake < 0.01:
            rows.append(_log(u, {**base, "ok": False, "reason": "all cash already in open trades"}))
            continue
        _claim(u, a)
        u["sim_usdg"] = round((u["sim_usdg"] or 0) - stake, 6)
        u["paper"].append({"alert": base["alert"], "symbol": a["symbol"], "token": a["token_address"],
                           "chain": a["chain"], "at": now, "stake": stake, "ret": 0.0,
                           "quant_policy": a["quant_system"]["policy_id"], "signal_at": now,
                           "source_signal_at": a["quant_system"]["in_at"],
                           "entry_price": paper_entry, "costs": a["quant_policy"]["costs"], **_why(a)})
        rows.append(_log(u, {**base, "ok": True, "usdg": stake}))
    return rows


async def _tick(users: list, now: float | None = None, market: Market | None = None,
               notify: Callable[[object, str], Awaitable[None]] | None = None) -> list[dict]:
    """One pass for every user whose switch is not off (or who still holds
    a position): exit first, then at most one entry."""
    now = now or time.time()
    label = alert_track.system_board("meme")["chosen"]
    done: list[dict] = []
    for user in users:
        u = _state.get(user.id)
        if not u:
            continue
        u.setdefault("paper", [])
        if u["mode"] == "sim" or u["paper"] or u.get("trend"):
            if u["sim_start"] is None:
                u.update(sim_usdg=SIM_START_USDG, sim_start=SIM_START_USDG)
            for row in paper_tick(u, label, now, open_new=u["mode"] == "sim"):
                done.append(row)
                if notify and row.get("ok"):
                    await notify(user, row)
        if u["mode"] != "real" and not u["position"]:
            continue
        wallet = (getattr(user, "onchain_wallets", {}) or {}).get(CHAIN)
        if wallet is None:
            u["note"] = "no Robinhood Chain wallet"
            continue
        market = market or await _default_market()
        try:
            if u["position"]:
                why = should_exit(u["position"], label, now)
                if why:
                    row = await _sell(user, u, why, now, market)
                    done.append(row)
                    if notify and row.get("ok"):
                        await notify(user, row)
            if not u["position"] and u["mode"] == "real":
                a = entry_candidate(label, now, u)
                if a:
                    _claim(u, a)
                    # Persist BEFORE awaiting any order. An uncertain attempt is never retried automatically.
                    write_text_atomic(_STORE_FILE, json.dumps(_state), encoding="utf-8")
                    row = await _buy(user, u, a, now, market)
                    done.append(row)
                    if notify and row.get("ok"):
                        await notify(user, row)
        except Exception as exc:
            logger.exception("autotrader %s", user.id)
            u["note"] = f"error: {exc}"[:200]
    if done:
        _save()
    return done


# --------------------------------------------------------------- trend book
#
# Lorenzo, 30/09: "il sistema deve fare ingressi con strategia dinamica su
# gemme e non piu' su crypto etichettate come meme o con caratteristiche da
# evitare" -- and, on the card, "Si', solo Sim". Simulated follows the trend
# follower 8h/1d of the ranking (strategy_lab's signals): in on its buy, out
# on its sell, only on tokens that pass the gem rules (quant_runtime.gem_verdict:
# no memes, ranked, fresh market facts, liquidity). Real is untouched: it
# still waits for a reviewed quant policy.

def _trend_ret(p: dict) -> float:
    price, entry = p.get("price"), p.get("entry_price")
    return price / entry - 1 if price and entry else 0.0


def _trend_open(u: dict) -> float:
    return sum(p["stake"] * (1 + _trend_ret(p)) for p in u.get("trend") or [])


def _trend_view(p: dict) -> dict:
    ret = _trend_ret(p)
    value = p["stake"] * (1 + ret)
    return {"symbol": p["symbol"], "token": p["token"], "chain": p["chain"], "at": p["at"], "mode": "sim",
            "cost_usdg": p["stake"], "value_usdg": round(value, 4), "ret": round(ret, 4),
            "price": p.get("price"), "qty": value / p["price"] if p.get("price") else None,
            "image": None, "route": f"trend {p.get('interval') or ''}".strip(), "pair": p.get("pair")}


def trend_signal(uids: list[str], sig: dict, now: float, gem_ok: bool,
                 max_usd: float | None = None) -> list[tuple[str, dict]]:
    """One strategy_lab signal on every simulated book. A buy opens a trade
    (only when gem_ok), a sell closes the one open on that token. `max_usd`
    is the largest entry the pool takes under the slippage limit (slippage.py)."""
    price = sig.get("price")
    if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return []
    out: list[tuple[str, dict]] = []
    for uid in uids:
        u = _state.get(uid)
        if not u:
            continue
        u.setdefault("trend", [])
        held = next((p for p in u["trend"] if p["chain"] == sig["chain"] and p["token"] == sig["token"]), None)
        base = {"t": now, "symbol": sig["symbol"], "token": sig["token"], "chain": sig["chain"], "mode": "sim"}
        if sig["side"] == "sell" and held:
            held["price"] = price
            ret = _trend_ret(held)
            cash = held["stake"] * (1 + ret)
            u["sim_usdg"] = round((u["sim_usdg"] or 0) + cash, 6)
            u["trend"].remove(held)
            out.append((uid, _log(u, {**base, "side": "sell", "ok": True, "usdg": round(cash, 6),
                                      "ret": round(ret, 4), "why": f"trend exit {sig.get('interval', '')}".strip()})))
        elif sig["side"] == "buy" and not held and gem_ok and u["mode"] == "sim":
            if u["sim_start"] is None:
                u.update(sim_usdg=SIM_START_USDG, sim_start=SIM_START_USDG)
            equity = ((u["sim_usdg"] or 0) + _trend_open(u)
                      + sum(p["stake"] * (1 + p.get("ret", 0.0)) for p in u["paper"]))
            wanted = min(u["sim_usdg"] or 0, equity * u["pct"] / 100)
            stake = round(min(wanted, max_usd) if isinstance(max_usd, (int, float)) and max_usd >= 0 else wanted, 6)
            why = f"trend entry {sig.get('interval', '')}".strip()
            if stake < wanted:
                why += f" · size capped by slippage (${wanted:,.0f} wanted)"
            if stake < 0.01:
                out.append((uid, _log(u, {**base, "side": "buy", "ok": False, "why": why,
                                          "reason": "pool too thin for the slippage limit" if wanted >= 0.01
                                          else "all cash already in open trades"})))
                continue
            u["sim_usdg"] = round((u["sim_usdg"] or 0) - stake, 6)
            u["trend"].append({"symbol": sig["symbol"], "token": sig["token"], "chain": sig["chain"],
                               "pair": sig.get("pair"), "interval": sig.get("interval"), "at": now,
                               "signal_at": sig.get("t"), "stake": stake, "entry_price": price, "price": price})
            out.append((uid, _log(u, {**base, "side": "buy", "ok": True, "usdg": stake, "why": why})))
    if out:
        _save()
    return out


def trend_marks(prices: dict[str, float]) -> None:
    """Re-mark the open trend trades: {"chain:token": price}."""
    for u in _state.values():
        for p in u.get("trend") or []:
            price = prices.get(f"{p['chain']}:{p['token']}")
            if isinstance(price, (int, float)) and math.isfinite(price) and price > 0:
                p["price"] = price


def trend_keys() -> list[tuple[str, str]]:
    return sorted({(p["chain"], p["token"]) for u in _state.values() for p in u.get("trend") or []})


_tick_running = False


async def tick(users: list, now: float | None = None, market: Market | None = None,
               notify: Callable[[object, str], Awaitable[None]] | None = None) -> list[dict]:
    global _tick_running
    if _tick_running:
        return []
    _tick_running = True
    try:
        return await _tick(users, now, market, notify)
    finally:
        _tick_running = False


async def _swap_or_sim(user, real: bool, *, token_in: str, token_out: str, amount_in: int, quote: dict,
                       slippage: float, side: str, token: str, price_usd: float, notional: float) -> dict:
    """One leg: simulated (the quote is the fill) or real (Uniswap v3 only)."""
    if not real:
        return {"ok": True, "out": quote["out"]}
    if quote["route"] != "uniswap-v3":
        return {"ok": False, "reason": "only reachable through an aggregator: not signable yet"}
    res = await _MARKET.swap(user=user, token_in=token_in, token_out=token_out, amount_in=amount_in,
                             min_out=int(quote["out"] * (1 - slippage)), fee=quote["fee"], notional_usd=notional,
                             impact=quote["impact"], price_usd=price_usd, side=side, token=token)
    return {**res, "out": int(quote["out"] * (1 - slippage))}


_MARKET: Market | None = None


async def _buy(user, u: dict, a: dict, now: float, market: Market) -> dict:
    """Cash is USDC (Lorenzo): the USDC the share needs becomes USDG first,
    then USDG buys the token. USDG already in the wallet counts as cash."""
    global _MARKET
    _MARKET = market
    owner = user.onchain_wallets[CHAIN].address
    mode = u["mode"]
    base = {"t": now, "side": "buy", "symbol": a["symbol"], "token": a["token_address"], "alert": alert_track.key_of(a),
            "mode": mode, "ok": False}
    usdc = await usdc_address(market, now)
    if mode == "sim":
        usdc_raw, usdg_raw = int((u["sim_usdg"] or 0) * 10 ** USDC_DECIMALS), 0
        if not usdc:
            usdc_raw, usdg_raw = 0, usdc_raw          # no USDC pool known: simulate straight from USDG
    else:
        usdg_raw = await market.usdg_balance(owner)
        usdc_raw = await market.token_balance(owner, usdc) if usdc else 0
        wei = await market.eth_balance_wei(owner)
        need = GAS_UNITS * await market.gas_price_wei() * 12 // 10
        if wei < need:
            return _log(u, {**base, "reason": f"not enough ETH for gas (need ~{need / 1e18:.6f} ETH)"})
    amount = buy_amount(usdc_raw + usdg_raw, u["pct"])
    if amount <= 0:
        return _log(u, {**base, "reason": "no USDC or USDG to spend"})
    # Lorenzo, 30/09: never one entry so large it moves an illiquid pool.
    # The quote's impact on the planned size gives the pool's depth, and the
    # size shrinks to what stays under the slippage limit (slippage.py).
    probe = await market.quote(USDG, a["token_address"], amount)
    top = slip_est.max_usd(slip_est.depth_from_quote(amount / 10 ** USDG_DECIMALS, probe.get("impact")))
    if top is not None and top * 10 ** USDG_DECIMALS < amount:
        base["capped_from"] = round(amount / 10 ** USDG_DECIMALS, 2)
        amount = int(top * 10 ** USDG_DECIMALS)
        if amount <= 0:
            return _log(u, {**base, "reason": "pool too thin for the slippage limit"})
    decimals = await market.decimals(a["token_address"])
    if decimals is None:
        return _log(u, {**base, "reason": "token decimals unavailable"})
    spend_usdg = min(amount, usdg_raw)
    convert = amount - spend_usdg
    via = []
    if convert > 0:
        q1 = await market.quote(usdc, USDG, convert)
        leg = await _swap_or_sim(user, mode == "real", token_in=usdc, token_out=USDG, amount_in=convert, quote=q1,
                                 slippage=CASH_SLIPPAGE, side="buy", token=USDG, price_usd=1.0,
                                 notional=convert / 10 ** USDC_DECIMALS)
        if not leg["ok"]:
            return _log(u, {**base, "reason": f"USDC to USDG: {leg['reason']}"})
        spend_usdg += leg["out"]
        via.append("USDC→USDG")
    q = await market.quote(USDG, a["token_address"], spend_usdg)
    usdg = spend_usdg / 10 ** USDG_DECIMALS
    base.update(usdg=round(usdg, 6), route=q["route"], impact=q["impact"], via=via)
    if q["out"] <= 0:
        return _log(u, {**base, "reason": "no market for this token"})
    leg = await _swap_or_sim(user, mode == "real", token_in=USDG, token_out=a["token_address"], amount_in=spend_usdg, quote=q,
                             slippage=SLIPPAGE, side="buy", token=a["token_address"], price_usd=_price(a) or 0.0,
                             notional=usdg)
    if not leg["ok"]:
        return _log(u, {**base, "reason": leg["reason"]})
    if mode == "real":
        base["tx"] = leg.get("tx")
        qty_raw = await market.token_balance(owner, a["token_address"]) or q["out"]
    else:
        u["sim_usdg"] = round((u["sim_usdg"] or 0) - amount / 10 ** USDC_DECIMALS, 6)
        qty_raw = q["out"]
    u["position"] = {"alert": base["alert"], "symbol": a["symbol"], "token": a["token_address"],
                     "pair": a["pair_address"], "qty_raw": int(qty_raw), "decimals": decimals,
                     "cost_usdg": round(amount / 10 ** USDC_DECIMALS, 6), "at": now, "mode": mode, "route": q["route"],
                     "quant_policy": (a.get("quant_system") or {}).get("policy_id"), **_why(a)}
    return _log(u, {**base, "ok": True, "qty": qty_raw / 10 ** decimals, **_why(a)})


def _why(a: dict) -> dict:
    """Lorenzo, 30/09 ("applica tutto"): every entry names the approved report
    it ran under and the receipt of the gem decision behind it, so any trade
    can be traced back to a rule someone approved."""
    return {"report_hash": (a.get("quant_system") or {}).get("report_hash"),
            "gem_receipt": gem_track.receipt_for(a.get("chain") or "", a.get("token_address") or "")}


async def _sell(user, u: dict, why: str, now: float, market: Market) -> dict:
    """Token -> USDG, then the USDG back to USDC: cash rests in USDC."""
    global _MARKET
    _MARKET = market
    pos = u["position"]
    owner = user.onchain_wallets[CHAIN].address
    base = {"t": now, "side": "sell", "symbol": pos["symbol"], "token": pos["token"], "alert": pos["alert"],
            "mode": pos["mode"], "why": why, "ok": False}
    qty_raw = pos["qty_raw"] if pos["mode"] == "sim" else await market.token_balance(owner, pos["token"])
    if qty_raw <= 0:
        u["position"] = None
        return _log(u, {**base, "reason": "nothing left to sell"})
    try:
        q = await market.quote(pos["token"], USDG, qty_raw)
    except Exception as exc:
        q = {"out": 0, "route": None, "impact": None}
        base["reason"] = f"no quote: {exc}"[:160]
    base.update(route=q["route"], impact=q["impact"])
    if pos["mode"] == "real" and (q["out"] <= 0 or q["route"] != "uniswap-v3"):
        return _log(u, {**base, "reason": base.get("reason") or "no signable route to sell right now"})
    got_usdg = 0
    if q["out"] > 0:
        leg = await _swap_or_sim(user, pos["mode"] == "real", token_in=pos["token"], token_out=USDG, amount_in=qty_raw, quote=q,
                                 slippage=SLIPPAGE, side="sell", token=pos["token"],
                                 price_usd=_price(alert_track._alerts.get(pos["alert"])) or 0.0,
                                 notional=q["out"] / 10 ** USDG_DECIMALS)
        if not leg["ok"]:
            return _log(u, {**base, "reason": leg["reason"]})
        base["tx"] = leg.get("tx")
        got_usdg = q["out"] if pos["mode"] == "sim" else leg["out"]
    cash_raw, via = got_usdg, []
    usdc = await usdc_address(market, now)
    if usdc and got_usdg > 0:
        back = got_usdg
        try:
            q2 = await market.quote(USDG, usdc, back)
            leg2 = await _swap_or_sim(user, pos["mode"] == "real", token_in=USDG, token_out=usdc, amount_in=back, quote=q2,
                                      slippage=CASH_SLIPPAGE, side="sell", token=USDG, price_usd=1.0,
                                      notional=back / 10 ** USDG_DECIMALS)
            if leg2["ok"]:
                cash_raw = q2["out"] if pos["mode"] == "sim" else leg2["out"]
                via.append("USDG→USDC")
            else:
                u["note"] = f"USDG to USDC: {leg2['reason']}"[:200]
        except Exception as exc:
            u["note"] = f"USDG to USDC: {exc}"[:200]
    cash = cash_raw / 10 ** USDC_DECIMALS
    if pos["mode"] == "sim":
        u["sim_usdg"] = round((u["sim_usdg"] or 0) + cash, 6)
    u["position"] = None
    ret = cash / pos["cost_usdg"] - 1 if pos["cost_usdg"] else None
    return _log(u, {**base, "ok": True, "usdg": round(cash, 6), "via": via,
                    "ret": round(ret, 4) if ret is not None else None})


_load()

