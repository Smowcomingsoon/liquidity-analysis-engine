/**
 * Telegram alerts (backend: alert_track.py): what buying when the message
 * arrived would have paid, at 1..48 hours and now, next to the best
 * backtested strategy. Three places use it:
 *  - Memes > Alerts: one card per alert, like a caller's post;
 *  - the trophy view: the leaderboard (alerts or gems) with the winner in
 *    gold, a crown and confetti;
 *  - the bell: the latest alerts.
 * Plus the daily review sheet (ai_review.py) and alertsFor() for the token page.
 */
import { openScan } from "@onchain/sheets.js";
import * as api from "@onchain/api.js";
import { age, bestSystem, changePill, escapeHtml, logo, strategyTrade, tile, tokenRow, usdShort } from "@onchain/format.js";
import { register } from "@onchain/detail.js";
import { renderPodium } from "@onchain/podium.js";

export { tile };

const POLL_MS = 60_000;
const HOURS = [1, 2, 3, 4, 5, 6, 8, 12, 24, 48];

const pct = (f) => (f == null ? "—" : `${f > 0 ? "+" : ""}${Math.round(f * 100)}%`);
const sign = (f) => (f == null ? "" : f >= 0 ? "up" : "down");

// Market cap now, from the cap at the alert and the move since; none when the pool is gone.
const capNow = (a) => (a.gone ? null : a.cap0 && a.now != null ? a.cap0 * (1 + a.now) : a.cap0);

// Leaderboard order. Lorenzo: "voglio le performance della strategia
// ingresso e uscita automatica": the system first, then the raw signal.
const SORTS = { system: "System", now: "Now", best: "Best" };
const state = { data: {}, board: null, kind: "meme", period: 7, sort: "backtest", soonAll: false };
// "About to fire": the first few, the rest on demand (Lorenzo, 28/09: "UI troppo affollata").
const SOON_SHOWN = 3;

/** The alerts on one token, for its page. */
export function alertsFor(chain, token) {
  return [...(state.data.meme?.items ?? []), ...(state.data.gem?.items ?? [])]
    .filter((a) => a.chain === chain && a.token_address === token);
}

function registerAlert(a, kind) {
  const id = `alert:${kind}:${a.chain}:${a.token_address}`;
  const mine = alertsFor(a.chain, a.token_address);
  register(id, {
    kind, symbol: a.symbol, name: a.name, chain: a.chain, token: a.token_address, pair: a.pair_address,
    pairUrl: a.pair_url, image: a.image, cap: capNow(a),
    change: a.now, ageTs: a.at, context: `Spotted at ${usdShort(a.cap0)} MC · ${age(a.at)[0]} ago`,
    alerts: mine.length ? mine : [a],
  });
  return id;
}

/* ------------------------------------------------------ Memes > Alerts */

function callCard(a, kind) {
  const id = registerAlert(a, kind);
  const cap = capNow(a);
  const s = bestSystem(a);
  return `<article class="call">
    <button type="button" class="call-head" data-open="${escapeHtml(id)}">
      ${logo(a.symbol, a.image, a.chain, a.token_address)}
      <span class="trow-main"><b>${escapeHtml(a.name || a.symbol)}</b><small>${escapeHtml(a.symbol)} · ${age(a.at)[0]} ago${a.gone ? " · pool gone" : ""}</small></span>
      <span class="trow-num"><b>${usdShort(cap)}</b>${changePill(a.now)}</span>
    </button>
    <div class="call-card">
      <span><small>At alert</small><b>${usdShort(a.cap0)}</b></span>
      <span><small>Held now</small><b data-sign="${sign(a.now)}">${pct(a.now)}</b></span>
      <span><small>Best 48h</small><b data-sign="${sign(a.max)}">${pct(a.max)}</b></span>
      <span><small>System</small><b data-sign="${sign(s?.ret)}">${s?.ret == null ? "—" : pct(s.ret)}</b><em class="sys-state" data-state="${s?.state ?? "waiting"}">${sysState(s)}</em></span>
    </div>
    <div class="strats">${bestSystem(a) ? strategyTrade(bestSystem(a), a.at) : ""}</div>
    <p class="hours-head">Just holding, from the alert</p>
    <ol class="hours">${HOURS.map((h) => {
      const r = a.returns?.[String(h)];
      return `<li><small>${h}h</small><b data-sign="${sign(r)}">${pct(r)}</b></li>`;
    }).join("")}</ol>
  </article>`;
}

