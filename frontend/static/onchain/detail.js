/**
 * Token detail: a full-screen page over the current view, like the
 * reference app. Every list registers its rows here (register) and any
 * element with data-open="<id>" opens the page: logo, name and market cap,
 * a line chart of the market cap with a marker where each Telegram alert
 * fired, timeframe chips, three tabs (Signal: what the alerts paid; Why:
 * score, reasons and risks; Info: contract, links, the gem dossier) and a
 * Buy button that hands the token to the swap. The back gesture closes it.
 */
import * as api from "@onchain/api.js";
import { age, bestSystem, changePill, escapeHtml, logo, strategyTrade, usdShort, walletStrategy } from "@onchain/format.js";
import { openUserSwap, walletFor } from "@onchain/userswap.js";
import { hideToken } from "@onchain/hidden.js";
import { shownTrades } from "@onchain/lab-shown.js";
import { morph } from "@onchain/morph-fx.js";

const registry = new Map();
const INTERVALS = ["1m", "5m", "15m", "1h", "4h", "8h", "1d", "1w"];
// GeckoTerminal has no 8-hour or weekly candles: they are built here from
// the 4-hour and daily ones, like strategy_lab._merge_8h on the server.
// Weeks start on Monday 00:00 UTC (the epoch was a Thursday).
const DERIVED = { "8h": ["4h", 28800, 0], "1w": ["1d", 604800, 4 * 86400] };
const STEP = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "8h": 28800, "1d": 86400, "1w": 604800 };
const WATCH_KEY = "watchlist-v1";
const loading = new Set();
let dlg = null;
let current = null;
let interval = "15m";
let tab = "signal";

// A window >= 1100px is a desktop terminal: the list stays on the left and
// the token page is a pane beside it, not a full-screen page over it.
const DESK = matchMedia("(width >= 1100px)");
export function isDesk() { return DESK.matches; }
const deskEmpty = document.createElement("div");
deskEmpty.className = "desk-empty";
deskEmpty.textContent = "Pick a token to see its chart and backtest";
document.body.append(deskEmpty);
DESK.addEventListener("change", () => { if (dlg?.open) dlg.close(); });

function markRows(id) {
  document.querySelectorAll("#app-shell [data-open][aria-current]").forEach((el) => el.removeAttribute("aria-current"));
  if (id) document.querySelectorAll("#app-shell [data-open]").forEach((el) => { if (el.dataset.open === id) el.setAttribute("aria-current", "true"); });
}

// Chains the wallet can swap on; the others (BSC, Base, Ethereum) are scanned, not traded here.
const TRADABLE = new Set(["solana", "robinhood"]);

/** entry: {kind, symbol, name, chain, token, pair, pairUrl, image, cap,
 *  change, ageTs, context, why, info, alerts: [alert_track items]} */
export function register(id, entry) {
  // Several lists know the same token (gems board, trophy, alerts): what
  // one adds (the dossier, the alerts) is kept when another re-registers.
  const old = registry.get(id) ?? {};
  const merged = { ...old };
  for (const [k, v] of Object.entries(entry)) if (v != null && !(Array.isArray(v) && !v.length && old[k]?.length)) merged[k] = v;
  registry.set(id, merged);
}

/** The wallet's own trade on a token, drawn on its chart as dots: every
 *  page registered for that token gets it. */
export function setOwn(chain, token, own) {
  const t = String(token ?? "").toLowerCase();
  for (const e of registry.values()) if (e.chain === chain && String(e.token ?? "").toLowerCase() === t) e.own = own;
}

const pct = (f) => (f == null ? "—" : `${f > 0 ? "+" : ""}${Math.round(f * 100)}%`);
const sign = (f) => (f == null ? "" : f >= 0 ? "up" : "down");

function watched() {
  try { return new Set(JSON.parse(localStorage.getItem(WATCH_KEY) || "[]")); } catch { return new Set(); }
}
function toggleWatch(id) {
  const w = watched();
  if (w.has(id)) w.delete(id); else w.add(id);
  try { localStorage.setItem(WATCH_KEY, JSON.stringify([...w])); } catch { /* private mode */ }
  return w.has(id);
}

function ensureDialog() {
  if (dlg) return dlg;
  dlg = document.createElement("dialog");
  dlg.className = "detail";
  dlg.setAttribute("aria-label", "Token");
  document.body.append(dlg);
  dlg.addEventListener("close", () => {
    disposeChart();
    document.documentElement.classList.remove("detail-open");
    markRows(null);
    if (history.state?.detail) history.back();
  });
  dlg.addEventListener("click", onClick);
  return dlg;
}

window.addEventListener("popstate", () => { if (dlg?.open && !history.state?.detail) dlg.close(); });

async function onClick(event) {
  const t = event.target;
  if (t.closest("[data-back]")) { dlg.close(); return; }
  const rc = t.closest("[data-deep-recheck]");
  if (rc) { deepRecheck(rc); return; }
  const iv = t.closest("[data-iv]");
  if (iv) { interval = iv.dataset.iv; current.picked = true; paintChart(); paintChips(); return; }
  const tb = t.closest("[data-dtab]");
  if (tb) { tab = tb.dataset.dtab; paintTabs(); return; }
  if (t.closest("[data-hide]")) {
    const e = current.e;
    dlg.close();
    hideToken(e.chain, e.token);
    return;
  }
  if (t.closest("[data-watch]")) {
    const on = toggleWatch(current.id);
    t.closest("[data-watch]").setAttribute("aria-pressed", String(on));
    return;
  }
  const shareBtn = t.closest("[data-share]");
  if (shareBtn) { shareToken(shareBtn, current.e); return; }
  const copyBtn = t.closest("[data-copy]");
  if (copyBtn) { copyAddress(dlg.querySelector(".dt-copy") ?? copyBtn, current.e.token); return; }
  if (t.closest("[data-user-swap]")) { openUserSwap(current.e); return; }
  if (t.closest("[data-buy]")) {
    const e = current.e;
    dlg.close();
    document.dispatchEvent(new CustomEvent("lae:buy", { detail: { chain: e.chain, address: e.token, symbol: e.symbol } }));
  }
}

