/**
 * Ask the agent: a mic button on every screen. Speech becomes text in the
 * browser while you talk (Web Speech API, nothing recorded on the server),
 * you check it, send it, and DeepSeek answers with read-only access to the
 * site's data (POST /api/agent/ask). Without speech support the same sheet
 * takes typed questions.
 *
 * Talk mode (Lorenzo, 01/10: "voglio un botta e risposta [...] lui deve
 * avere una voce"): a pause in speech sends the question, the answer is read
 * aloud, then the mic reopens for the next turn until the panel is closed.
 * Tapping the mic cuts the voice off. The voice is Piper on the server
 * (POST /api/agent/speak, a neural voice); the device's own speechSynthesis
 * is the fallback. While talk mode is on, listening is kept alive: phones
 * end recognition at every pause, so it restarts until the question is sent.
 */
import * as api from "@onchain/api.js";
import { escapeHtml } from "@onchain/format.js";

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const LANGS = ["it-IT", "en-US"];
const LANG_KEY = "ask.lang";
const TALK_KEY = "ask.talk";
const TTS = window.speechSynthesis;
const SILENCE_MS = 1700;        // a pause this long ends the spoken question
const WATCHDOG_MS = 1500;       // talk mode: reopen the mic if it went quiet
// A silent WAV: played on the opening tap so iOS lets later answers play.
const SILENT_WAV = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=";
const SPOKEN_MAX = 700;         // characters read aloud; the full text stays on screen

const MIC = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg>`;
const SEND = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h13M13 6l6 6-6 6"/></svg>`;

function loadTalk() {
  try { return localStorage.getItem(TALK_KEY) !== "0"; } catch { return true; }
}

// The best voice the device has for the language: the "enhanced"/natural
// ones first, then Google's, then any.
function pickVoice(lang) {
  const all = TTS?.getVoices() ?? [];
  const same = all.filter((v) => v.lang?.replace("_", "-").toLowerCase().startsWith(lang.slice(0, 2)));
  const rank = (v) => (/premium|enhanced|natural|neural/i.test(v.name) ? 0 : /google/i.test(v.name) ? 1 : v.localService ? 2 : 3);
  return same.sort((a, b) => rank(a) - rank(b))[0] ?? null;
}

