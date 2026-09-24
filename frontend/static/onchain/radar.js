/**
 * Il radar: la prima cosa che si vede entrando in DEGEN.
 *
 * Lorenzo: "vorrei che la prima pagina quando entro sul sito sia proprio
 * questa qui [...] i pop-up che poi vanno comunque in una lista e rimangono
 * impressi. E poi il grafico [...] di tutti quelli rilevati quale strategia
 * performerebbe meglio facendo un backtest e voglio vedere in tempo reale
 * con gli ingressi".
 *
 * Tre pezzi:
 * - "Appena usciti": i listing che hanno passato il filtro anti-rug (sito,
 *   social, GitHub, sicurezza), spinti dal server via SSE. Ognuno arriva
 *   come avviso e resta in lista con il suo backtest, che il server rifa' da
 *   solo mentre il token cresce;
 * - "Proposte": token fino a 3 settimane (oltre, solo con GitHub eccezionale) che una strategia avrebbe tradato bene;
 * - il palco: il grafico del token scelto (di default l'ultimo uscito) con
 *   gli ingressi e le uscite della strategia migliore, dal vivo.
 *
 * Propone e basta: l'interruttore accende un alert, l'acquisto vero resta
 * del pilota.
 */
import * as api from "@onchain/api.js";
import { escapeHtml } from "@onchain/format.js";

const ROOT = document.getElementById("onchain-embed") ?? document;
const POLL_MS = 60_000;
const CHAINS = { solana: "Solana", robinhood: "Robinhood" };
const PIN_MS = 3 * 60_000;  // dopo un tocco su una riga il palco non cambia da solo per un po'

let root = null;
let stage = null;
let data = null;
let timer = null;
let known = null;  // proposte gia' viste: le nuove entrano con l'animazione
let pinnedAt = 0;

const pct = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v) >= 100 ? Math.round(Math.abs(v)) : Math.abs(v).toFixed(1)}%`);
const usd = (v) => {
  if (v == null) return "—";
  if (v >= 1e6) return `$${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M`;
  if (v >= 1e3) return `$${Math.round(v / 1e3)}k`;
  return `$${Math.round(v)}`;
};
const age = (h) => (h < 1 ? ageShort(h * 3600) : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} g`);
const ago = (ts) => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return s < 90 ? "ora" : s < 3600 ? `${Math.round(s / 60)} min fa` : s < 172_800 ? `${Math.round(s / 3600)} h fa`
    : `${Math.round(s / 86_400)} g fa`;
};
const key = (p) => `${p.chain}:${p.token_address}`;
const lkey = (l) => `${l.chain}:${l.pair_address}`;
const rkey = (r) => `${r.chain}:${r.token_address || r.pair_address}`;
const ageShort = (s) => (s < 90 ? `${Math.max(1, Math.round(s))} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`);
// Il timeframe piu' largo che da' ancora 150 candele: come radar.auto_interval.
const INTERVAL_S = [["4h", 14_400], ["1h", 3_600], ["15m", 900], ["5m", 300], ["1m", 60]];
const autoInterval = (ageS) => (INTERVAL_S.find(([, s]) => ageS / s >= 150) ?? ["1m"])[0];
const short = (label) => (label ?? "").split(" · ")[0].split(" (")[0];

let toast = null;
let stream = null;

