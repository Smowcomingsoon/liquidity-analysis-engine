/**
 * La classifica: i progetti migliori fra quelli visti dal radar.
 *
 * Richiesta: "una specie di leaderboard [...] cose estetiche che facciano
 * capire primo posto, secondo posto, terzo posto [...] dinamica [...]
 * aggiornata in tempo reale [...] segnalare anche da quanto e' che sono
 * usciti".
 *
 * Il podio per i primi tre, poi una riga a testa. Tre barrette per riga:
 * GitHub, sito, tokenomics (vedi app/trading/onchain/depth.py). Un tocco
 * apre i numeri che giustificano il voto. Quando l'ordine cambia le righe
 * scivolano al posto nuovo, con la freccia di quanto sono salite o scese.
 */
import * as api from "@onchain/api.js";
import { escapeHtml } from "@onchain/format.js";

const ROOT = document.getElementById("onchain-embed") ?? document;
const POLL_MS = 10_000;
const MOVE_SHOWN_S = 15 * 60;
const PILLARS = [["github", "GitHub", "G"], ["site", "Website", "S"], ["tokenomics", "Tokenomics", "T"]];
const CHAINS = { solana: "Solana", robinhood: "Robinhood" };
const PLACES = ["First", "Second", "Third"];

let root = null;
let data = null;
let version = -1;
let open = null;
let timer = null;

