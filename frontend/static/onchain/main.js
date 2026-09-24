/**
 * Punto d'ingresso della schermata wallet.
 *
 * Tiene insieme la sessione, il disegno della schermata per la chain
 * scelta, e il collegamento fra i pannelli sempre visibili (grafico,
 * ordine, posizioni/ordini/storico/strategie) e i pochi dialog rimasti
 * (Ricevi, Limiti, Simula, Rivela chiave). Tutto il resto sta nei moduli
 * accanto — questo file monta e smonta, non disegna i form.
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { freshness, formatToken, formatUsd, shortAddress, usdParts } from "@onchain/format.js";
import { startReveal } from "@onchain/reveal.js";
import * as sheets from "@onchain/sheets.js";
import { drawSparkline } from "@onchain/sparkline.js";
import { appendHistory, chainInfo, readCache, readHistory, state, writeCache } from "@onchain/store.js";
import { mountSwap } from "@onchain/swap.js";
import { mountLimitOrder, mountOpenOrders, mountHistory } from "@onchain/limitOrders.js";
import { mountChart, onTokenChanged, resetForChainSwitch, setToken as setChartToken } from "@onchain/chart.js";
import { mountStrategies } from "@onchain/strategies.js";
import { mountPortfolio } from "@onchain/portfolio.js";
import { openSend } from "@onchain/send.js";
import { mountPilot, refreshPilot } from "@onchain/pilot.js";
import { mountRadar } from "@onchain/radar.js";
import { refreshHoldings } from "@onchain/holdings.js";
import { mountLeaderboard } from "@onchain/leaderboard.js";
import { mountMemes } from "@onchain/memes.js";

// Sulla pagina a se' (onchain-app.html) la radice e' il documento intero.
// Fusa dentro la tab Trading, invece, questo modulo viene importato dentro
// un contenitore con id "onchain-embed": restringere qui la ricerca evita
// che un id del wallet (nessuno oggi collide, ma un domani sì) catturi un
// elemento omonimo del resto del sito invece del suo.
const ROOT = document.getElementById("onchain-embed") ?? document;
const $ = (selector) => ROOT.querySelector(selector);
const $$ = (selector) => ROOT.querySelectorAll(selector);

const view = {
  login: $("#login-screen"),
  shell: $("#app-shell"),
  balance: $("#balance"),
  address: $("#address"),
  chainLabel: $("#chain-label"),
  chainIcon: $("#chain-ic"),
  chainFlip: $("#chain-flip"),
  fresh: $("#fresh"),
  liveDot: $("#live-dot"),
  spark: $("#spark"),
  status: $("#status"),
  statusText: $("#status-text"),
  marketOrderRow: $("#market-order-row"),
  marketPanel: $("#market-panel"),
  orderPanelBody: $("#order-panel-body"),
  bottomPanel: $("#bottom-panel"),
  bottomPanelBody: $("#bottom-panel-body"),
  topbarAddress: $("#topbar-address-text"),
  statusSub: $("#status-sub"),
  sniperTags: $("#sniper-tags"),
  sniperCard: $("#sniper-card"),
  sniperToggle: $("#sniper-toggle"),
  sniperPulse: $("#sniper-pulse"),
  sniperStateText: $("#sniper-state-text"),
  actions: $("#actions"),
  reveal: $("#reveal-row"),
  more: $("#more-btn"),
  pilot: $("#pilot"),
  moreSheet: $("#more"),
  empty: $("#onchain-empty-state"),
  dock: $("#dock"),
  holdings: $("#holdings"),
  walletView: $("#wallet-view"),
  toast: $("#toast"),
};

/* ------------------------------------------------------------------ chain */

const CHAIN_ICON = { solana: "◎", robinhood: "◆" };