export function mountRadar({ toast: notify } = {}) {
  toast = notify ?? null;
  root = ROOT.querySelector("#radar");
  if (!root) return;  // la copia del wallet nella tab Trading non ha il radar
  stage = root.querySelector("[data-radar-stage]");
  root.addEventListener("click", (event) => {
    const row = event.target.closest("[data-radar]");
    if (row) { pinnedAt = Date.now(); showProposal(row.dataset.radar); return; }
    const listing = event.target.closest("[data-listing]");
    if (listing) { pinnedAt = Date.now(); showListing(listing.dataset.listing); return; }
    if (event.target.closest("[data-rejected-more]")) { rejectedAll = !rejectedAll; paintRejected(); return; }
    if (event.target.closest("[data-listings-more]")) { listingsAll = !listingsAll; paintListings(false); return; }
    onStageClick(event);
  });
  listen();
  setInterval(() => data && paintListings(false), 30_000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  refresh();
}

// I listing arrivano spinti dal server appena passano il filtro, e tornano
// ("update") ogni volta che il loro backtest cambia.
function listen() {
  if (!window.EventSource || stream) return;
  stream = new EventSource("/api/onchain/radar/stream");
  stream.addEventListener("listing", (event) => {
    let item;
    try { item = JSON.parse(event.data); } catch { return; }
    arrivals.push(item);
    if (!draining) drain();
  });
  stream.addEventListener("pulse", (event) => {
    try { paintPulse(JSON.parse(event.data)); } catch { /* battito perso: arriva il prossimo */ }
  });
  stream.addEventListener("rejected", (event) => {
    let item;
    try { item = JSON.parse(event.data); } catch { return; }
    if (!data) return;
    data.rejected = [item, ...(data.rejected ?? []).filter((x) => rkey(x) !== rkey(item))];
    paintRejected(rkey(item));
    flashPulse(`✕ ${item.symbol} scartato · ${item.reason}`);
  });
  stream.addEventListener("update", (event) => {
    let item;
    try { item = JSON.parse(event.data); } catch { return; }
    if (!data?.listings) return;
    data.listings = data.listings.map((x) => (lkey(x) === lkey(item) ? item : x));
    paintListings(false);
    if (view?.id === lkey(item)) {
      view.p = { ...view.p, ...item, interval: view.p.interval };
      paintStageHead();
    }
  });
}

// Piu' listing nello stesso istante entrano uno alla volta: ognuno ha il
// suo momento, invece di un blocco che salta dentro tutto insieme.
const ARRIVAL_GAP_MS = 480;
const arrivals = [];
let draining = false;

function drain() {
  const item = arrivals.shift();
  if (!item || !data) { draining = false; return; }
  draining = true;
  data.listings = [item, ...(data.listings ?? []).filter((x) => lkey(x) !== lkey(item))];
  paintListings(true);
  const born = item.created_at ? ` · nato ${ageShort(Date.now() / 1000 - item.created_at)} fa` : "";
  toast?.(`Nuovo: ${item.symbol}`, `${CHAINS[item.chain] ?? item.chain} · cap ${usd(item.market_cap)} · liq. ${usd(item.liquidity_usd)}${born}`);
  navigator.vibrate?.(25);
  // Il palco segue l'ultimo uscito, a meno che non si stia guardando altro.
  if (Date.now() - pinnedAt > PIN_MS) showListing(lkey(item));
  setTimeout(drain, ARRIVAL_GAP_MS);
}

async function refresh() {
  clearTimeout(timer);
  try {
    data = await api.getRadar();
    paint();
  } catch {
    // Il radar e' un di piu': se non risponde resta com'era, il pilota no.
  }
  api.getRadarLessons().then(paintMemory, () => {});
  if (document.visibilityState === "visible") timer = setTimeout(refresh, POLL_MS);
}

function clock() {
  if (data.running) return "scansiono…";
  if (!data.last_scan) return "primo giro a momenti";
  return ago(data.last_scan);
}

function paint() {
  const { proposals, chains } = data;
  root.hidden = false;
  delete root.dataset.loading;
  root.dataset.running = String(Boolean(data.running));
  root.querySelector("[data-radar-clock]").textContent = clock();

  const isFirst = known === null;
  const seen = new Set(proposals.map(key));
  paintListings(true);
  if (data.pulse) paintPulse(data.pulse);
  paintRejected();
  paintShame();
  root.querySelector("[data-radar-list]").innerHTML = proposals.length
    ? proposals.map((p) => row(p, !isFirst && !known.has(key(p)))).join("")
    : `<li class="pilot-empty">${escapeHtml(emptyText())}</li>`;
  known = seen;

  const st = data.listing_status ?? {};
  const parts = Object.entries(chains ?? {}).map(([c, info]) => `${CHAINS[c] ?? c} ${info.seen}`);
  const rejected = st.rejected ? ` · ${st.rejected} scartati dal filtro` : "";
  const rules = `cap < ${usd(data.filters.mcap_max_usd)}, sopra $100k solo pagella eccellente · sito, social, GitHub, RugCheck e GoPlus obbligatori${st.ai_review ? " · controllo Claude" : ""}`;
  // Lorenzo: "qui ci sono troppe scritte". Le regole restano a portata di
  // dito (tenendo premuto / passandoci sopra), non a schermo.
  const foot = root.querySelector("[data-radar-foot]");
  foot.hidden = true;
  foot.closest(".radar")?.querySelector("header h3")?.setAttribute("title", data.last_scan
    ? `Visti: ${parts.join(" · ")}${rejected} · ${rules}`
    : `Solana e Robinhood · ${rules}`);

  if (!view) {
    const first = data.listings?.[0];
    if (first) showListing(lkey(first));
    else if (proposals[0]) showProposal(key(proposals[0]));
    else paintIdleStage();
  }
  markOn();
}

let shownListings = null;
let listingsAll = false;

const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Il backtest di un listing in una parola: quale strategia e quanto.
function listingBadge(l) {
  const bt = l.backtest;
  if (!bt) return { num: "…", chip: "backtest in corso", state: "watch" };
  if (bt.state === "young") return { num: `${bt.bars}/${bt.need}`, chip: "candele per il test", state: "watch" };
  if (bt.state === "none" || bt.return_pct == null) return { num: "—", chip: "nessun segnale", state: "watch" };
  return { num: pct(bt.return_pct), chip: short(bt.label), state: bt.in_position ? "in" : "watch",
    up: bt.return_pct > 0 };
}

// animate=false per il ticchettio delle eta' e per gli aggiornamenti del
// backtest: ridisegna senza muovere niente.
function paintListings(animate) {
  const box = root.querySelector("[data-radar-new]");
  const list = box.querySelector("ul");
  // Tutto quello che e' stato scelto resta, anche dopo un riavvio: i primi
  // 8 in vista, gli altri a un tocco.
  const all = data.listings ?? [];
  const items = listingsAll ? all : all.slice(0, 8);
  const more = box.querySelector("[data-listings-more]");
  if (more) {
    more.hidden = all.length <= 8;
    more.textContent = listingsAll ? "Mostra meno" : `Mostra tutti (${all.length})`;
  }
  const empty = box.querySelector("[data-radar-new-empty]");
  if (empty) empty.hidden = items.length > 0;
  const now = Date.now() / 1000;
  const before = shownListings;
  const firstReveal = animate && before === null && items.length > 0;
  // FLIP: dove stava ogni riga prima, per farla scivolare al posto nuovo
  // invece di saltarci quando ne entra una sopra.
  const tops = new Map([...list.querySelectorAll("[data-listing]")]
    .map((el) => [el.dataset.listing, el.getBoundingClientRect().top]));
  list.innerHTML = items.map((l, i) => {
    const born = l.created_at ? now - l.created_at : null;
    const meta = `${CHAINS[l.chain] ?? l.chain} · ${born == null ? "" : `${ageShort(born)} · `}cap ${usd(l.market_cap)} · liq. ${usd(l.liquidity_usd)}`;
    const isNew = animate && before && !before.has(lkey(l));
    const cls = [firstReveal ? "is-enter" : isNew ? "is-arrival" : "", view?.id === lkey(l) ? "is-on" : ""].join(" ");
    const b = listingBadge(l);
    return `
      <li class="${cls}" style="--i:${i}">
        <button type="button" data-listing="${escapeHtml(lkey(l))}">
          <span class="tk" data-chain="${escapeHtml(l.chain)}" aria-hidden="true">${escapeHtml(l.symbol.slice(0, 2))}</span>
          <span class="tk-name"><b>${escapeHtml(l.symbol)}</b><small>${escapeHtml(meta)}</small></span>
          <span class="tk-state"><span class="num" ${b.up ? 'data-sign="up"' : ""}>${escapeHtml(b.num)}</span><span class="chip" data-state="${b.state}">${escapeHtml(b.chip)}</span></span>
        </button>
      </li>`;
  }).join("");
  if (!firstReveal) {
    list.querySelectorAll("[data-listing]").forEach((el) => {
      const was = tops.get(el.dataset.listing);
      if (was == null || reducedMotion()) return;
      const dy = was - el.getBoundingClientRect().top;
      if (Math.abs(dy) > 1) {
        el.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }],
          { duration: 520, easing: "cubic-bezier(.2,.8,.2,1)" });
      }
    });
  }
  if (items.length) shownListings = new Set(items.map(lkey));
}

