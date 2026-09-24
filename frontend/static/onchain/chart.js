/**
 * Grafico ereditato dal sito principale — stesso motore (Lightweight
 * Charts + DexScreener/GeckoTerminal), non un secondo grafico scritto da
 * zero. L'endpoint delle candele (/api/onchain/chart) e' volutamente
 * leggero: niente strategia/backtest come in /api/analyze, che per un
 * grafico nel wallet sarebbero solo latenza sprecata.
 *
 * SEMPRE VISIBILE, non piu' dentro una sheet: la revisione "deve essere
 * come un exchange" (richiesta) e' arrivata anche a un team di esperti
 * consultato in questa sessione — nessun exchange nasconde il grafico
 * dietro un tap, e' la prima cosa che si vede.
 *
 * Due velocita', non una sola: le candele (storia, cambia poco) restano
 * su DexScreener/GeckoTerminal ogni 8s, ma il PREZZO CORRENTE aggiorna
 * ogni 2.5s via /api/onchain/price — lo stesso aggregatore/quoter gia'
 * usato per i preventivi di swap (Jupiter su Solana, QuoterV2 su
 * Robinhood Chain), fatto apposta per essere interrogato spesso.
 *
 * Segnali sul grafico: ogni ordine (swap o limite eseguito) di questo
 * token diventa un marker sulla candela in cui e' avvenuto; un ordine
 * limite ancora aperto diventa una riga orizzontale al prezzo target.
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { formatUsd } from "@onchain/format.js";
import * as sheets from "@onchain/sheets.js";
import { state } from "@onchain/store.js";

const CANDLE_REFRESH_MS = 8000; // storia: DexScreener/GeckoTerminal, non fatti per un polling piu' stretto
const PRICE_REFRESH_MS = 2500;  // corrente: lo stesso quoter dello swap, pensato per un polling stretto

const INTERVALS = ["15m", "1h", "4h", "1d"];

// Senza un token scelto il grafico parte dal nativo della chain invece di
// un riquadro vuoto: la prima cosa che si vede e' gia' un mercato.
const DEFAULT_TOKEN = {
  solana: { address: "So11111111111111111111111111111111111111112", symbolHint: "SOL" },
  robinhood: null,
};

let root = null;
let chart = null;
let series = null;
let candleTimer = null;
let priceTimer = null;
let currentToken = null;
let currentInterval = "4h";

// Chi altro deve sapere quale token sta guardando il grafico — il
// pannello ordine (swap/limite), per compilarsi da solo con la stessa
// coppia invece di farla scegliere due volte.
const listeners = new Set();
export const onTokenChanged = (fn) => listeners.add(fn);

export function getCurrentToken() {
  return currentToken;
}

// La libreria del grafico (160 KB) arriva con un <script async>: la pagina
// non la aspetta piu' per partire, e il grafico la aspetta insieme alle
// candele, che arrivano comunque dopo. In index.html e' uno script normale,
// quindi qui la trova gia' pronta.
function chartLibrary() {
  if (window.LightweightCharts) return Promise.resolve(window.LightweightCharts);
  const tag = document.querySelector('script[src*="lightweight-charts"]');
  if (!tag) return Promise.reject(new Error("chart library missing"));
  return new Promise((resolve, reject) => {
    tag.addEventListener("load", () => resolve(window.LightweightCharts), { once: true });
    tag.addEventListener("error", () => reject(new Error("chart library failed to load")), { once: true });
  });
}

// Il token scelto dal pannello ordine (pillola "Ricevi" dello swap): il
// grafico lo segue senza riavvisare i listener, che sono proprio il
// pannello da cui arriva — un giro solo, non un ping-pong.
export function setToken(token) {
  if (!root || !token || token.address === currentToken?.address) return;
  currentToken = token;
  paintShell();
  loadAndRender();
}

export function mountChart(container) {
  root = container;
  if (!currentToken && DEFAULT_TOKEN[state.chain]) currentToken = { ...DEFAULT_TOKEN[state.chain] };
  paintShell();
  if (currentToken) loadAndRender();
  else setStatus("Pick a token to see the chart and swap.");
}

function paintShell() {
  chart = null;
  series = null;
  const name = currentToken ? currentToken.symbolHint || shortAddr(currentToken.address) : "Pick a token";
  root.innerHTML = `
    <div class="chart-head">
      <div>
        <button type="button" class="token-head" id="market-pick-token" title="Change token">
          <span class="token-ic" aria-hidden="true"></span><span id="market-pair">${name}</span><span class="caret">▾</span>
        </button>
        <p class="price-line" id="market-live-price"></p>
      </div>
      <div class="tf-pills" role="tablist">
        ${INTERVALS.map((i) => `<button type="button" data-tf="${i}" class="${i === currentInterval ? "is-active" : ""}">${i}</button>`).join("")}
      </div>
    </div>
    <div id="market-chart-container" class="market-chart"></div>
    <div class="metrics-row" id="market-metrics"></div>
    <p class="swap-detail" id="market-status"></p>
  `;
  root.querySelector("#market-pick-token").addEventListener("click", () => {
    sheets.openTokenPicker(state.chain, {
      onPick: (token) => {
        currentToken = token;
        listeners.forEach((fn) => fn(token));
        paintShell();
        loadAndRender();
      },
    });
  });
  root.querySelectorAll("[data-tf]").forEach((btn) => btn.addEventListener("click", () => {
    if (!currentToken || btn.dataset.tf === currentInterval) return;
    currentInterval = btn.dataset.tf;
    paintShell();
    loadAndRender();
  }));
}

function compactUsd(value) {
  if (value == null) return "—";
  if (value >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  if (value >= 1e3) return `$${(value / 1e3).toFixed(1)}k`;
  return `$${value.toFixed(0)}`;
}

function formatAge(hours) {
  if (hours == null) return "—";
  if (hours < 48) return `${Math.max(1, Math.round(hours))}h`;
  return `${Math.round(hours / 24)}d`;
}

function paintStats(stats) {
  const pair = root?.querySelector("#market-pair");
  if (pair && stats.base_symbol) pair.textContent = `${stats.base_symbol} / ${stats.quote_symbol ?? "USD"}`;
  const metrics = root?.querySelector("#market-metrics");
  if (!metrics) return;
  const change = stats.price_change_h24;
  metrics.innerHTML = `
    <div>Liquidity<b>${compactUsd(stats.liquidity_usd)}</b></div>
    <div>Volume 24h<b>${compactUsd(stats.volume_h24)}</b></div>
    <div>Age<b>${formatAge(stats.age_hours)}</b></div>
    <div>24h chg<b class="${change > 0 ? "is-up" : change < 0 ? "is-down" : ""}">${change != null ? `${change > 0 ? "+" : ""}${change}%` : "—"}</b></div>
  `;
  root.dataset.change = change ?? "";
}

function shortAddr(address) {
  return `${address.slice(0, 4)}…${address.slice(-4)}`;
}

function setStatus(text) {
  const el = root?.querySelector("#market-status");
  if (el) el.textContent = text;
}

// Cambiare chain (Solana/Robinhood) invalida il token in vista: il
// grafico non deve mostrare una candela che non appartiene piu' alla
// chain selezionata.
export function resetForChainSwitch() {
  clearInterval(candleTimer);
  clearInterval(priceTimer);
  chart = null;
  series = null;
  currentToken = null;
}

function loadAndRender() {
  clearInterval(candleTimer);
  clearInterval(priceTimer);
  if (!root) return;
  const chain = state.chain;
  const container = root.querySelector("#market-chart-container");
  if (!container) return;

  const drawCandles = async () => {
    try {
      const [data, LightweightCharts] = await Promise.all([
        api.getChart(chain, currentToken.address, currentInterval),
        chartLibrary(),
      ]);
      if (!chart) {
        // Area menta, non candele: la linea del mockup approvato — il
        // prezzo di chiusura di ogni candela, stessi dati di prima.
        chart = LightweightCharts.createChart(container, {
          layout: { background: { color: "transparent" }, textColor: "#9aa0ab", fontSize: 11, attributionLogo: false },
          grid: { vertLines: { color: "rgba(255,255,255,0.03)" }, horzLines: { visible: false } },
          rightPriceScale: { borderVisible: false },
          timeScale: { timeVisible: true, borderVisible: false },
          crosshair: { vertLine: { color: "#5f6672" }, horzLine: { color: "#5f6672" } },
        });
        series = chart.addAreaSeries({
          lineColor: "#2de0a8", lineWidth: 2,
          topColor: "rgba(45,224,168,0.28)", bottomColor: "rgba(45,224,168,0.02)",
          priceLineVisible: false,
        });
      }
      series.setData(data.candles.map((c) => ({ time: c.time, value: c.close })));
      if (data.stats) paintStats(data.stats);
      await paintMarkersAndLimits(chain, data.symbol);
      setStatus("");
    } catch (error) {
      setStatus(error instanceof ApiError ? error.message : "Chart unavailable.");
    }
  };

  const drawLivePrice = async () => {
    const priceEl = root.querySelector("#market-live-price");
    if (!priceEl) return;
    try {
      const { price_usd } = await api.getLivePrice(chain, currentToken.address);
      const change = Number(root.dataset.change);
      priceEl.textContent = root.dataset.change
        ? `${formatUsd(price_usd)} · ${change > 0 ? "+" : ""}${change}% (24h)`
        : formatUsd(price_usd);
      priceEl.classList.toggle("is-down", change < 0);
      priceEl.classList.remove("is-warn");
    } catch (error) {
      priceEl.textContent = error instanceof ApiError ? error.message : "Live price unavailable.";
      priceEl.classList.add("is-warn");
    }
  };

  drawCandles();
  drawLivePrice();
  candleTimer = setInterval(drawCandles, CANDLE_REFRESH_MS);
  priceTimer = setInterval(drawLivePrice, PRICE_REFRESH_MS);
}

async function paintMarkersAndLimits(chain, symbol) {
  if (!series) return;
  try {
    const [history, openOrders] = await Promise.all([
      api.getHistory(chain),
      api.listLimitOrders(chain),
    ]);
    const tokenSymbol = symbol.split("@")[0];

    const markers = history.orders
      .filter((o) => o.status === "confirmed" && o.token === tokenSymbol)
      .map((o) => ({
        time: o.updated_at, position: o.side === "buy" ? "belowBar" : "aboveBar",
        color: o.side === "buy" ? "#2de0a8" : "#ff5c5c", shape: o.side === "buy" ? "arrowUp" : "arrowDown",
        text: o.side === "buy" ? "Buy" : "Sell",
      }))
      .sort((a, b) => a.time - b.time);
    series.setMarkers(markers);

    series.createPriceLine && openOrders
      .filter((o) => o.status === "open" && (o.pair.startsWith(`${tokenSymbol}→`) || o.pair.endsWith(`→${tokenSymbol}`)))
      .forEach((o) => {
        series.createPriceLine({
          price: o.target_price_usd, color: "#f0b90b", lineWidth: 1, lineStyle: 2,
          axisLabelVisible: true, title: o.direction === "buy_below" ? "limit buy" : "limit sell",
        });
      });
  } catch { /* i marker/target sono un di piu': un grafico senza di loro resta comunque utile */ }
}