function paintChainStatic() {
  const { label, unit, suffix } = chainInfo();
  const cached = readCache(state.chain);
  const wallet = state.wallets[state.chain];
  const hasWallet = Boolean(wallet || cached);

  view.chainLabel.textContent = view.chainFlip ? label : `${label}${suffix}`;
  if (view.chainFlip) view.chainFlip.title = `${label}${suffix} · tap to switch network`;
  if (view.chainIcon) view.chainIcon.textContent = CHAIN_ICON[state.chain] ?? "";
  for (const node of [view.actions, view.status, view.reveal, view.more, view.pilot, view.marketOrderRow, view.bottomPanel, view.sniperCard, view.dock, view.walletView]) {
    if (node) node.hidden = !hasWallet;
  }
  view.empty.hidden = hasWallet;
  view.balance.hidden = !hasWallet;
  view.address.hidden = !hasWallet;

  if (!hasWallet) {
    view.empty.querySelector("[data-chain-name]").textContent = label;
    view.spark.hidden = true;
    if (view.holdings) view.holdings.hidden = true;
    if (!["entry", "gems", "meme"].includes(view.shell.dataset.view)) setView("gems");
    return;
  }

  const address = wallet?.address ?? cached?.address ?? "";
  view.address.textContent = shortAddress(address);
  if (view.topbarAddress) {
    view.topbarAddress.textContent = shortAddress(address);
    view.topbarAddress.closest("button").dataset.full = address;
  }
  view.address.dataset.full = address;
  view.fresh.textContent = cached ? freshness(cached.at) : "waiting for first balance";
  paintBalance(cached, unit);
  drawSparkline(view.spark, readHistory(state.chain), sparkFormatter());
}

function sparkFormatter() {
  const usdMode = readCache(state.chain)?.native_usd_value != null;
  return (value) => (usdMode ? formatUsd(value) : formatToken(value));
}

// Un tocco sulla cifra cambia unita': nativa (SOL/ETH), dollari, BTC, e
// si ricomincia. Richiesta: "se clicco su ETH mi switchi la visualizzazione
// in dollari, in Bitcoin e basta". Le viste senza prezzo si saltano, mai
// un valore inventato; la scelta resta fra una visita e l'altra.
const BALANCE_MODES = ["native", "usd", "btc"];
const BALANCE_MODE_KEY = "onchain.balanceMode";
let balanceMode = "usd";
try { balanceMode = localStorage.getItem(BALANCE_MODE_KEY) || "usd"; } catch { /* resta "usd" */ }
let lastPainted = null;

function balanceModes(reading) {
  const usdValue = reading.native_usd_value;
  return BALANCE_MODES.filter((mode) =>
    mode === "native" || (usdValue != null && (mode === "usd" || reading.btc_price_usd > 0)));
}

function paintBalance(reading, unit) {
  lastPainted = reading ? { reading, unit } : null;
  if (!reading) { view.balance.textContent = "—"; return; }
  const modes = balanceModes(reading);
  const mode = modes.includes(balanceMode) ? balanceMode : modes.includes("usd") ? "usd" : "native";
  view.balance.dataset.mode = mode;
  view.balance.title = modes.length > 1 ? "Tap to switch unit" : "";
  if (mode === "usd") {
    const { currency, integer, fraction } = usdParts(reading.native_usd_value);
    view.balance.innerHTML =
      `<span class="unit-mark">${currency}</span>${integer}<span class="cents">${fraction}</span>`;
    return;
  }
  if (mode === "btc") {
    const btc = reading.native_usd_value / reading.btc_price_usd;
    view.balance.innerHTML = `${formatToken(btc)}<span class="cents"> BTC</span>`;
    return;
  }
  view.balance.innerHTML = `${formatToken(reading.native_balance)}<span class="cents"> ${unit}</span>`;
}

function cycleBalanceMode() {
  if (!lastPainted) return;
  const modes = balanceModes(lastPainted.reading);
  if (modes.length < 2) return;
  const current = modes.indexOf(view.balance.dataset.mode);
  balanceMode = modes[(current + 1) % modes.length];
  try { localStorage.setItem(BALANCE_MODE_KEY, balanceMode); } catch { /* solo per questa visita */ }
  paintBalance(lastPainted.reading, lastPainted.unit);
}

view.balance.setAttribute("role", "button");
view.balance.tabIndex = 0;
view.balance.addEventListener("click", cycleBalanceMode);
view.balance.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  cycleBalanceMode();
});

