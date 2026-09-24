/**
 * Swap — pannello ordine, SEMPRE VISIBILE (non piu' una sheet): la
 * revisione "deve essere come un exchange" (richiesta) ha portato anche
 * questo fuori dai pannelli a comparsa — un vero exchange non nasconde
 * il form d'ordine dietro un tap.
 *
 * Due tempi separati anche nell'interfaccia, non solo nel backend:
 * 1. Preventivo: nessuna chiave, nessun impegno. Si aggiorna da solo mentre
 *    l'utente digita (con un debounce, non a ogni tasto).
 * 2. Conferma: solo un tocco esplicito su "Firma e invia" chiama l'ordine
 *    vero, e solo dopo aver rimostrato il prezzo appena letto — mai una
 *    firma innescata dal preventivo stesso.
 *
 * Il backend rifiuta comunque l'invio se onchain_execution_enabled e' spento
 * (di default lo e'): qui lo si dice PRIMA di far compilare il modulo,
 * non dopo un tentativo fallito.
 *
 * Richiesta: "lo swap deve essere possibile in ogni valuta o crypto possibile"
 * — non piu' solo nativo→stablecoin: entrambe le gambe sono un indirizzo
 * qualsiasi sulla chain scelta, con la coppia nativa come scorciatoia di
 * default (e' l'unica per cui il saldo e' gia' noto senza chiederlo).
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { formatUsd } from "@onchain/format.js";
import * as sheets from "@onchain/sheets.js";
import { state } from "@onchain/store.js";

// Il token nativo di ogni chain: e' l'unica gamba per cui questa pagina
// conosce gia' saldo e prezzo senza una chiamata in piu' — usato come
// default e come sentinella "vendo/ricevo il gas", non come limite.
const NATIVE = {
  solana: { address: "So11111111111111111111111111111111111111112", symbol: "SOL", decimals: 9 },
  robinhood: { address: "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", symbol: "ETH", decimals: 18 },
};

// Stablecoin di destinazione predefinita — solo per compilare il campo
// "Ricevi" al primo apertura: un tocco su "Cambia token" la sostituisce
// con qualunque altro indirizzo.
const DEFAULT_OUTPUT = {
  solana: { address: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", symbol: "USDC", decimals: 6 },
  robinhood: { address: "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168", symbol: "USDG", decimals: 6, fee: 3000 },
};

const toBaseUnits = (amount, decimals) => BigInt(Math.round(amount * 10 ** decimals));
const fromBaseUnits = (amount, decimals) => Number(amount) / 10 ** decimals;

// Vendere l'intero saldo nativo lascerebbe zero per pagare la transazione
// stessa (e per Robinhood Chain anche il quote successivo). Riserva fissa e
// prudente, non una percentuale — a saldi piccoli una percentuale lascia
// troppo poco. Si applica solo quando si vende il nativo: un token qualsiasi
// non paga il gas, quindi non ha bisogno di riserva.
const GAS_RESERVE = { solana: 0.003, robinhood: 0.0008 };

let hostEl = null;
const host = () => hostEl;

let leg = { in: null, out: null };
let latestQuote = null;
let debounceTimer = null;
let tokenCache = new Map();
let onExecutedCb = null;
let onTokenPickedCb = null;

// Cosa c'e' nel wallet, dal valore piu' alto: l'utente, "devo sapere subito
// cosa ho disponibile anche quando vado a fare lo swap [...] deve essere
// selezionata automaticamente la valuta detenuta, quella con maggior
// quantita'". Dopo uno swap ETH -> USDG lo swap parte da USDG.
let holdings = [];
let holdingsKey = { chain: null, at: 0 };
let currentArmed = false;
let pendingFrom = null;  // moneta toccata sotto il saldo prima che lo swap fosse pronto
const HOLDINGS_TTL_MS = 20_000;  // lo swap si rimonta a ogni token scelto sul grafico

const sameAddress = (a, b) => Boolean(a && b) && a.toLowerCase() === b.toLowerCase();
const fmtAmount = (n) => n.toLocaleString("en-US", { maximumFractionDigits: n >= 1 ? 4 : 6 });

async function loadHoldings(chain, force = false) {
  if (!force && holdingsKey.chain === chain && Date.now() - holdingsKey.at < HOLDINGS_TTL_MS) return;
  const [balance, portfolio] = await Promise.all([
    api.getBalance(chain).catch(() => (state.balance?.chain === chain ? state.balance : null)),
    api.getPortfolio(chain).catch(() => ({ holdings: [] })),
  ]);
  if (balance) state.balance = balance;
  const native = NATIVE[chain];
  const stable = DEFAULT_OUTPUT[chain];
  const rows = [];
  if (typeof balance?.native_balance === "number" && balance.native_balance > 0) {
    rows.push({ ...native, amount: balance.native_balance, priceUsd: balance.native_price_usd ?? null, isNative: true });
  }
  for (const h of portfolio.holdings || []) {
    if (!(h.amount > 0) || sameAddress(h.address, native.address)) continue;
    const known = sameAddress(h.address, stable.address) ? stable : null;
    rows.push({
      address: h.address, symbol: known?.symbol || h.symbol, decimals: known?.decimals ?? null,
      amount: h.amount, priceUsd: h.price_usd ?? (known ? 1 : null), isNative: false,
    });
  }
  for (const r of rows) r.valueUsd = r.priceUsd != null ? r.amount * r.priceUsd : null;
  holdings = rows.sort((a, b) => (b.valueUsd ?? -1) - (a.valueUsd ?? -1));
  holdingsKey = { chain, at: Date.now() };
}

// Quanto si puo' vendere di un token: il saldo, meno la riserva per il gas
// se e' il nativo. Per difetto a 8 decimali: sottrarre due float da'
// artefatti come "0.0025700000000000002", e arrotondare per eccesso
// chiederebbe piu' di quanto c'e'.
function availableFor(chain, token) {
  const h = token && holdings.find((x) => sameAddress(x.address, token.address));
  if (!h) return null;
  const reserve = h.isNative ? (GAS_RESERVE[chain] ?? 0) : 0;
  return Math.max(0, Math.floor((h.amount - reserve) * 1e8) / 1e8);
}

// Un token del wallet pronto come gamba dello swap: i decimali del nativo
// e della stablecoin sono noti, gli altri arrivano dal contratto.
async function holdingAsLeg(chain, h) {
  if (h.isNative) return { ...NATIVE[chain], priceUsd: h.priceUsd, isNative: true };
  if (h.decimals != null) return { address: h.address, symbol: h.symbol, decimals: h.decimals, priceUsd: h.priceUsd, isNative: false };
  const resolved = await resolveToken(chain, h.address);
  return { ...resolved, symbol: h.symbol || resolved.symbol, priceUsd: h.priceUsd ?? resolved.priceUsd, isNative: false };
}

// Lato "ricevi" quando non lo sceglie il grafico: dal nativo alla
// stablecoin, da tutto il resto al nativo.
function defaultOutFor(chain, inLeg) {
  if (!inLeg.isNative) return { ...NATIVE[chain], priceUsd: state.balance?.native_price_usd ?? null, isNative: true };
  return { ...DEFAULT_OUTPUT[chain], priceUsd: null, isNative: false };
}

// Metadati di un token qualsiasi: cache per indirizzo, mai indovinati
// (i decimali arrivano da lookup on-chain reale, lato backend).
async function resolveToken(chain, address) {
  const key = `${chain}:${address.toLowerCase()}`;
  if (tokenCache.has(key)) return tokenCache.get(key);
  const info = await api.getTokenInfo(chain, address);
  const resolved = {
    address, decimals: info.decimals,
    symbol: info.symbol || `${address.slice(0, 4)}…${address.slice(-4)}`,
    priceUsd: info.price_usd,
  };
  tokenCache.set(key, resolved);
  return resolved;
}

/** Monta il form di swap dentro `container`, che resta cosi' finche' non
 * viene chiamato di nuovo (cambio chain) o smontato da chi possiede il
 * pannello ordine. Un token preselezionato (dal grafico) evita di doverlo
 * scegliere due volte. */
