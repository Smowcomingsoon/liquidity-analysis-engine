/**
 * Long tasks that survive the app (Lorenzo, 01/10: "qualsiasi task [...]
 * deve continuare in background se io cambio finestra o butto giu'
 * l'applicazione [...] e arrivarmi una notifica al completamento [...] che
 * mi riporta esattamente al punto").
 *
 * A long task (DeepSeek re-check, manual scan, a question to the agent) is a
 * job on the server (backend app/jobs.py): the request returns its id at
 * once and the work goes on whatever the phone does. Here:
 * - the ids not yet shown live in localStorage, so a page the phone killed,
 *   or reloaded on a new version, picks them up again (`resume`);
 * - while the page is on screen they are polled with `visible=1`, so the
 *   server sends no push for a result already in front of you; when the
 *   page leaves the screen a beacon says so and the push goes out;
 * - the push (sw.js) opens /onchain-app.html?job=<id>#<view>; the kind's
 *   handler (detail.js, ask.js, sheets.js) puts you back where it started,
 *   with the result.
 * iPhone: web push only reaches the app added to the home screen (iOS 16.4+).
 */
import * as api from "@onchain/api.js";

const KEY = "lae.jobs";
const POLL_MS = 2000;
const handlers = new Map();        // kind -> { show(job, meta), pending?(meta) }
const waiters = new Map();         // id -> resolve, for a caller still on the page
let polling = false;

const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || {}; } catch { return {}; } };
const save = (all) => { try { localStorage.setItem(KEY, JSON.stringify(all)); } catch { /* private mode */ } };
function remember(id, meta) { const all = load(); all[id] = { ...meta, at: Date.now() }; save(all); }
function forget(id) { const all = load(); if (id in all) { delete all[id]; save(all); } }

/** Where the app is now, as the URL fragment the notification will reopen. */
export function here(extra = "") {
  const v = document.getElementById("app-shell")?.dataset.view || location.hash.slice(1).split("&")[0] || "gems";
  return `${v}${extra}`;
}

export function handle(kind, h) { handlers.set(kind, h); }

/* ------------------------------------------------------------ push */

const isIOS = () => /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
let subscribed = false;

function b64u(s) {
  const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4));
  return Uint8Array.from(b, (c) => c.charCodeAt(0));
}

async function subscribe() {
  if (subscribed || !("serviceWorker" in navigator) || !("PushManager" in window)) return;
  try {
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const { key } = await api.getPushKey();
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u(key) });
    }
    await api.pushSubscribe(sub.toJSON());
    subscribed = true;
  } catch { /* refused, or no push service: the job still finishes, the app shows it on return */ }
}

/** Call it straight from the tap that launches a task, before any await:
 * Safari asks for notifications only inside a user gesture. */
export function askPush() {
  if (!("Notification" in window) || !("PushManager" in window)) {
    if (isIOS() && !standalone()) installHint();
    return;
  }
  if (Notification.permission === "granted") { subscribe(); return; }
  if (Notification.permission === "default") {
    Notification.requestPermission().then((p) => { if (p === "granted") subscribe(); }, () => {});
  }
}

// iPhone in a Safari tab: no notifications at all until the app is on the
// home screen. Said once a week, small, dismissable.
function installHint() {
  try {
    const last = Number(localStorage.getItem("lae.installHint") || 0);
    if (Date.now() - last < 7 * 86400e3) return;
    localStorage.setItem("lae.installHint", String(Date.now()));
  } catch { /* show it anyway */ }
  if (document.querySelector(".bg-hint")) return;
  const el = document.createElement("div");
  el.className = "bg-hint";
  el.setAttribute("role", "status");
  el.innerHTML = `<span>To get a notification when this is done, tap <b>Share</b> then <b>Add to Home Screen</b>, and open DEGEN from there.</span><button type="button" aria-label="Close">×</button>`;
  el.querySelector("button").addEventListener("click", () => el.remove());
  document.body.append(el);
  setTimeout(() => el.remove(), 15000);
}

/* ------------------------------------------------------------ jobs */

/** Start a job and wait for it. `start()` returns the server's {job}; the
 * result comes back as the finished job ({state, result, error}). */
export async function run(kind, start, meta = {}) {
  const r = await start();
  if (!r?.job) return { state: "finito", result: r };
  return follow(r.job, kind, meta);
}

/** A job whose id the caller already has (the scan's stream): followed like
 * the others, `settle` when the caller has shown it. */
export function follow(id, kind, meta = {}) {
  remember(id, { kind, ...meta });
  return new Promise((resolve) => { waiters.set(id, resolve); poll(); });
}

export function settle(id) { waiters.delete(id); forget(id); }

const visible = () => document.visibilityState === "visible";

async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (visible() && Object.keys(load()).length) {
      for (const [id, meta] of Object.entries(load())) {
        let job;
        try { job = await api.getJob(id, true); }
        catch (err) {
          if (err?.status === 404 || Date.now() - (meta.at || 0) > 86400e3) {
            forget(id);
            const w = waiters.get(id);
            if (w) { waiters.delete(id); w({ state: "fallito", error: "the task was lost, run it again" }); }
          }
          continue;
        }
        if (job.state === "in corso") continue;
        const w = waiters.get(id);
        if (w) { waiters.delete(id); forget(id); w(job); }
        else show(job, meta);
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  } finally { polling = false; }
}

function show(job, meta) {
  forget(job.id);
  const h = handlers.get(job.kind ?? meta.kind);
  if (!h) return;
  const name = (job.view || meta.view || "").split("&")[0];
  if (name) document.dispatchEvent(new CustomEvent("lae:go", { detail: name }));
  try { h.show(job, { ...meta, view: job.view || meta.view || "" }); } catch (err) { console.error(err); }
}

// Off screen: tell the server now, so the end of the job is a push even if
// it was polled a second ago.
function away() {
  for (const id of Object.keys(load())) navigator.sendBeacon?.(`/api/jobs/${encodeURIComponent(id)}/away`);
}

async function openJob(id) {
  if (!id) return false;
  let job;
  try { job = await api.getJob(id, true); } catch { return false; }
  const meta = load()[id] ?? {};
  const w = waiters.get(id);
  if (job.state === "in corso") {
    remember(id, { kind: job.kind, view: job.view, ...meta });
    handlers.get(job.kind)?.pending?.({ ...meta, view: job.view, id });
    poll();
    return true;
  }
  if (w) { waiters.delete(id); w(job); }
  show(job, meta);
  return true;
}

/** At boot, signed in: the notification's link (?job=), the jobs a killed
 * page left behind, the service worker's messages. True when a link was
 * handled (the caller skips its own #token opening). */
export async function boot() {
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
    navigator.serviceWorker.addEventListener("message", (ev) => {
      if (ev.data?.type === "job") openJob(ev.data.job);
    });
  }
  if ("Notification" in window && Notification.permission === "granted") subscribe();
  document.addEventListener("visibilitychange", () => (visible() ? poll() : away()));
  window.addEventListener("pagehide", away);

  const url = new URL(location.href);
  const id = url.searchParams.get("job");
  if (id) {
    url.searchParams.delete("job");
    history.replaceState(history.state, "", url.pathname + url.search + url.hash);
  }
  const linked = id ? await openJob(id) : false;
  // Left running when the page went away: show where it was, then the result.
  for (const [jid, meta] of Object.entries(load())) {
    if (jid !== id) handlers.get(meta.kind)?.pending?.({ ...meta, id: jid });
  }
  poll();
  return linked;
}