async function selectChain(next) {
  if (next === state.chain) return;
  state.chain = next;
  state.balance = null;
  // Il saldo e l'indirizzo hanno un view-transition-name: il browser li
  // interpola da una chain all'altra invece di farli sparire e riapparire.
  const swap = () => paintChainStatic();
  if (document.startViewTransition) await document.startViewTransition(swap).updateCallbackDone;
  else swap();
  resetForChainSwitch(); // il token in vista non appartiene piu' alla chain nuova
  presetToken = null;
  loadChainData();
}

/* ------------------------------------------------------------------- dati */

function loadChainData() {
  if (view.empty.hidden === false) return;  // nessun wallet: niente da chiedere
  refreshBalance();
  refreshStatus();
  refreshSniperCard();
  refreshPilot();
  mountChart(view.marketPanel);
  mountOrderTab(currentOrderTab);
  mountBottomTab(currentBottomTab);
  paintHoldings(true);
}

function paintHoldings(force = false) {
  refreshHoldings(view.holdings, {
    force,
    // Il tocco su una moneta apre lo scambio con quella in vendita.
    onPick: (holding) => {
      setView("manual");
      if (currentOrderTab !== "swap") $('[data-order-tab="swap"]')?.click();
      window.dispatchEvent(new CustomEvent("onchain:swap-from", { detail: holding }));
    },
  });
}

// Ponte sniper -> trend EMA (2026-09-23): la card in alto dice quanti token
// della watchlist girano ora in auto-trade trend EMA su questa chain, e
// quali; l'interruttore sta nel pannello ordine. Tutti gli elementi sono
// opzionali: la copia del wallet dentro la tab Trading (index.html) non
// li ha, e un null qui fermerebbe l'intero modulo.
const TREND_EMA = "Lorenzo (trend-follower)";

function parseChainFromSymbol(symbol) {
  const m = /^.+@([a-z]+):.+$/.exec(symbol);
  return m ? m[1] : null;
}

async function trendAutoEntries(chain) {
  const entries = await api.getWatchlist();
  return entries.filter((e) => parseChainFromSymbol(e.symbol) === chain && e.auto_trade?.[TREND_EMA]);
}