export async function mountSwap(container, { onExecuted, onTokenPicked, presetToken } = {}) {
  hostEl = container;
  onExecutedCb = onExecuted ?? null;
  onTokenPickedCb = onTokenPicked ?? null;
  const chain = state.chain;
  const native = NATIVE[chain];
  tokenCache = new Map();

  const [risk] = await Promise.all([
    api.getRiskStatus(chain).catch(() => null),
    loadHoldings(chain),
  ]);
  const armed = Boolean(risk?.execution_enabled);
  currentArmed = armed;

  // Si vende quello che si ha di piu' (in dollari), non sempre il nativo.
  leg.in = { ...native, priceUsd: state.balance?.native_price_usd ?? null, isNative: true };
  if (holdings.length) {
    const start = holdings.find((h) => sameAddress(h.address, pendingFrom)) ?? holdings[0];
    pendingFrom = null;
    try { leg.in = await holdingAsLeg(chain, start); } catch { /* resta il nativo */ }
  }
  const usablePreset = presetToken && !sameAddress(presetToken.address, native.address)
    && !sameAddress(presetToken.address, leg.in.address);
  leg.out = usablePreset
    ? { ...presetToken, priceUsd: presetToken.priceUsd ?? null, isNative: false }
    : defaultOutFor(chain, leg.in);

  paintForm(armed);
  const available = availableFor(chain, leg.in);
  const amountInput = host().querySelector("#swap-amount");
  if (available != null && available > 0) { amountInput.value = available; scheduleQuote(armed); }
}

