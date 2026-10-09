/**
 * The project database under the leaderboard: everything the Gem Finder has
 * seen, with its verdict and why (backend: projects_db.py). Search by name,
 * ticker, address or site. Filters (verdict, chain or layer, narratives) live
 * in one sheet behind the "Filters" button; what is picked shows as chips that
 * clear with a tap. The funnel line says how many projects are known, how
 * many sit in the cap band and where they stand.
 */
import * as api from "@onchain/api.js";
import { register } from "@onchain/detail.js";
import { escapeHtml } from "@onchain/format.js";
import { isHidden, onHiddenChange, withEntrance } from "@onchain/hidden.js";
import * as narr from "@onchain/narrative_pick.js";
import { closeSheet, openSheet, setBody, sheetBody } from "@onchain/sheets.js";

const PAGE = 30;
const CHAINS = { solana: "Solana", robinhood: "Robinhood", base: "Base", bsc: "BSC", ethereum: "Ethereum", arc: "Arc", layer: "Layer" };
const LABEL = { ranked: "Ranked", studied: "Studied", rejected: "Rejected", new: "Waiting", analyzing: "Analyzing" };
// Layer projects (layers.py) carry their type in overall.layer: filtered by
// type, tagged L0/L1/L2 in the row.
const LAYERS = { L0: "Layer 0", L1: "Layer 1", L2: "Layer 2" };
const VERDICTS = ["ranked", "studied", "rejected", "new"];
const KINDS = ["L0", "L1", "L2", "solana", "bsc", "ethereum", "base", "robinhood", "arc"];
const kindLabel = (k) => LAYERS[k] ?? CHAINS[k] ?? k;

let root = null;
let query = { q: "", status: "", kind: "", offset: 0 };
let stats = null;
let total = 0;
let items = [];
let typing = null;
let narratives = [];   // coverage items, most projects first

const usd = (v) => (v == null ? "—" : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `$${Math.round(v / 1e3)}k` : `$${Math.round(v)}`);
const num = (n) => (n ?? 0).toLocaleString("en-US");

function row(p) {
  const why = p.why ? `<small class="pj-why">${escapeHtml(p.why)}</small>` : "";
  // Only a ranked project's score competes with the board; the others show
  // theirs dimmed, with why they are out (the reason line below).
  const score = p.score == null ? "—" : Math.round(p.score);
  const out = p.status !== "ranked" && p.score != null;
  const site = p.website ? (() => { try { return new URL(p.website).hostname.replace(/^www\./, ""); } catch { return ""; } })() : "";
  const id = `gem:${p.chain}:${p.token}`;
  const layer = p.chain === "layer" && LAYERS[p.overall?.layer] ? p.overall.layer : "";
  register(id, {
    kind: "gem",
    symbol: p.symbol,
    name: p.name,
    chain: p.chain,
    token: p.token,
    image: p.image,
    cap: p.mcap,
    context: `Database · score ${score} · ${p.status}${p.why ? ` · ${p.why}` : ""}`,
  });
  return `<li data-status="${escapeHtml(p.status)}">
    <button type="button" class="pj-open" data-open="${escapeHtml(id)}">
      <span class="pj-score${out ? " is-out" : ""}" title="${out ? "Score, not on the board" : "Score"}">${score}</span>
      <span class="pj-name"><b>${escapeHtml(p.symbol ?? "?")}${layer ? ` <i class="pj-tag">${layer}</i>` : ""}</b> <span>${escapeHtml(p.name ?? "")}</span>
        <small>${layer ? LAYERS[layer] : CHAINS[p.chain] ?? escapeHtml(p.chain)} · cap ${usd(p.mcap)} · liq ${usd(p.liq)}${site ? ` · ${escapeHtml(site)}` : ""}${p.github ? " · code" : ""}</small>
        ${why}</span>
      <em class="pj-status">${LABEL[p.status] ?? escapeHtml(p.status)}</em>
    </button>
  </li>`;
}

