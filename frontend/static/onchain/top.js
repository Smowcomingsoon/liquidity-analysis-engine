/**
 * Arenas: the best quality low caps, each ranked against its own narrative.
 *
 * Lorenzo, 27/09, replaced Picks with a Top 15/25/50 ("voglio semmai
 * sniperare progetti di qualita' con capitalizzazioni basse"); 07/10: "la
 * top50 come concetto e' limitante, serve un sistema meritocratico [...]
 * data la complessita' dei progetti e le diverse narrative". Now the list is
 * arenas.py: one arena per narrative (and one for layer chains), no fixed
 * size, the projects at or above the arena's median "in" and the rest on its
 * bench. The podium is pound for pound: the projects that lead their own
 * arena by the most. Refreshed every POLL_MS; a tap opens the token page.
 */
import * as api from "@onchain/api.js";
import { register } from "@onchain/detail.js";
import { isHidden, onHiddenChange, withEntrance } from "@onchain/hidden.js";
import { escapeHtml, tile, tokenRow, usdShort } from "@onchain/format.js";
import { renderPodium } from "@onchain/podium.js";
import { shownSummary } from "@onchain/lab-shown.js";
import * as narr from "@onchain/narrative_pick.js";
import { disintegrate } from "@onchain/particles.js";
import "@onchain/motion.js";

const POLL_MS = 30_000;
// Trading off (Lorenzo, 07/10): rows show the market, not a backtest.
const TRADING = document.documentElement.dataset.trading === "on";

let root = null;
let arenas = [];
let p4p = [];
let labStatus = null;
let filtered = false;   // the last load was narrowed by narrative
let labels = {};        // narrative key -> label, from the coverage
// One arena on its own, bench open (Lorenzo, 07/10: "la top aggiornata per
// ogni categoria"); null shows them all. Remembered per device.
let only = (() => { try { return localStorage.getItem("lae:arena") || null; } catch { return null; } })();
function setOnly(k) { only = k; try { k ? localStorage.setItem("lae:arena", k) : localStorage.removeItem("lae:arena"); } catch { /* private mode */ } }

// Each arena's champion on the last visit (Lorenzo, 28/09: "se rientro dopo
// un po' e c'e' stato un capovolgimento [...] fai una bella animazione"):
// arena key -> {id, symbol}.
const SEEN_KEY = "lae:arena-crowns";
function readSeen() { try { return JSON.parse(localStorage.getItem(SEEN_KEY)) || null; } catch { return null; } }
function writeSeen(map) { try { localStorage.setItem(SEEN_KEY, JSON.stringify(map)); } catch { /* private mode */ } }
let seen = readSeen();
let shown = false;
let stale = false;          // new data arrived while the arenas were off screen
const idOf = (e) => `gem:${e.chain}:${e.token_address}`;
// The arenas sit in a tab: painted while another tab is open, the "since your
// last visit" animation would play unseen (Lorenzo, 29/09). It waits until it shows.
const onScreen = () => document.visibilityState === "visible" && root.getClientRects().length > 0;

let playing = false;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/** Each dethroned champion turns to dust over the row of the one that took
 *  its place, one arena after the other. Leaving the tab stops it and keeps
 *  the crowns unseen, so it plays again next time. */
async function sequence(changes, crowns) {
  playing = true;
  let aborted = false;
  for (const { li, was } of changes) {
    if (!onScreen()) { aborted = true; break; }
    const ghost = li.cloneNode(true);
    ghost.classList.remove("rank-new", "rank-wait");
    ghost.setAttribute("aria-hidden", "true");
    const r = li.getBoundingClientRect();
    Object.assign(ghost.style, { position: "fixed", visibility: "hidden", top: `${r.top}px`, left: `${r.left}px`,
      width: `${r.width}px`, margin: "0", animation: "none" });
    const name = ghost.querySelector(".trow-main b");
    if (name) name.textContent = `1. ${was.symbol ?? ""}`;
    ghost.querySelectorAll(".trow-main small, .trow-num b, img, svg").forEach((x) => x.remove());
    document.body.append(ghost);
    await disintegrate(ghost, 900);
    ghost.remove();
    li.classList.remove("rank-wait");
    li.classList.add("rank-new");
    await wait(300);
  }
  root.querySelectorAll("[data-top-arenas] li.rank-wait").forEach((li) => li.classList.remove("rank-wait"));
  playing = false;
  if (aborted) { stale = true; return; }
  seen = crowns;
  writeSeen(seen);
}