async function refreshSniperCard() {
  if (!view.sniperCard && !view.sniperToggle) return;
  const chain = state.chain;
  try {
    const [{ chains }, active] = await Promise.all([api.getSniperAutotrade(), trendAutoEntries(chain)]);
    if (chain !== state.chain) return;
    paintSniperCard(chains.includes(chain), active);
  } catch { /* resta lo stato di default, spento */ }
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function paintSniperCard(enabled, active = []) {
  view.sniperToggle?.setAttribute("aria-checked", String(enabled));
  if (view.sniperPulse) view.sniperPulse.hidden = !enabled;
  if (view.sniperStateText) {
    view.sniperStateText.textContent = !enabled
      ? "Off"
      : active.length === 1 ? "1 token live" : `${active.length} tokens live`;
  }
  if (view.sniperTags) {
    const tags = active.slice(0, 3).map((e) => `<span>$${escapeHtml(e.symbol.split("@")[0])}</span>`);
    if (active.length > 3) tags.push(`<span>+${active.length - 3}</span>`);
    tags.push("<span>liquidity floor $3k</span>");
    view.sniperTags.innerHTML = tags.join("");
  }
}

view.sniperToggle?.addEventListener("click", async () => {
  const chain = state.chain;
  const next = view.sniperToggle.getAttribute("aria-checked") !== "true";
  view.sniperToggle.disabled = true;
  try {
    await api.setSniperAutotrade(chain, next);
    await refreshSniperCard();
    refreshPilot();
    toast(next ? "Autopilot on" : "Autopilot off", chainInfo().label);
  } catch (error) {
    toast("Didn't work", error instanceof ApiError ? error.message : "unexpected error");
  } finally {
    view.sniperToggle.disabled = false;
  }
});

// Tab "Sniper" del pannello ordine: i token che il ponte ha gia' messo in
// watchlist in auto-trade trend EMA su questa chain — sola lettura, lo
// spegnimento per token resta nel tab Strategie.
async function mountSniperTab(container) {
  container.innerHTML = "<p class='sheet-note'>Loading sniper tokens…</p>";
  const chain = state.chain;
  try {
    const active = await trendAutoEntries(chain);
    if (chain !== state.chain) return;
    container.innerHTML = active.length
      ? `<ul class="tx-list">${active.map((e) => `
          <li><span><b>${escapeHtml(e.symbol.split("@")[0])}</b></span><span class="strategy-tag">Trend EMA · auto</span></li>`).join("")}</ul>`
      : "<p class='sheet-note'>No tokens on auto-trade on this network. Flip the sniper on below: new tokens with $3k+ liquidity and under 72h old get in on their own.</p>";
  } catch (error) {
    container.innerHTML = `<p class="sheet-note is-error">${escapeHtml(error instanceof ApiError ? error.message : "Unexpected error.")}</p>`;
  }
}

let balanceRetry = null;

async function refreshBalance() {
  clearTimeout(balanceRetry);
  const chain = state.chain;
  try {
    const reading = await api.getBalance(chain);
    if (chain !== state.chain) return;  // l'utente ha cambiato chain nel frattempo
    state.balance = reading;
    writeCache(chain, reading);
    paintBalance(reading, chainInfo().unit);
    view.liveDot.classList.add("is-live");
    view.fresh.textContent = "updated just now";
    drawSparkline(view.spark, appendHistory(chain, reading.native_usd_value ?? reading.native_balance), sparkFormatter());
  } catch (error) {
    if (chain !== state.chain) return;
    view.fresh.textContent = `${error.message} — retrying`;
    balanceRetry = setTimeout(refreshBalance, 5000);
  }
}

// Una riga sola, e dice la cosa che puo' cambiare: se l'esecuzione reale e'
// armata. E' lo stesso interruttore che il backend legge prima di inviare
// qualunque transazione, non un'etichetta decorativa.
async function refreshStatus() {
  const chain = state.chain;
  try {
    const risk = await api.getRiskStatus(chain);
    if (chain !== state.chain) return;
    view.status.dataset.armed = String(Boolean(risk.execution_enabled));
    if (view.statusSub) {
      view.statusText.textContent = risk.execution_enabled ? "Live execution on" : "Live execution off";
      view.statusSub.textContent = risk.execution_enabled
        ? `Up to ${formatUsd(risk.per_trade_cap_usd)} per order · ${formatUsd(risk.daily_cap_usd)} per day`
        : "Quotes only · no auto-signing";
    } else {
      view.statusText.textContent = risk.execution_enabled
        ? `Live execution on — up to ${formatUsd(risk.per_trade_cap_usd)} per order, ${formatUsd(risk.daily_cap_usd)} per day.`
        : "Read-only — nothing gets signed from this page.";
    }
  } catch { /* resta la riga di default, che e' quella prudente */ }
}

/* --------------------------------------------------------- pannello ordine */
// Swap e Limite condividono lo stesso spazio, sempre visibile — due tab,
// non due bottoni che aprono due sheet diverse.

let currentOrderTab = "swap";

// Un token si sceglie una volta sola: quello scelto sul grafico diventa
// l'altra gamba di swap e limite, e quello scelto nel pannello ordine
// sposta il grafico. Prima erano due scelte separate, e da telefono il
// pannello ordine sta due schermate sotto il grafico — Richiesta: "se
// seleziono un token si aprono troppe schermate".
let presetToken = null;
let presetSeq = 0;
const tokenInfoCache = new Map();

async function resolvePreset(token) {
  const key = `${state.chain}:${token.address.toLowerCase()}`;
  if (!tokenInfoCache.has(key)) {
    const info = await api.getTokenInfo(state.chain, token.address);
    tokenInfoCache.set(key, {
      address: token.address,
      decimals: info.decimals,
      symbol: info.symbol || token.symbolHint || shortAddress(token.address),
      priceUsd: info.price_usd ?? null,
    });
  }
  return tokenInfoCache.get(key);
}

onTokenChanged(async (token) => {
  const seq = ++presetSeq;
  presetToken = null;
  if (token && !sheets.isNativeAddress(state.chain, token.address)) {
    try {
      presetToken = await resolvePreset(token);
    } catch (error) {
      toast("Token unverified", error.message);
    }
  }
  if (seq !== presetSeq || currentOrderTab === "sniper") return;
  mountOrderTab(currentOrderTab);
});

const followInChart = (token) => {
  presetToken = token;
  setChartToken({ address: token.address, symbolHint: token.symbol });
};

function mountOrderTab(tab) {
  currentOrderTab = tab;
  $$("#order-tabs .mo-side-tab").forEach((t) => {
    const active = t.dataset.orderTab === tab;
    t.classList.toggle("mo-tab-active", active);
    t.setAttribute("aria-selected", String(active));
  });
  if (tab === "swap") {
    mountSwap(view.orderPanelBody, {
      presetToken,
      onTokenPicked: followInChart,
      onExecuted: () => { refreshBalance(); mountBottomTab(currentBottomTab); toast("Swap sent", "check the signature in history"); },
    });
  } else if (tab === "sniper") {
    mountSniperTab(view.orderPanelBody);
  } else {
    mountLimitOrder(view.orderPanelBody, {
      presetToken,
      onTokenPicked: followInChart,
      onPlaced: () => { mountBottomTab(currentBottomTab); toast("Limit order placed", "it's waiting in history"); },
    });
  }
}

$$("#order-tabs .mo-side-tab").forEach((tab) => {
  tab.addEventListener("click", () => mountOrderTab(tab.dataset.orderTab));
});

/* -------------------------------------------------- pannello inferiore */
// Posizioni / Ordini aperti / Storico / Strategie: "cosa ho, cosa ho
// ordinato, cosa e' successo" in un colpo d'occhio, non tre pannelli
// separati da riaprire ogni volta.

let currentBottomTab = "positions";

function mountBottomTab(tab) {
  currentBottomTab = tab;
  $$("#bottom-tabs .mo-side-tab").forEach((t) => {
    const active = t.dataset.bottomTab === tab;
    t.classList.toggle("mo-tab-active", active);
    t.setAttribute("aria-selected", String(active));
  });
  const mounters = {
    positions: mountPortfolio,
    orders: mountOpenOrders,
    history: mountHistory,
    strategies: mountStrategies,
  };
  mounters[tab]?.(view.bottomPanelBody);
}

$$("#bottom-tabs .mo-side-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const name = tab.dataset.bottomTab;
    mountBottomTab(name);
  });
});