// The system's position on one alert, in two words.
function sysState(s) {
  if (!s) return "reading";
  if (s.state === "in") return "in trade";
  const last = (s.events ?? []).filter((v) => v.side !== "buy").slice(-1)[0];
  if (last?.why === "crash") return "sold after a crash";
  if (last?.why === "rug") return "pool vanished";
  if (s.done) return `${s.trades ?? 0} trade${s.trades === 1 ? "" : "s"}`;
  if (s.state === "out") return "out";
  return "waiting";
}

function systemLine(d) {
  const live = d?.system?.live;
  if (!live?.n) return "";
  return `<p class="sys-strip"><b>System</b><span data-sign="${sign(live.median)}">median ${pct(live.median)}</span>`
    + `<span>${Math.round((live.positive ?? 0) * 100)}% in profit</span><span>${live.n} alerts</span>`
    + `${live.holding ? `<span>in ${live.holding} now</span>` : ""}</p>`;
}

// The scanners' live tape (Lorenzo, 28/09: "voglio vedere i ticker girare,
// non cose ferme"): every pool judged in the last minutes, scrolling, with
// the hour's counts. Polled on its own, faster than the list.
const VERDICT = { seen: "·", check: "…", wait: "…", pass: "✓", tested: "◆", alert: "★", fail: "✗" };
const nfmt = (n) => Number(n ?? 0).toLocaleString("en-US");

function scanStats(f) {
  return `<span class="scan-dot" aria-hidden="true"></span><b>Scanning</b>`
    + `<span><em>${nfmt(f.seen_1h + f.checked_1h)}</em> pools/h</span><span><em>${f.per_min}</em>/min</span>`
    + `<span><em>${nfmt(f.checked_1h)}</em> checked</span><span data-v="pass"><em>${nfmt(f.passed_1h)}</em> passed</span>`
    + `<span data-v="fail"><em>${nfmt(f.rejected_1h)}</em> rejected</span>`;
}

function scanTape(f) {
  const items = (f.feed ?? []).slice(0, 40).map((x) => `<span class="tape-item" data-v="${escapeHtml(x.v)}">`
    + `<i>${VERDICT[x.v] ?? "·"}</i>${escapeHtml(x.symbol)}${x.why ? ` <small>${escapeHtml(x.why)}</small>` : ""}</span>`).join("");
  if (!items) return "";
  // Twice, so the loop has no seam; the speed follows the length.
  return `<div class="scan-tape" aria-hidden="true"><div class="tape-track" style="--n:${Math.min(40, f.feed.length)}">${items}${items}</div></div>`;
}

function scanHtml(f) {
  if (!f) return `<div class="scan-live" data-scan-live></div>`;
  return `<div class="scan-live" data-scan-live data-head="${f.feed?.[0]?.t ?? 0}">`
    + `<p class="scan-stats" data-scan-stats>${scanStats(f)}</p>${scanTape(f)}</div>`;
}

const SCAN_FORM = `<form class="scan-form" data-scan-form role="search">
  <input name="q" placeholder="Scan a token: ticker or address" autocomplete="off" spellcheck="false" aria-label="Ticker or address" readonly />
  <button type="submit" aria-label="Scan"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg></button></form>`;

import { isHidden, onHiddenChange, withEntrance } from "@onchain/hidden.js";