// The token's on-chain address, whole, with a visible "Copied": the
// clipboard API is missing in some in-app browsers (Telegram), so a hidden
// textarea is the fallback.
// Share: the app's own link to this token, the same one the Telegram
// alerts use. Whoever opens it signed in lands on this token (Lorenzo, 29/09).
export function tokenLink(e) {
  const view = e.kind === "gem" ? "gems" : "meme";
  return `${location.origin}/onchain-app.html#${view}&token=${encodeURIComponent(e.chain)}:${encodeURIComponent(e.token)}`;
}
async function shareToken(btn, e) {
  if (!e.token) return;
  const url = tokenLink(e);
  if (navigator.share) {
    try { await navigator.share({ title: e.symbol, text: `${e.symbol} on DEGEN`, url }); return; } catch (err) {
      if (err?.name === "AbortError") return;
    }
  }
  copyAddress(btn, url, "Share");
}

async function copyAddress(btn, text, label = "Copy address") {
  if (!text) return;
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.append(ta);
    ta.select();
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
  }
  btn.dataset.copied = ok ? "yes" : "no";
  btn.setAttribute("aria-label", ok ? "Address copied" : "Copy failed");
  clearTimeout(btn._t);
  btn._t = setTimeout(() => { delete btn.dataset.copied; btn.setAttribute("aria-label", label); }, 1600);
}

// Telegram alerts link to #<view>&token=<chain>:<address>: open that
// token's page. The lists register their tokens as they load; one the
// lists do not show is built from its dossier.
export async function openFromHash() {
  const m = location.hash.match(/&token=([^:&]+):([^&]+)/);
  if (!m) return;
  const chain = decodeURIComponent(m[1]), token = decodeURIComponent(m[2]);
  history.replaceState(null, "", location.pathname + location.search + location.hash.split("&")[0]);
  const find = () => [...registry].find(([, e]) => e.chain === chain && String(e.token ?? "").toLowerCase() === token.toLowerCase())?.[0];
  const r = await api.getToken(chain, token).catch(() => null);
  let id = find();
  if (!id) {
    const p = r?.entry, a = r?.alerts?.[0];
    const kind = p ? "gem" : "meme";
    id = `${kind === "gem" ? "gem" : `alert:${kind}`}:${chain}:${token}`;
    register(id, {
      kind, chain, token, symbol: p?.symbol ?? a?.symbol ?? token.slice(0, 6), name: p?.name ?? a?.name,
      pair: p?.pair_address ?? a?.pair_address, pairUrl: p?.pair_url ?? a?.pair_url, image: p?.image ?? a?.image,
      cap: p?.market_cap ?? a?.cap0, ageTs: a?.at, alerts: r?.alerts ?? [],
    });
  }
  openDetail(id);
}