function paintFunnel(st) {
  const band = st?.band_by_status ?? st?.by_status ?? {};
  root.querySelector("[data-projects-meta]").textContent = st ? `${num(st.by_chain?.solana ?? 0)} Solana · ${num(st.by_chain?.robinhood ?? 0)} Robinhood · ${num(st.by_chain?.base ?? 0)} Base · ${num(st.by_chain?.ethereum ?? 0)} Ethereum${st.by_chain?.arc ? ` · ${num(st.by_chain.arc)} Arc` : ""}${st.by_chain?.layer ? ` · ${num(st.by_chain.layer)} Layer` : ""}` : "";
  root.querySelector("[data-projects-funnel]").innerHTML = st
    ? `<b>${num(st.total)}</b> projects known · <b>${num(st.in_band)}</b> in the ${usd(st.band?.[0] ?? 10000)}–${usd(st.band?.[1] ?? 10000000)} band · <b>${num((band.studied ?? 0) + (band.ranked ?? 0))}</b> studied · <b>${num(band.rejected ?? 0)}</b> rejected · <b>${num(st.band_waiting ?? 0)}</b> waiting${st.top_rejections?.[0] ? ` (most often: ${escapeHtml(st.top_rejections[0].why)})` : ""}`
    : "";
}

function paintList() {
  const shown = items.filter((p) => !isHidden(p.chain, p.token));
  withEntrance(root.querySelector("[data-projects-list]"), () => {
    root.querySelector("[data-projects-list]").innerHTML = shown.length
      ? shown.map(row).join("")
      : `<li class="pj-empty">${narr.get().length ? "No project in these narratives yet."
        : query.q || query.status || query.kind ? "Nothing matches." : "The database is filling up: sources are being read."}</li>`;
  });
}

// What is picked, as chips under the search: a tap on one clears it.
function paintActive() {
  const keys = narr.get();
  const labels = Object.fromEntries(narratives.map((n) => [n.key, n.label]));
  const chips = [
    ...(query.status ? [[`status`, LABEL[query.status]]] : []),
    ...(query.kind ? [[`kind`, kindLabel(query.kind)]] : []),
    ...keys.map((k) => [`narr:${k}`, labels[k] ?? k]),
  ];
  const n = chips.length;
  root.querySelector("[data-projects-filter-n]").textContent = n ? String(n) : "";
  root.querySelector("[data-projects-filter]").classList.toggle("is-on", n > 0);
  const box = root.querySelector("[data-projects-active]");
  box.hidden = !n;
  box.innerHTML = chips.map(([k, l]) => `<button type="button" data-clear="${escapeHtml(k)}">${escapeHtml(l)} <span aria-hidden="true">✕</span></button>`).join("")
    + (n > 1 ? `<button type="button" class="pj-clear-all" data-clear="all">Clear all</button>` : "");
}

const opt = (attr, value, label, on, count) =>
  `<button type="button" ${attr}="${escapeHtml(value)}" aria-pressed="${on}">${escapeHtml(label)}${count != null ? ` <small>${num(count)}</small>` : ""}</button>`;

function filterSheet() {
  const by = stats?.by_status ?? {};
  const kindN = (k) => (LAYERS[k] ? stats?.by_layer?.[k] : stats?.by_chain?.[k]);
  const sel = new Set(narr.get());
  return `<div class="pjf">
    <section><h4>Verdict</h4><div class="pjf-opts">
      ${opt("data-f-status", "", "All", !query.status, stats?.total)}
      ${VERDICTS.map((v) => opt("data-f-status", v, LABEL[v], query.status === v, by[v] ?? 0)).join("")}
    </div></section>
    <section><h4>Type</h4><div class="pjf-opts">
      ${opt("data-f-kind", "", "All", !query.kind)}
      ${KINDS.map((k) => opt("data-f-kind", k, LAYERS[k] ? k : kindLabel(k), query.kind === k, kindN(k) ?? 0)).join("")}
    </div></section>
    ${narratives.length ? `<section><h4>Narratives <small>any of</small></h4><div class="pjf-opts">
      ${opt("data-f-narr", "", "All", !sel.size)}
      ${narratives.map((n) => opt("data-f-narr", n.key, n.label, sel.has(n.key), n.projects)).join("")}
    </div></section>` : ""}
    <footer><button type="button" class="pjf-reset" data-f-reset>Reset</button>
      <button type="button" class="pjf-done" data-f-done>Show <span data-f-total>${num(total)}</span></button></footer>
  </div>`;
}

