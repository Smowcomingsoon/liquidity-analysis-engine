"""Kill switch and per-exchange circuit breaker for automated trading.

All checks are non-blocking reads — they never raise, they return a
(allowed: bool, reason: str) pair that the caller logs and acts on.
"""
import time
from collections import defaultdict

from app.config import settings

# Per-exchange failure tracking: exchange -> {failures, open_until}
_exchange_state: dict[str, dict] = defaultdict(lambda: {"failures": 0, "open_until": 0.0})

_MAX_FAILURES = 5        # consecutive failures before tripping
_OPEN_DURATION_S = 300   # 5 minutes in open state before auto-reset attempt


def check_halt() -> tuple[bool, str]:
    """Return (True, "") when trading is allowed, (False, reason) when blocked."""
    if settings.trading_halt:
        return False, "trading paused (kill switch on)"
    return True, ""


def check_exchange(exchange: str) -> tuple[bool, str]:
    """Per-exchange circuit breaker check. Returns (allowed, reason)."""
    state = _exchange_state[exchange]
    now = time.time()
    if state["open_until"] > now:
        remaining = int(state["open_until"] - now)
        return False, f"circuit breaker open on {exchange} — retry in {remaining}s"
    return True, ""


def record_exchange_success(exchange: str) -> None:
    _exchange_state[exchange]["failures"] = 0
    _exchange_state[exchange]["open_until"] = 0.0


def record_exchange_failure(exchange: str) -> bool:
    """Record a failure. Returns True if the circuit just tripped."""
    state = _exchange_state[exchange]
    state["failures"] += 1
    if state["failures"] >= _MAX_FAILURES and state["open_until"] == 0.0:
        state["open_until"] = time.time() + _OPEN_DURATION_S
        return True
    return False
