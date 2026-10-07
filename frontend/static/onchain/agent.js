/**
 * The agent's presence: a living orb in the top bar and one line of what
 * DeepSeek is doing, always there (Lorenzo, 27/09: "dobbiamo dare l'idea
 * animata che tutto il sito sia gestito da DeepSeek [...] ci deve essere
 * qualcosa che si muove [...] un agent che esamina le cose").
 *
 * The orb breathes when idle and spins its rings while working (activity in
 * the last three minutes). The line cycles through the latest actions from
 * /api/agent; a tap opens the log. Nothing here is decoration only: every
 * line is something the server really did.
 */
import { scoutCube } from "@onchain/scout-cube.js";
import { mountQuantPanel } from "@onchain/quant-panel.js";
import * as api from "@onchain/api.js";
import { escapeHtml } from "@onchain/format.js";
import { mountAgentMap } from "@onchain/agent-map.js";

const POLL_MS = 15_000;
const SAY_MS = 4_000;

let el = null;
let data = null;
let lines = [];
let at = 0;

const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? "now" : s < 3600 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`;
};

function sayings() {
  if (!data) return ["Scout connecting"];
  const out = (data.items ?? []).slice(0, 6).map((a) => a.text);
  if (data.watching) out.push(`Watching ${data.watching} alerts`);
  const judged = data.today?.scanner ?? 0;
  if (judged) out.push(`${judged} token checks today`);
  return out.length ? out : [data.active ? "Scout watching" : "Scout offline"];
}

function say() {
  const target = el?.querySelector("[data-agent-say]");
  if (!target || !lines.length) return;
  at = (at + 1) % lines.length;
  target.classList.remove("is-in");
  void target.offsetWidth;
  target.textContent = lines[at];
  target.classList.add("is-in");
}

async function poll() {
  if (document.visibilityState !== "visible") return;
  try {
    data = await api.getAgent();
  } catch {
    return;
  }
  lines = sayings();
  el.dataset.state = !data.active ? "off" : data.working ? "working" : "idle";
}

export function mountAgent() {
  el = document.getElementById("agent");
  if (!el) return;
  el.addEventListener("click", () => document.dispatchEvent(new CustomEvent("lae:go", { detail: "agent" })));
  poll();
  setInterval(poll, POLL_MS);
  setInterval(say, SAY_MS);
}

/* --- the agent's own tab ------------------------------------------------ */

const VIEW_POLL_MS = 10_000;

let viewEl = null;
let viewData = null;
let viewItems = [];
let viewTimer = null;
let quantPanel = null;
let viewBusy = false;
let agentMap = null;
let mapReceivedAt = null;

function statusLine() {
  if (!viewData) return "Off";
  if (viewData.working) return "Working now";
  return viewData.active ? "Watching" : "Off";
}

const PLURAL = { backtest: "backtests", study: "projects studied", research: "deep reads", scanner: "token checks",
  judge: "board reviews", chat: "chats", scan: "scans" };

const compact = (n) => n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);

function chips() {
  // Two different things, never merged: AI calls (tokens spent) and work done
  // (every note, LLM or not). A token studied 30 times counts as one project.
  const calls = Object.values(viewData?.today ?? {}).reduce((a, n) => a + n, 0);
  const cap = viewData?.cap_usd ? ` · $${(viewData.spent_usd ?? 0).toFixed(2)} of $${viewData.cap_usd.toFixed(2)}` : "";
  const ai = calls ? `AI: ${calls} calls · ${compact(viewData?.tokens ?? 0)} tokens${cap}` : "";
  const distinct = viewData?.distinct ?? {};
  const work = Object.entries(viewData?.done ?? {})
    .map(([k, n]) => [k, distinct[k] ?? n])
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${compact(n)} ${escapeHtml(PLURAL[k] ?? k)}`)
    .join(" · ");
  return [ai, work].filter(Boolean).join(" — ") || "Nothing yet today";
}

const LIVE_S = 180;

function nowHtml() {
  const a = viewItems[0];
  if (!a || Date.now() / 1000 - a.t > LIVE_S) return "";
  return `<span class="ag-kind" data-kind="${escapeHtml(a.kind)}">${escapeHtml(a.kind)}</span>
    <p>${escapeHtml(a.text)}</p><i class="ag-scan" aria-hidden="true"></i>`;
}

