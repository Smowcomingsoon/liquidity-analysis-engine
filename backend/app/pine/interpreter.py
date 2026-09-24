from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.pine import ast_nodes as ast
from app.pine.builtins_stateful import Atr, Ema, Extremum, Macd, Rma, Rsi, Sma, Stdev, Wma
from app.pine.errors import PineUnsupportedError

_BUILTIN_SERIES = {"open", "high", "low", "close", "volume", "hl2", "hlc3", "ohlc4", "bar_index"}
_NOOP_FUNCS = {
    "plot", "plotshape", "plotchar", "plotarrow", "plotbar", "plotcandle",
    "bgcolor", "hline", "fill", "alertcondition", "barcolor",
    "line.new", "label.new", "table.new", "table.cell", "box.new",
}


@dataclass
class ParamSpec:
    var_name: str
    kind: str  # int/float/bool/string/source
    default: Any
    title: str
    minval: float | None = None
    maxval: float | None = None


def truthy(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    return bool(v)


def literal_value(expr: ast.Expr) -> Any:
    if isinstance(expr, ast.NumberLit):
        return expr.value
    if isinstance(expr, ast.StringLit):
        return expr.value
    if isinstance(expr, ast.BoolLit):
        return expr.value
    if isinstance(expr, ast.NaLit):
        return None
    if isinstance(expr, ast.Identifier):
        return expr.name
    if isinstance(expr, ast.Unary) and expr.op == "-" and isinstance(expr.operand, ast.NumberLit):
        return -expr.operand.value
    raise PineUnsupportedError("expected a constant value here")


def _iter_calls_in_expr(expr: ast.Expr):
    if isinstance(expr, ast.Call):
        yield expr
        for a in expr.args:
            yield from _iter_calls_in_expr(a)
        for v in expr.kwargs.values():
            yield from _iter_calls_in_expr(v)
    elif isinstance(expr, ast.Unary):
        yield from _iter_calls_in_expr(expr.operand)
    elif isinstance(expr, ast.Binary):
        yield from _iter_calls_in_expr(expr.left)
        yield from _iter_calls_in_expr(expr.right)
    elif isinstance(expr, ast.Ternary):
        yield from _iter_calls_in_expr(expr.cond)
        yield from _iter_calls_in_expr(expr.if_true)
        yield from _iter_calls_in_expr(expr.if_false)
    elif isinstance(expr, ast.HistoryRef):
        yield from _iter_calls_in_expr(expr.base)
        yield from _iter_calls_in_expr(expr.offset)


def _iter_calls_in_stmts(stmts: list[ast.Stmt]):
    for s in stmts:
        if isinstance(s, ast.ExprStmt):
            yield from _iter_calls_in_expr(s.expr)
        elif isinstance(s, (ast.Assign, ast.Reassign, ast.TupleAssign)):
            yield from _iter_calls_in_expr(s.expr)
        elif isinstance(s, ast.If):
            yield from _iter_calls_in_expr(s.cond)
            yield from _iter_calls_in_stmts(s.body)
            for c, b in s.elifs:
                yield from _iter_calls_in_expr(c)
                yield from _iter_calls_in_stmts(b)
            if s.else_body:
                yield from _iter_calls_in_stmts(s.else_body)


def _walk_assigns(stmts: list[ast.Stmt]):
    for s in stmts:
        if isinstance(s, ast.Assign):
            yield s
        elif isinstance(s, ast.If):
            yield from _walk_assigns(s.body)
            for _, b in s.elifs:
                yield from _walk_assigns(b)
            if s.else_body:
                yield from _walk_assigns(s.else_body)


def scan_inputs(stmts: list[ast.Stmt]) -> list[ParamSpec]:
    specs: list[ParamSpec] = []
    for a in _walk_assigns(stmts):
        if not (isinstance(a.expr, ast.Call) and a.expr.func_name.startswith("input.")):
            continue
        call = a.expr
        kind = call.func_name.split(".", 1)[1]
        defval = None
        if call.args:
            defval = literal_value(call.args[0])
        elif "defval" in call.kwargs:
            defval = literal_value(call.kwargs["defval"])
        title = literal_value(call.kwargs["title"]) if "title" in call.kwargs else a.name
        minval = literal_value(call.kwargs["minval"]) if "minval" in call.kwargs else None
        maxval = literal_value(call.kwargs["maxval"]) if "maxval" in call.kwargs else None
        specs.append(ParamSpec(var_name=a.name, kind=kind, default=defval, title=str(title), minval=minval, maxval=maxval))
    return specs


def is_strategy_style(stmts: list[ast.Stmt]) -> bool:
    return any(
        c.func_name in ("strategy.entry", "strategy.close", "strategy.close_all")
        for c in _iter_calls_in_stmts(stmts)
    )


class PineInterpreter:
    """Bar-by-bar evaluator for a constrained Pine Script subset. Produces a
    DataFrame with the same entry_long/entry_short/exit_long/exit_short
    contract as strategies.compute_signals(), so it plugs directly into the
    existing confirmed_events()/run_backtest() pipeline."""

    def __init__(self, df: pd.DataFrame, params: dict[str, Any] | None = None, signal_map: dict[str, str] | None = None):
        self.df = df
        self.n = len(df)
        self.params = params or {}
        self.signal_map = signal_map or {}
        self._o = df["open"].to_numpy(dtype=float)
        self._h = df["high"].to_numpy(dtype=float)
        self._l = df["low"].to_numpy(dtype=float)
        self._c = df["close"].to_numpy(dtype=float)
        self._v = df["volume"].to_numpy(dtype=float)

        self.series: dict[str, list] = {}
        self.inprogress: dict[str, Any] = {}
        self.stateful: dict[int, Any] = {}
        self.var_initialized: set[int] = set()
        self.entry_id_direction: dict[str, str] = {}
        self.flags: dict[str, bool] = {}
        self.bar = 0
        self.is_strategy_style = False

    # ---- top-level run --------------------------------------------------

    def run(self, stmts: list[ast.Stmt]) -> pd.DataFrame:
        self._prescan(stmts)
        rows = []
        for bar in range(self.n):
            self.bar = bar
            self.flags = {"entry_long": False, "entry_short": False, "exit_long": False, "exit_short": False}
            self._exec_block(stmts)
            self._finalize_bar()
            rows.append(dict(self.flags))

        if not self.is_strategy_style and self.signal_map:
            out = {}
            for role in ("entry_long", "entry_short", "exit_long", "exit_short"):
                varname = self.signal_map.get(role)
                lst = self.series.get(varname, []) if varname else []
                padded = [bool(v) for v in lst] + [False] * (self.n - len(lst))
                out[role] = padded[: self.n]
            return pd.DataFrame(out, index=self.df.index)

        return pd.DataFrame(rows, index=self.df.index)

    def boolean_candidates(self) -> list[dict]:
        candidates = []
        for name, values in self.series.items():
            non_null = [v for v in values if v is not None]
            if non_null and all(isinstance(v, bool) for v in non_null):
                candidates.append({"name": name, "true_count": sum(non_null)})
        return candidates

    def _prescan(self, stmts: list[ast.Stmt]) -> None:
        calls = list(_iter_calls_in_stmts(stmts))
        self.is_strategy_style = any(
            c.func_name in ("strategy.entry", "strategy.close", "strategy.close_all") for c in calls
        )
        for c in calls:
            if c.func_name == "strategy.exit":
                raise PineUnsupportedError(
                    "strategy.exit (stop/limit) is not supported — use strategy.close() with an explicit condition",
                    c.line_no,
                )
            if c.func_name == "strategy.entry":
                id_expr = c.args[0] if c.args else c.kwargs.get("id")
                dir_expr = c.args[1] if len(c.args) > 1 else (c.kwargs.get("direction") or c.kwargs.get("long"))
                if id_expr is None or dir_expr is None:
                    continue
                try:
                    id_val = literal_value(id_expr)
                except PineUnsupportedError:
                    continue
                if isinstance(dir_expr, ast.Identifier) and dir_expr.name in ("strategy.long", "strategy.short"):
                    self.entry_id_direction[id_val] = "long" if dir_expr.name == "strategy.long" else "short"

    def _finalize_bar(self) -> None:
        for name in self.inprogress:
            lst = self.series.setdefault(name, [])
            while len(lst) < self.bar:
                lst.append(None)
            if len(lst) == self.bar:
                lst.append(self.inprogress[name])
            else:
                lst[self.bar] = self.inprogress[name]

    # ---- statement execution ---------------------------------------------

    def _exec_block(self, stmts: list[ast.Stmt]) -> None:
        for s in stmts:
            self._exec_stmt(s)

    def _exec_stmt(self, s: ast.Stmt) -> None:
        if isinstance(s, ast.Assign):
            self._exec_assign(s)
        elif isinstance(s, ast.Reassign):
            self.inprogress[s.name] = self.eval_expr(s.expr)
        elif isinstance(s, ast.TupleAssign):
            val = self.eval_expr(s.expr)
            if not isinstance(val, tuple):
                raise PineUnsupportedError(
                    "multiple assignment needs a function that returns a tuple (e.g. ta.macd)", s.line_no
                )
            for name, v in zip(s.names, val):
                self.inprogress[name] = v
        elif isinstance(s, ast.ExprStmt):
            self.eval_expr(s.expr)
        elif isinstance(s, ast.If):
            if truthy(self.eval_expr(s.cond)):
                self._exec_block(s.body)
                return
            for econd, ebody in s.elifs:
                if truthy(self.eval_expr(econd)):
                    self._exec_block(ebody)
                    return
            if s.else_body is not None:
                self._exec_block(s.else_body)

    def _exec_assign(self, s: ast.Assign) -> None:
        if isinstance(s.expr, ast.Call) and s.expr.func_name.startswith("input."):
            default = literal_value(s.expr.args[0]) if s.expr.args else literal_value(s.expr.kwargs.get("defval", ast.NaLit()))
            self.inprogress[s.name] = self.params.get(s.name, default)
            return
        if s.is_var:
            if id(s) not in self.var_initialized:
                self.var_initialized.add(id(s))
                self.inprogress[s.name] = self.eval_expr(s.expr)
            return
        self.inprogress[s.name] = self.eval_expr(s.expr)

    # ---- expression evaluation --------------------------------------------

    def eval_expr(self, expr: ast.Expr) -> Any:
        if isinstance(expr, ast.NumberLit):
            return expr.value
        if isinstance(expr, ast.StringLit):
            return expr.value
        if isinstance(expr, ast.BoolLit):
            return expr.value
        if isinstance(expr, ast.NaLit):
            return None
        if isinstance(expr, ast.Identifier):
            return self.eval_identifier(expr.name)
        if isinstance(expr, ast.HistoryRef):
            return self.eval_history(expr.base, expr.offset)
        if isinstance(expr, ast.Unary):
            if expr.op == "not":
                return not truthy(self.eval_expr(expr.operand))
            v = self.eval_expr(expr.operand)
            if v is None:
                return None
            return v if expr.op == "+" else -v
        if isinstance(expr, ast.Binary):
            return self._eval_binary(expr)
        if isinstance(expr, ast.Ternary):
            return self.eval_expr(expr.if_true) if truthy(self.eval_expr(expr.cond)) else self.eval_expr(expr.if_false)
        if isinstance(expr, ast.Call):
            return self.eval_call(expr)
        raise PineUnsupportedError(f"unsupported expression node: {type(expr).__name__}")

    def _eval_binary(self, expr: ast.Binary) -> Any:
        left = self.eval_expr(expr.left)
        right = self.eval_expr(expr.right)
        op = expr.op
        if op == "and":
            return truthy(left) and truthy(right)
        if op == "or":
            return truthy(left) or truthy(right)
        if left is None or right is None:
            return None
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return left / right if right != 0 else None
        if op == "%":
            return left % right if right != 0 else None
        raise PineUnsupportedError(f"unsupported operator: {op}")

    def eval_identifier(self, name: str) -> Any:
        if name in _BUILTIN_SERIES or name in ("strategy.long", "strategy.short"):
            return self._eval_base(name, 0)
        if name in self.inprogress:
            return self.inprogress[name]
        raise PineUnsupportedError(f"undefined variable: {name}")

    def eval_history(self, base: ast.Expr, offset: ast.Expr) -> Any:
        if not isinstance(base, ast.Identifier):
            raise PineUnsupportedError("history indexing is only supported on plain identifiers (e.g. close[1])")
        name = base.name
        off_val = self.eval_expr(offset)
        k = int(off_val) if off_val is not None else 0
        if name in _BUILTIN_SERIES or name in ("strategy.long", "strategy.short"):
            return self._eval_base(name, k)
        if k == 0:
            return self.eval_identifier(name)
        if name not in self.series and name not in self.inprogress:
            raise PineUnsupportedError(f"undefined variable: {name}")
        lst = self.series.get(name, [])
        idx = len(lst) - k
        return lst[idx] if 0 <= idx < len(lst) else None

    def _eval_base(self, name: str, k: int) -> Any:
        idx = self.bar - k
        if name == "strategy.long":
            return "long"
        if name == "strategy.short":
            return "short"
        if idx < 0 or idx >= self.n:
            return None
        if name == "open":
            return float(self._o[idx])
        if name == "high":
            return float(self._h[idx])
        if name == "low":
            return float(self._l[idx])
        if name == "close":
            return float(self._c[idx])
        if name == "volume":
            return float(self._v[idx])
        if name == "hl2":
            return float((self._h[idx] + self._l[idx]) / 2)
        if name == "hlc3":
            return float((self._h[idx] + self._l[idx] + self._c[idx]) / 3)
        if name == "ohlc4":
            return float((self._o[idx] + self._h[idx] + self._l[idx] + self._c[idx]) / 4)
        if name == "bar_index":
            return float(idx)
        raise PineUnsupportedError(f"serie base sconosciuta: {name}")

    # ---- function calls -----------------------------------------------

    def eval_call(self, node: ast.Call) -> Any:
        fn = node.func_name
        if fn in _NOOP_FUNCS:
            for a in node.args:
                self.eval_expr(a)
            for v in node.kwargs.values():
                self.eval_expr(v)
            return None
        if fn == "nz":
            val = self.eval_expr(node.args[0]) if node.args else None
            if val is not None:
                return val
            return self.eval_expr(node.args[1]) if len(node.args) > 1 else 0.0
        if fn.startswith("math."):
            return self._eval_math(fn, node)
        if fn.startswith("ta."):
            return self._eval_ta(fn, node)
        if fn.startswith("strategy."):
            return self._eval_strategy(fn, node)
        if fn.startswith("input."):
            raise PineUnsupportedError(
                "input.* is only supported as a direct assignment: name = input.xxx(...)", node.line_no
            )
        raise PineUnsupportedError(f"unsupported function: {fn}", node.line_no)

    def _eval_math(self, fn: str, node: ast.Call) -> Any:
        vals = [self.eval_expr(a) for a in node.args]
        if any(v is None for v in vals):
            return None
        name = fn.split(".", 1)[1]
        if name == "abs":
            return abs(vals[0])
        if name == "max":
            return max(vals)
        if name == "min":
            return min(vals)
        if name == "sqrt":
            return vals[0] ** 0.5 if vals[0] >= 0 else None
        if name == "pow":
            return vals[0] ** vals[1]
        if name == "log":
            return float(np.log(vals[0])) if vals[0] > 0 else None
        if name == "round":
            return float(round(vals[0]))
        if name == "floor":
            return float(np.floor(vals[0]))
        if name == "ceil":
            return float(np.ceil(vals[0]))
        raise PineUnsupportedError(f"math.{name} not supported", node.line_no)

    def _eval_ta(self, fn: str, node: ast.Call) -> Any:
        name = fn.split(".", 1)[1]
        state = self.stateful.get(id(node))

        if name == "ema":
            src = self.eval_expr(node.args[0])
            length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Ema(length)
                self.stateful[id(node)] = state
            return state.update(src)
        if name == "sma":
            src = self.eval_expr(node.args[0])
            length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Sma(length)
                self.stateful[id(node)] = state
            return state.update(src)
        if name == "rma":
            src = self.eval_expr(node.args[0])
            length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Rma(length)
                self.stateful[id(node)] = state
            return state.update(src)
        if name == "wma":
            src = self.eval_expr(node.args[0])
            length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Wma(length)
                self.stateful[id(node)] = state
            return state.update(src)
        if name == "stdev":
            src = self.eval_expr(node.args[0])
            length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Stdev(length)
                self.stateful[id(node)] = state
            return state.update(src)
        if name == "rsi":
            src = self.eval_expr(node.args[0])
            length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Rsi(length)
                self.stateful[id(node)] = state
            return state.update(src)
        if name == "atr":
            length = int(self.eval_expr(node.args[0]))
            if state is None:
                state = Atr(length)
                self.stateful[id(node)] = state
            idx = self.bar
            return state.update(float(self._h[idx]), float(self._l[idx]), float(self._c[idx]))
        if name == "macd":
            src = self.eval_expr(node.args[0])
            fast = int(self.eval_expr(node.args[1]))
            slow = int(self.eval_expr(node.args[2]))
            signal = int(self.eval_expr(node.args[3]))
            if state is None:
                state = Macd(fast, slow, signal)
                self.stateful[id(node)] = state
            return state.update(src)
        if name in ("highest", "lowest"):
            if len(node.args) == 1:
                src = "high" if name == "highest" else "low"
                src_val = self._eval_base(src, 0)
                length = int(self.eval_expr(node.args[0]))
            else:
                src_val = self.eval_expr(node.args[0])
                length = int(self.eval_expr(node.args[1]))
            if state is None:
                state = Extremum(length, "high" if name == "highest" else "low")
                self.stateful[id(node)] = state
            return state.update(src_val)
        if name == "change":
            if not isinstance(node.args[0], ast.Identifier):
                raise PineUnsupportedError("ta.change needs a plain variable as its argument", node.line_no)
            now = self.eval_expr(node.args[0])
            prev = self.eval_history(node.args[0], ast.NumberLit(1))
            if now is None or prev is None:
                return None
            return now - prev
        if name in ("crossover", "crossunder", "cross"):
            return self._eval_cross(name, node)
        raise PineUnsupportedError(f"ta.{name} not supported", node.line_no)

    def _cross_now_prev(self, expr: ast.Expr, node: ast.Call, kind: str) -> tuple[Any, Any]:
        # A bare numeric constant acts as a flat reference line (e.g. ta.crossover(rsi, 30)):
        # its "previous" value is itself, no history lookup needed.
        if isinstance(expr, (ast.NumberLit, ast.BoolLit)) or (
            isinstance(expr, ast.Unary) and expr.op == "-" and isinstance(expr.operand, ast.NumberLit)
        ):
            v = self.eval_expr(expr)
            return v, v
        if isinstance(expr, ast.Identifier):
            return self.eval_expr(expr), self.eval_history(expr, ast.NumberLit(1))
        raise PineUnsupportedError(
            f"ta.{kind} needs already-assigned variables or numeric constants, e.g. ta.crossover(fastMA, slowMA) or ta.crossover(rsi, 30)",
            node.line_no,
        )

    def _eval_cross(self, kind: str, node: ast.Call) -> Any:
        a_expr, b_expr = node.args[0], node.args[1]
        a_now, a_prev = self._cross_now_prev(a_expr, node, kind)
        b_now, b_prev = self._cross_now_prev(b_expr, node, kind)
        if None in (a_now, b_now, a_prev, b_prev):
            return False
        crossed_up = a_prev <= b_prev and a_now > b_now
        crossed_down = a_prev >= b_prev and a_now < b_now
        if kind == "crossover":
            return crossed_up
        if kind == "crossunder":
            return crossed_down
        return crossed_up or crossed_down

    def _eval_strategy(self, fn: str, node: ast.Call) -> Any:
        name = fn.split(".", 1)[1]
        if name == "entry":
            id_val = literal_value(node.args[0]) if node.args else literal_value(node.kwargs["id"])
            dir_expr = node.args[1] if len(node.args) > 1 else (node.kwargs.get("direction") or node.kwargs.get("long"))
            direction = "long"
            if isinstance(dir_expr, ast.Identifier) and dir_expr.name == "strategy.short":
                direction = "short"
            when_expr = node.kwargs.get("when")
            when_val = truthy(self.eval_expr(when_expr)) if when_expr is not None else True
            if when_val:
                self.flags["entry_long" if direction == "long" else "entry_short"] = True
            return None
        if name == "close":
            id_val = literal_value(node.args[0]) if node.args else literal_value(node.kwargs.get("id"))
            direction = self.entry_id_direction.get(id_val)
            if direction == "long":
                self.flags["exit_long"] = True
            elif direction == "short":
                self.flags["exit_short"] = True
            else:
                self.flags["exit_long"] = True
                self.flags["exit_short"] = True
            return None
        if name == "close_all":
            self.flags["exit_long"] = True
            self.flags["exit_short"] = True
            return None
        raise PineUnsupportedError(f"strategy.{name} not supported", node.line_no)