/* ------------------------------------------------------------------ toast */

let toastTimer = null;

function toast(title, detail) {
  clearTimeout(toastTimer);
  view.toast.querySelector(".toast-title").textContent = title;
  view.toast.querySelector(".toast-detail").textContent = detail;
  view.toast.hidden = false;
  toastTimer = setTimeout(() => { view.toast.hidden = true; }, 6000);
}

/* ---------------------------------------------------------------- comandi */

$$("[data-chain]").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$("[data-chain]").forEach((other) =>
      other.setAttribute("aria-selected", String(other === tab)));
    $("#chain-switch").dataset.selected = tab.dataset.chain;
    selectChain(tab.dataset.chain);
  });
});

view.address.addEventListener("click", async (event) => {
  const button = event.currentTarget;
  await navigator.clipboard?.writeText(button.dataset.full ?? "");
  toast("Address copied", shortAddress(button.dataset.full ?? ""));
});

const SHEETS = {
  receive: () => sheets.openReceive(),
  send: () => openSend({
    onSent: () => { refreshBalance(); paintHoldings(true); mountBottomTab(currentBottomTab); toast("Sent", "check the signature in history"); },
  }),
  risk: () => sheets.openRisk(() => { refreshStatus(); mountBottomTab(currentBottomTab); }),
  simulate: () => sheets.openSimulate({
    onAccepted: () => { mountBottomTab(currentBottomTab); refreshStatus(); toast("Simulation accepted", "logged to the ledger"); },
  }),
};
$$("[data-sheet]").forEach((button) =>
  button.addEventListener("click", () => SHEETS[button.dataset.sheet]?.()));

view.reveal.addEventListener("click", () => sheets.openKey({ onStart: startReveal }));

// Menu "⋯" (solo sulla pagina a se': la copia nella tab Trading non ce
// l'ha, da qui i ?.): si chiude da solo sul tocco di una voce (la voce apre il suo
// foglio) o fuori dal pannello.
view.more?.addEventListener("click", () => view.moreSheet.showModal());

// Two sections chosen at the door, each with its own purpose: Progetti
// (a round-the-clock scan of serious low/micro caps, ranked) and Meme
// (the sniper radar plus its autopilot). Then Scambia and Portafoglio.
// "entry" is the door itself; the dock hides there.
const VIEWS = ["gems", "meme", "manual", "wallet"];