/* ------------------------------------------------------------ il battito */

// Lorenzo: "voglio capire che e' una cosa live [...] deve esserci un senso di
// movimento". Il server manda ogni 5 s cosa sta guardando: i pool nuovi
// comparsi, i token in controllo adesso. I numeri scorrono, i nomi cambiano.
let lastPulse = null;
let flashTimer = null;

function tween(el, to) {
  const from = Number(el.dataset.v ?? to);
  el.dataset.v = String(to);
  if (from === to || reducedMotion()) { el.textContent = String(to); return; }
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 700);
    el.textContent = String(Math.round(from + (to - from) * (1 - (1 - k) ** 3)));
    if (k < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
  el.animate?.([{ color: "var(--live)" }, { color: "inherit" }], { duration: 1200 });
}

function paintPulse(p) {
  const box = root?.querySelector("[data-radar-pulse]");
  if (!box || !p) return;
  box.hidden = false;
  lastPulse = p;
  box.dataset.busy = String((p.checking ?? []).length > 0);
  tween(box.querySelector("[data-pulse-arrivals]"), p.arrivals_10m ?? 0);
  tween(box.querySelector("[data-pulse-rejected]"), data?.rejected?.length ?? p.rejected ?? 0);
  const last = box.querySelector("[data-pulse-last]");
  last.textContent = p.last_arrival ? ` · ${ageShort(Date.now() / 1000 - p.last_arrival)} fa` : "";
  if (flashTimer) return;  // un avviso di scarto resta visibile il suo tempo
  const now = box.querySelector("[data-pulse-now]");
  const text = p.checking?.length ? `Controllo ${p.checking.join(", ")}` : "In ascolto";
  if (now.textContent !== text) {
    now.textContent = text;
    now.animate?.([{ opacity: 0, translate: "0 6px" }, { opacity: 1, translate: "0 0" }],
      { duration: 380, easing: "cubic-bezier(.2,.8,.2,1)" });
  }
}

