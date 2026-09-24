/**
 * Il pilota: la vista principale del wallet.
 *
 * Richiesta: "voglio sapere cosa sta monitorando, voglio vedere cosa sta
 * scansionando [...] poca confusione a schermo, pochi tasti". Un solo
 * comando (l'interruttore), una riga che dice se sta lavorando e quando ha
 * guardato l'ultima volta, e sotto i token che segue, ognuno con il suo
 * stato in parole. Toccando un token o la riga di stato si apre la diagnosi.
 *
 * Tutto viene da GET /api/onchain/pilot/{chain}: questo modulo non deduce
 * niente da solo, disegna quello che il server sa.
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { escapeHtml, formatUsd, strategyLabel } from "@onchain/format.js";
import { chainInfo, state } from "@onchain/store.js";

const ROOT = document.getElementById("onchain-embed") ?? document;
const $ = (selector) => ROOT.querySelector(selector);

const POLL_MS = 15_000;

let root = null;
let sheet = null;
let data = null;
let pollTimer = null;
let clockTimer = null;
let onChange = null;
let onToast = null;
let busy = false;

const ago = (ts) => {
  if (!ts) return null;
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};
const until = (ts) => {
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "any moment";
  return s < 60 ? `in ${s}s` : `in ${Math.round(s / 60)} min`;
};
const money = (v) => (v == null ? "—" : formatUsd(v));
// Limiti e budget sono cifre tonde: "$50", non "$50.00".
const whole = (v) => (v == null ? "—" : `$${Math.round(v).toLocaleString("en-US")}`);
const compact = (v) => {
  if (v == null) return "—";
  if (v < 1000) return `$${Math.round(v)}`;
  const k = v / 1000;
  return `$${k >= 10 || Number.isInteger(k) ? Math.round(k) : k.toFixed(1)}k`;
};
const hours = (h) => (h == null ? "—" : h < 1 ? `${Math.round(h * 60)} min` : `${Math.round(h)} h`);

/* ------------------------------------------------------------------ montaggio */

export function mountPilot({ onChange: changed, toast } = {}) {
  root = $("#pilot");
  sheet = $("#pilot-sheet");
  if (!root) return;  // la copia del wallet nella tab Trading non ha il pilota
  onChange = changed ?? null;
  onToast = toast ?? null;

  root.addEventListener("click", (event) => {
    if (event.target.closest("[data-pilot-toggle]")) return toggle();
    const row = event.target.closest("[data-token]");
    if (row) return openToken(row.dataset.token);
    if (event.target.closest("[data-pilot-status]")) return openHealth();
  });
  sheet?.addEventListener("click", (event) => {
    if (event.target === sheet || event.target.closest("[data-close]")) sheet.close();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshPilot();
  });
}

export async function refreshPilot() {
  if (!root) return;
  clearTimeout(pollTimer);
  const chain = state.chain;
  try {
    const next = await api.getPilot(chain);
    if (chain !== state.chain) return;
    data = next;
    paint();
  } catch (error) {
    if (chain !== state.chain) return;
    data = null;
    root.querySelector(".pilot-card").innerHTML =
      `<p class="pilot-error">${escapeHtml(error instanceof ApiError ? error.message : "Can't read the autopilot.")}</p>`;
  }
  // Si aggiorna da solo solo mentre si guarda: in background non serve.
  if (document.visibilityState === "visible" && !root.closest("[hidden]")) {
    pollTimer = setTimeout(refreshPilot, POLL_MS);
  }
}

/* --------------------------------------------------------------------- disegno */

function headline() {
  const { label } = chainInfo();
  if (!data.enabled) {
    return { tone: "off", title: "Autopilot off", sub: `${label}` };
  }
  const blocker = data.health.find((h) => !h.ok);
  if (blocker) return { tone: "warn", title: "On, but stuck", sub: `${blocker.label}: ${blocker.detail}` };
  if (!data.scan.last_at) return { tone: "live", title: "Autopilot warming up", sub: "First scan any moment now" };
  return { tone: "live", title: "Autopilot is hunting", sub: null };
}

