/**
 * Ask the agent: a mic button on every screen. Speech becomes text in the
 * browser while you talk (Web Speech API, nothing recorded on the server),
 * you check it, send it, and DeepSeek answers with read-only access to the
 * site's data (POST /api/agent/ask). Without speech support the same sheet
 * takes typed questions.
 */
import * as api from "@onchain/api.js";
import { escapeHtml } from "@onchain/format.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const LANGS = ["it-IT", "en-US"];
const LANG_KEY = "ask.lang";

const MIC = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`;
const SEND = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>`;

function loadLang() {
  try { return LANGS.includes(localStorage.getItem(LANG_KEY)) ? localStorage.getItem(LANG_KEY) : LANGS[0]; }
  catch { return LANGS[0]; }
}

export function mountAsk() {
  if (document.getElementById("ask-fab")) return;
  let lang = loadLang();
  let rec = null;
  let listening = false;
  let spoken = false;           // the text came from the mic: the server is told to expect typos
  let base = "";                // text before this listening run
  let busy = false;

  const fab = document.createElement("button");
  fab.type = "button";
  fab.id = "ask-fab";
  fab.className = "ask-fab";
  fab.setAttribute("aria-label", "Ask the agent");
  fab.setAttribute("aria-expanded", "false");
  fab.innerHTML = MIC;

  const sheet = document.createElement("section");
  sheet.className = "ask-sheet";
  sheet.hidden = true;
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-label", "Ask the agent");
  sheet.innerHTML = `
    <header class="ask-head">
      <strong>Ask the agent</strong>
      <button type="button" class="ask-chip" data-ask-lang aria-label="Speech language"></button>
      <button type="button" class="ask-chip" data-ask-clear>Clear</button>
      <button type="button" class="ask-x" data-ask-close aria-label="Close">×</button>
    </header>
    <ol class="ask-log" data-ask-log aria-live="polite"></ol>
    <form class="ask-bar" data-ask-form>
      <button type="button" class="ask-mic" data-ask-mic aria-label="Speak" aria-pressed="false">${MIC}</button>
      <textarea rows="1" maxlength="1500" data-ask-text placeholder="${SR ? "Tap the mic and talk" : "Type your question"}"></textarea>
      <button type="submit" class="ask-send" data-ask-send aria-label="Send" disabled>${SEND}</button>
    </form>`;
  document.body.append(fab, sheet);

  const $ = (sel) => sheet.querySelector(sel);
  const log = $("[data-ask-log]");
  const text = $("[data-ask-text]");
  const mic = $("[data-ask-mic]");
  const send = $("[data-ask-send]");
  const langBtn = $("[data-ask-lang]");
  mic.hidden = !SR;
  langBtn.hidden = !SR;
  langBtn.textContent = lang.slice(0, 2).toUpperCase();

  const grow = () => {
    text.style.blockSize = "";
    if (text.value) text.style.blockSize = `${Math.min(text.scrollHeight + 2, 140)}px`;
  };
  const sync = () => { send.disabled = busy || !text.value.trim(); grow(); };

  function add(role, body) {
    const li = document.createElement("li");
    li.className = `ask-msg ask-${role}`;
    li.innerHTML = body;
    log.append(li);
    log.scrollTop = log.scrollHeight;
    return li;
  }

  function setListening(on) {
    listening = on;
    mic.setAttribute("aria-pressed", String(on));
    sheet.classList.toggle("is-listening", on);
    fab.classList.toggle("is-listening", on);
  }

  function startListening() {
    if (!SR || listening) return;
    rec = new SR();
    rec.lang = lang;
    rec.continuous = true;
    rec.interimResults = true;
    base = text.value.trim() ? `${text.value.trim()} ` : "";
    rec.onresult = (event) => {
      let finals = "", interim = "";
      for (let i = 0; i < event.results.length; i++) {
        const r = event.results[i];
        if (r.isFinal) finals += r[0].transcript; else interim += r[0].transcript;
      }
      text.value = (base + finals + interim).replace(/\s+/g, " ").trimStart();
      spoken = true;
      sync();
    };
    rec.onerror = (event) => {
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        add("err", "Microphone blocked: allow it in the browser settings, or type.");
      }
    };
    rec.onend = () => { setListening(false); rec = null; };
    try { rec.start(); setListening(true); } catch { setListening(false); }
  }

  function stopListening() {
    try { rec?.stop(); } catch { /* already stopped */ }
  }

  async function ask(question, voice) {
    busy = true;
    sync();
    add("me", escapeHtml(question));
    const wait = add("bot", `<span class="ask-dots" aria-label="Thinking"><i></i><i></i><i></i></span>`);
    try {
      const res = await api.askAgent(question, voice);
      wait.innerHTML = escapeHtml(res?.answer || "No answer.");
      if (sheet.hidden) fab.classList.add("has-new");
    } catch (err) {
      wait.className = "ask-msg ask-err";
      wait.textContent = err?.message || "The agent is not answering.";
    } finally {
      busy = false;
      sync();
      log.scrollTop = log.scrollHeight;
    }
  }

  function open() {
    sheet.hidden = false;
    fab.setAttribute("aria-expanded", "true");
    fab.classList.remove("has-new");
    if (SR && !text.value.trim()) startListening(); else text.focus();
  }

  function close({ refocus = true } = {}) {
    stopListening();
    sheet.hidden = true;
    fab.setAttribute("aria-expanded", "false");
    if (refocus) fab.focus({ preventScroll: true });
  }

  fab.addEventListener("click", () => (sheet.hidden ? open() : close()));
  $("[data-ask-close]").addEventListener("click", close);
  mic.addEventListener("click", () => (listening ? stopListening() : startListening()));
  langBtn.addEventListener("click", () => {
    lang = LANGS[(LANGS.indexOf(lang) + 1) % LANGS.length];
    langBtn.textContent = lang.slice(0, 2).toUpperCase();
    try { localStorage.setItem(LANG_KEY, lang); } catch { /* private mode */ }
    if (listening) { stopListening(); setTimeout(startListening, 250); }
  });
  $("[data-ask-clear]").addEventListener("click", () => {
    log.replaceChildren();
    api.askAgent("/reset").catch(() => {});
  });
  text.addEventListener("input", () => { if (!listening) spoken = false; sync(); });
  text.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $("[data-ask-form]").requestSubmit();
    }
  });
  $("[data-ask-form]").addEventListener("submit", (event) => {
    event.preventDefault();
    const q = text.value.trim();
    if (!q || busy) return;
    stopListening();
    const voice = spoken;
    text.value = "";
    spoken = false;
    sync();
    ask(q, voice);
  });
  // Non-blocking: the page stays usable, and a tap outside folds the panel
  // (the conversation and a pending answer stay; the tab shows a dot).
  document.addEventListener("pointerdown", (event) => {
    if (!sheet.hidden && !sheet.contains(event.target) && !fab.contains(event.target)) close({ refocus: false });
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !sheet.hidden) close();
  });
  sync();
}
