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
  if (s < 60) return `${s} s fa`;
  if (s < 3600) return `${Math.round(s / 60)} min fa`;
  if (s < 86400) return `${Math.round(s / 3600)} h fa`;
  return `${Math.round(s / 86400)} g fa`;
};
const until = (ts) => {
  const s = Math.round(ts - Date.now() / 1000);
  if (s <= 0) return "a momenti";
  return s < 60 ? `tra ${s} s` : `tra ${Math.round(s / 60)} min`;
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
      `<p class="pilot-error">${escapeHtml(error instanceof ApiError ? error.message : "Non riesco a leggere il pilota.")}</p>`;
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
    return { tone: "off", title: "Pilota spento", sub: `Non compra niente da solo su ${label}` };
  }
  const blocker = data.health.find((h) => !h.ok);
  if (blocker) return { tone: "warn", title: "Acceso, ma fermo", sub: `${blocker.label}: ${blocker.detail}` };
  if (!data.scan.last_at) return { tone: "live", title: "Il pilota sta partendo", sub: "Primo giro di controllo a momenti" };
  return { tone: "live", title: "Il pilota sta lavorando", sub: null };
}

function paint() {
  const head = headline();
  const { limits, scan, tokens } = data;
  root.dataset.tone = head.tone;

  root.querySelector(".pilot-card").innerHTML = `
    <button type="button" class="pilot-status" data-pilot-status aria-label="Stato del pilota, apri i dettagli">
      <span class="pilot-dot" aria-hidden="true"></span>
      <span class="pilot-line">
        <b>${escapeHtml(head.title)}</b>
        <small data-clock>${escapeHtml(head.sub ?? clockText())}</small>
      </span>
      <span class="pilot-chev" aria-hidden="true">›</span>
    </button>
    <dl class="pilot-stats">
      <div><dt>Sotto controllo</dt><dd>${tokens.length}</dd></div>
      <div><dt>Esaminati oggi</dt><dd>${scan.seen_today}</dd></div>
      <div><dt>Spesi oggi</dt><dd>${whole(limits.spent_today_usd)}<span> / ${whole(limits.daily_usd)}</span></dd></div>
    </dl>
    <span class="pilot-scan" aria-hidden="true"></span>`;

  const toggleEl = root.querySelector("[data-pilot-toggle]");
  toggleEl.setAttribute("aria-checked", String(data.enabled));
  toggleEl.querySelector("small").textContent = data.enabled
    ? `Compra da solo, al massimo ${whole(limits.per_trade_usd)} per volta e ${whole(limits.daily_usd)} al giorno`
    : "Spento: non compra niente da solo. Le posizioni aperte si chiudono comunque";

  const intro = root.querySelector(".pilot-intro");
  intro.hidden = data.enabled || tokens.length > 0;
  intro.querySelector("[data-rules]").innerHTML = `
    <li><span>Per ogni acquisto</span><b>${whole(limits.per_trade_usd)}</b></li>
    <li><span>Al giorno</span><b>${whole(limits.daily_usd)}</b></li>
    <li><span>Token nuovi</span><b>liquidità &gt; ${compact(data.filters.min_liquidity_usd)} · età &lt; ${hours(data.filters.max_age_hours)}</b></li>
    <li><span>Strategia</span><b>${escapeHtml(strategyLabel(data.strategy))}</b></li>`;

  const feed = root.querySelector(".pilot-feed");
  feed.hidden = !data.enabled && tokens.length === 0;
  const known = new Set([...feed.querySelectorAll("[data-token]")].map((li) => li.dataset.token));
  feed.querySelector("ul").innerHTML = tokens.length
    ? tokens.map((t) => row(t, !known.has(t.watch_symbol) && known.size > 0)).join("")
    : `<li class="pilot-empty">Ancora nessun token. Al prossimo giro guardo i nuovi arrivi su ${escapeHtml(chainInfo().label)}.</li>`;
  feed.querySelector("[data-updated]").textContent = "aggiornato ora";

  const rejected = root.querySelector(".pilot-rejected");
  rejected.hidden = !data.enabled || data.rejected.length === 0;
  rejected.querySelector("ul").innerHTML = data.rejected.slice(0, 5).map((r) => `
    <li><b>${escapeHtml(r.symbol)}</b><span>${escapeHtml(r.reason)}</span></li>`).join("");

  startClock();
}