const heatBar = (h) => `<span class="ar-heat" aria-label="Narrative heat ${h} of 3">${"█".repeat(h)}${"░".repeat(Math.max(0, 3 - h))}</span>`;
const sigma = (z) => `${z >= 0 ? "+" : "−"}${Math.abs(z).toFixed(1)}σ`;

function row(e, a) {
  const id = idOf(e);
  const rank = e.arena_rank;
  register(id, { kind: "gem", symbol: e.symbol, name: e.name, chain: e.chain, token: e.token_address, pair: e.pair_address,
    pairUrl: e.pair_url, image: e.image, cap: e.market_cap, change: e.change_24h != null ? e.change_24h / 100 : null,
    ageTs: e.launched_at, rank, context: `${a.label} · #${rank} of ${a.size} · score ${Math.round(e.score ?? 0)}` });
  const html = tokenRow({ id, symbol: e.symbol, name: `${rank}. ${e.name || e.symbol}`, chain: e.chain, token: e.token_address,
    image: e.image, cap: e.market_cap, change: e.change_24h != null ? e.change_24h / 100 : null, ageTs: e.launched_at,
    score: e.score, badge: e.early ? "early" : e.hidden ? "hidden" : null, sub: TRADING ? btSub(e.backtests) : marketSub(e) });
  return html.replace('<li class="trow"', `<li class="trow${rank === 1 ? " ar-champ" : ""}" style="--v:${Math.max(0, Math.min(100, Math.round(e.score ?? 0)))}%"`);
}

function arenaHtml(a, open = false) {
  const rows = a.entries.filter((e) => !isHidden(e.chain, e.token_address));
  if (!rows.length) return "";
  const inn = open ? rows : rows.filter((e) => e.in);
  const bench = open ? [] : rows.filter((e) => !e.in);
  return `<section class="arena" data-arena="${escapeHtml(a.key)}">
    <header class="ar-head"><h4>${escapeHtml(a.label)}</h4>${a.heat ? heatBar(a.heat) : ""}<span class="ar-count">${open ? `${rows.length} projects` : `${inn.length} in`} · median ${Math.round(a.median)}</span></header>
    <ol class="memes-list" data-top-rows>${inn.map((e) => row(e, a)).join("")}</ol>
    ${bench.length ? `<details class="ar-bench"><summary>${bench.length} below the arena median</summary>
      <ol class="memes-list" data-top-rows>${bench.map((e) => row(e, a)).join("")}</ol></details>` : ""}
  </section>`;
}