function openFilters() {
  openSheet("Filters", "Pick what the database shows.");
  setBody(filterSheet());
  const box = sheetBody().querySelector(".pjf");
  const press = (attr, on) => box.querySelectorAll(`[${attr}]`).forEach((b) => b.setAttribute("aria-pressed", String(on(b.getAttribute(attr)))));
  box.addEventListener("click", (event) => {
    const b = event.target.closest("button");
    if (!b) return;
    if (b.hasAttribute("data-f-done")) { closeSheet(); return; }
    if (b.hasAttribute("data-f-reset")) { reset(); openFilters(); return; }
    if (b.hasAttribute("data-f-status")) {
      query = { ...query, status: b.dataset.fStatus, offset: 0 };
      press("data-f-status", (v) => v === query.status);
      load();
    } else if (b.hasAttribute("data-f-kind")) {
      query = { ...query, kind: b.dataset.fKind, offset: 0 };
      press("data-f-kind", (v) => v === query.kind);
      load();
    } else if (b.hasAttribute("data-f-narr")) {
      const key = b.dataset.fNarr;
      const keys = narr.get();
      const next = !key ? [] : keys.includes(key) ? keys.filter((k) => k !== key) : [...keys, key];
      press("data-f-narr", (v) => (v ? next.includes(v) : !next.length));
      narr.set(next);
    }
  });
}

function reset() {
  query = { ...query, status: "", kind: "", offset: 0 };
  if (narr.get().length) narr.set([]); else load();
}

async function load(append = false) {
  try {
    const params = { q: query.q, status: query.status, sort: "score", limit: PAGE, offset: query.offset };
    if (query.kind) params[LAYERS[query.kind] ? "layer" : "chain"] = query.kind;
    const keys = narr.get();
    if (keys.length) params.narrative = keys.join(",");
    const data = await api.getProjects(params);
    items = append ? [...items, ...(data.items ?? [])] : (data.items ?? []);
    total = data.total ?? 0;
    stats = data.stats ?? stats;
    paintActive();
    const shown = document.querySelector("#sheet [data-f-total]");
    if (shown) shown.textContent = num(total);
    delete root.dataset.loading;
    paintList();
    root.querySelector("[data-projects-more]").hidden = items.length >= (data.total ?? 0);
    paintFunnel(data.stats);
  } catch {
    delete root.dataset.loading;
  }
}

export function mountProjects() {
  root = document.getElementById("projects");
  if (!root) return;
  onHiddenChange(() => paintList());
  root.querySelector("[data-projects-q]").addEventListener("input", (event) => {
    clearTimeout(typing);
    typing = setTimeout(() => { query = { ...query, q: event.target.value.trim(), offset: 0 }; load(); }, 250);
  });
  root.querySelector("[data-projects-filter]").addEventListener("click", openFilters);
  root.querySelector("[data-projects-active]").addEventListener("click", (event) => {
    const k = event.target.closest("[data-clear]")?.dataset.clear;
    if (!k) return;
    if (k === "all") { reset(); return; }
    if (k === "status" || k === "kind") { query = { ...query, [k]: "", offset: 0 }; load(); return; }
    narr.set(narr.get().filter((x) => `narr:${x}` !== k));
  });
  narr.onChange(() => {
    query = { ...query, offset: 0 };
    load();
  });
  narr.coverage().then((list) => {
    narratives = list.slice().sort((a, b) => (b.projects ?? 0) - (a.projects ?? 0));
    paintActive();
  });
  root.querySelector("[data-projects-more]").addEventListener("click", () => {
    query = { ...query, offset: items.length };
    load(true);
  });
  load();
}