function availableLabel(chain) {
  const available = availableFor(chain, leg.in);
  if (available == null) return { text: `In wallet: 0 ${leg.in.symbol}`, empty: true };
  return { text: `Available ${fmtAmount(available)} ${leg.in.symbol}`, empty: available <= 0 };
}

// "Nel wallet": cosa c'e', sempre in vista sopra lo swap. Un tocco su una
// voce la mette in vendita.
function walletStrip() {
  if (!holdings.length) return `<p class="swap-wallet-empty">No balance on this network</p>`;
  return holdings.map((h) => `
    <button type="button" class="wallet-chip${sameAddress(h.address, leg.in.address) ? " is-current" : ""}" data-hold="${h.address}">
      <b>${fmtAmount(h.amount)} ${h.symbol}</b>${h.valueUsd != null ? `<small>${formatUsd(h.valueUsd)}</small>` : ""}
    </button>`).join("");
}

function paintForm(armed) {
  const chain = state.chain;
  const available = availableFor(chain, leg.in);
  const availableInfo = availableLabel(chain);
  // Campo alla Uniswap (mockup approvato dall'utente): importo grande e
  // pillola del token sulla stessa riga, didascalia e disponibile sotto,
  // dentro lo stesso riquadro. La pillola E' il selettore del token.
  host().innerHTML = `
    <div class="swap-wallet" id="swap-wallet" aria-label="In wallet">${walletStrip()}</div>
    <form class="form swap-form" id="swap-form">
      <div class="swap-field">
        <div class="swap-field-top">
          <input id="swap-amount" name="amount" type="number" min="0" step="any" value="" placeholder="0" required />
          <button type="button" class="swap-max" id="swap-max" ${available > 0 ? "" : "hidden"}>Max</button>
          <button type="button" class="token-pill" id="swap-pick-in"><span class="token-ic" aria-hidden="true"></span><span id="swap-in-symbol">${leg.in.symbol}</span> ▾</button>
        </div>
        <div class="swap-field-bottom">
          <label for="swap-amount">Sell <span id="swap-in-usd"></span></label>
          <span id="swap-available" class="${availableInfo.empty ? "is-warn" : ""}">${availableInfo.text}</span>
        </div>
      </div>

      <button type="button" class="swap-arrow" id="swap-flip" aria-label="Flip sell and receive">↓</button>

      <div class="swap-field is-readonly">
        <div class="swap-field-top">
          <output id="swap-out">0</output>
          <button type="button" class="token-pill" id="swap-pick-out"><span class="token-ic is-out" aria-hidden="true"></span><span id="swap-out-symbol">${leg.out.symbol}</span> ▾</button>
        </div>
        <div class="swap-field-bottom">
          <label for="swap-out">Receive (est.)</label>
          <span id="swap-out-usd"></span>
        </div>
      </div>

      <dl class="route-info">
        <div><dt>Price impact</dt><dd id="swap-detail">—</dd></div>
        <div><dt>Route</dt><dd id="swap-route">${chain === "solana" ? "Jupiter" : "Uniswap v3"}</dd></div>
      </dl>

      <label class="swap-confirm">
        <input type="checkbox" id="swap-confirm-box" />
        <span>I confirm: real money, <b id="swap-confirm-summary">—</b></span>
      </label>

      <button type="submit" class="btn-primary cta-btn" id="swap-submit" disabled>Enter a size</button>
    </form>
    <output class="outcome" id="swap-outcome" hidden></output>
  `;

  const amountInput = host().querySelector("#swap-amount");
  const submitButton = host().querySelector("#swap-submit");
  const form = host().querySelector("#swap-form");
  const maxButton = host().querySelector("#swap-max");
  const confirmBox = host().querySelector("#swap-confirm-box");
  const pctSlider = host().querySelector("#swap-pct-slider");
  const pctVal = host().querySelector("#swap-pct-val");

  amountInput.addEventListener("input", () => { confirmBox.checked = false; scheduleQuote(armed); });
  confirmBox.addEventListener("change", () => scheduleQuote(armed));
  maxButton.addEventListener("click", () => {
    const max = availableFor(chain, leg.in);
    if (max != null) { amountInput.value = max; confirmBox.checked = false; scheduleQuote(armed); }
  });
  if (pctSlider) {
    const applyPct = (pct) => {
      const max = availableFor(chain, leg.in) ?? 0;
      amountInput.value = Math.round(max * (pct / 100) * 1e8) / 1e8;
      confirmBox.checked = false;
      scheduleQuote(armed);
    };
    pctSlider.addEventListener("input", () => { pctVal.value = pctSlider.value; applyPct(Number(pctSlider.value)); });
    pctVal.addEventListener("input", () => { pctSlider.value = pctVal.value; applyPct(Number(pctVal.value)); });
  }
  host().querySelector("#swap-wallet").addEventListener("click", async (event) => {
    const chip = event.target.closest("[data-hold]");
    const h = chip && holdings.find((x) => sameAddress(x.address, chip.dataset.hold));
    if (!h || sameAddress(h.address, leg.in.address)) return;
    try { await setLeg("in", await holdingAsLeg(chain, h), armed); } catch { /* resta com'era */ }
  });
  host().querySelector("#swap-flip").addEventListener("click", () => {
    leg = { in: leg.out, out: leg.in };
    repaintWithAmount(armed, availableFor(chain, leg.in));
  });
  host().querySelector("#swap-pick-in").addEventListener("click", () => pickToken("in", armed));
  host().querySelector("#swap-pick-out").addEventListener("click", () => pickToken("out", armed));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (confirmBox.checked && latestQuote?.route !== "aggregator") confirmAndExecute(armed);
  });
}