function paint() {
  if (!root || playing) return;
  delete root.dataset.loading;
  const firstPaint = !shown;
  // The podium is pound for pound: the projects leading their own arena by the most.
  let podium = root.querySelector("[data-top-podium]");
  if (!podium) {
    podium = document.createElement("div");
    podium.className = "top-hero";
    podium.dataset.topPodium = "";
    root.insertBefore(podium, root.firstChild);
  }
  podium.classList.remove("top-wait");
  podium.removeAttribute("role");
  const top3 = p4p.filter((e) => !isHidden(e.chain, e.token_address)).slice(0, 3).map((e) => {
    register(idOf(e), { kind: "gem", symbol: e.symbol, name: e.name, chain: e.chain, token: e.token_address,
      pair: e.pair_address, pairUrl: e.pair_url, image: e.image, cap: e.market_cap,
      change: e.change_24h != null ? e.change_24h / 100 : null, ageTs: e.launched_at,
      context: `Pound for pound · leads ${e.arena_label} by ${sigma(e.z)}` });
    return { id: idOf(e), symbol: e.symbol, value: `${sigma(e.z)} ${e.arena_label}`,
      logoHtml: tile(e.symbol, e.image, e.chain, e.token_address) };
  });
  const sig = top3.map((x) => x.id).join("|");
  if (top3.length) renderPodium(podium, top3, { still: podium.dataset.sig === sig });
  else podium.innerHTML = "";
  podium.dataset.sig = sig;

  const box = root.querySelector("[data-top-arenas]");
  if (only && !arenas.some((a) => a.key === only)) setOnly(null);
  const chips = arenas.length > 1 ? `<div class="ar-chips" role="group" aria-label="Arena">${[{ key: "", label: "All" }, ...arenas]
    .map((a) => `<button type="button" class="chip" data-arena-pick="${escapeHtml(a.key)}" aria-pressed="${(only ?? "") === a.key}">${escapeHtml(a.label)}</button>`).join("")}</div>` : "";
  withEntrance(box, () => {
    const html = chips + (only ? arenaHtml(arenas.find((a) => a.key === only), true) : arenas.map((a) => arenaHtml(a)).join(""));
    box.innerHTML = html || `<p class="muted pad">The arenas are filling up: projects appear as they pass the checks.</p>`;
  });

  // A narrowed view is not what the last visit saw: no comparison.
  const visible = onScreen();
  if (!visible) stale = true;
  const prev = !filtered && seen && visible ? seen : null;
  const crowns = Object.fromEntries(arenas.filter((a) => a.entries.length)
    .map((a) => [a.key, { id: idOf(a.entries[0]), symbol: a.entries[0].symbol }]));
  const changes = [];
  const lead = [];
  if (prev) {
    for (const a of arenas) {
      const was = prev[a.key];
      const now = crowns[a.key];
      if (!was || !now || was.id === now.id) continue;
      const li = box.querySelector(`[data-arena="${CSS.escape(a.key)}"] li.ar-champ`);
      if (li) { li.classList.add("rank-wait"); changes.push({ li, was }); }
      lead.push(`${now.symbol} took ${a.label} from ${was.symbol}`);
    }
  }
  if (arenas.length && !filtered && visible) {
    shown = true;
    stale = false;
    if (changes.length) sequence(changes, crowns);
    else { seen = crowns; writeSeen(seen); }
  }
  const total = arenas.reduce((n, a) => n + a.size, 0);
  const narrLine = filtered
    ? `<button type="button" class="top-narr-clear" data-top-narr-clear>${escapeHtml(narr.get().map((k) => labels[k] ?? k).join(", "))} ✕</button> `
    : "";
  const head = lead.length ? `${firstPaint ? "Since your last visit: " : ""}${lead.slice(0, 3).join(", ")}. ` : "";
  const bt = labStatus;
  root.querySelector("[data-top-note]").innerHTML = narrLine + escapeHtml(head
    + (total ? `${arenas.length} arenas · ${total} projects` : "")
    + (TRADING && bt?.total ? ` · backtests ${bt.ready}/${bt.total} ready` : ""))
    + (TRADING ? ' <button type="button" class="top-narr-clear" data-top-wallet>$10k wallet on these trades →</button>' : "");
}

// Without trading: measured usage (usage.py), what put the project here.
const compact = (n) => new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(n);

function marketSub(e) {
  const u = e.usage ?? {};
  const vol = u.vol_7d_avg ?? u.vol_24h;
  // Short enough for a phone row: "$812k/day · 15.2k holders".
  const parts = [vol != null ? `${usdShort(vol)}/day` : null,
    u.holders != null ? `${compact(u.holders)} holders` : null];
  const line = parts.filter(Boolean).join(" · ");
  return line || (e.liquidity_usd != null ? `Liquidity ${usdShort(e.liquidity_usd)}` : "");
}