function paint() {
  const head = headline();
  const { limits, scan, tokens } = data;
  root.dataset.tone = head.tone;

  root.querySelector(".pilot-card").innerHTML = `
    <button type="button" class="pilot-status" data-pilot-status aria-label="Autopilot status, open details">
      <span class="pilot-dot" aria-hidden="true"></span>
      <span class="pilot-line">
        <b>${escapeHtml(head.title)}</b>
        <small data-clock>${escapeHtml(head.sub ?? clockText())}</small>
      </span>
      <span class="pilot-chev" aria-hidden="true">›</span>
    </button>`;
  root.querySelector("[data-stats]").innerHTML = `
    <div><dt>Watching</dt><dd>${tokens.length}</dd></div>
    <div><dt>Scanned today</dt><dd>${scan.seen_today}</dd></div>
    <div><dt>Spent today</dt><dd>${whole(limits.spent_today_usd)}</dd></div>`;

  const toggleEl = root.querySelector("[data-pilot-toggle]");
  toggleEl.setAttribute("aria-checked", String(data.enabled));

  // The limits stay on screen whether it is on or off: they are the
  // answer to "how much can it spend without me".
  root.querySelector("[data-rules]").innerHTML = `
    <li><span>Per buy</span><b>${whole(limits.per_trade_usd)}</b></li>
    <li><span>Per day</span><b>${whole(limits.daily_usd)}</b></li>
    <li><span>Liquidity</span><b>&gt; ${compact(data.filters.min_liquidity_usd)}</b></li>
    <li><span>Age</span><b>&lt; ${hours(data.filters.max_age_hours)}</b></li>
    <li><span>Strategy</span><b>${escapeHtml(strategyLabel(data.strategy))}</b></li>`;
  root.querySelector("[data-note]").textContent = data.enabled
    ? "Buys on its own, only on a strategy signal and within these limits."
    : "Buys nothing on its own. Open positions still get closed.";

  const feed = root.querySelector(".pilot-feed");
  feed.hidden = !data.enabled && tokens.length === 0;
  const known = new Set([...feed.querySelectorAll("[data-token]")].map((li) => li.dataset.token));
  feed.querySelector("ul").innerHTML = tokens.length
    ? tokens.map((t) => row(t, !known.has(t.watch_symbol) && known.size > 0)).join("")
    : `<li class="pilot-empty">No tokens yet. Next scan checks fresh launches on ${escapeHtml(chainInfo().label)}.</li>`;
  feed.querySelector("[data-updated]").textContent = "updated just now";

  const rejected = root.querySelector(".pilot-rejected");
  rejected.hidden = !data.enabled || data.rejected.length === 0;
  rejected.querySelector("ul").innerHTML = data.rejected.slice(0, 5).map((r) => `
    <li><b>${escapeHtml(r.symbol)}</b><span>${escapeHtml(r.reason)}</span></li>`).join("");

  startClock();
}

function row(t, isNew) {
  const detail = t.state === "watch" && t.added_at
    ? `new · ${ago(t.added_at).replace(" ago", "")} · liq. ${compact(t.liquidity_at_add)}`
    : t.reason;
  const price = t.position ? money(t.position.cost_usd) : t.last_price != null ? `$${formatPrice(t.last_price)}` : "—";
  return `
    <li class="${isNew ? "is-new" : ""}">
      <button type="button" data-token="${escapeHtml(t.watch_symbol)}">
        <span class="tk" aria-hidden="true">${escapeHtml(t.symbol.slice(0, 2))}</span>
        <span class="tk-name"><b>${escapeHtml(t.symbol)}</b><small>${escapeHtml(detail)}</small></span>
        <span class="tk-state"><span class="num">${price}</span><span class="chip" data-state="${t.state}">${escapeHtml(t.state_label)}</span></span>
      </button>
    </li>`;
}

function formatPrice(p) {
  return p >= 1 ? p.toFixed(2) : String(Number(p.toPrecision(3)));
}

function clockText() {
  const { last_at: last, next_at: next } = data.scan;
  if (!last) return "First scan any moment now";
  return `Last check ${ago(last)} · next ${until(next)}`;
}

function startClock() {
  clearInterval(clockTimer);
  if (headline().sub) return;  // niente da far scorrere
  clockTimer = setInterval(() => {
    const el = root.querySelector("[data-clock]");
    if (!el || !data) return clearInterval(clockTimer);
    el.textContent = clockText();
  }, 1000);
}

/* ---------------------------------------------------------------- interruttore */