const key = (e) => `${e.chain}:${e.token_address}`;
const usd = (v) => (v == null ? "—" : v >= 1e6 ? `$${(v / 1e6).toFixed(v >= 1e7 ? 0 : 1)}M`
  : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${Math.round(v)}`);
const since = (ts) => {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 3600 ? `${Math.max(1, Math.round(s / 60))} min` : s < 172_800 ? `${Math.round(s / 3600)} h`
    : s < 86_400 * 60 ? `${Math.round(s / 86_400)} d` : `${Math.round(s / (86_400 * 30))} mo`;
};
const score = (v) => (v == null ? "—" : Math.round(v));
const yes = (v) => (v ? "yes" : "no");

export function mountLeaderboard() {
  root = ROOT.querySelector("#board");
  if (!root) return;
  root.addEventListener("click", (event) => {
    const row = event.target.closest("[data-board]");
    if (!row || event.target.closest("a")) return;
    open = open === row.dataset.board ? null : row.dataset.board;
    paint(false);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  setInterval(() => data && paintAges(), 30_000);
  refresh();
}

async function refresh() {
  clearTimeout(timer);
  try {
    const next = await api.getLeaderboard();
    if (next.version !== version) {
      data = next;
      version = next.version;
      paint(true);
    }
  } catch {
    // Come il radar: se non risponde resta com'era.
  }
  if (document.visibilityState === "visible") timer = setTimeout(refresh, POLL_MS);
}

// ------------------------------------------------------------------ disegno

function paint(animate) {
  root.hidden = false;
  delete root.dataset.loading;
  const before = animate ? positions() : null;
  const entries = data.entries ?? [];
  const podium = root.querySelector("[data-board-podium]");
  const list = root.querySelector("[data-board-list]");
  podium.innerHTML = entries.slice(0, 3).map(podiumCard).join("");
  // Sul podio il secondo sta a sinistra del primo, come nelle gare.
  podium.dataset.count = String(Math.min(entries.length, 3));
  list.innerHTML = entries.slice(3).map(row).join("");
  const detail = root.querySelector("[data-board-detail]");
  const chosen = entries.find((e) => key(e) === open);
  detail.hidden = !chosen;
  detail.innerHTML = chosen ? details(chosen) : "";
  if (chosen && chosen.rank > 3) {
    list.querySelector(`[data-board="${CSS.escape(open)}"]`)?.after(detail);
  } else {
    podium.after(detail);
  }
  root.querySelector("[data-board-empty]").hidden = entries.length > 0;
  root.querySelector("[data-board-empty]").textContent = emptyText();
  paintMeta();
  if (before) slide(before);
}

function positions() {
  const out = new Map();
  root.querySelectorAll("[data-board]").forEach((el) => out.set(el.dataset.board, el.getBoundingClientRect()));
  return out;
}

/** FLIP: ogni riga parte da dove stava e scivola al posto nuovo. */
function slide(before) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  root.querySelectorAll("[data-board]").forEach((el) => {
    const was = before.get(el.dataset.board);
    if (!was) { el.classList.add("is-new"); return; }
    const now = el.getBoundingClientRect();
    const dx = was.left - now.left;
    const dy = was.top - now.top;
    if (Math.abs(dx) < 1 && Math.abs(dy) < 1) return;
    el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
      { duration: 620, easing: "cubic-bezier(.2,.8,.2,1)" });
  });
}

function move(e) {
  if (!e.moved_at || Date.now() / 1000 - e.moved_at > MOVE_SHOWN_S) return "";
  if (e.rank_before == null) return `<i class="bd-move" data-dir="new">new</i>`;
  const d = e.rank_before - e.rank;
  if (!d) return "";
  return `<i class="bd-move" data-dir="${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"}${Math.abs(d)}</i>`;
}

function bars(e) {
  return `<span class="bd-bars">${PILLARS.map(([k, label, letter]) => {
    const v = e.pillars?.[k]?.score ?? 0;
    return `<span title="${label} ${v}" style="--v:${v}"><b>${letter}</b></span>`;
  }).join("")}</span>`;
}

function badges(e) {
  const out = [];
  if (e.benchmark) out.push(`<em data-tone="gold">benchmark</em>`);
  else if (e.similarity?.score >= 25) out.push(`<em class="bd-sim">≈ DREGG ${e.similarity.score}%</em>`);
  if (e.exception) out.push(`<em data-tone="gold">exception</em>`);
  if (e.bonding) out.push(`<em>pump.fun</em>`);
  return out.join("");
}

function podiumCard(e, i) {
  const k = key(e);
  return `<li data-place="${i + 1}"${open === k ? ' aria-expanded="true"' : ""}>
    <button type="button" data-board="${escapeHtml(k)}" aria-label="${PLACES[i]} place, ${escapeHtml(e.symbol)}">
      <span class="bd-medal" aria-hidden="true">${i + 1}</span>
      <span class="bd-sym"><b>${escapeHtml(e.symbol)}</b>${move(e)}</span>
      <span class="bd-total">${score(e.score)}</span>
      ${bars(e)}
      <small><span data-age="${e.launched_at ?? ""}">${since(e.launched_at)}</span> · ${usd(e.market_cap)}</small>
      <span class="bd-tags">${badges(e)}</span>
      ${e.benchmark ? `<p class="bd-honor">Honorable mention</p>` : ""}
    </button>
  </li>`;
}

function row(e) {
  const k = key(e);
  return `<li${open === k ? ' aria-expanded="true"' : ""}>
    <button type="button" data-board="${escapeHtml(k)}">
      <span class="bd-rank">${e.rank}</span>
      <span class="tk-name"><b>${escapeHtml(e.symbol)} ${move(e)}</b>
        <small>launched <span data-age="${e.launched_at ?? ""}">${since(e.launched_at)}</span> ago · ${usd(e.market_cap)} · ${CHAINS[e.chain] ?? e.chain} ${badges(e)}</small></span>
      ${bars(e)}
      <span class="bd-total">${score(e.score)}</span>
    </button>
  </li>`;
}

function fact(label, value) {
  return value == null || value === "" ? "" : `<div><dt>${label}</dt><dd>${value}</dd></div>`;
}

function pillarBlock(e, k, label) {
  const p = e.pillars?.[k] ?? {};
  const f = p.facts ?? {};
  let rows = "";
  if (k === "github") {
    rows = fact("Repository", f.url ? `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(f.repo)}</a>` : null)
      + fact("Commits, 90 days", f.commits_90d) + fact("Active weeks (of 12)", f.active_weeks_12)
      + fact("Authors", Math.max(f.authors ?? 0, f.contributors ?? 0)) + fact("Last commit", f.last_commit_days == null ? null : `${f.last_commit_days} d ago`)
      + fact("Code", f.code_kb == null ? null : `${f.code_kb} KB · ${(f.languages ?? []).join(", ")}`)
      + fact("Test · CI", `${yes(f.tests)} · ${yes(f.ci)}`) + fact("Trivial commits", f.trivial_pct == null ? null : `${f.trivial_pct}%`)
      + fact("Stars", f.stars);
  } else if (k === "site") {
    rows = fact("Website", f.url ? `<a href="${escapeHtml(f.url)}" target="_blank" rel="noopener">${escapeHtml(new URL(f.url).hostname)}</a>` : null)
      + fact("Words", f.words) + fact("Internal pages", f.internal_pages == null ? null : `${f.internal_pages} · ${f.pages_ok ?? 0} load with content`)
      + fact("Docs", f.docs == null ? null : f.docs_ok ? "yes, reachable" : f.docs ? "linked, empty" : "no")
      + fact("App", f.app == null ? null : f.app_ok ? "yes, responds" : f.app ? "linked, not responding" : "no")
      + fact("Sections", (f.sections ?? []).join(", ") || null)
      + fact("Domain", f.domain_days == null ? null : `registered ${f.domain_days} d ago`);
  } else {
    rows = fact("Top wallet", f.top1_pct == null ? null : `${f.top1_pct}%`) + fact("Top 10", f.top10_pct == null ? null : `${f.top10_pct}%`)
      + fact("Creator", f.creator_pct == null ? null : `${f.creator_pct}%`) + fact("Holder", f.holders)
      + fact("Locked liquidity", f.lp_locked_pct == null ? null : `${f.lp_locked_pct}%`)
      + fact("Mint · freeze", f.mint == null ? null : `${f.mint ? "active" : "revoked"} · ${f.freeze ? "active" : "revoked"}`)
      + fact("Tax", f.tax_pct == null ? null : `${f.tax_pct}%`);
  }
  const flags = (p.flags ?? []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
  return `<section style="--v:${p.score ?? 0}"><h5>${label}<b>${score(p.score)}</b></h5><dl>${rows}</dl>
    ${flags ? `<ul class="bd-flags">${flags}</ul>` : ""}</section>`;
}

function details(e) {
  const chart = e.pair_url ? `<a href="${escapeHtml(e.pair_url)}" target="_blank" rel="noopener">Chart</a>` : "";
  const scan = e.scan_url ? `<a href="${escapeHtml(e.scan_url)}" target="_blank" rel="noopener">Security report</a>` : "";
  return `<header><b>${escapeHtml(e.name ?? e.symbol)}</b>
      <small>${CHAINS[e.chain] ?? e.chain} · launched ${since(e.launched_at)} ago · liquidity ${usd(e.liquidity_usd)}${e.change_24h != null ? ` · 24h ${e.change_24h > 0 ? "+" : ""}${e.change_24h}%` : ""}</small>
      <span>${chart}${scan}</span></header>
    ${e.honor ? `<p class="bd-honor" style="text-align:start;margin:0 0 12px">${escapeHtml(e.honor)}</p>` : ""}
    ${traits(e)}
    <div class="bd-pillars">${PILLARS.map(([k, label]) => pillarBlock(e, k, label)).join("")}</div>`;
}

function traits(e) {
  const t = e.similarity?.traits ?? [];
  if (!t.length) return "";
  const head = e.benchmark ? "The benchmark" : `Technical similarity to DREGG · ${e.similarity.score}%`;
  return `<p class="bd-honor" style="text-align:start;margin:0 0 6px">${head}</p><ul class="bd-traits">${t.map((x) =>
    `<li data-got="${x.got >= 1 ? "1" : x.got > 0 ? "half" : "0"}">${escapeHtml(x.label)}</li>`).join("")}</ul>`;
}

function paintAges() {
  root.querySelectorAll("[data-age]").forEach((el) => {
    const ts = Number(el.dataset.age);
    if (ts) el.textContent = since(ts);
  });
  paintMeta();
}

function paintMeta() {
  const meta = root.querySelector("[data-board-meta]");
  const d = data ?? {};
  const gh = d.github_rate?.core;
  const starved = !gh ? "" : !d.github_rate.token || gh.limit <= 60 ? " · GitHub limited: no token"
    : gh.remaining === 0 ? " · GitHub paused" : "";
  meta.textContent = `${d.examined ?? 0} examined${d.analyzing ? ` · ${d.analyzing} under analysis` : ""}${starved}`;
}

function emptyText() {
  const d = data ?? {};
  return d.waiting
    ? `No project has passed the full analysis yet. ${d.waiting} waiting on data (GitHub, RugCheck or website).`
    : "No project has passed the GitHub, website and tokenomics checks yet.";
}