export function openDetail(id) {
  const e = registry.get(id);
  if (!e) return;
  current = { id, e };
  tab = "signal";
  interval = defaultInterval(e);
  const d = ensureDialog();
  const [a] = age(e.ageTs);
  d.innerHTML = `
    <header class="dt-bar">
      <button type="button" class="dt-back" data-back aria-label="Back"><svg viewBox="0 0 24 24"><path d="m15 5-7 7 7 7"/></svg></button>
      <span class="dt-crumbs"><b>${escapeHtml(e.symbol)}</b>${e.ageTs ? `<i></i><span class="age"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 14V7M8 9c0-3 2-4.5 5-4.5 0 3-2 4.5-5 4.5ZM8 8C8 5.5 6.3 4 3.5 4c0 2.6 1.7 4 4.5 4Z"/></svg>${a}</span>` : ""}</span>
      <span class="dt-actions">
        <button type="button" class="sq" data-hide aria-label="Hide from my lists"><svg viewBox="0 0 24 24"><path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 8.5 4 9.5 6-.5 1-1.6 2.6-3.3 3.9M6.2 7.6C4.3 8.9 3 10.8 2.5 12c1 2 4.5 6 9.5 6 1.5 0 2.9-.4 4.1-1M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg></button>
        <button type="button" class="sq" data-watch aria-pressed="${watched().has(id)}" aria-label="Watch"><svg viewBox="0 0 24 24"><path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.8 6.8 19.6l1-5.8L3.5 9.7l5.9-.8z"/></svg></button>
        <button type="button" class="sq" data-share aria-label="Share"><svg viewBox="0 0 24 24"><path d="M12 15V3.5M8 7.5l4-4 4 4M6 11H5v9.5h14V11h-1"/></svg></button>
      </span>
    </header>
    <div class="dt-scroll">
      <section class="dt-id">
        ${logo(e.symbol, e.image, e.chain, e.token).replace('class="logo"', 'class="logo big"')}
        <span class="dt-name"><b>${escapeHtml(e.name || e.symbol)}</b>
          <small><span class="dt-sym" data-copy role="button" tabindex="0" title="Copy address">${escapeHtml(e.symbol)}</span>
            <button type="button" class="ic dt-copy" data-copy aria-label="Copy address" title="${escapeHtml(e.token ?? "")}"><svg viewBox="0 0 24 24"><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V5H5v11h3"/></svg><span>${e.token ? `${escapeHtml(e.token.slice(0, 4))}…${escapeHtml(e.token.slice(-4))}` : ""}</span></button>
            ${e.pairUrl ? `<a class="ic" href="${escapeHtml(e.pairUrl)}" target="_blank" rel="noopener" aria-label="Open chart"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></svg></a>` : ""}
          </small></span>
        <span class="dt-mc"><small>MC</small><b>${usdShort(e.cap)}</b>${changePill(e.change)}</span>
      </section>
      ${e.context ? `<p class="dt-context">${e.context}</p>` : ""}
      <div class="dt-chart" data-chart><div class="dt-chart-empty">Loading chart…</div></div>
      <p class="dt-chart-note" data-chart-note></p>
      <div class="dt-chips" data-chips></div>
      <nav class="dt-tabs">
        <button type="button" data-dtab="signal">Signal</button>
        <button type="button" data-dtab="why">Why</button>
        <button type="button" data-dtab="info">Info</button>
      </nav>
      <div class="dt-pane" data-pane-body></div>
    </div>
    ${e.noTrade ? "" : `<div class="dt-cta" data-two="${Boolean(walletFor(e.chain) && TRADABLE.has(e.chain))}">${walletFor(e.chain) && TRADABLE.has(e.chain)
      ? `<button type="button" class="dt-mine" data-user-swap><svg viewBox="0 0 24 24"><rect x="3.5" y="6" width="17" height="13" rx="3"/><path d="M3.5 10h17M16 14.5h1.5"/></svg>My wallet</button>` : ""}${TRADABLE.has(e.chain)
      ? `<button type="button" data-buy><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v8M8.5 12.5 12 16l3.5-3.5"/></svg>Buy</button>`
      : `<a class="buy-out" href="${escapeHtml(e.pairUrl || `https://dexscreener.com/${e.chain}/${e.token}`)}" target="_blank" rel="noopener"><svg viewBox="0 0 24 24"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></svg>Trade on DexScreener</a>`}</div>`}`;
  paintChips();
  paintTabs();
  paintChart();
  markRows(id);
  if (!d.open) {
    document.documentElement.classList.add("detail-open");
    if (isDesk()) {
      // Non-modal: the list stays usable, and keeps the focus it had.
      const was = document.activeElement;
      d.show();
      if (was && was !== document.body) was.focus({ preventScroll: true });
      else d.querySelector(":focus")?.blur();
    }
    else {
      history.pushState({ detail: id }, "");
      d.showModal();
    }
  }
  d.querySelector(".dt-scroll").scrollTop = 0;
  if (e.chain && e.token) enrich(id, e);
}

// Desktop: the token lists (memes, gems, strategies) sit in a column with the
// token page beside them; the other views (agent, profile, wallet, swap)
// take the whole width, so the pane closes.
const LIST_VIEWS = new Set(["meme", "gems", "top"]);
export function deskView(name) {
  document.documentElement.dataset.deskList = String(LIST_VIEWS.has(name));
  if (!isDesk()) return;
  if (!LIST_VIEWS.has(name)) { if (dlg?.open) dlg.close(); return; }
  setTimeout(openFirst, 700);
}

// Desktop only: with nothing open, the first token of the view on screen.
export function openFirst() {
  if (document.documentElement.dataset.deskList === "false") return;
  if (!isDesk() || dlg?.open) return;
  const el = [...document.querySelectorAll("#app-shell [data-open]")].find((x) => x.offsetParent !== null);
  if (el) openDetail(el.dataset.open);
}

// The page fetches the full dossier itself: whichever list opened it (Top,
// Database, radar, memes) Signal, Why and Info must be complete (Lorenzo,
// 27/09). Only what is missing is filled, so a list that already knows more
// (the gem board, the alerts) is not overwritten.
async function enrich(id, e) {
  loading.add(id);
  const r = await api.getToken(e.chain, e.token).catch(() => null);
  loading.delete(id);
  if (!r) { if (current?.id === id) paintTabs(); return; }
  if (r.alerts?.length && !(e.alerts?.length)) e.alerts = r.alerts;
  // The H24 lab's backtest on the token's latest chart (strategy_lab.py):
  // every token of the Strategies tab has one, alert or not.
  if (r.backtests) e.lab = r.backtests;
  // The chart may have landed before the backtest: redraw it with the
  // strategy's arrows, on the backtest's own timeframe if none was picked.
  if (r.backtests && current?.id === id && dlg?.open) {
    const iv = labInterval(e);
    if (iv && !current.picked && iv !== interval) { interval = iv; paintChips(); }
    paintChart();
  }
  if (e.kind === "gem" && !e.slip) {
    api.getSlippage(e.chain, e.token).then((p) => { e.slip = p; if (current?.id === id) paintTabs(); }).catch(() => {});
  }
  if (r.deep) e.deep = r.deep;
  if (r.deep_wait_s != null) e.deepWait = r.deep_wait_s;
  if (r.entry) {
    const lb = await import("@onchain/leaderboard.js");
    if (e.deep) e.why = whyOf(lb, r.entry, e);
    else e.why ||= whyOf(lb, r.entry, e);
    e.info ||= lb.details(r.entry) + descriptionBlock(r.entry);
    e.dossier = r.entry;
  }
  if (current?.id === id) paintTabs();
}

// The Why tab of a dossier, with DeepSeek's dedicated re-check (Lorenzo,
// 01/10). A newer re-check replaces the board review's drop line.
function whyOf(lb, entry, e) {
  if (e.deep) {
    const clean = { ...entry, review: null, judge: entry.judge ? { ...entry.judge, strengths: [], problems: [], admit_reason: null } : entry.judge };
    return lb.gemWhy(clean);
  }
  const why = entry.why;
  const off = entry.on_board ? "" : `<p class="muted">Not on the board${why ? `: ${escapeHtml(why)}` : ""}.</p>`;
  return lb.gemWhy(entry) + off;
}

function ago(at) {
  if (!at) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - at));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function deepBlock(e) {
  const d = e.deep;
  const wait = e.deepWait ?? 0;
  let body = "";
  if (d) {
    // Every point carries the quote that backs it (#464); old results are plain strings.
    const why = (x) => (typeof x === "string" ? { text: x } : x ?? {});
    const quote = (q) => (q ? `<small>“${escapeHtml(q)}”</small>` : "");
    const row = (tone, label, x) => `<li data-tone="${tone}">${label}${escapeHtml(why(x).text ?? "")}${quote(why(x).evidence || why(x).note)}</li>`;
    const head = d.unsupported ? row("warn", "Unverified: ", { text: d.reason })
      : row(d.keep ? "good" : "bad", d.keep ? "Kept: " : "Dropped: ", { text: d.reason });
    const claims = d.claims ?? [];
    const tone = { refuted: ["good", "Cleared: "], confirmed: ["bad", ""], unverified: ["warn", "Unverified: "] };
    const count = (k) => (claims.length ? claims.filter((c) => c.status === k).length : d[k === "refuted" ? "cleared" : k] ?? 0);
    body = `<ul class="why-list">${head}
      ${(d.strengths ?? []).map((x) => row("good", "", x)).join("")}
      ${(d.problems ?? []).map((x) => row("bad", "", x)).join("")}
      ${claims.map((c) => row(tone[c.status]?.[0] ?? "warn", tone[c.status]?.[1] ?? "", { text: c.text, evidence: c.evidence || c.note })).join("")}
    </ul>
    <p class="muted">${count("refuted")} cleared · ${count("unverified")} unverified · ${count("confirmed")} confirmed · ${escapeHtml(ago(d.at))}</p>`;
  }
  let label = "Re-check with DeepSeek";
  let disabled = "";
  if (e.deepBusy) { disabled = " disabled"; label = "Re-checking…"; }
  else if (wait > 0) {
    disabled = " disabled";
    label = wait >= 3600 ? `Re-check in ${Math.round(wait / 3600)}h` : `Re-check in ${Math.max(1, Math.round(wait / 60))}m`;
  }
  return `<div class="ds-deep"><h5>Deep re-check${d ? ` <b>${escapeHtml(d.verdict ?? "")} · ${d.score ?? "—"}/100</b>` : ""}</h5>
    ${body}
    <button class="ds-recheck" type="button" data-deep-recheck${disabled}>${label}</button></div>`;
}

