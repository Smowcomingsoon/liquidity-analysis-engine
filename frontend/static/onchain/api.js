/**
 * L'unico posto che parla col backend. Ogni chiamata passa da `request`,
 * quindi il timeout e la forma dell'errore sono uguali ovunque: il resto
 * dell'app vede o un oggetto, o un ApiError con un messaggio in italiano
 * gia' pronto da mostrare.
 */

export class ApiError extends Error {}

const DEFAULT_TIMEOUT_MS = 8000;

// GET identici in volo insieme diventano una richiesta sola: all'avvio
// saldo e stato di rischio li chiedevano due pannelli ciascuno, e ogni
// saldo e' una chiamata RPC vera lato server. Chi arriva dopo riceve la
// stessa risposta (o lo stesso errore) di chi e' partito per primo.
const inFlight = new Map();

function request(path, options = {}) {
  if ((options.method ?? "GET") !== "GET") {
    inFlight.clear();  // una scrittura rende vecchia qualunque lettura partita prima
    return send(path, options);
  }
  let pending = inFlight.get(path);
  if (!pending) {
    pending = send(path, options).finally(() => inFlight.delete(path));
    inFlight.set(path, pending);
  }
  return pending;
}

async function send(path, { method = "GET", body, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      // AbortSignal.timeout al posto di una Promise.race con setTimeout:
      // qui la richiesta viene davvero interrotta, non solo ignorata.
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch (cause) {
    throw new ApiError(cause.name === "TimeoutError" ? "network is slow" : "network unreachable", { cause });
  }

  let payload = null;
  try { payload = await response.json(); } catch { /* 204, o una risposta non-JSON */ }

  if (!response.ok) throw new ApiError(payload?.detail || `error ${response.status}`);
  return payload;
}

export const getSession = () => request("/api/auth/me");
export const login = (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } });

export const listWallets = () => request("/api/onchain/wallets");
export const createWallet = (chain) => request("/api/onchain/wallets", { method: "POST", body: { chain } });
export const setWalletEnabled = (chain, enabled) =>
  request(`/api/onchain/wallets/${chain}`, { method: "PATCH", body: { enabled } });

export const getBalance = (chain) => request(`/api/onchain/wallets/${chain}/balance`);
export const getTransactions = (chain) => request(`/api/onchain/wallets/${chain}/transactions`, { timeoutMs: 15000 });
export const getPositions = (chain) => request(`/api/onchain/positions/${chain}`);
export const getRiskStatus = (chain) => request(`/api/onchain/risk/status/${chain}`);
export const resetCircuitBreaker = (chain) =>
  request(`/api/onchain/risk/circuit-breaker/${chain}/reset`, { method: "POST" });

export const simulateOrder = (order) => request("/api/onchain/simulate", { method: "POST", body: order });

// Lo swap in due tempi, sempre: prima il preventivo (nessuna chiave, nessun
// impegno), poi — solo se l'utente conferma e solo se il server ha
// l'esecuzione accesa — l'ordine vero. Il backend rifa' comunque il quote da
// se' prima di firmare: quello qui sotto serve a far vedere il prezzo, non e'
// il numero su cui si firma.
export const quoteSolana = (body) => request("/api/onchain/quote/solana", { method: "POST", body, timeoutMs: 15000 });
export const getTokenInfo = (chain, address) => request(`/api/onchain/token/${chain}/${address}`, { timeoutMs: 12000 });
export const searchTokens = (chain, query) =>
  request(`/api/onchain/tokens/search?chain=${chain}&query=${encodeURIComponent(query)}`, { timeoutMs: 10000 });
export const quoteRobinhood = (body) => request("/api/onchain/quote/robinhood", { method: "POST", body, timeoutMs: 15000 });

export const executeSolana = (body) =>
  request("/api/onchain/execute/solana", { method: "POST", body, timeoutMs: 45000 });
export const executeRobinhood = (body) =>
  request("/api/onchain/execute/robinhood", { method: "POST", body, timeoutMs: 45000 });

export const sendToken = (body) => request("/api/onchain/send", { method: "POST", body, timeoutMs: 45000 });

export const requestReveal = (chain) =>
  request(`/api/onchain/wallets/${chain}/reveal/request`, { method: "POST" });
export const consumeReveal = (chain, token) =>
  request(`/api/onchain/wallets/${chain}/reveal`, { method: "POST", body: { token } });

// Ordini limite (piano DEX): non un book on-chain vero, size+prezzo+
// direzione persistiti ed eseguiti dal server quando il prezzo tocca il
// target — vedi limit_order_loop in main.py.
export const createLimitOrder = (body) => request("/api/onchain/limit-orders", { method: "POST", body });
export const listLimitOrders = (chain) => request(`/api/onchain/limit-orders/${chain}`);
export const cancelLimitOrder = (id) => request(`/api/onchain/limit-orders/${id}`, { method: "DELETE" });
export const getHistory = (chain) => request(`/api/onchain/history/${chain}`, { timeoutMs: 15000 });

// Ecosistema DEX (piano 2026-09-23): portafoglio reale, grafico ereditato
// dal motore del sito principale, e le stesse strategie dell'archivio
// (Watchlist) attivabili da qui invece che da un'altra tab.
export const getPortfolio = (chain) => request(`/api/onchain/portfolio/${chain}`, { timeoutMs: 15000 });
export const getChart = (chain, address, interval = "15m") =>
  request(`/api/onchain/chart/${chain}/${address}?interval=${interval}`, { timeoutMs: 15000 });
export const getLivePrice = (chain, address) => request(`/api/onchain/price/${chain}/${address}`, { timeoutMs: 8000 });

// Ponte sniper -> trend EMA (2026-09-23): il server aggiunge da solo alla
// watchlist, con la strategia trend EMA attiva, i token scoperti dallo
// sniper su questa chain — qui solo l'interruttore per utente/chain.
export const getSniperAutotrade = () => request("/api/onchain/sniper-autotrade");
export const getPilot = (chain) => request(`/api/onchain/pilot/${chain}`);
export const getRadar = () => request("/api/onchain/radar");
export const getLeaderboard = () => request("/api/onchain/leaderboard");
export const getRadarLessons = () => request("/api/onchain/radar/lessons");
export const getRadarMemes = () => request("/api/onchain/radar/memes");
export const getRadarChart = (chain, pool, symbol, interval) =>
  request(`/api/onchain/radar/chart?${new URLSearchParams({ chain, pool, symbol, interval })}`);
export const followRadar = (body) => request("/api/onchain/radar/follow", { method: "POST", body });
export const setSniperAutotrade = (chain, enabled) =>
  request(`/api/onchain/sniper-autotrade/${chain}`, { method: "PATCH", body: { enabled } });

export const getStrategies = () => request("/api/strategies");
export const getWatchlist = () => request("/api/watchlist");
export const addWatchlistEntry = (body) => request("/api/watchlist", { method: "POST", body });
export const setWatchlistStrategy = (id, strategyName, enabled) =>
  request(`/api/watchlist/${id}/strategy`, { method: "PATCH", body: { strategy_name: strategyName, enabled } });
export const setWatchlistAutoTrade = (id, strategyName, enabled) =>
  request(`/api/watchlist/${id}/auto_trade`, { method: "PATCH", body: { strategy_name: strategyName, enabled } });