function setView(name) {
  if (!VIEWS.includes(name) && name !== "entry") return;
  const from = view.shell.dataset.view;
  const paint = () => {
    view.shell.dataset.view = name;
    if (name === "gems" || name === "meme") view.shell.dataset.section = name;
    view.dock?.querySelectorAll("[data-go]").forEach((b) => {
      if (b.dataset.go === name) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    if (view.dock && name !== "entry") view.dock.dataset.at = String(VIEWS.indexOf(name));
    window.scrollTo({ top: 0 });
  };
  if (from !== name && document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    view.shell.dataset.dir = VIEWS.indexOf(name) > VIEWS.indexOf(from) ? "fwd" : "back";
    document.startViewTransition(paint);
  } else paint();
  if (name === "meme") refreshPilot();
  if (name === "wallet" && !view.bottomPanel.hidden) mountBottomTab(currentBottomTab);
  paintHoldings();
}
// Da computer la barra sta nella testata, al centro, come le tab di un DEX;
// da telefono resta in basso, sotto il pollice.
const wide = matchMedia("(width >= 1100px)");
function placeDock() {
  const topbar = document.querySelector(".topbar");
  if (!view.dock || !topbar) return;
  if (wide.matches) topbar.querySelector(".topbar-right")?.before(view.dock);
  else document.body.append(view.dock);
}
placeDock();
wide.addEventListener("change", placeDock);

for (const el of [view.dock, view.shell]) {
  el?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-go]");
    if (button) setView(button.dataset.go);
  });
}

mountPilot({ toast, onChange: refreshSniperCard });
mountRadar({ toast });
mountLeaderboard();
mountMemes();

// La rete si cambia dal suo nome, accanto al saldo: un tocco, niente menu.
view.chainFlip?.addEventListener("click", () => {
  view.chainFlip.classList.remove("is-flipping");
  void view.chainFlip.offsetWidth;
  view.chainFlip.classList.add("is-flipping");
  $('#chain-switch button[aria-selected="false"]')?.click();
});
view.moreSheet?.addEventListener("click", (event) => {
  if (event.target === view.moreSheet || event.target.closest("button")) view.moreSheet.close();
});

view.empty.querySelector("[data-create]").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    const wallet = await api.createWallet(state.chain);
    state.wallets[state.chain] = wallet;
    paintChainStatic();
    loadChainData();
    toast("Wallet created", shortAddress(wallet.address));
  } catch (error) {
    toast("Not created", error.message);
  } finally {
    button.disabled = false;
  }
});

view.toast.querySelector(".toast-close").addEventListener("click", () => { view.toast.hidden = true; });

/* ---------------------------------------------------------------- sessione */

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = $("#login-error");
  const button = form.querySelector("button");
  button.disabled = true;
  message.hidden = true;
  try {
    const fields = new FormData(form);
    await api.login(String(fields.get("email")).trim(), String(fields.get("password")));
    await boot();
  } catch (error) {
    message.textContent = error.message;
    message.hidden = false;
    button.disabled = false;
  }
});

// Sessione e wallet che il server ha gia' messo nell'HTML (onchain-app.html):
// due richieste in fila risparmiate. Si usa una volta sola — dopo un login
// o dentro index.html, che non lo porta, si torna a chiederli.
function takeBootData() {
  const node = document.getElementById("onchain-boot");
  if (!node) return null;
  node.remove();
  try { return JSON.parse(node.textContent); } catch { return null; }
}

async function boot() {
  const preloaded = takeBootData();
  let signedIn = false;
  if (preloaded) signedIn = Boolean(preloaded.user);
  else try { signedIn = Boolean((await api.getSession())?.user); } catch { /* offline: si tratta come non loggato */ }

  view.login.hidden = signedIn;
  view.shell.hidden = !signedIn;
  if (!signedIn) return;

  try {
    const wallets = preloaded?.wallets ?? await api.listWallets();
    for (const wallet of wallets) state.wallets[wallet.chain] = wallet;
  } catch { /* al primo carico senza rete resta la cache locale, se c'e' */ }

  paintChainStatic();
  loadChainData();
}

boot();