// Il picker vive in sheets.js (dialog dedicato): mai un prompt() del
// browser dentro l'app.
function pickToken(side, armed) {
  const chain = state.chain;
  sheets.openTokenPicker(chain, {
    title: side === "in" ? "You sell" : "You receive",
    onPick: async ({ address }) => {
      const submitButton = host().querySelector("#swap-submit");
      if (submitButton) { submitButton.disabled = true; submitButton.textContent = "Checking token…"; }
      try {
        // Il nativo (proposto nel picker fra "i tuoi token") non passa da
        // resolveToken(): i suoi decimali sono gia' noti, e chiederli al
        // contratto puo' tornare None su Robinhood Chain (indirizzo di
        // routing, non garantito un ERC20 vero) — stesso bug gia' visto
        // sull'endpoint del prezzo live.
        let picked;
        if (address.toLowerCase() === NATIVE[chain].address.toLowerCase()) {
          picked = { ...NATIVE[chain], priceUsd: state.balance?.native_price_usd ?? null, isNative: true };
        } else {
          const resolved = await resolveToken(chain, address);
          picked = { ...resolved, isNative: false };
          // Il grafico segue quello che si compra, non quello che si vende.
          if (side === "out") onTokenPickedCb?.(resolved);
        }
        await setLeg(side, picked, armed);
      } catch (error) {
        if (submitButton) submitButton.textContent = "Retry";
        const detailEl = host().querySelector("#swap-detail");
        if (detailEl) {
          detailEl.textContent = error instanceof ApiError ? error.message : "Token not recognized.";
          detailEl.classList.add("is-warn");
        }
      }
    },
  });
}

