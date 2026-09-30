import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../static/onchain/lab-shown.js', import.meta.url), 'utf8');
const { shownSummary, shownTrades } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

// main._lab_summary, the same fields the Top list receives
// main._lab_summary with strategy_lab.drawn_row
function drawnRow(part) {
  const rows = (part?.rows ?? []).filter((r) => r.trades && r.return_pct != null && ['Trend EMA', 'Trend HF', 'RSI+BB'].includes(r.label));
  const row = rows.sort((a, b) => b.return_pct - a.return_pct)[0];
  if (!row || part.best?.label !== row.label) return null;
  return (part.best.events ?? []).filter((e) => e.side === 'sell').length === row.trades ? row : null;
}
function summaryOf(lab) {
  const best = drawnRow(lab), drow = lab.daily ? drawnRow(lab.daily) : null;
  return { best: best ? { label: best.label, return_pct: best.return_pct, trades: best.trades } : null,
    daily: drow ? { interval: lab.daily.interval, return_pct: drow.return_pct, trades: drow.trades, in_position: drow.in_position } : null };
}
const buy = (t) => ({ t, side: 'buy', price: 1 }), sell = (t, ret) => ({ t, side: 'sell', price: 2, ret });
const fast = { rows: [{ label: 'Trend HF', trades: 2, return_pct: 400 }, { label: 'RSI+BB L/S', trades: 5, return_pct: 900 }],
  best: { label: 'Trend HF', events: [buy(1), sell(2, 1), buy(3), sell(4, 1)] } };

test('a gem whose 8h trend follower stayed out shows the 5-minute trades it lists (MERRYMEN)', () => {
  const lab = { ...fast, daily: { interval: '8h', rows: [{ label: 'Trend EMA', trades: 0, return_pct: 0 }], best: null } };
  const s = shownSummary(summaryOf(lab)), c = shownTrades(lab);
  assert.equal(s.return_pct, 400); assert.equal(s.interval, '5m');
  assert.equal(c.interval, s.interval); assert.equal(c.events.filter((e) => e.side === 'sell').length, s.trades);
});
test('the 8h/1d trend follower leads when it traded, on both sides', () => {
  const lab = { ...fast, daily: { interval: '8h', rows: [{ label: 'Trend EMA', trades: 1, return_pct: 30 }],
    best: { label: 'Trend EMA', events: [buy(10), sell(20, 0.3)] } } };
  const s = shownSummary(summaryOf(lab)), c = shownTrades(lab);
  assert.equal(s.interval, '8h'); assert.equal(c.interval, '8h'); assert.equal(c.events.length, 2);
  assert.equal(c.events.filter((e) => e.side === 'sell').length, s.trades);
});
test('no trade anywhere: nothing in the list, nothing claimed on the chart', () => {
  const lab = { rows: [{ label: 'Trend EMA', trades: 0, return_pct: 0 }], daily: null };
  assert.equal(shownSummary(summaryOf(lab)), null); assert.equal(shownTrades(lab), null);
});
test('RSI+BB is listed and drawn when its own trades are saved; RSI+BB L/S never', () => {
  const lab = { rows: [{ label: 'RSI+BB', trades: 1, return_pct: 50 }], best: { label: 'RSI+BB', events: [buy(1), sell(2, 0.5)] }, daily: null };
  assert.equal(shownSummary(summaryOf(lab)).label, 'RSI+BB'); assert.equal(shownTrades(lab).label, 'RSI+BB');
  const ls = { rows: [{ label: 'RSI+BB L/S', trades: 1, return_pct: 50 }], best: { label: 'RSI+BB L/S', events: [buy(1), sell(2, 0.5)] }, daily: null };
  assert.equal(shownSummary(summaryOf(ls)), null); assert.equal(shownTrades(ls), null);
});
test('saved arrows of another strategy or another run: neither listed nor drawn (BEB1M: 30 trades, 1 sell)', () => {
  const lab = { rows: [{ label: 'Trend HF', trades: 30, return_pct: -67 }],
    best: { label: 'Trend HF', events: [buy(1), sell(2, -0.1)] }, daily: null };
  assert.equal(shownSummary(summaryOf(lab)), null); assert.equal(shownTrades(lab), null);
  const other = { rows: [{ label: 'Trend EMA', trades: 1, return_pct: 5 }],
    best: { label: 'RSI+BB', events: [buy(1), sell(2, 0.05)] }, daily: null };
  assert.equal(shownSummary(summaryOf(other)), null); assert.equal(shownTrades(other), null);
});
