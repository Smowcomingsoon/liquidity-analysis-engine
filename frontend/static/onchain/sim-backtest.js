/**
 * The simulated wallet replayed on the Top 50's backtests
 * (backend: sim_backtest.py). Lorenzo, 30/09: "applichi a quel portafoglio
 * tutti gli ingressi e tutti i rendimenti di tutte le gemme in top 50 per
 * vedere come avrebbe performato". Read only: the live simulation is not
 * touched. A row opens the token's page.
 */
import * as api from "@onchain/api.js";
import { escapeHtml, logo } from "@onchain/format.js";
import { closeSheet, openSheet, setBody, sheetBody } from "@onchain/sheets.js";

const usd = (v) => (v == null ? "—" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(Math.abs(v) < 0.1 ? 1 : 0)}%`);
const sign = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
const day = (t) => new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "2-digit" });
let tab = "open";
const own = new Map();   // "chain:token" -> this wallet's trade, drawn on the token's chart
let data = null;

function curve(rows) {
  if (!rows?.length || rows.length < 2) return "";
  const W = 320, H = 90, t0 = rows[0][0], t1 = rows.at(-1)[0] || t0 + 1;
  const vals = rows.map((r) => r[1]), lo = Math.min(...vals), hi = Math.max(...vals) || lo + 1;
  const x = (t) => ((t - t0) / (t1 - t0 || 1)) * W, y = (v) => H - 4 - ((v - lo) / (hi - lo || 1)) * (H - 8);
  const d = rows.map((r, i) => `${i ? "L" : "M"}${x(r[0]).toFixed(1)},${y(r[1]).toFixed(1)}`).join("");
  return `<svg class="sbt-curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-sign="${sign(vals.at(-1) - vals[0])}" aria-hidden="true"><path d="${d}"/></svg>`;
}

function row(p, open) {
  const how = `${escapeHtml(p.interval ?? "")}${p.strategy && p.strategy !== "unknown" ? ` · ${escapeHtml(p.strategy)}` : ""}`;
  const when = open ? `in since ${day(p.at)} · ${how}` : `${day(p.at)} → ${day(p.exit_at)} · ${how}`;
  const val = open ? usd(p.value) : `${p.pnl >= 0 ? "+" : "−"}${usd(Math.abs(p.pnl))}`;
  own.set(`${p.chain}:${p.token}`, { symbol: p.symbol ?? "?",
    trades: [{ t: p.at, side: "buy" }, ...(open ? [] : [{ t: p.exit_at, side: "sell", ret: p.ret }])] });
  return `<button type="button" class="auto-pos sbt-row" data-sbt-token="${escapeHtml(`${p.chain}:${p.token}`)}">
    ${logo(p.symbol, null, p.chain, p.token)}
    <span class="trow-main"><b>${escapeHtml(p.symbol ?? "?")}</b><small>${when} · ${usd(p.stake)}</small></span>
    <span class="trow-num"><b>${val}</b><em class="chg" data-sign="${sign(p.ret)}">${open && p.ret == null ? "no price" : pct(p.ret)}</em></span>
  </button>`;
}

function body(d) {
  const list = tab === "open" ? d.open : d.closed;
  return `<div class="auto sbt">
    <p class="sbt-total"><b>${usd(d.equity)}</b> <em class="chg" data-sign="${sign(d.ret)}">${pct(d.ret)}</em></p>
    <p class="auto-line">From ${usd(d.capital)}${d.from ? ` on ${day(d.from)}` : ""} · ${d.trades} closed trades, ${d.win_rate == null ? "—" : `${Math.round(d.win_rate * 100)}% won`} · max drawdown ${pct(-d.max_dd)} · ${d.tokens} tokens</p>
    ${curve(d.curve)}
    <p class="auto-line">Every entry and exit of the backtest shown next to each Top 50 token (trend follower 8h/1d, or the best 5m: trend follower or RSI+BB), in time order on one wallet: ${Math.round(d.share * 100)}% of the equity per entry, fees included. Open trades at the live price. Nothing is traded.</p>
    <nav class="pf-seg" aria-label="Positions">
      <button type="button" data-sbt-tab="open" aria-pressed="${tab === "open"}">Open ${d.open.length}</button>
      <button type="button" data-sbt-tab="closed" aria-pressed="${tab === "closed"}">Closed ${d.closed.length}</button>
    </nav>
    <div class="sbt-list">${list.length ? list.map((p) => row(p, tab === "open")).join("") : `<p class="auto-flat">None.</p>`}</div>
  </div>`;
}

async function openToken(key) {
  const [chain, ...rest] = key.split(":");
  closeSheet();
  history.replaceState(null, "", `${location.pathname}${location.search}#gems&token=${encodeURIComponent(chain)}:${encodeURIComponent(rest.join(":"))}`);
  const detail = await import("@onchain/detail.js");
  const mine = own.get(key);
  if (mine) {
    detail.register(`gem:${chain}:${rest.join(":")}`, { kind: "gem", chain, token: rest.join(":"), symbol: mine.symbol });
    detail.setOwn(chain, rest.join(":"), mine.trades);
  }
  detail.openFromHash();
}

let bound = false;
export async function openSimBacktest() {
  openSheet("Top 50 backtest", "How the simulated wallet would have done on every strategy trade of the Top 50.");
  setBody(`<p class="auto-flat">Replaying the trades…</p>`);
  if (!bound) {
    bound = true;
    sheetBody().addEventListener("click", (event) => {
      if (!event.target.closest(".sbt") || !data) return;
      const t = event.target.closest("[data-sbt-tab]");
      if (t) { tab = t.dataset.sbtTab; setBody(body(data)); return; }
      const r = event.target.closest("[data-sbt-token]");
      if (r) openToken(r.dataset.sbtToken);
    });
  }
  try {
    data = await api.getTopBacktest();
    setBody(body(data));
  } catch (e) {
    setBody(`<p class="auto-note">${escapeHtml(e?.message ?? "Couldn't replay it right now.")}</p>`);
  }
}