// Its own view in the dock (Lorenzo, 07/10: "la sezione EARLY non la trovo").
function paintEarly(list) {
  const box = document.querySelector("[data-early-view]");
  if (!box) return;
  box.querySelector("[data-early-empty]").hidden = list.length > 0;
  box.querySelector("[data-early-rows]").innerHTML = list.map((e) => {
    const id = `gem:${e.chain}:${e.token_address}`;
    register(id, { kind: "gem", symbol: e.symbol, name: e.name, chain: e.chain, token: e.token_address, pair: e.pair_address,
      pairUrl: e.pair_url, image: e.image, cap: e.market_cap, change: e.change_24h != null ? e.change_24h / 100 : null,
      ageTs: e.launched_at, context: `Early · score ${Math.round(e.score ?? 0)}` });
    return tokenRow({ id, symbol: e.symbol, name: e.name || e.symbol, chain: e.chain, token: e.token_address,
      image: e.image, cap: e.market_cap, change: e.change_24h != null ? e.change_24h / 100 : null, ageTs: e.launched_at,
      score: e.score, badge: "early", sub: marketSub(e) });
  }).join("");
}

// The H24 lab's best backtest (strategy_lab.py), in the row: what the
// Strategies tab is for (Lorenzo, 28/09).
function btSub(b) {
  if (!b) return '<span class="bt-live" role="img" aria-label="Backtest on its way"><i></i><i></i><i></i></span>';
  const pct = (v) => { const r = Math.round(v ?? 0); return `${r > 0 ? "+" : ""}${r}%`; };
  const s = shownSummary(b);
  if (!s) return "bt: no entry";
  const win = s.win_rate != null ? ` · ${Math.round(s.win_rate)}% win` : "";
  return `${s.interval} ${pct(s.return_pct)} · ${s.trades} trades${win}${s.in_position ? " · long" : ""}`;
}

async function load() {
  try {
    const keys = narr.get();
    const top = await api.getTop(50, keys);
    filtered = keys.length > 0;
    // A page served before the arenas: one arena holding the old list.
    arenas = top?.arenas ?? [{ key: "all", label: "All projects", heat: 0, size: (top?.entries ?? []).length, median: 0,
      entries: (top?.entries ?? []).map((e, i) => ({ ...e, arena_rank: i + 1, in: true })) }];
    p4p = top?.p4p ?? [];
    labStatus = top?.backtests ?? null;
    paint();
    paintEarly(top?.early ?? []);   // the board's own early list, never narrowed by narrative
  } catch {
    // Keep the last view; with none yet, the cube says the next poll retries.
    const w = root.querySelector(".top-wait");
    if (!w) return;
    w.querySelector("engn-motion").setAttribute("state", "reconnect");
    w.querySelector("p").textContent = "Reconnecting…";
  }
}

/** Until the first arenas land, the podium's place holds the ENGN cube
 * working (the motion kit's loading state), not an empty gap. */
function waiting() {
  if (root.querySelector("[data-top-podium]")) return;
  const el = document.createElement("div");
  el.className = "top-hero top-wait";
  el.dataset.topPodium = "";
  el.setAttribute("role", "status");
  el.innerHTML = '<engn-motion state="loading" decorative></engn-motion><p>Loading the arenas…</p>';
  root.insertBefore(el, root.firstChild);
}

export function mountTop() {
  root = document.getElementById("top-picks");
  if (!root) return;
  waiting();
  root.addEventListener("click", (event) => {
    if (event.target.closest("[data-top-narr-clear]")) { narr.set([]); return; }
    const pick = event.target.closest("[data-arena-pick]");
    if (pick) { setOnly(pick.dataset.arenaPick || null); paint(); return; }
    if (event.target.closest("[data-top-wallet]")) { import("@onchain/sim-backtest.js").then((m) => m.openSimBacktest()); }
  });
  onHiddenChange(paint);
  // First time the arenas are on screen in this visit: now the comparison can play.
  new IntersectionObserver((seenNow) => {
    if (seenNow.some((x) => x.isIntersecting) && (!shown || stale) && arenas.length) paint();
  }).observe(root);
  document.addEventListener("visibilitychange", () => { if ((!shown || stale) && arenas.length && onScreen()) paint(); });
  narr.onChange(() => load());
  narr.coverage().then((list) => {
    labels = Object.fromEntries(list.map((n) => [n.key, n.label ?? n.key]));
    if (filtered) paint();
  });
  load();
  setInterval(() => document.visibilityState === "visible" && load(), POLL_MS);
}