function flashPulse(text) {
  const now = root?.querySelector("[data-pulse-now]");
  if (!now) return;
  clearTimeout(flashTimer);
  now.textContent = text;
  now.dataset.tone = "no";
  now.animate?.([{ opacity: 0, translate: "0 6px" }, { opacity: 1, translate: "0 0" }], { duration: 380 });
  flashTimer = setTimeout(() => {
    flashTimer = null;
    delete now.dataset.tone;
    now.textContent = "";
    paintPulse(lastPulse);
  }, 4_000);
}

/* ------------------------------------------------------------ gli scartati */

// Lorenzo: "scorrendo giu' voglio una sezione che mi faccia vedere tutti
// quelli che sono stati scartati". Un tocco apre il perche', controllo per
// controllo, senza grafico: non serve a nessuno quello di un token scartato.
const REJECTED_SHOWN = 12;
let rejectedAll = false;

function paintRejected(arrived) {
  const box = root.querySelector("[data-radar-rejected]");
  if (!box) return;
  const all = data.rejected ?? [];
  box.hidden = all.length === 0;
  box.querySelector("[data-rejected-count]").textContent = all.length ? `· ${all.length}` : "";
  const open = new Set([...box.querySelectorAll("details[open]")].map((d) => d.dataset.key));
  const items = rejectedAll ? all : all.slice(0, REJECTED_SHOWN);
  box.querySelector("ul").innerHTML = items.map((r) => {
    const born = r.created_at ? ` · ${ageShort(Math.max(0, r.at - r.created_at))} di vita` : "";
    const meta = `${CHAINS[r.chain] ?? r.chain}${r.market_cap ? ` · cap ${usd(r.market_cap)}` : ""}${born} · ${ago(r.at)}`;
    return `
      <li class="${rkey(r) === arrived ? "is-arrival" : ""}">
        <details data-key="${escapeHtml(rkey(r))}" ${open.has(rkey(r)) ? "open" : ""}>
          <summary>
            <span class="tk" data-chain="${escapeHtml(r.chain)}" aria-hidden="true">${escapeHtml((r.symbol ?? "?").slice(0, 2))}</span>
            <span class="tk-name"><b>${escapeHtml(r.symbol ?? "?")}</b><small>${escapeHtml(meta)}</small></span>
            <span class="rj-why">${escapeHtml(r.reason ?? "")}</span>
          </summary>
          ${reportCard(r)}
          <nav class="stage-links">${links(r)}</nav>
        </details>
      </li>`;
  }).join("");
  const more = box.querySelector("[data-rejected-more]");
  more.hidden = all.length <= REJECTED_SHOWN;
  more.textContent = rejectedAll ? "Mostra meno" : `Mostra tutti (${all.length})`;
}

/* ------------------------------------------------- il muro delle inculate */

const SHAME_QUIPS = ["ci ha salutati con la manina", "ha preso i soldi ed è scappato", "promosso, poi evaporato",
  "tanti auguri a chi l'ha tenuto", "il GitHub era di qualcun altro"];

function paintShame() {
  const box = root?.querySelector("[data-radar-shame]");
  if (!box) return;
  const all = data.shame ?? [];
  box.hidden = all.length === 0;
  box.querySelector("[data-shame-count]").textContent = all.length ? `· ${all.length}` : "";
  box.querySelector("ul").innerHTML = all.map((e, i) => {
    const when = e.rug_at ? new Date(e.rug_at * 1000).toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit" }) : "";
    const quip = e.fooled_by ? `ci ha fregati con ${e.fooled_by}` : SHAME_QUIPS[i % SHAME_QUIPS.length];
    return `
    <li>
      <span class="tk" data-chain="${escapeHtml(e.chain)}" aria-hidden="true">${escapeHtml((e.symbol ?? "?").slice(0, 2))}</span>
      <span class="tk-name"><b>${escapeHtml(e.symbol ?? "?")}</b><small>${escapeHtml(`${when ? `${when} · ` : ""}${quip}`)}</small></span>
      <span class="rj-why">${escapeHtml(e.rug_why ?? "rug")}</span>
    </li>`;
  }).join("");
}