async function toggle() {
  if (busy || !data) return;
  busy = true;
  const next = !data.enabled;
  const toggleEl = root.querySelector("[data-pilot-toggle]");
  toggleEl.setAttribute("aria-checked", String(next));  // subito: il dito vuole una risposta
  toggleEl.disabled = true;
  try {
    await api.setSniperAutotrade(state.chain, next);
    onToast?.(next ? "Autopilot on" : "Autopilot off",
      next ? `max ${whole(data.limits.per_trade_usd)} per buy` : "no more solo buying");
    onChange?.();
  } catch (error) {
    toggleEl.setAttribute("aria-checked", String(!next));
    onToast?.("Didn't work", error instanceof ApiError ? error.message : "unexpected error");
  } finally {
    toggleEl.disabled = false;
    busy = false;
    refreshPilot();
  }
}

/* -------------------------------------------------------------------- diagnosi */

function openSheet(markup) {
  if (!sheet) return;
  sheet.querySelector(".pilot-sheet-body").innerHTML = markup;
  if (!sheet.open) sheet.showModal();
}

// ok: true = passato, false = lo blocca, null = in attesa, "info" = solo un dato.
function check(ok, label, value) {
  const [mark, tone] = ok === true ? ["✓", "ok"] : ok === false ? ["✕", "no"] : ok === "info" ? ["·", "info"] : ["◌", "wait"];
  return `<li><span class="mark" data-tone="${tone}">${mark}</span><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></li>`;
}

function openToken(symbol) {
  const t = data?.tokens.find((x) => x.watch_symbol === symbol);
  if (!t) return;
  const { limits, filters } = data;
  const free = Math.max(0, limits.daily_usd - limits.spent_today_usd);
  const real = data.health.find((h) => h.key === "execution");
  const signals = data.health.find((h) => h.key === "signals");
  const since = t.added_at ? `Watching for ${ago(t.added_at).replace(" ago", "")}` : "On the watchlist";
  openSheet(`
    <header class="diag-head">
      <span class="tk" aria-hidden="true">${escapeHtml(t.symbol.slice(0, 2))}</span>
      <div><h2>${escapeHtml(t.symbol)}</h2><p>${escapeHtml(since)} · ${escapeHtml(t.interval)} candles</p></div>
    </header>
    <div class="verdict" data-state="${t.state}"><b>${escapeHtml(t.state_label)}</b>${escapeHtml(t.reason)}</div>
    <ul class="checks">
      ${t.liquidity_at_add != null ? check(t.liquidity_at_add >= filters.min_liquidity_usd, "Liquidity when found", compact(t.liquidity_at_add)) : ""}
      ${t.age_at_add != null ? check(t.age_at_add <= filters.max_age_hours, "Age when found", hours(t.age_at_add)) : ""}
      ${check(t.last_eval_at ? true : signals.ok ? null : false, "Strategy checked",
        t.last_eval_at ? ago(t.last_eval_at) : signals.ok ? "next scan" : "stalled")}
      ${check(free >= limits.per_trade_usd, "Today's budget", `${whole(free)} left`)}
      ${check(real.ok, "Live orders", real.ok ? "enabled" : "off")}
      ${t.position ? check("info", "Open position", `${money(t.position.cost_usd)} in`) : ""}
    </ul>
    ${t.events.length ? `<ol class="diag-log">${t.events.map((e) => `
      <li><time>${escapeHtml(ago(e.ts))}</time><span>${escapeHtml(e.text)}</span></li>`).join("")}</ol>` : ""}
    <button type="button" class="btn-quiet" data-close>Close</button>`);
}

function openHealth() {
  if (!data) return;
  const { scan } = data;
  openSheet(`
    <header class="diag-head"><div><h2>Autopilot health</h2><p>${escapeHtml(chainInfo().label)} · ${escapeHtml(strategyLabel(data.strategy))}</p></div></header>
    <ul class="checks">
      ${check(data.enabled, "Switch", data.enabled ? "on" : "off")}
      ${data.health.map((h) => check(h.ok, h.label, h.detail)).join("")}
    </ul>
    <ul class="checks">
      ${check(scan.last_at ? true : null, "Last scanner run", scan.last_at ? ago(scan.last_at) : "not yet")}
      ${check("info", "Tokens scanned today", String(scan.seen_today))}
      ${check("info", "Added today", String(scan.accepted_today))}
      ${check("info", "Signals rechecked every", `${data.signals_every_s} s`)}
    </ul>
    <button type="button" class="btn-quiet" data-close>Close</button>`);
}