// Le stesse monete per la riga sotto il saldo (holdings.js): un solo
// posto che le legge e le ordina.
export async function walletHoldings(chain, force = false) {
  await loadHoldings(chain, force);
  return holdingsKey.chain === chain ? holdings : [];
}

// Tocco su una moneta sotto il saldo: quella va in vendita. Se lo swap non
// e' ancora montato, la sceglie mountSwap al posto della piu' grande.
window.addEventListener("onchain:swap-from", async (event) => {
  const address = event.detail?.address;
  if (!address) return;
  const h = holdingsKey.chain === state.chain && holdings.find((x) => sameAddress(x.address, address));
  if (!hostEl?.querySelector("#swap-form") || !leg.in || !h) { pendingFrom = address; return; }
  if (sameAddress(h.address, leg.in.address)) return;
  try { await setLeg("in", await holdingAsLeg(state.chain, h), currentArmed); } catch { /* resta com'era */ }
});

// Cambia una gamba. Se diventa uguale all'altra le due si scambiano, come
// su un DEX. Scegliendo cosa vendere l'importo diventa quanto se ne ha.
async function setLeg(side, picked, armed) {
  const other = side === "in" ? "out" : "in";
  if (sameAddress(picked.address, leg[other].address)) leg[other] = leg[side];
  leg[side] = picked;
  const amountInput = host().querySelector("#swap-amount");
  const keep = amountInput ? amountInput.value : "";
  repaintWithAmount(armed, side === "in" ? availableFor(state.chain, leg.in) : Number(keep) || null);
}

function repaintWithAmount(armed, amount) {
  paintForm(armed);
  const amountInput = host().querySelector("#swap-amount");
  if (amount != null && amount > 0) amountInput.value = amount;
  scheduleQuote(armed);
}

function scheduleQuote(armed) {
  clearTimeout(debounceTimer);
  const amountInput = host().querySelector("#swap-amount");
  debounceTimer = setTimeout(() => requestQuote(amountInput.value, armed), 350);
}

async function requestQuote(rawAmount, armed) {
  const chain = state.chain;
  const amount = Number(rawAmount);
  const outEl = host().querySelector("#swap-out");
  const detailEl = host().querySelector("#swap-detail");
  const inUsdEl = host().querySelector("#swap-in-usd");
  const outUsdEl = host().querySelector("#swap-out-usd");
  const submitButton = host().querySelector("#swap-submit");
  const confirmBox = host().querySelector("#swap-confirm-box");
  const confirmSummary = host().querySelector("#swap-confirm-summary");
  if (!(amount > 0)) {
    latestQuote = null;
    outEl.textContent = "0";
    detailEl.textContent = "—";
    inUsdEl.textContent = "";
    outUsdEl.textContent = "";
    submitButton.disabled = true;
    submitButton.textContent = "Enter a size";
    return;
  }

  submitButton.disabled = true;
  submitButton.textContent = "Refreshing quote…";
  detailEl.textContent = "";
  inUsdEl.textContent = leg.in.priceUsd != null ? `≈ ${formatUsd(amount * leg.in.priceUsd)}` : "";

  try {
    const amountIn = toBaseUnits(amount, leg.in.decimals);
    const quote = chain === "solana"
      ? await api.quoteSolana({ input_mint: leg.in.address, output_mint: leg.out.address, amount_lamports: Number(amountIn) })
      : await api.quoteRobinhood({ token_in: leg.in.address, token_out: leg.out.address, fee: leg.out.fee ?? leg.in.fee ?? 3000, amount_in: Number(amountIn) });

    latestQuote = { ...quote, amountIn, sourceAmount: amount };
    const outAmount = fromBaseUnits(quote.out_amount, leg.out.decimals);
    outEl.textContent = outAmount.toLocaleString("en-US", { maximumFractionDigits: 6 });
    outUsdEl.textContent = leg.out.priceUsd != null
      ? `≈ ${formatUsd(outAmount * leg.out.priceUsd)}`
      : (isStable(leg.out) ? `≈ ${formatUsd(outAmount)} (stablecoin, pegged to 1 USD)` : "");
    detailEl.textContent = `${quote.price_impact_pct.toFixed(2)}%`;
    detailEl.classList.toggle("is-warn", quote.price_impact_pct > 1);

    if (confirmSummary) {
      confirmSummary.textContent =
        `${amount} ${leg.in.symbol} → ~${outAmount.toFixed(6)} ${leg.out.symbol}, `
        + `price impact ${quote.price_impact_pct.toFixed(2)}%`;
    }
    // Fuori da Uniswap v3 il prezzo arriva da un aggregatore (KyberSwap,
    // LI.FI): si vede quanto si riceverebbe, la firma su quel percorso non
    // e' ancora collegata.
    const viaAggregator = quote.route === "aggregator";
    const routeEl = host().querySelector("#swap-route");
    if (routeEl && chain === "robinhood") routeEl.textContent = viaAggregator ? quote.source : "Uniswap v3";
    submitButton.disabled = !armed || viaAggregator || !confirmBox?.checked;
    submitButton.textContent = !armed ? "Execution off on server"
      : viaAggregator ? `Price from ${quote.source}: swap coming soon` : "Sign & send";
  } catch (error) {
    latestQuote = null;
    outEl.textContent = "—";
    outUsdEl.textContent = "";
    detailEl.textContent = error.message;
    detailEl.classList.add("is-warn");
    submitButton.disabled = true;
    submitButton.textContent = "Quote unavailable";
  }
}