/* ------------------------------------------------------------ la memoria */

// Lorenzo, dopo VISE: "imparare dai segnali, da cosa effettivamente succede".
// Una riga di numeri e i casi che contano: i rug che il filtro ha lasciato
// passare e i token scartati che poi sono saliti.
function paintMemory(m) {
  const box = root?.querySelector("[data-radar-memory]");
  if (!box || !m) return;
  box.hidden = !m.tracked;
  box.querySelector("[data-memory-count]").textContent = m.tracked ? `· ${m.tracked} seguiti` : "";
  const rate = (g) => (g?.rug_rate == null ? "—" : `${Math.round(g.rug_rate * 100)}% rug`);
  const known = (m.passed?.decided ?? 0) + (m.rejected?.decided ?? 0);
  box.querySelector("[data-memory-line]").textContent = known
    ? `Passati ${rate(m.passed)} · scartati ${rate(m.rejected)}${m.notes?.[1] ? ` · ${m.notes[1]}` : ""}`
    : "Raccolgo gli esiti: servono qualche ora di prezzi";
  // I rug passati dal filtro stanno nel muro qui sotto; qui gli scartati poi saliti.
  const rows = (m.missed ?? []).slice(0, 4).map((e) => ({ ...e, why: `${pct(e.max_ret * 100)} dopo lo scarto` }));
  box.querySelector("ul").innerHTML = rows.map((e) => `
    <li>
      <span class="tk" data-chain="${escapeHtml(e.chain)}" aria-hidden="true">${escapeHtml((e.symbol ?? "?").slice(0, 2))}</span>
      <span class="tk-name"><b>${escapeHtml(e.symbol ?? "?")}</b><small>${escapeHtml(e.decision === "rejected" ? e.reason : "passato dal filtro")} · ${ago(e.at)}</small></span>
      <span class="rj-why">${escapeHtml(e.why)}</span>
    </li>`).join("");
}

function emptyText() {
  if (!data.last_scan) return "Primo giro in corso";
  const c = Object.values(data.chains ?? {});
  const sum = (k) => c.reduce((n, x) => n + (x[k] ?? 0), 0);
  return `Nessuna per ora · ${sum("passing")} candidati, ${sum("tested")} provati`;
}

function row(p, isNew) {
  const meta = `${p.exception ? "eccezione GitHub · " : ""}${CHAINS[p.chain] ?? p.chain} · ${age(p.age_hours)} · cap ${usd(p.market_cap)} · liq. ${usd(p.liquidity_usd)}`;
  return `
    <li class="${isNew ? "is-new" : ""}">
      <button type="button" data-radar="${escapeHtml(key(p))}">
        <span class="tk" data-chain="${escapeHtml(p.chain)}" aria-hidden="true">${escapeHtml(p.symbol.slice(0, 2))}</span>
        <span class="tk-name"><b>${escapeHtml(p.symbol)}</b><small>${escapeHtml(meta)}</small></span>
        <span class="tk-state"><span class="num" data-sign="up">${pct(p.return_pct)}</span><span class="chip" data-state="${p.in_position ? "in" : "watch"}">${escapeHtml(short(p.strategy))}</span></span>
      </button>
    </li>`;
}

/* ---------------------------------------------------------------- il palco */

// Lo stile del grafico del sito principale (static/app.js, initCharts):
// candele vuote in salita, piene in discesa, mono, linea del prezzo
// tratteggiata. IN sotto la candela, OUT sopra, come li' — ma un colore
// per strategia, perche' qui se ne possono guardare piu' insieme.
const BULL = "#00d68f";
const BEAR = "#ff4560";
const PALETTE = ["#00d68f", "#f0a500", "#6ea8ff", "#ff7ad9", "#c8f169", "#9b6bff"];
const LIVE_MS = 30_000;
const RETRY_MS = 4_000;

// { kind, id, p, chart, series, strategies, best, shown:Set, active:Set, timer, fitted, seeded }
let view = null;

function chartLibrary() {
  if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
  const tag = document.querySelector('script[src*="lightweight-charts"]');
  if (!tag) return Promise.reject(new Error("libreria del grafico mancante"));
  return new Promise((resolve, reject) => {
    tag.addEventListener("load", () => resolve(window.LightweightCharts), { once: true });
    tag.addEventListener("error", () => reject(new Error("libreria del grafico non caricata")), { once: true });
  });
}

function closeView() {
  if (!view) return;
  clearTimeout(view.timer);
  view.chart?.remove();
  view = null;
}

function markOn() {
  root.querySelectorAll("li.is-on").forEach((li) => li.classList.remove("is-on"));
  if (!view) return;
  const sel = view.kind === "listing" ? `[data-listing="${CSS.escape(view.id)}"]` : `[data-radar="${CSS.escape(view.id)}"]`;
  root.querySelector(sel)?.closest("li")?.classList.add("is-on");
}