function paintNow() {
  const box = viewEl?.querySelector("[data-ag-now]");
  if (!box) return;
  const html = nowHtml();
  box.hidden = !html;
  if (html && box.dataset.t !== String(viewItems[0].t)) {
    box.dataset.t = String(viewItems[0].t);
    box.innerHTML = html;
  }
}

function stepHtml(step, i) {
  const tone = step.startsWith("✗") ? "bad" : step.startsWith("✓") ? "good" : "plain";
  return `<li data-tone="${tone}" style="--i:${i}">${escapeHtml(step)}</li>`;
}

function entryHtml(a) {
  const steps = Array.isArray(a.steps) && a.steps.length
    ? `<details class="ag-steps"><summary>Reasoning</summary><ol>${a.steps.map(stepHtml).join("")}</ol></details>`
    : "";
  const outcome = a.outcome ? ` <b class="ag-out">${escapeHtml(a.outcome)}</b>` : "";
  return `<li class="ag-card" data-kind="${escapeHtml(a.kind)}" data-t="${a.t}">
    <header><span class="ag-kind" data-kind="${escapeHtml(a.kind)}">${escapeHtml(a.kind)}</span><small>${ago(a.t)}</small></header>
    <p>${escapeHtml(a.text)}${outcome}</p>${steps}</li>`;
}

function paint() {
  if (!viewEl) return;
  const list = viewEl.querySelector("[data-ag-list]");
  if (!list) return;
  list.innerHTML = viewItems.length
    ? viewItems.map(entryHtml).join("")
    : `<li class="ag-empty">No activity recorded yet today.</li>`;
  const more = viewEl.querySelector("[data-ag-more]");
  if (more) more.hidden = viewItems.length < 60;
}

function paintHero() {
  if (!viewEl) return;
  const orb = viewEl.querySelector(".ag-orb");
  if (orb) orb.dataset.state = !viewData ? "off" : viewData.working ? "working" : viewData.active ? "idle" : "off";
  const status = viewEl.querySelector("[data-ag-status]");
  if (status) status.textContent = statusLine();
  const today = viewEl.querySelector("[data-ag-today]");
  if (today) today.textContent = chips();
  const prio = viewEl.querySelector("[data-ag-prio]");
  if (prio) {
    const p = document.documentElement.dataset.trading === "on" ? viewData?.priority : null;
    if (p) {
      const total = (p.total ?? 0) - (p.no_data ?? 0);
      prio.textContent = `Priority: Strategies backtests ${p.ready ?? 0}/${total}`;
      prio.hidden = false;
    } else {
      prio.hidden = true;
    }
  }
  paintProgress();
  paintNow();
}

function progressParts(row) {
  const total = row.total ?? 0;
  const done = row.done ?? 0;
  const right = row.key === "scan" ? `${total} left` : `${done}/${total}`;
  const r = total > 0 ? Math.min(1, Math.max(0, done / total)) : 0;
  return { right, r };
}

function progressHtml(row) {
  const { right } = progressParts(row);
  const bar = row.key === "scan" ? "" : `<div class="ag-bar"><i style="transform:scaleX(0)"></i></div>`;
  return `<div class="ag-prog-item" data-key="${escapeHtml(row.key)}"><div class="ag-prog-row"><span>${escapeHtml(row.label)}</span><b>${escapeHtml(right)}</b></div>${bar}</div>`;
}

function paintProgress() {
  const box = viewEl?.querySelector("[data-ag-prog]");
  if (!box) return;
  const rows = viewData?.progress ?? [];
  const keys = rows.map((row) => row.key).join();
  if (box.dataset.keys !== keys) {
    box.innerHTML = rows.map(progressHtml).join("");
    box.dataset.keys = keys;
  }
  // bars start at 0 and grow to their value, then move in place on each poll
  requestAnimationFrame(() => rows.forEach((row, i) => {
    const item = box.children[i];
    if (!item) return;
    const { right, r } = progressParts(row);
    item.querySelector("b").textContent = right;
    const fill = item.querySelector(".ag-bar i");
    if (fill) fill.style.transform = `scaleX(${r})`;
  }));
}

function viewFeedback(message = "") {
  const notice = viewEl?.querySelector("[data-ag-feedback]");
  if (!notice) return;
  notice.textContent = message;
  notice.hidden = !message;
}