function paintFeed() {
  const root = document.getElementById("meme-alerts");
  if (!root) return;
  const d = state.data.meme;
  delete root.dataset.loading;
  const body = root.querySelector("[data-alerts-body]");
  // Lorenzo, 27/09: "la tab alert e' confusionale, troppe informazioni,
  // troppe cripto [...] mi interessa vedere quali alert sono scattati in
  // ordine cronologico, cosa sta per scattare [...] e riesumare roba vecchia
  // che sta ritornando in moda". One line per token; the numbers are on its
  // page. Rugs and alerts older than 48h leave this list (the Strategies
  // hall of fame keeps them).
  const nSoon = (d?.upcoming ?? []).filter((u) => !u.token_address || !isHidden(u.chain, u.token_address)).length;
  const soon = (d?.upcoming ?? []).filter((u) => !u.token_address || !isHidden(u.chain, u.token_address)).map((u) => `<li class="soon" data-stage="${u.stage}">
      <span class="soon-dot" aria-hidden="true"></span>
      <span class="trow-main"><b>${escapeHtml(u.symbol)} <em class="soon-tag">${u.stage === "imminent" ? "IMMINENT" : "CHECKING"}</em></b>
      <small>${escapeHtml(u.chain)} · ${escapeHtml(u.waiting_for)} · seen ${age(u.since)[0]} ago</small></span></li>`).join("");
  const fired = (d?.feed ?? []).filter((a) => !isHidden(a.chain, a.token_address)).map((a) => {
    const risk = a.ai?.risk;
    const sys = a.system ? `${sysState(a.system)}${a.system.ret != null ? ` ${pct(a.system.ret)}` : ""}` : "";
    return tokenRow({ id: registerAlert(a, "meme"), symbol: a.symbol, name: a.name || a.symbol, chain: a.chain,
      token: a.token_address, image: a.image, cap: capNow(a), change: a.now, ageTs: a.at, badge: a.early ? "early" : null,
      sub: [sys && `system ${escapeHtml(sys)}`, risk != null && `<span class="risk" data-r="${risk >= 60 ? "hi" : risk >= 30 ? "mid" : "lo"}">risk ${risk}</span>`].filter(Boolean).join(" · ") });
  }).join("");
  const back = (d?.reviving ?? []).filter((r) => !isHidden(r.chain, r.token_address)).map((r) => tokenRow({
    id: registerAlert({ ...r, now: r.chg / 100, cap0: null, returns: {} }, "meme"), symbol: r.symbol, name: r.name || r.symbol,
    chain: r.chain, token: r.token_address, image: r.image, cap: null, change: r.chg / 100, ageTs: r.at,
    sub: `alerted ${age(r.at)[0]} ago · ${r.buys_h1 ?? 0} buys / ${r.sells_h1 ?? 0} sells 1h`, pill: `<em class="hot">BACK</em>` })).join("");
  // The feed repaints every poll: keep what is being typed in the scan box.
  const typing = body.querySelector("[data-scan-form] input");
  const kept = typing ? { v: typing.value, focus: document.activeElement === typing } : null;
  withEntrance(body, () => {
    body.innerHTML = SCAN_FORM + systemLine(d) + scanHtml(state.scan)
      + (soon ? `<h4 class="feed-sec">About to fire</h4><ol class="soon-list" data-all="${state.soonAll}">${soon}</ol>`
        + (nSoon > SOON_SHOWN ? `<button type="button" class="more-row" data-soon-more>${state.soonAll ? "Show less" : `+${nSoon - SOON_SHOWN} more`}</button>` : "") : "")
      + `<h4 class="feed-sec">Fired · last 48h</h4>`
      + (fired ? `<ol class="memes-list">${fired}</ol>` : `<p class="muted pad">No alert in the last 48 hours.</p>`)
      + (back ? `<h4 class="feed-sec">Coming back</h4><ol class="memes-list">${back}</ol>` : "");
  });
  if (kept) {
    const input = body.querySelector("[data-scan-form] input");
    input.value = kept.v;
    if (kept.focus) { input.removeAttribute("readonly"); input.focus(); }
  }
}

/* ------------------------------------------------------------ trophy */