function paintIdleStage() {
  stage.innerHTML = `<p class="radar-idle">Qui il grafico del primo token che passa</p>`;
}

function links(p) {
  const out = [];
  const a = (url, label, cls = "") => `<a class="${cls}" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${label} ↗</a>`;
  if (p.website) out.push(a(p.website, "Sito del progetto", "is-main"));
  if (p.github) out.push(a(p.github, "GitHub"));
  for (const s of p.socials ?? []) {
    if (s.type === "twitter") out.push(a(s.url, "X"));
    if (s.type === "telegram") out.push(a(s.url, "Telegram"));
  }
  if (p.scan_url) out.push(a(p.scan_url, "RugCheck"));
  if (p.pair_url) out.push(a(p.pair_url, "DexScreener"));
  return out.join("");
}

// La pagella: quanto bene ha passato ogni filtro, non solo se l'ha passato.
// Lorenzo: "un bel benchmark che mi faccia capire [...] se in modo
// eccellente oppure [...] mediocri".
const GRADE_TONE = { eccellente: "top", buono: "good", sufficiente: "mid", mediocre: "low" };

function reportCard(p) {
  const checks = p.checks ?? [];
  if (!checks.length) return "";
  const mark = (ok) => (ok === true ? "✓" : ok === false ? "✕" : "·");
  const tone = (ok) => (ok === true ? "ok" : ok === false ? "no" : "info");
  const r = p.report;
  const head = r ? `
    <div class="rc-head" data-grade="${GRADE_TONE[r.grade] ?? "mid"}">
      <span class="rc-score">${r.score}</span>
      <span><b>${escapeHtml(r.grade)}</b><small>media di ${r.counted} controlli · punto debole: ${escapeHtml(r.weakest)}</small></span>
    </div>` : "";
  const rows = checks.map((c) => {
    const bar = c.score == null ? "" : `
      <span class="rc-meter" data-grade="${GRADE_TONE[c.grade] ?? "mid"}"><span class="rc-bar"><i style="--w:${Math.max(3, Math.min(100, c.score))}%"></i></span><span class="rc-grade">${escapeHtml(c.grade ?? "")} · ${Math.round(c.score)}</span></span>`;
    return `
      <li>
        <span class="mark" data-tone="${tone(c.ok)}">${mark(c.ok)}</span>
        <span class="rc-label">${escapeHtml(c.label)}</span>
        <span class="rc-detail">${escapeHtml(c.detail ?? "")}</span>
        ${bar}
        ${factsOf(c)}
      </li>`;
  }).join("");
  return `<section class="report-card">${head}<ul class="rc-list">${rows}</ul></section>`;
}

const when = (iso) => {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? null : ago(t / 1000);
};