function row(t, isNew) {
  const detail = t.state === "watch" && t.added_at
    ? `nuovo · ${ago(t.added_at).replace(" fa", "")} · liq. ${compact(t.liquidity_at_add)}`
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
  if (!last) return "Primo giro di controllo a momenti";
  return `Ultimo controllo ${ago(last)} · prossimo ${until(next)}`;
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
    onToast?.(next ? "Pilota acceso" : "Pilota spento",
      next ? `compra al massimo ${whole(data.limits.per_trade_usd)} per volta` : "non compra più niente da solo");
    onChange?.();
  } catch (error) {
    toggleEl.setAttribute("aria-checked", String(!next));
    onToast?.("Non riuscito", error instanceof ApiError ? error.message : "errore inatteso");
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
  const since = t.added_at ? `In osservazione da ${ago(t.added_at).replace(" fa", "")}` : "Nella watchlist";
  openSheet(`
    <header class="diag-head">
      <span class="tk" aria-hidden="true">${escapeHtml(t.symbol.slice(0, 2))}</span>
      <div><h2>${escapeHtml(t.symbol)}</h2><p>${escapeHtml(since)} · candele ${escapeHtml(t.interval)}</p></div>
    </header>
    <div class="verdict" data-state="${t.state}"><b>${escapeHtml(t.state_label)}</b>${escapeHtml(t.reason)}</div>
    <ul class="checks">
      ${t.liquidity_at_add != null ? check(t.liquidity_at_add >= filters.min_liquidity_usd, "Liquidità alla scoperta", compact(t.liquidity_at_add)) : ""}
      ${t.age_at_add != null ? check(t.age_at_add <= filters.max_age_hours, "Età alla scoperta", hours(t.age_at_add)) : ""}
      ${check(t.last_eval_at ? true : signals.ok ? null : false, "Strategia valutata",
        t.last_eval_at ? ago(t.last_eval_at) : signals.ok ? "al prossimo giro" : "ferma")}
      ${check(free >= limits.per_trade_usd, "Budget di oggi", `${whole(free)} liberi`)}
      ${check(real.ok, "Ordini reali", real.ok ? "abilitati" : "spenti")}
      ${t.position ? check("info", "Posizione aperta", `${money(t.position.cost_usd)} investiti`) : ""}
    </ul>
    ${t.events.length ? `<ol class="diag-log">${t.events.map((e) => `
      <li><time>${escapeHtml(ago(e.ts))}</time><span>${escapeHtml(e.text)}</span></li>`).join("")}</ol>` : ""}
    <button type="button" class="btn-quiet" data-close>Chiudi</button>`);
}

function openHealth() {
  if (!data) return;
  const { scan } = data;
  openSheet(`
    <header class="diag-head"><div><h2>Come sta il pilota</h2><p>${escapeHtml(chainInfo().label)} · ${escapeHtml(strategyLabel(data.strategy))}</p></div></header>
    <ul class="checks">
      ${check(data.enabled, "Interruttore", data.enabled ? "acceso" : "spento")}
      ${data.health.map((h) => check(h.ok, h.label, h.detail)).join("")}
    </ul>
    <ul class="checks">
      ${check(scan.last_at ? true : null, "Ultimo giro dello scanner", scan.last_at ? ago(scan.last_at) : "non ancora")}
      ${check("info", "Token esaminati oggi", String(scan.seen_today))}
      ${check("info", "Aggiunti oggi", String(scan.accepted_today))}
      ${check("info", "Segnali ricontrollati ogni", `${data.signals_every_s} s`)}
    </ul>
    <button type="button" class="btn-quiet" data-close>Chiudi</button>`);
}