function isStable(token) {
  return ["USDC", "USDG", "USDT"].includes(token.symbol);
}

async function confirmAndExecute(armed) {
  // La conferma e' la checkbox spuntata prima del submit (vedi paintForm):
  // mai un window.confirm() del browser dentro l'app.
  if (!latestQuote || !armed) return;
  const chain = state.chain;
  const outcome = host().querySelector("#swap-outcome");
  const submitButton = host().querySelector("#swap-submit");

  submitButton.disabled = true;
  submitButton.textContent = "Sending…";
  outcome.hidden = false;
  outcome.dataset.tone = "";
  outcome.textContent = "Signing and sending…";

  // I tetti di rischio sono in USD: notional_usd deve essere il controvalore
  // vero, non l'importo nel token venduto — se non conosciamo il prezzo
  // (token non nativo e non tracciato da DexScreener), l'importo nel token
  // resta un fallback dichiarato (quasi certamente una sottostima, non una
  // sovrastima: mai un modo per aggirare i tetti verso l'alto).
  const notionalUsd = leg.in.priceUsd != null
    ? latestQuote.sourceAmount * leg.in.priceUsd
    : latestQuote.sourceAmount;

  try {
    const result = chain === "solana"
      ? await api.executeSolana({
          token: leg.out.symbol, strategy_name: "manuale", side: "buy",
          input_mint: leg.in.address, output_mint: leg.out.address,
          amount_lamports: Number(latestQuote.amountIn),
          notional_usd: notionalUsd, price_usd: 1,
        })
      : await api.executeRobinhood({
          token: leg.out.symbol, strategy_name: "manuale", side: "buy",
          token_in: leg.in.address, token_out: leg.out.address, fee: leg.out.fee ?? leg.in.fee ?? 3000,
          amount_in: Number(latestQuote.amountIn),
          notional_usd: notionalUsd, price_usd: 1,
        });

    outcome.dataset.tone = result.accepted ? "ok" : "no";
    outcome.textContent = result.accepted
      ? `Sent. Signature: ${result.tx_signature}`
      : result.reason;
    if (result.accepted) {
      onExecutedCb?.();
      // Il saldo cambia: la striscia "Nel wallet" e il disponibile si
      // aggiornano senza rifare il modulo (l'esito resta a schermo).
      loadHoldings(chain, true).then(() => {
        const strip = host()?.querySelector("#swap-wallet");
        if (strip) strip.innerHTML = walletStrip();
        const availableEl = host()?.querySelector("#swap-available");
        if (availableEl) availableEl.textContent = availableLabel(chain).text;
      }).catch(() => {});
    }
  } catch (error) {
    outcome.dataset.tone = "no";
    outcome.textContent = error instanceof ApiError ? error.message : "Unexpected error.";
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Sign & send";
  }
}