/** Normalised rows for the current tab: alerts by return, gems by rank. */
function rows() {
  if (state.kind === "gem") {
    return (state.board?.entries ?? []).filter((e) => !isHidden(e.chain, e.token_address)).map((e) => ({
      id: `gem:${e.chain}:${e.token_address}`, symbol: e.symbol, name: e.name, chain: e.chain, token: e.token_address,
      image: e.image, label: `${Math.round(e.score ?? 0)}`, sub: `${usdShort(e.market_cap)} MC`, sign: "", early: e.early,
    }));
  }
  // Hall of fame (Lorenzo: "il podio non deve avere timeframe, e' la hall
  // of fame"): every alert ever, ranked by what the system made on it.
  const since = 0;
  const key = state.sort;
  const val = (a) => (key === "system" ? a.system?.ret : key === "best" ? a.max : a.now);
  const items = (state.data.meme?.items ?? []).filter((a) => a.at >= since && val(a) != null && !isHidden(a.chain, a.token_address));
  if (key === "backtest") {
    // Lorenzo, 28/09: "non deve accadere che venga scritto no entry yet
    // perche' in realta' la strategia su questa roba sarebbe entrata perche'
    // sono stati segnalati [...] voglio vedere le performance del backtest
    // applicato a quei token". The alert is the entry: the trend follower
    // buys at the alert and exits on its own signal (on_alert, system.py),
    // so every alerted token has a result. The dots only mean "not computed
    // yet".
    const x = (a) => a.on_alert;
    const has = (a) => !!x(a);
    return items.sort((a, b) => (has(b) - has(a)) || (has(a) ? x(b).ret - x(a).ret : b.at - a.at))
      .map((a) => {
        const r = x(a);
        const closed = r?.trades ?? 0;
        const label = r ? pct(r.ret) : '<span class="bt-live" role="img" aria-label="Analyzing"><i></i><i></i><i></i></span>';
        const trades = closed + (r?.state === "in" ? 1 : 0);
        const sys = !r ? "backtest on its way"
          : `${trades} trade${trades === 1 ? "" : "s"}`
            + (closed ? ` · win ${Math.round((r.wins / closed) * 100)}%` : "")
            + (r.state === "in" ? " · in the trade" : "");
        return { id: registerAlert(a, "meme"), symbol: a.symbol, name: a.name, chain: a.chain, token: a.token_address,
          image: a.image, label, ranked: Boolean(r),
          sub: `${age(a.at)[0]} ago · at ${usdShort(a.cap0)} · held ${pct(a.now)}`,
          sign: r ? sign(r.ret) : "", best: null, sys };
      });
  }
  if (key === "system") {
    // Lorenzo: "qui voglio vedere i backtest!!! non voglio vedere 0%". A
    // system that never entered has ret 0, which is not a result: those rows
    // go after the traded ones, newest first, and say so.
    const traded = (s) => s && ((s.trades ?? 0) > 0 || s.state === "in");
    return items.sort((a, b) => (traded(b.system) - traded(a.system)) || (traded(a.system) ? val(b) - val(a) : b.at - a.at))
      .map((a) => {
        const t = traded(a.system);
        return { id: registerAlert(a, "meme"), symbol: a.symbol, name: a.name, chain: a.chain, token: a.token_address,
          image: a.image, label: t ? pct(a.system.ret) : "—",
          sub: `${age(a.at)[0]} ago · at ${usdShort(a.cap0)}${t ? "" : ` · held ${pct(a.now)}`}`,
          sign: t ? sign(a.system.ret) : "", best: null,
          sys: t ? sysState(a.system) : a.system?.done ? "did not enter" : "no entry yet" };
      });
  }
  return items.sort((a, b) => val(b) - val(a))
    .map((a) => ({ id: registerAlert(a, "meme"), symbol: a.symbol, name: a.name, chain: a.chain, token: a.token_address,
      image: a.image, label: pct(val(a)), sub: `${age(a.at)[0]} ago · at ${usdShort(a.cap0)}`,
      sign: sign(val(a)), best: key === "now" ? a.max : null, sys: null }));
}

