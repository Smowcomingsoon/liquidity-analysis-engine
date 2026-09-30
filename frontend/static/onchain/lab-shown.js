/**
 * Which backtest a gem shows: one rule for the Top list and the chart.
 *
 * Lorenzo, 30/09 (MERRYMEN, Rank #2): the list said about +400%, the chart
 * said "The strategy stayed out". The list fell back to the 5-minute
 * backtest when the 8h/1d trend follower had no trade, the chart only ever
 * drew the 8h/1d one. Now both ask this module: the 8h/1d trend follower
 * when it traded, else the best 5-minute spot strategy (trend follower or
 * RSI+BB), else nothing. The number in the
 * list and the arrows on the chart always come from the same trades.
 *
 * Pure functions, no DOM: frontend/tests/lab-shown.test.mjs checks them.
 */

const traded = (row) => row && (row.trades ?? 0) > 0 && row.return_pct != null;
// system.SPOT_LABELS: the trend followers and RSI+BB, long only.
const TREND = new Set(["Trend EMA", "Trend HF", "RSI+BB"]);

/** From /api/onchain/top's summary (main._lab_summary). */
export function shownSummary(s) {
  if (!s) return null;
  const d = s.daily;
  if (d && (d.trades ?? 0) > 0) {
    return { source: "daily", interval: d.interval ?? "1d", return_pct: d.return_pct, trades: d.trades,
      in_position: Boolean(d.in_position), win_rate: d.win_rate ?? null };
  }
  if (traded(s.best)) {
    return { source: "fast", interval: "5m", label: s.best.label, return_pct: s.best.return_pct, trades: s.best.trades,
      in_position: false, win_rate: s.best.win_rate ?? null };
  }
  return null;
}

// The saved chart events must be this row's own trades: same strategy,
// one sell per closed trade (strategy_lab.drawn_row does the same for the
// list). Anything else would draw another strategy's arrows.
function ownEvents(part, row) {
  const best = part?.best;
  if (!best || best.label !== row.label) return null;
  const ev = best.events ?? [];
  return ev.filter((e) => e.side === "sell").length === row.trades ? ev : null;
}

/** From the full lab result (/api/onchain/token backtests: strategy_lab.get). */
export function shownTrades(lab) {
  if (!lab) return null;
  const d = lab.daily;
  const drow = d?.rows?.[0];
  const dev = traded(drow) && TREND.has(drow.label) ? ownEvents(d, drow) : null;
  if (dev) return { source: "daily", interval: d.interval ?? "1d", label: drow.label, events: dev, rug: d.rug ?? null, backtests: d };
  const rows = (lab.rows ?? []).filter((r) => traded(r) && TREND.has(r.label)).sort((a, b) => b.return_pct - a.return_pct);
  const fev = rows.length ? ownEvents(lab, rows[0]) : null;
  if (fev) return { source: "fast", interval: "5m", label: rows[0].label, events: fev, rug: lab.rug ?? null, backtests: lab };
  return null;
}