async function deepRecheck(btn) {
  const cur = current;
  const e = cur?.e;
  if (!e?.dossier || btn.disabled) return;
  e.deepBusy = true;
  btn.disabled = true;
  btn.textContent = "Re-checking…";
  try {
    const r = await api.recheckToken(e.chain, e.token);
    e.deep = r.deep;
    e.deepWait = 6 * 3600;
  } catch (err) {
    if (err?.detail?.wait_s) e.deepWait = err.detail.wait_s;
    else {
      btn.textContent = "Re-check failed";
      setTimeout(() => { e.deepBusy = false; btn.textContent = "Re-check with DeepSeek"; btn.disabled = false; }, 4000);
      return;
    }
  } finally {
    if (e.deep || e.deepWait) e.deepBusy = false;
  }
  const lb = await import("@onchain/leaderboard.js");
  e.why = whyOf(lb, e.dossier, e);
  if (current === cur) {
    const body = dlg.querySelector("[data-pane-body]");
    if (dlg.open && tab === "why" && body) {
      await morph(body, whyHtml(e));
    } else {
      paintTabs();
    }
  }
}

function descriptionBlock(p) {
  const links = [["Website", p.website], ["GitHub", p.github],
                 ...(p.socials ?? []).map((s) => [s.type, s.url])]
    .filter(([, url]) => url)
    .map(([label, url]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`);
  if (!p.description && !links.length) return "";
  return `${p.description ? `<p class="bd-summary">${escapeHtml(p.description)}</p>` : ""}${links.length ? `<p class="dt-links">${links.join("")}</p>` : ""}`;
}

function paintChips() {
  dlg.querySelector("[data-chips]").innerHTML = INTERVALS.map((iv) =>
    `<button type="button" data-iv="${iv}"${iv === interval ? ' aria-pressed="true"' : ""}>${iv}</button>`).join("");
}

// The system's own trades on one alert, each with why it sold. A sell
// after a crash inside one candle says so: that loss was not a late exit.
const WHY = { signal: "trend turned", trail: "fell 30% from its peak", crash: "crashed inside one 5-min candle: first price to sell at", "48h": "48h window over", rug: "pool vanished while in" };
function systemTrades(al) {
  const sy = bestSystem(al);
  const ev = sy?.events ?? [];
  if (!ev.length) return sy ? `<p class="sys-none">System: ${sy.state === "in" ? "in the trade" : "no entry yet"}</p>` : "";
  const t0 = al.at;
  return `<ol class="sys-trades">${ev.map((v) => {
    const after = Math.max(0, Math.round((v.t - t0) / 60));
    const when = after < 90 ? `${after} min` : `${Math.round(after / 60)}h`;
    if (v.side === "buy") return `<li data-side="buy"><b>Bought</b><span>${when} after the alert</span></li>`;
    const why = WHY[v.why] ?? "sold";
    return `<li data-side="sell" data-why="${escapeHtml(v.why ?? "")}"><b data-sign="${sign(v.ret)}">Sold ${pct(v.ret)}</b><span>${when} after · ${why}${v.why === "crash" && v.drop != null ? ` (${pct(v.drop)} in one candle)` : ""}</span></li>`;
  }).join("")}</ol>`;
}

function whyHtml(e) {
  const pending = loading.has(current.id);
  return (e.why || `<p class="muted">${pending ? "Loading…" : "Nothing to add yet."}</p>`) + (e.dossier ? deepBlock(e) : "");
}

function paintTabs() {
  dlg.querySelectorAll("[data-dtab]").forEach((b) => b.toggleAttribute("aria-current", b.dataset.dtab === tab));
  const e = current.e;
  const body = dlg.querySelector("[data-pane-body]");
  const pending = loading.has(current.id);
  if (tab === "why") { body.innerHTML = whyHtml(e); return; }
  if (tab === "info") { body.innerHTML = e.info || infoFallback(e); return; }
  // Gems: only the daily trend follower, not the alerts' 5-minute tracks.
  const gem = e.kind === "gem";
  // The same backtest the Top list shows (lab-shown.js), so the table, the
  // number in the list and the arrows on the chart are the same trades.
  const shown = gem ? shownTrades(e.lab) : null;
  const lab = gem ? (shown?.backtests ?? (e.lab?.daily?.rows?.length ? e.lab.daily : null)) : e.lab;
  const labNote = !gem ? "Backtested on the latest 5-minute chart, every hour."
    : shown?.source === "fast" ? "Best backtest on the latest 5-minute chart, as in the Top list: the trend follower has no trade on the longer charts yet."
    : `Trend follower backtested on the ${IV_NAME[lab?.interval] ?? "daily"} chart: from the longest one with 60 closed candles down to the first where it enters.`;
  const alerts = gem ? [] : (e.alerts ?? []);
  body.innerHTML = alerts.length ? alerts.map((al) => `
    <article class="call">
      <p><b>Telegram alert</b><small>${age(al.at)[0]} ago</small></p>
      <div class="call-card">
        <span><small>At alert</small><b>${usdShort(al.cap0)} MC</b></span>
        <span><small>Held now</small>${changePill(al.now)}</span>
        <span><small>Best 48h</small><b data-sign="${sign(al.max)}">${pct(al.max)}</b></span>
        <span><small>System</small><b data-sign="${sign(bestSystem(al)?.ret)}">${bestSystem(al)?.ret == null ? "—" : pct(bestSystem(al).ret)}</b></span>
      </div>
      <div class="strats">${bestSystem(al) ? strategyTrade(bestSystem(al), al.at) : ""}</div>
      ${systemTrades(al)}
      ${backtestTable(al)}
      <p class="hours-head">Just holding, from the alert</p>
      <ol class="hours">${[1, 2, 3, 4, 5, 6, 8, 12, 24, 48].map((h) => {
        const r = al.returns?.[String(h)];
        return `<li><small>${h}h</small><b data-sign="${sign(r)}">${pct(r)}</b></li>`;
      }).join("")}</ol>
    </article>`).join("")
    : `<dl class="kv">
        ${e.dossier ? `<div><dt>Score</dt><dd>${e.dossier.score != null ? Math.round(e.dossier.score) : "—"}</dd></div>
        <div><dt>Board</dt><dd>${e.dossier.on_board ? "on the board" : e.rank ? `Top #${e.rank}, from the database` : `not ranked${e.dossier.why ? `: ${escapeHtml(e.dossier.why)}` : ""}`}</dd></div>` : ""}
        <div><dt>Market cap</dt><dd>${usdShort(e.cap)}</dd></div>
        ${e.dossier?.liquidity_usd != null ? `<div><dt>Liquidity</dt><dd>${usdShort(e.dossier.liquidity_usd)}</dd></div>` : ""}
        ${slipRows(e.slip)}
        ${e.ageTs ? `<div><dt>Age</dt><dd>${age(e.ageTs)[0]}</dd></div>` : ""}
      </dl>
      ${lab ? `${backtestTable({ backtests: lab, age_h: 99 })}<p class="muted">${labNote}</p>` : ""}
      ${gem && lab ? "" : `<p class="muted">${pending ? "Loading…" : gem ? "No daily backtest on this gem yet." : "No Telegram alert on this token yet."}</p>`}`;
}

// Lorenzo, 30/09: before an entry, how much the pool would slip on the
// wallet's next stake, and the largest stake under the limit.
function slipRows(p) {
  if (!p || p.max_usd == null) return "";
  const src = p.source === "quote" ? "DEX quote" : "pool depth";
  return `<div><dt>Slippage on ${usdShort(p.usd)}</dt><dd data-sign="${p.slip_pct > p.max_slippage_pct ? "down" : ""}">${p.slip_pct.toFixed(2)}% · ${src}</dd></div>
        <div><dt>Max entry at ${p.max_slippage_pct}%</dt><dd>${usdShort(p.max_usd)}</dd></div>`;
}

function backtestTable(al) {
  const rows = al.backtests?.rows ?? [];
  if (!rows.length) return "";
  const best = rows.filter((r) => r.trades > 0)
    .sort((a, b) => (b.return_pct ?? -1e9) - (a.return_pct ?? -1e9))[0];
  const hold = al.backtests.hold_pct;
  return `<table class="bt-table">
    <caption>Best backtest on this chart</caption>
    <thead><tr><th></th><th>Return</th><th>Trades</th><th>Win</th><th>Max DD</th></tr></thead>
    <tbody>${best ? `<tr>
      <td>Best</td>
      <td data-sign="${best.return_pct == null ? "" : sign(best.return_pct / 100)}">${best.return_pct == null ? "—" : pct(best.return_pct / 100)}</td>
      <td>${best.trades ?? "—"}</td>
      <td>${best.win_rate == null ? "—" : `${Math.round(best.win_rate)}%`}</td>
      <td>${best.max_dd == null ? "—" : `${best.max_dd.toFixed(1)}%`}</td>
    </tr>` : `<tr><td colspan="5">${(al.age_h ?? 0) < 48 ? "No entry so far" : "Stayed out: no entry on this chart"}${al.now != null && al.now < 0 ? `, dodged a ${Math.round(-al.now * 100)}% drop` : ""}</td></tr>`}
    <tr>
      <td>Just holding</td>
      <td data-sign="${hold == null ? "" : sign(hold / 100)}">${hold == null ? "—" : pct(hold / 100)}</td>
      <td>—</td><td>—</td><td>—</td>
    </tr></tbody>
  </table>`;
}

function infoFallback(e) {
  return `<dl class="kv">
    <div><dt>Chain</dt><dd>${escapeHtml(e.chain)}</dd></div>
    <div><dt>Contract</dt><dd class="mono">${escapeHtml(e.token ?? "—")}</dd></div>
    ${e.pairUrl ? `<div><dt>Chart</dt><dd><a href="${escapeHtml(e.pairUrl)}" target="_blank" rel="noopener">DexScreener</a></dd></div>` : ""}
  </dl>`;
}

// Charts. Lorenzo: "il grafico impiega almeno 10 secondi ad aprirsi" and
// "in alcuni ticker non c'e' grafico proprio". GeckoTerminal is the only free
// source of candles and allows ~30 calls a minute per IP: through our server
// every viewer and every background job shared one quota. Now three sources
// race and the first with a line wins:
//   1. the server's copy, if it already has one (peek: never calls anyone);
//   2. GeckoTerminal straight from the browser, on the viewer's own quota;
//   3. the server fetching it, only if both above came back empty.
// No pool known (some gems) -> DexScreener says which is the token's main
// pool. Still no candles -> DexScreener's own chart, embedded.
const charts = new Map();   // "chain:pair:interval" -> {at, promise, candles}
const pools = new Map();    // "chain:token" -> pair address (or "" if none)
const CHART_FRESH_MS = 30_000;
const GECKO = "https://api.geckoterminal.com/api/v2";
const GECKO_NET = { solana: "solana", robinhood: "robinhood", base: "base", bsc: "bsc", ethereum: "eth" };
const GECKO_TF = { "1m": ["minute", 1], "5m": ["minute", 5], "15m": ["minute", 15], "1h": ["hour", 1], "4h": ["hour", 4],
                   "1d": ["day", 1] };
const DS_EMBED = new Set(["solana", "base", "bsc", "ethereum"]);   // chains DexScreener charts

async function fetchJson(url, ms) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await fetch(url, { signal: ctl.signal, headers: { Accept: "application/json" }, referrerPolicy: "no-referrer" });
    if (!r.ok) throw new Error(String(r.status));
    return await r.json();
  } finally { clearTimeout(t); }
}

async function poolOf(e) {
  if (e.pair) return e.pair;
  const k = `${e.chain}:${e.token}`;
  if (pools.has(k)) return pools.get(k);
  let pair = "";
  try {
    const rows = await fetchJson(`https://api.dexscreener.com/tokens/v1/${e.chain}/${e.token}`, 4000);
    const best = (Array.isArray(rows) ? rows : []).sort((x, y) => (y.liquidity?.usd ?? 0) - (x.liquidity?.usd ?? 0))[0];
    pair = best?.pairAddress ?? "";
  } catch { /* try GeckoTerminal below */ }
  // DexScreener does not index every chain: ask GeckoTerminal for the first
  // pool of this token when it has nothing (or fails).
  if (!pair) {
    const net = GECKO_NET[e.chain];
    if (net) {
      try {
        const d = await fetchJson(`${GECKO}/networks/${net}/tokens/${e.token}/pools?page=1`, 4000);
        pair = d?.data?.[0]?.attributes?.address ?? "";
      } catch { /* no pool found: the page says so */ }
    }
  }
  if (pair) { pools.set(k, pair); e.pair = pair; }
  return pair;
}

async function geckoDirect(e, pair, iv) {
  const net = GECKO_NET[e.chain];
  const [tf, agg] = GECKO_TF[iv] ?? GECKO_TF["5m"];
  if (!net) throw new Error("chain");
  const q = new URLSearchParams({ aggregate: agg, limit: 1000, currency: "usd", ...(e.token ? { token: e.token } : {}) });
  const d = await fetchJson(`${GECKO}/networks/${net}/pools/${pair}/ohlcv/${tf}?${q}`, 6000);
  const rows = d?.data?.attributes?.ohlcv_list ?? [];
  return rows.map((r) => ({ time: r[0], open: Number(r[1]), high: Number(r[2]), low: Number(r[3]), close: Number(r[4]) }))
    .filter((c) => Number.isFinite(c.close)).sort((a, b) => a.time - b.time);
}

const clean = (d) => (d?.candles ?? []).filter((c) => Number.isFinite(c.close));
const enough = (c) => { if (c.length < 2) throw new Error("empty"); return c; };

function chartFor(e, iv) {
  if (DERIVED[iv]) {
    const [base, step, offset] = DERIVED[iv];
    return chartFor(e, base).then((c) => aggregate(c, step, offset));
  }
  const k = `${e.chain}:${e.pair || e.token}:${iv}`;
  const hit = charts.get(k);
  if (hit && Date.now() - hit.at < CHART_FRESH_MS) return hit.promise;
  const promise = (async () => {
    const pair = await poolOf(e);
    if (!pair) return [];
    const peek = api.getTokenChart(e.chain, pair, e.symbol, iv, true).then(clean).then(enough);
    const direct = geckoDirect(e, pair, iv).then(enough);
    try {
      return await Promise.any([peek, direct]);
    } catch {
      return clean(await api.getTokenChart(e.chain, pair, e.symbol, iv));
    }
  })().then((candles) => { charts.set(k, { at: Date.now(), promise: Promise.resolve(candles), candles }); return candles; },
    (err) => { if (charts.get(k)?.promise === promise) charts.delete(k); throw err; });
  charts.set(k, { at: Date.now(), promise, candles: hit?.candles });
  if (charts.size > 60) charts.delete(charts.keys().next().value);
  return promise;
}

// Gems open on the timeframe their trend follower ran on (strategy_lab's
// ladder, 5m to 1d). The rungs with no chip open on the widest chip below
// them: every trade still lands on the bar at or before it.
const IV_NAME = { "5m": "5-minute", "15m": "15-minute", "30m": "30-minute", "1h": "1-hour", "2h": "2-hour",
                  "4h": "4-hour", "6h": "6-hour", "8h": "8-hour", "12h": "12-hour", "1d": "daily" };
const CHIP_OF = { "30m": "15m", "2h": "1h", "6h": "4h", "12h": "8h" };
const labInterval = (e) => {
  const iv = e.kind === "gem" ? shownTrades(e.lab)?.interval ?? e.lab?.daily?.interval : null;
  return iv ? CHIP_OF[iv] ?? iv : null;
};
const defaultInterval = (e) => labInterval(e) ?? (e.kind === "gem" ? "1d" : "5m");

function aggregate(candles, step, offset) {
  const out = [];
  for (const c of candles) {
    const t = Math.floor((c.time - offset) / step) * step + offset;
    const cur = out[out.length - 1];
    if (cur && cur.time === t) {
      cur.high = Math.max(cur.high, c.high); cur.low = Math.min(cur.low, c.low); cur.close = c.close;
    } else out.push({ time: t, open: c.open, high: c.high, low: c.low, close: c.close });
  }
  return out;
}

export function prefetchChart(id) {
  const e = registry.get(id);
  if (e?.pair || e?.token) chartFor(e, defaultInterval(e)).catch(() => {});
}

function embed(e) {
  if (!e.pair || !DS_EMBED.has(e.chain)) return `<div class="dt-chart-empty">No trades to chart yet.</div>`;
  const iv = { "1m": "1", "5m": "5", "15m": "15", "1h": "60", "4h": "240" }[interval] ?? "5";
  const src = `https://dexscreener.com/${e.chain}/${e.pair}?embed=1&loadChartSettings=0&trades=0&tabs=0&info=0&chartLeftToolbar=0&chartTheme=dark&theme=dark&chartStyle=2&chartType=marketCap&interval=${iv}`;
  return `<iframe class="dt-embed" src="${escapeHtml(src)}" title="${escapeHtml(e.symbol)} chart on DexScreener" loading="lazy" referrerpolicy="no-referrer"></iframe>`;
}

async function paintChart() {
  const e = current.e;
  const box = dlg.querySelector("[data-chart]");
  if (!e.pair && !e.token) { box.innerHTML = `<div class="dt-chart-empty">No pool to chart.</div>`; return; }
  const want = `${current.id}:${interval}`;
  box.dataset.want = want;
  const [base, step, offset] = DERIVED[interval] ?? [interval];
  let stale = charts.get(`${e.chain}:${e.pair || e.token}:${base}`)?.candles;
  if (stale && step) stale = aggregate(stale, step, offset);
  if (stale?.length >= 2) await drawChart(box, stale, e, interval);
  let candles = [];
  let shown = interval;
  // An interval with no candles (1m on a quiet pool) steps up to the next
  // one instead of DexScreener's embed, which knows nothing of our entries
  // and exits (Lorenzo, 28/09: "sul grafico del minuto si apre dex
  // screener, ma poi perdo i dati di ingresso e uscita"). The embed stays
  // for tokens with no candles at all.
  for (const iv of INTERVALS.slice(INTERVALS.indexOf(interval))) {
    try { candles = await chartFor(e, iv); } catch { candles = []; }
    if (box.dataset.want !== want) return;
    if (candles.length >= 2) { shown = iv; break; }
  }
  if (candles.length < 2) { if (!stale) { disposeChart(); box.innerHTML = embed(e); } return; }
  const drawn = await drawChart(box, candles, e, shown);
  // A chart too short for the listed trades (300 candles of 5 minutes are
  // one day, the backtest's are three and a half) steps up until every
  // arrow fits, unless the viewer picked the timeframe.
  const expected = e.kind === "gem" ? (shownTrades(e.lab)?.events ?? []).length : 0;
  const next = INTERVALS[INTERVALS.indexOf(shown) + 1];
  if (!current.picked && drawn < expected && next && candles[0].time > Math.min(...shownTrades(e.lab).events.map((v) => v.t))) {
    interval = next;
    paintChips();
    return paintChart();
  }
  const note = box.parentElement?.querySelector("[data-chart-note]");
  if (note) {
    if (shown !== interval) note.textContent = `No ${interval} candles for this token: showing ${shown}.`;
    else if (lw?.before) note.textContent = `${lw.before} strategy ${lw.before === 1 ? "entry or exit is" : "entries and exits are"} older than this chart: pick a longer timeframe.`;
    else if (drawn === 0) note.textContent = e.kind === "gem" && shownTrades(e.lab) ? "" : "The strategy stayed out of this chart.";
    else note.textContent = "";
  }
}

// The main site's engine (Lorenzo, 28/09: "sti ingressi e uscite nel grafico
// a linea fanno schifo, dovevamo usare il motore del sito principale"):
// TradingView Lightweight Charts, candles in market-cap terms, the alert and
// the system's buys and sells as markers on the bars. Falls back to the old
// line only if the library cannot load.
let lw = null;   // { chart, series, box }

function disposeChart() {
  if (lw) { try { lw.chart.remove(); } catch { /* already gone */ } lw = null; }
}

function chartLib() {
  if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
  const tag = document.querySelector('script[src*="lightweight-charts"]');
  if (!tag) return Promise.reject(new Error("chart library missing"));
  return new Promise((resolve, reject) => {
    tag.addEventListener("load", () => resolve(window.LightweightCharts), { once: true });
    tag.addEventListener("error", () => reject(new Error("chart library failed")), { once: true });
  });
}

async function drawChart(box, candles, e, iv = interval) {
  let LC;
  try { LC = await chartLib(); } catch { disposeChart(); box.innerHTML = svgChart(candles, e); return; }
  const last = candles[candles.length - 1].close;
  const k = e.cap && last ? e.cap / last : 1;         // prices shown as market cap
  const bars = candles.map((c) => {
    const close = c.close * k;
    const open = (Number.isFinite(c.open) ? c.open : c.close) * k;
    return { time: c.time, open, close,
      high: Math.max(open, close, (Number.isFinite(c.high) ? c.high : c.close) * k),
      low: Math.min(open, close, (Number.isFinite(c.low) ? c.low : c.close) * k) };
  });
  if (!lw || lw.box !== box || !box.contains(lw.chart.chartElement?.() ?? box.firstChild)) {
    disposeChart();
    box.innerHTML = "";          // also drops a failed main-site iframe
    const chart = LC.createChart(box, {
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#9aa0ab", fontSize: 11, attributionLogo: false },
      grid: { vertLines: { color: "rgba(255,255,255,0.03)" }, horzLines: { color: "rgba(255,255,255,0.03)" } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.08 } },
      // Room on the right: the last marker's label ("Wallet out +16%") was
      // cut at the edge (Lorenzo, 30/09, MERRYMEN 1d).
      timeScale: { timeVisible: true, secondsVisible: false, borderVisible: false, rightOffset: 8 },
      crosshair: { vertLine: { color: "#5f6672" }, horzLine: { color: "#5f6672" } },
      handleScale: { axisPressedMouseMove: false },
    });
    const series = chart.addCandlestickSeries({
      upColor: "#2de0a8", downColor: "#f87171", borderVisible: false,
      wickUpColor: "#2de0a8", wickDownColor: "#f87171",
      // Market cap when the cap is known; otherwise the raw price, which
      // usdShort rounded to "$0" (HYDX at $0.00038, 30/09).
      // A price is never below zero: the axis showed "$-100000" under a
      // chart starting near zero.
      priceFormat: { type: "custom", minMove: 1e-10,
        formatter: (v) => (v < 0 ? "" : lw?.k === 1 && Math.abs(v) < 1 ? `$${Number(v.toPrecision(3))}` : usdShort(v)) },
      autoscaleInfoProvider: (original) => {
        const r = original();
        if (r?.priceRange) r.priceRange.minValue = Math.max(0, r.priceRange.minValue);
        return r;
      },
    });
    lw = { chart, series, box };
  }
  lw.k = k;
  lw.series.setData(bars);
  // A marker sits on a bar: each event goes to the last bar at or before it.
  // The last bar's own span: a trade inside today's daily candle, or on a
  // finer timeframe than the chart, still belongs on it (the old limit of
  // one hour dropped every 8-hour trade after 1:00 on the daily chart).
  const t0 = bars[0].time, t1 = bars[bars.length - 1].time + (STEP[iv] ?? 3600);
  const barAt = (t) => { let b = bars[0].time; for (const x of bars) { if (x.time <= t) b = x.time; else break; } return b; };
  const marks = [];
  let drawn = 0;
  // Gems say which strategy trade it is and on which timeframe it was
  // taken, on every chip (Lorenzo, 30/09: "la strategia se e' in
  // classifica top 50 deve essere applicata al grafico e la devo vedere").
  const gemIv = e.kind === "gem" ? shownTrades(e.lab)?.interval : null;
  let before = 0;
  for (const a of withLab(e)) {
    for (const v of tradesOf(a)) {
      if (v.t < t0) { before++; continue; }
      if (v.t >= t1) continue;
      const buy = v.side === "buy";
      const text = gemIv
        ? `${buy ? "Strategy entry" : "Strategy exit"} · ${gemIv}${!buy && v.ret != null ? ` ${pct(v.ret)}` : ""}`
        : buy ? "Buy" : v.ret != null ? `Sell ${pct(v.ret)}` : "Sell";
      marks.push({ time: barAt(v.t), position: buy ? "belowBar" : "aboveBar", color: buy ? "#2de0a8" : "#f87171",
        shape: buy ? "arrowUp" : "arrowDown", text });
      drawn++;
    }
    const rug = a.backtests?.rug?.at;
    if (rug != null && rug >= t0 && rug < t1) {
      // A fall of 85% stops the strategy but is not a rug (HYDX, 30/09).
      const tag = a.backtests.rug.kind === "fall" || /from its top/.test(a.backtests.rug.why ?? "") ? "Stop" : "Rug";
      const why = a.backtests?.rug?.why ? `${tag} · ${a.backtests.rug.why}`.slice(0, 40) : `${tag} · strategy off`;
      marks.push({ time: barAt(rug), position: "aboveBar", color: "#f87171", shape: "square", text: why });
    }
  }
  // The wallet's own trade on this token (a position opened from the
  // profile): Lorenzo, 30/09, "le posizioni aperte e chiuse se ci clicco
  // voglio vedere quali sono stati graficamente".
  for (const v of e.own ?? []) {
    if (v.t == null || v.t < t0 || v.t >= t1) continue;
    const buy = v.side === "buy";
    marks.push({ time: barAt(v.t), position: buy ? "belowBar" : "aboveBar", color: "#f0b90b", shape: "circle",
      // The simulated wallet's trades are the strategy's: they say so, with
      // the timeframe that took them (Lorenzo, 30/09: "scritto ingresso
      // strategia [...] con un time frame chiaro").
      text: v.iv
        ? `${buy ? "Strategy entry" : "Strategy exit"} · ${v.iv}${walletStrategy(v.strategy) ? ` · ${walletStrategy(v.strategy)}` : ""}${!buy && v.ret != null ? ` ${pct(v.ret)}` : ""}`
        : buy ? "Wallet in" : v.ret != null ? `Wallet out ${pct(v.ret)}` : "Wallet out" });
    drawn++;
  }
  marks.sort((x, y) => x.time - y.time);
  lw.series.setMarkers(marks);
  lw.chart.timeScale().fitContent();
  lw.before = before;
  return drawn;
}

/** The trades drawn on the chart: the best backtest's, the one the ranking
 *  shows (Lorenzo, 28/09: "qui deve comparire la strategia [...] ingressi e
 *  uscite"); the 48h system's until the backtest is stored. */
function withLab(e) {
  // Gems: the site's trend follower on the daily chart, never the fast
  // 5-minute strategies (Lorenzo, 28/09: "la strategia da applicare non e'
  // quella sul minuto ma [...] il trend follower originario [...] sul
  // giornaliero [...] al massimo 8 ore").
  // The trades behind the number the Top list shows (lab-shown.js): the
  // trend follower's when it traded, else the 5-minute best's. Lorenzo,
  // 30/09 (MERRYMEN): "+400% in classifica [...] ma non vedo niente".
  if (e.kind === "gem") {
    const shown = shownTrades(e.lab);
    if (shown) return [{ backtests: { best: { events: shown.events }, rug: shown.rug } }];
    return e.lab?.daily?.rows ? [{ backtests: e.lab.daily }] : [];
  }
  return (e.alerts?.length || !e.lab) ? (e.alerts ?? []) : [{ backtests: e.lab }];
}

function tradesOf(a) {
  // An alert: its one trade, in at the alert and out on the trend follower's
  // exit (Lorenzo, 28/09: "troppi ingressi"), not the re-entering backtest.
  if (a.on_alert?.events?.length) return a.on_alert.events;
  const best = a.backtests?.best?.events;
  return best?.length ? best : bestSystem(a)?.events ?? [];
}

/** A line of the market cap (price scaled by the current cap), right axis,
 *  dashed "now" line with its tag, and alert markers on the line. */
function svgChart(candles, e) {
  const W = 360, H = 260, R = 58, T = 14, B = 18;
  const last = candles[candles.length - 1].close;
  const k = e.cap && last ? e.cap / last : 1;
  const ys = candles.map((c) => c.close * k);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi === lo) { hi *= 1.05; lo *= 0.95; }
  const pad = (hi - lo) * 0.08;
  lo -= pad; hi += pad;
  const t0 = candles[0].time, t1 = candles[candles.length - 1].time;
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (W - R);
  const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const d = candles.map((c, i) => `${i ? "L" : "M"}${x(c.time).toFixed(1)},${y(ys[i]).toFixed(1)}`).join("");
  const ticks = Array.from({ length: 5 }, (_, i) => lo + ((hi - lo) * i) / 4);
  const now = ys[ys.length - 1];
  // The system's own trades on this token: green up for a buy, red down for a sell.
  const trades = withLab(e).flatMap(tradesOf).filter((v) => v.t >= t0 && v.t <= t1).map((v) => {
    const i = candles.findIndex((c) => c.time >= v.t);
    const yv = y(ys[Math.max(0, i)]);
    const buy = v.side === "buy";
    return `<path class="tr ${buy ? "buy" : "sell"}" transform="translate(${x(v.t).toFixed(1)},${(yv + (buy ? 12 : -12)).toFixed(1)})" d="${buy ? "M0-5 5 3H-5z" : "M0 5 5-3H-5z"}"><title>System ${buy ? "buy" : "sell"}</title></path>`;
  }).join("");
  const up = ys[ys.length - 1] >= ys[0];
  return `<svg viewBox="0 0 ${W} ${H}" class="line-chart" data-sign="${up ? "up" : "down"}" role="img" aria-label="Market cap chart">
    ${ticks.map((v) => `<text x="${W - 4}" y="${y(v) + 4}" text-anchor="end">${usdShort(v)}</text>`).join("")}
    <line class="now" x1="0" x2="${W - R}" y1="${y(now)}" y2="${y(now)}"/>
    <path class="ln" d="${d}"/>
    <circle class="dot" cx="${x(t1)}" cy="${y(now)}" r="4"/>${trades}
    <g class="tag" transform="translate(${W - R + 4},${y(now) - 10})"><path d="M0 10 6 0h50v20H6z"/><text x="31" y="14" text-anchor="middle">${usdShort(now)}</text></g>
  </svg>`;
}

// Any row anywhere with data-open="<id>" opens the page.
// The finger is on the row: start the chart now, it lands while the page opens.
document.addEventListener("pointerdown", (event) => {
  const b = event.target.closest?.("[data-open]");
  if (b && registry.has(b.dataset.open)) prefetchChart(b.dataset.open);
}, { passive: true });
document.addEventListener("click", (event) => {
  const b = event.target.closest("[data-open]");
  if (!b || !registry.has(b.dataset.open)) return;
  event.preventDefault();
  openDetail(b.dataset.open);
});