const INFO_IC = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/></svg>`;

function paintTop() {
  const root = document.getElementById("leaders");
  if (!root) return;
  delete root.dataset.loading;
  const list = rows();
  const gem = state.kind === "gem";
  const w = (state.board?.track?.horizons ?? []).find((h) => h.days === 7 && h.top?.n);
  root.querySelector("[data-top-info]").innerHTML = INFO_IC + (gem
    ? (w ? `Top 10 median ${pct(w.top.median)} after 7 days on the board.` : "Scored on code, research and tokenomics. Research, not advice.")
    : state.sort === "backtest"
      ? `Returns backtested on each alert's 5-minute chart: every entry and exit, fees paid.`
        + ((b) => b && b.pending > 0 ? ` Backtests ready: ${b.ready}/${b.total - b.no_data}.` : "")(state.data.meme?.system?.backtests)
      : state.sort === "system"
        ? `The system: enters and exits on its own signals after each alert, fees paid.`
        : "Returns if you had bought when each Telegram alert arrived.");
  const hero = root.querySelector("[data-top-hero]");
  // The podium is a hall of fame of backtests: a row the system never traded
  // has nothing to show there.
  const podium = state.sort === "system" || state.sort === "backtest" ? list.filter((x) => x.ranked ?? x.label !== "—") : list;
  const top3 = podium.slice(0, 3).map((x) => ({ id: x.id, symbol: x.symbol, value: gem ? `${x.label} pts` : x.label,
    logoHtml: tile(x.symbol, x.image, x.chain, x.token) }));
  if (!top3.length) {
    hero.dataset.sig = "";
    hero.innerHTML = `<p class="muted pad">Nothing in this period yet.</p>`;
  } else {
    // The entrance plays when the podium changes; a refresh of the numbers redraws it still.
    const sig = `${state.kind}:${top3.map((x) => x.id).join("|")}`;
    const same = hero.dataset.sig === sig;
    hero.dataset.sig = sig;
    renderPodium(hero, top3, { still: same });
  }
  const seg = root.querySelector(".seg");
  if (seg) seg.hidden = true;
  const sortBtn = root.querySelector("[data-sort]");
  sortBtn.hidden = true;   // one order here: the strategy's own result
  sortBtn.querySelector("span").textContent = SORTS[state.sort];
  root.querySelector("[data-top-you]").innerHTML = `<span class="avatar you" aria-hidden="true"></span>
    <span class="you-main"><b>You</b><small>${gem ? "Gems you hold" : "Trades on these alerts"}: none yet</small></span>
    <span class="you-num"><small>P&amp;L (USD)</small><b>—</b></span>`;
  withEntrance(root.querySelector("[data-top-list]"), () => {
    root.querySelector("[data-top-list]").innerHTML = list.slice(0, 50).map((x, i) => `
      <li data-place="${i + 1}"><button type="button" data-open="${escapeHtml(x.id)}">
        <span class="top-rank">${i + 1}</span>
        ${logo(x.symbol, x.image, x.chain, x.token)}
        <span class="top-name"><b>${escapeHtml(x.symbol)}${i < 3 ? ` <svg class="mini-crown" viewBox="0 0 24 16" aria-hidden="true"><path d="M2 14 4 3l5 5 3-7 3 7 5-5 2 11z"/></svg>` : ""}${x.early ? ' <em class="hot early">EARLY</em>' : ""}</b><small>${x.sub}</small></span>
        <span class="top-ret"><b data-sign="${x.sign}">${x.label}</b>${x.sys ? `<small>${escapeHtml(x.sys)}</small>` : x.best != null ? `<small>best ${pct(x.best)}</small>` : gem ? "<small>score</small>" : ""}</span>
      </button></li>`).join("");
  });
}

// Gems registered here too, so the trophy's gem rows open their page even
// before the Gems view has been visited (leaderboard.js adds the dossier).
function registerGem(e) {
  register(`gem:${e.chain}:${e.token_address}`, {
    kind: "gem", symbol: e.symbol, name: e.name, chain: e.chain, token: e.token_address, pair: e.pair_address,
    pairUrl: e.pair_url, image: e.image, cap: e.market_cap, change: e.change_24h != null ? e.change_24h / 100 : null,
    ageTs: e.launched_at, context: `Rank #${e.rank ?? "—"} · score ${Math.round(e.score ?? 0)}${e.early ? " · early gem" : ""}`,
    alerts: alertsFor(e.chain, e.token_address),
  });
}