// What a voice should not read: markdown marks, links, long hashes.
function speakable(answer, lang) {
  let t = answer.replace(/https?:\/\/\S+/g, "").replace(/0x[0-9a-f]{12,}|[1-9A-HJ-NP-Za-km-z]{32,}/gi, "")
    .replace(/[*_#`>|]+/g, "").replace(/^\s*[-•]\s*/gm, "").replace(/\s+/g, " ").trim();
  if (t.length > SPOKEN_MAX) t = `${t.slice(0, t.lastIndexOf(".", SPOKEN_MAX) + 1 || SPOKEN_MAX)} ${lang.startsWith("it") ? "Il resto è sullo schermo." : "The rest is on screen."}`;
  return t;
}

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
  let talk = SR ? loadTalk() : false;
  let speaking = false;
  let silence = 0;
  let blocked = false;          // mic refused, or it keeps dying: wait for a tap
  let restarts = [];            // recent restarts that heard nothing
  let serverVoice = true;       // false after the server says it has no voice
  const player = new Audio();

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
      <strong>Carlo</strong>
      <button type="button" class="ask-chip" data-ask-talk aria-pressed="false" title="Hands-free: it answers out loud">Voice</button>
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
  const talkBtn = $("[data-ask-talk]");
  mic.hidden = !SR;
  langBtn.hidden = !SR;
  talkBtn.hidden = !SR;
  const hint = () => {
    text.placeholder = !SR ? "Type your question" : speaking ? "Speaking… tap the mic to cut in"
      : busy ? "Thinking…" : listening ? (talk ? "Listening… pause to send" : "Listening…")
      : blocked ? "Tap the mic to talk" : talk ? "Tap the mic and talk" : "Tap the mic and talk, then send";
  };
  const showTalk = () => { talkBtn.setAttribute("aria-pressed", String(talk)); sheet.classList.toggle("is-talk", talk); hint(); };
  langBtn.textContent = lang.slice(0, 2).toUpperCase();

  const grow = () => {
    text.style.blockSize = "";
    if (text.value) text.style.blockSize = `${Math.min(text.scrollHeight + 2, 140)}px`;
  };
  const sync = () => { send.disabled = busy || !text.value.trim(); grow(); hint(); };

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
    hint();
  }

  function setSpeaking(on) {
    speaking = on;
    sheet.classList.toggle("is-speaking", on);
    hint();
  }

  function hush() {
    if (!speaking) return;
    setSpeaking(false);
    player.pause();
    try { TTS.cancel(); } catch { /* nothing playing */ }
  }

  // Read the answer aloud, then hand the turn back to the mic.
  function afterVoice() {
    if (!speaking) return;              // cut off by a tap: the tap already reopened the mic
    setSpeaking(false);
    if (talk && !sheet.hidden) startListening();
  }

  async function say(answer) {
    const line = speakable(answer, lang);
    if (!talk || !line || sheet.hidden) return;
    setSpeaking(true);
    if (serverVoice) {
      try {
        const blob = await api.speakAgent(line, lang.slice(0, 2));
        if (!speaking) return;
        const url = URL.createObjectURL(blob);
        player.onended = player.onerror = () => { URL.revokeObjectURL(url); afterVoice(); };
        player.src = url;
        await player.play();
        return;
      } catch (err) {
        if (err?.status === 503) serverVoice = false;
        if (!speaking) return;
      }
    }
    sayLocally(line);
  }

  function sayLocally(line) {
    if (!TTS) { afterVoice(); return; }
    const u = new SpeechSynthesisUtterance(line);
    u.lang = lang;
    const voice = pickVoice(lang);
    if (voice) u.voice = voice;
    u.rate = 1.05;
    u.onend = afterVoice;
    u.onerror = afterVoice;
    try { TTS.cancel(); } catch { /* idle */ }
    TTS.speak(u);
  }

  // Talk mode: the question goes out by itself after a pause.
  function sendSpoken() {
    clearTimeout(silence);
    if (talk && !busy && text.value.trim()) $("[data-ask-form]").requestSubmit();
  }

  function startListening() {
    if (!SR || listening || busy || speaking) return;
    blocked = false;
    rec = new SR();
    rec.lang = lang;
    rec.continuous = true;
    rec.interimResults = true;
    base = text.value.trim() ? `${text.value.trim()} ` : "";
    let heard = false;
    rec.onresult = (event) => {
      if (busy) return;                 // a late result after the question left
      heard = true;
      let finals = "", interim = "";
      for (let i = 0; i < event.results.length; i++) {
        const r = event.results[i];
        if (r.isFinal) finals += r[0].transcript; else interim += r[0].transcript;
      }
      text.value = (base + finals + interim).replace(/\s+/g, " ").trimStart();
      spoken = true;
      sync();
      clearTimeout(silence);
      if (talk && text.value.trim()) silence = setTimeout(() => { stopListening(); sendSpoken(); }, SILENCE_MS);
    };
    rec.onerror = (event) => {
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        if (!blocked) add("err", "Microphone blocked: allow it in the browser settings, or type.");
        blocked = true;
      }
    };
    // Phones end recognition at every pause. In talk mode the question goes
    // out on the silence timer, not here: reopen the mic and keep the text.
    rec.onend = () => {
      setListening(false);
      rec = null;
      if (!talk || sheet.hidden || busy || speaking || blocked) return;
      const now = Date.now();
      restarts = heard ? [] : [...restarts.filter((t) => now - t < 10000), now];
      if (restarts.length > 6) { blocked = true; hint(); return; }   // dying in a loop: wait for a tap
      setTimeout(startListening, 120);
    };
    try { rec.start(); setListening(true); } catch { setListening(false); rec = null; }
  }

  function stopListening() {
    clearTimeout(silence);
    try { rec?.stop(); } catch { /* already stopped */ }
  }

  async function ask(question, voice) {
    busy = true;
    sync();
    add("me", escapeHtml(question));
    const wait = add("bot", `<span class="ask-dots" aria-label="Thinking"><i></i><i></i><i></i></span>`);
    try {
      const res = await api.askAgent(question, voice, talk);
      const answer = res?.answer || "No answer.";
      wait.innerHTML = escapeHtml(answer);
      if (sheet.hidden) fab.classList.add("has-new");
      busy = false;
      say(answer);
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
    // iOS plays sound only after a tap: unlock both voices on this one.
    if (talk) {
      player.src = SILENT_WAV;
      player.play().catch(() => {});
      try { TTS?.speak(new SpeechSynthesisUtterance("")); } catch { /* no voice */ }
    }
    if (SR && !text.value.trim()) startListening(); else text.focus();
  }

  function close({ refocus = true } = {}) {
    hush();
    stopListening();
    sheet.hidden = true;
    fab.setAttribute("aria-expanded", "false");
    if (refocus) fab.focus({ preventScroll: true });
  }

  fab.addEventListener("click", () => (sheet.hidden ? open() : close()));
  $("[data-ask-close]").addEventListener("click", close);
  mic.addEventListener("click", () => {
    if (speaking) { hush(); startListening(); return; }
    if (listening) {
      if (talk && text.value.trim()) { stopListening(); sendSpoken(); return; }   // done talking: send now
      blocked = true;                   // otherwise a tap pauses the always-on mic
      stopListening();
      return;
    }
    startListening();
  });
  talkBtn.addEventListener("click", () => {
    talk = !talk;
    try { localStorage.setItem(TALK_KEY, talk ? "1" : "0"); } catch { /* private mode */ }
    if (!talk) hush();
    showTalk();
  });
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
  // Talk mode keeps the mic open: if recognition went quiet without a
  // question in flight or a voice speaking, reopen it.
  setInterval(() => {
    if (talk && !sheet.hidden && !busy && !speaking && !listening && !blocked) startListening();
  }, WATCHDOG_MS);
  TTS?.getVoices();               // Chrome fills the list on first ask
  showTalk();
  sync();
}