function factsOf(c) {
  const f = c.facts;
  const pairs = [];
  if (c.key === "github" && f) {
    pairs.push(["Repository", f.url ? `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(f.repo)} ↗</a>` : escapeHtml(f.repo)]);
    if (f.description) pairs.push(["Descrizione", escapeHtml(f.description)]);
    pairs.push(["Linguaggio", escapeHtml(f.language ?? "—")], ["Commit", escapeHtml(String(f.commits))],
      ["Giorni attivi", String(f.active_days)], ["Autori", String(f.authors)], ["Stelle", String(f.stars)],
      ["Fork", String(f.forks)], ["Osservatori", String(f.watchers)], ["Issue aperte", String(f.open_issues)],
      ["Creato", escapeHtml(when(f.created_at) ?? "—")], ["Ultimo push", escapeHtml(when(f.pushed_at) ?? "—")],
      ["Licenza", escapeHtml(f.license ?? "nessuna")]);
  } else if (c.key === "site" && f) {
    pairs.push(["Pagina", `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(f.title || f.url)} ↗</a>`],
      ["Parole", String(f.words)], ["HTTPS", f.https ? "sì" : "no"], ["Cita il contratto", f.cites_contract ? "sì" : "no"]);
  } else if (c.key === "contract" && f) {
    pairs.push(["Nome e logo", f.metadata_mutable ? "modificabili" : "bloccati"],
      ["Tassa di trasferimento", `${f.transfer_fee_pct ?? 0}%`]);
    if (f.holders) pairs.push(["Holder", escapeHtml(String(f.holders))]);
  } else if (c.key === "ai" && c.red_flags?.length) {
    pairs.push(["Campanelli d'allarme", escapeHtml(c.red_flags.join(", "))]);
  }
  if (!pairs.length) return "";
  return `<dl class="rc-facts">${pairs.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
}

function showListing(id) {
  const l = data?.listings?.find((x) => lkey(x) === id);
  if (!l || !stage) return;
  const ageS = l.created_at ? Date.now() / 1000 - l.created_at : 0;
  openStage("listing", id, { ...l, interval: autoInterval(ageS), age_hours: ageS / 3600 });
}

function showProposal(id) {
  const p = data?.proposals.find((x) => key(x) === id);
  if (!p || !stage) return;
  openStage("proposal", id, p);
}

function openStage(kind, id, p) {
  if (view?.id === id) return;
  closeView();
  stage.innerHTML = `
    <header class="stage-head">
      <span class="tk" data-chain="${escapeHtml(p.chain)}" aria-hidden="true">${escapeHtml(p.symbol.slice(0, 2))}</span>
      <div><h2>${escapeHtml(p.symbol)}${p.name && p.name !== p.symbol ? ` <small>${escapeHtml(p.name)}</small>` : ""}</h2>
        <p data-stage-meta></p></div>
      <span class="radar-live" data-radar-live>…</span>
    </header>
    <p class="stage-best" data-stage-best></p>
    <div class="radar-chart" data-radar-chart></div>
    <nav class="stage-links">${links(p)}</nav>
    <ul class="radar-strats" data-radar-strats></ul>
    ${reportCard(p)}
    <p class="radar-note">Backtest solo long con costi DEX (${data?.costs?.commission_pct ?? 0.3}% + ${data?.costs?.slippage_pct ?? 0.5}% per lato), sulle stesse candele del grafico. L'interruttore accende solo l'alert: compra solo il pilota, e solo se lo attivi.</p>`;
  view = { kind, id, p, chart: null, series: null, strategies: [], best: null, shown: new Set(), active: new Set(),
    timer: null, fitted: false, seeded: false };
  paintStageHead();
  markOn();
  loadChart();
}

function paintStageHead() {
  if (!view) return;
  const { p } = view;
  const meta = stage.querySelector("[data-stage-meta]");
  if (meta) meta.textContent = `${CHAINS[p.chain] ?? p.chain} · ${age(p.age_hours ?? 0)} · cap ${usd(p.market_cap)} · liq. ${usd(p.liquidity_usd)} · candele ${p.interval}`;
  const best = stage.querySelector("[data-stage-best]");
  if (!best) return;
  const s = view.strategies.find((x) => x.label === view.best);
  const bt = p.backtest;
  if (s) {
    best.innerHTML = `Migliore finora: <b style="color:${colorOf(s.label)}">${escapeHtml(short(s.label))}</b> · <span data-sign="${s.return_pct > 0 ? "up" : "down"}">${pct(s.return_pct)}</span> · ${s.trades} trade · ${s.in_position ? "adesso dentro" : "adesso fuori"}${s.qualifies ? "" : " · test ancora debole"}`;
  } else if (bt?.state === "young") {
    best.textContent = `Backtest appena ci sono ${bt.need} candele (ora ${bt.bars}). Intanto si vedono tutte le strategie.`;
  } else {
    best.textContent = view.strategies.length ? "Nessuna strategia ha ancora fatto un trade qui." : "Backtest su tutte le strategie…";
  }
}

async function loadChart() {
  if (!view) return;
  const current = view;
  const { p } = current;
  try {
    const [payload, LightweightCharts] = await Promise.all([
      api.getRadarChart(p.chain, p.pair_address, p.symbol, p.interval),
      chartLibrary(),
    ]);
    if (view !== current) return;
    current.strategies = payload.strategies;
    current.best = payload.best;
    // Di default si vede la migliore; senza una migliore, tutte.
    if (!current.seeded) {
      if (payload.best) current.shown.add(payload.best);
      else payload.strategies.filter((x) => !x.excluded).forEach((x) => current.shown.add(x.label));
      current.seeded = true;
    }
    current.active = new Set(payload.active);
    if (!current.chart) {
      const box = stage.querySelector("[data-radar-chart]");
      box.textContent = "";
      current.chart = LightweightCharts.createChart(box, {
        autoSize: true,
        layout: { background: { color: "transparent" }, textColor: "#6e7681", fontSize: 11,
          fontFamily: "'JetBrains Mono', monospace", attributionLogo: false },
        grid: { vertLines: { color: "rgba(255,255,255,0.035)" }, horzLines: { color: "rgba(255,255,255,0.035)" } },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 4 },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        // In prima pagina il dito in verticale scorre la pagina, non il grafico.
        handleScroll: { vertTouchDrag: false },
        localization: { priceFormatter: (v) => (v >= 1 ? v.toFixed(2) : Number(v.toPrecision(3)).toString()) },
      });
      current.series = current.chart.addCandlestickSeries({
        upColor: "rgba(0,0,0,0)", downColor: BEAR, borderVisible: true,
        borderUpColor: BULL, borderDownColor: BEAR, wickUpColor: BULL, wickDownColor: BEAR,
        priceLineVisible: true, priceLineColor: "#f0a500", priceLineWidth: 1,
        priceLineStyle: LightweightCharts.LineStyle.Dashed, lastValueVisible: true,
        priceFormat: { type: "price", minMove: 1e-10, precision: 10 },
      });
    }
    current.series.setData(payload.candles);
    if (!current.fitted) {
      // Le ultime ~150 candele: abbastanza per leggere gli ingressi, poi zoom.
      const n = payload.candles.length;
      current.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 150), to: n + 4 });
      current.fitted = true;
    }
    paintMarkers();
    paintStrategies();
    paintStageHead();
    stage.querySelector("[data-radar-live]").textContent = "live";
  } catch {
    if (view !== current) return;
    const live = stage.querySelector("[data-radar-live]");
    if (live) live.textContent = "non aggiornato";
    // Il token e' su DexScreener: un rifiuto momentaneo della fonte delle
    // candele si riprova in pochi secondi, senza mostrare errori grezzi.
    if (!current.chart) {
      stage.querySelector("[data-radar-chart]").textContent = "Carico le candele…";
      current.timer = setTimeout(loadChart, RETRY_MS);
      return;
    }
  }
  if (view === current) current.timer = setTimeout(loadChart, LIVE_MS);
}

function colorOf(label) {
  const i = view.strategies.findIndex((s) => s.label === label);
  return PALETTE[Math.max(0, i) % PALETTE.length];
}

function paintMarkers() {
  const markers = [];
  for (const s of view.strategies) {
    if (!view.shown.has(s.label)) continue;
    const color = colorOf(s.label);
    for (const e of s.events) {
      markers.push(e.kind === "in"
        ? { time: e.time, position: "belowBar", color, shape: "arrowUp", text: "IN" }
        : { time: e.time, position: "aboveBar", color, shape: "circle", text: "OUT" });
    }
  }
  markers.sort((a, b) => a.time - b.time);
  view.series.setMarkers(markers);
}

function paintStrategies() {
  const ordered = [...view.strategies].sort((a, b) =>
    Number(Boolean(a.excluded)) - Number(Boolean(b.excluded)) || (b.return_pct ?? -1e9) - (a.return_pct ?? -1e9));
  stage.querySelector("[data-radar-strats]").innerHTML = ordered.map((s) => {
    const shown = view.shown.has(s.label);
    const on = view.active.has(s.strategy_name);
    const sub = s.excluded
      ? s.excluded
      : `${pct(s.return_pct)} · ${s.trades} trade · ${s.events.length ? (s.in_position ? "adesso dentro" : "adesso fuori") : "nessun segnale ancora"}`;
    return `
      <li data-shown="${shown}">
        <button type="button" class="rs-show" data-show="${escapeHtml(s.label)}" aria-pressed="${shown}">
          <span class="rs-swatch" style="--c:${colorOf(s.label)}"></span>
          <span class="tk-name"><b>${escapeHtml(s.label)}${s.label === view.best ? " ★" : ""}</b><small>${escapeHtml(sub)}</small></span>
        </button>
        ${s.excluded ? "" : `<button type="button" class="rs-switch" role="switch" aria-checked="${on}"
          aria-label="Alert ${escapeHtml(s.label)}" data-follow="${escapeHtml(s.label)}"><span></span></button>`}
      </li>`;
  }).join("");
}

async function follow(label) {
  const s = view?.strategies.find((x) => x.label === label);
  if (!s) return;
  const { p } = view;
  const next = !view.active.has(s.strategy_name);
  const el = stage.querySelector(`[data-follow="${CSS.escape(label)}"]`);
  el?.setAttribute("aria-checked", String(next));
  try {
    const res = await api.followRadar({
      chain: p.chain, pool: p.pair_address, symbol: p.symbol, token_address: p.token_address,
      interval: p.interval, strategy_name: s.strategy_name, params: s.params, enabled: next,
    });
    if (!view) return;
    view.active = new Set(res.active);
  } catch {
    el?.setAttribute("aria-checked", String(!next));
    return;
  }
  paintStrategies();
}

function onStageClick(event) {
  if (!view) return;
  const show = event.target.closest("[data-show]");
  if (show) {
    const label = show.dataset.show;
    view.shown.has(label) ? view.shown.delete(label) : view.shown.add(label);
    paintMarkers();
    paintStrategies();
    return;
  }
  const sw = event.target.closest("[data-follow]");
  if (sw) follow(sw.dataset.follow);
}
