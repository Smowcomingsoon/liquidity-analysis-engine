import statistics
from collections import deque


class Ema:
    def __init__(self, length: int):
        self.length = length
        self.value: float | None = None

    def update(self, src: float | None) -> float | None:
        if src is None:
            return self.value
        if self.value is None:
            self.value = src
        else:
            alpha = 2 / (self.length + 1)
            self.value = alpha * src + (1 - alpha) * self.value
        return self.value


class Rma:
    """Wilder's smoothing — used internally by RSI/ATR and directly via ta.rma."""

    def __init__(self, length: int):
        self.length = length
        self.buffer: list[float] = []
        self.value: float | None = None

    def update(self, src: float | None) -> float | None:
        if src is None:
            return None
        if self.value is None:
            self.buffer.append(src)
            if len(self.buffer) < self.length:
                return None
            self.value = sum(self.buffer) / self.length
            return self.value
        self.value = (self.value * (self.length - 1) + src) / self.length
        return self.value


class Sma:
    def __init__(self, length: int):
        self.length = length
        self.buf: deque = deque(maxlen=length)

    def update(self, src: float | None) -> float | None:
        if src is None:
            return None
        self.buf.append(src)
        if len(self.buf) < self.length:
            return None
        return sum(self.buf) / self.length


class Wma:
    def __init__(self, length: int):
        self.length = length
        self.buf: deque = deque(maxlen=length)

    def update(self, src: float | None) -> float | None:
        if src is None:
            return None
        self.buf.append(src)
        if len(self.buf) < self.length:
            return None
        n = len(self.buf)
        weights = range(1, n + 1)
        denom = sum(weights)
        return sum(w * v for w, v in zip(weights, self.buf)) / denom


class Stdev:
    def __init__(self, length: int):
        self.length = length
        self.buf: deque = deque(maxlen=length)

    def update(self, src: float | None) -> float | None:
        if src is None:
            return None
        self.buf.append(src)
        if len(self.buf) < self.length:
            return None
        return statistics.pstdev(self.buf)


class Extremum:
    def __init__(self, length: int, kind: str):
        self.length = length
        self.kind = kind
        self.buf: deque = deque(maxlen=length)

    def update(self, src: float | None) -> float | None:
        if src is None:
            return None
        self.buf.append(src)
        if len(self.buf) < self.length:
            return None
        return max(self.buf) if self.kind == "high" else min(self.buf)


class Rsi:
    def __init__(self, length: int):
        self.length = length
        self.prev: float | None = None
        self.gain = Rma(length)
        self.loss = Rma(length)

    def update(self, src: float | None) -> float | None:
        if src is None:
            return None
        if self.prev is None:
            self.prev = src
            return None
        change = src - self.prev
        self.prev = src
        g = self.gain.update(max(change, 0.0))
        loss_val = self.loss.update(max(-change, 0.0))
        if g is None or loss_val is None:
            return None
        if loss_val == 0:
            return 100.0
        rs = g / loss_val
        return 100 - 100 / (1 + rs)


class Macd:
    def __init__(self, fast: int, slow: int, signal: int):
        self.fast = Ema(fast)
        self.slow = Ema(slow)
        self.signal_ema = Ema(signal)

    def update(self, src: float | None) -> tuple[float | None, float | None, float | None]:
        f = self.fast.update(src)
        s = self.slow.update(src)
        if f is None or s is None:
            return None, None, None
        macd = f - s
        sig = self.signal_ema.update(macd)
        hist = None if sig is None else macd - sig
        return macd, sig, hist


class Atr:
    def __init__(self, length: int):
        self.length = length
        self.prev_close: float | None = None
        self.rma = Rma(length)

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        return self.rma.update(tr)