function read(k) { try { return localStorage.getItem(k); } catch { return null; } }
function write(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

async function load() {
  try {
    const [meme, gem, board] = await Promise.all([
      api.getAlerts("meme"), api.getAlerts("gem").catch(() => ({ items: [] })), api.getLeaderboard().catch(() => null)]);
    state.data = { meme, gem };
    state.board = board;
    for (const e of board?.entries ?? []) {
      // leaderboard.js registers the full entry with its dossier; only fill gaps
      registerGem(e);
    }
    paintFeed();
    paintTop();
    const last = Math.max(0, ...[...(meme.items ?? []), ...(gem.items ?? [])].map((a) => a.at));
    document.querySelector("[data-inbox-dot]")?.toggleAttribute("hidden", !(last > Number(read("inbox-seen") ?? 0)));
  } catch { /* keep what is on screen */ }
}

/* ------------------------------------------------------------ review */

function mountReview() {
  const dlg = document.getElementById("review");
  if (!dlg) return;
  dlg.addEventListener("click", (event) => { if (event.target === dlg) dlg.close(); });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("[data-open-review]")) return;
    const body = dlg.querySelector("[data-review-body]");
    body.innerHTML = `<p class="muted">Loading…</p>`;
    dlg.showModal();
    api.getAiReview().then((data) => {
      const r = data.latest;
      if (!r) {
        body.innerHTML = `<p class="muted">${data.status?.enabled
          ? "The first review arrives within a day: what worked, what did not, what to change."
          : "Off until the Anthropic API key is set on the server."}</p>`;
        return;
      }
      dlg.querySelector("[data-review-meta]").textContent = new Date(r.at * 1000).toLocaleDateString();
      body.innerHTML = `<p class="rv-summary">${escapeHtml(r.summary ?? "")}</p>
        ${(r.actions ?? []).length ? `<ul class="rv-actions">${r.actions.map((a) => `<li>${escapeHtml(a)}</li>`).join("")}</ul>` : ""}`;
    }).catch(() => { body.innerHTML = `<p class="muted">Review unavailable right now.</p>`; });
  });
}

/* ------------------------------------------------------------- bell */

export function openInbox() {
  const all = [...(state.data.meme?.items ?? []).map((a) => [a, "meme"]), ...(state.data.gem?.items ?? []).map((a) => [a, "gem"])]
    .sort((x, y) => y[0].at - x[0].at).slice(0, 25);
  let dlg = document.getElementById("inbox");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "inbox";
    dlg.className = "sheet";
    dlg.setAttribute("aria-label", "Alerts");
    dlg.addEventListener("click", (event) => { if (event.target === dlg || event.target.closest("[data-open]")) dlg.close(); });
    document.body.append(dlg);
  }
  dlg.innerHTML = `<div class="grip" aria-hidden="true"></div><h2 class="sheet-title">Alerts</h2>
    <ol class="rows">${all.length ? all.map(([a, kind]) => tokenRow({ id: registerAlert(a, kind), symbol: a.symbol, name: a.name,
      chain: a.chain, token: a.token_address, image: a.image, cap: capNow(a),
      change: a.now, ageTs: a.at, badge: kind === "gem" ? "early" : null })).join("") : `<li class="muted pad">No alert yet.</li>`}</ol>`;
  dlg.showModal();
  write("inbox-seen", String(Date.now() / 1000));
  document.querySelector("[data-inbox-dot]")?.setAttribute("hidden", "");
}

/* ------------------------------------------------------------- mount */

export function mountAlerts() {
  onHiddenChange(() => { paintFeed(); paintTop(); });
  const top = document.getElementById("leaders");
  top?.addEventListener("click", (event) => {
    const p = event.target.closest("[data-period]");
    if (p) {
      p.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === p)));
      state.period = Number(p.dataset.period);
      paintTop();
    }
    if (event.target.closest("[data-sort]")) {
      const order = Object.keys(SORTS);
      state.sort = order[(order.indexOf(state.sort) + 1) % order.length];
      paintTop();
    }
  });
  document.querySelector('.tabs[data-tabs="top"]')?.addEventListener("click", (event) => {
    const b = event.target.closest("[data-tab]");
    if (!b) return;
    state.kind = b.dataset.tab === "gem" ? "gem" : "meme";
    paintTop();
  });
  document.getElementById("meme-alerts")?.addEventListener("click", (event) => {
    if (!event.target.closest("[data-soon-more]")) return;
    state.soonAll = !state.soonAll;
    paintFeed();
  });
  document.getElementById("meme-alerts")?.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-scan-form]");
    if (!form) return;
    event.preventDefault();
    const q = form.q.value.trim();
    if (q) openScan(q);
  });
  mountReview();
  load();
  setInterval(() => document.visibilityState === "visible" && load(), POLL_MS);
}