async function loadView() {
  if (viewBusy) return;
  viewBusy = true;
  try {
    quantPanel?.refresh();
    const [info, log] = await Promise.all([api.getAgent(), api.getAgentLog()]);
    viewData = info;
    viewItems = log.items ?? [];
    mapReceivedAt = Date.now();
    agentMap?.update({ info, items: log.items ?? [], receivedAt: mapReceivedAt, failed: false });
    paint();
    paintHero();
    viewFeedback();
  } catch {
    viewFeedback("Activity could not be loaded. Scout will retry automatically.");
    agentMap?.update({ failed: true });
    const status = viewEl?.querySelector("[data-ag-status]");
    if (status) status.textContent = "Connection unavailable";
    const today = viewEl?.querySelector("[data-ag-today]");
    if (today) today.textContent = "Today's activity is not available yet.";
  } finally {
    viewBusy = false;
  }
}

async function refreshView() {
  if (viewBusy || document.visibilityState !== "visible") return;
  viewBusy = true;
  try {
    quantPanel?.refresh();
    const [info, log] = await Promise.all([api.getAgent(), api.getAgentLog()]);
    const firstResponse = !viewData;
    viewData = info;
    mapReceivedAt = Date.now();
    agentMap?.update({ info, items: log.items ?? [], receivedAt: mapReceivedAt, failed: false });
    const fresh = (log.items ?? []).filter((a) => !viewItems.some((b) => b.t === a.t));
    if (fresh.length) {
      viewItems = [...fresh, ...viewItems];
      const list = viewEl?.querySelector("[data-ag-list]");
      if (list) {
        list.querySelector(".ag-empty")?.remove();
        list.insertAdjacentHTML("afterbegin", fresh.map(entryHtml).join(""));
        list.querySelectorAll(".ag-card").forEach((c, i) => { if (i < fresh.length) c.classList.add("is-new"); });
      }
    }
    if (firstResponse) paint();
    paintHero();
    viewFeedback();
  } catch {
    agentMap?.update({ failed: true });
    viewFeedback(viewData
      ? "Updates interrupted. Showing the last received activity; retrying automatically."
      : "Activity could not be loaded. Scout will retry automatically.");
  } finally {
    viewBusy = false;
  }
}

async function loadEarlier() {
  if (!viewItems.length) return;
  const before = viewItems[viewItems.length - 1].t;
  try {
    const log = await api.getAgentLog(before);
    const older = (log.items ?? []).filter((a) => !viewItems.some((b) => b.t === a.t));
    if (!older.length) return;
    viewItems = [...viewItems, ...older];
    const list = viewEl?.querySelector("[data-ag-list]");
    if (list) list.insertAdjacentHTML("beforeend", older.map(entryHtml).join(""));
  } catch {
    /* keep what is on screen */
  }
}

export function mountAgentView() {
  clearInterval(viewTimer);
  agentMap?.dispose();
  quantPanel?.dispose();
  agentMap = null;
  viewEl = document.getElementById("agent-view");
  if (!viewEl) return;
  viewEl.innerHTML = `
    <div class="ag-hero">
      <div class="ag-orb scout-cube" data-state="off" aria-hidden="true">${scoutCube}</div>
      <h2>Scout</h2>
      <p class="ag-status" data-ag-status>Connecting</p>
      <p class="ag-today" data-ag-today>Loading activity…</p>
      <p class="ag-prio" data-ag-prio hidden></p>
    </div>
    <div data-agent-map></div>
    <div data-quant-panel></div>
    <div class="ag-prog" data-ag-prog></div>
    <div class="ag-now" data-ag-now hidden></div>
    <h3 class="ag-list-title">Activity</h3>
    <p class="ag-feedback" data-ag-feedback role="status" hidden></p>
    <ul class="ag-list" data-ag-list aria-label="Scout activity"></ul>
    <button type="button" class="ag-more" data-ag-more hidden>Load earlier</button>`;
  viewEl.querySelector("[data-ag-more]")?.addEventListener("click", loadEarlier);
  agentMap = mountAgentMap(viewEl.querySelector("[data-agent-map]"), { loadJourney: api.getAgentJourney });
  // The quant panel is about real entries: gone with trading (Lorenzo, 07/10).
  if (document.documentElement.dataset.trading === "on") quantPanel = mountQuantPanel(viewEl.querySelector("[data-quant-panel]"));
  loadView();
  viewTimer = setInterval(() => {
    if (viewEl && document.getElementById("agent-view") && viewEl.offsetParent !== null) refreshView();
  }, VIEW_POLL_MS);
}
