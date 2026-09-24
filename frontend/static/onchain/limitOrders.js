/**
 * Ordini limite, ordini aperti e storico — la parte "operativa tipo DEX"
 * del piano, ora tutta SEMPRE VISIBILE (pannello ordine + pannello
 * inferiore a tab), non piu' dentro sheet separate: un vero exchange
 * mostra "cosa ho ordinato" e "cosa e' successo" senza doverli aprire.
 *
 * Non un secondo motore: l'interazione eredita l'"Ordine manuale" del
 * resto del sito (tab BUY/SELL, conferma esplicita prima di firmare), ma
 * chi esegue resta lo stesso execute_order on-chain di sempre, con gli
 * stessi guardrail — vedi limit_order_loop in main.py.
 *
 * Ne' Jupiter ne' Uniswap v3 hanno un book con ordini limite resting: qui
 * un ordine e' size+prezzo+direzione persistiti, controllati a intervalli
 * (oggi ogni 45s) — un target toccato per un istante fra due controlli
 * puo' non essere colto. Lo si dice in chiaro nel form, non lo si nasconde.
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { formatUsd } from "@onchain/format.js";
import * as sheets from "@onchain/sheets.js";
import { state } from "@onchain/store.js";

const NATIVE = {
  solana: { address: "So11111111111111111111111111111111111111112", symbol: "SOL", decimals: 9 },
  robinhood: { address: "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", symbol: "ETH", decimals: 18 },
};

const STATUS_LABEL = {
  open: "In attesa", filled: "Eseguito", cancelled: "Annullato", failed: "Falso",
};

let hostEl = null;
const host = () => hostEl;
let otherToken = null; // { address, symbol, decimals } — l'altra gamba, risolta via picker
let onPlacedCb = null;
let onTokenPickedCb = null;

/* ------------------------------------------------------------ Form ord. */

let lastChain = null;

export function mountLimitOrder(container, { onPlaced, onTokenPicked, presetToken } = {}) {
  hostEl = container;
  onPlacedCb = onPlaced ?? null;
  onTokenPickedCb = onTokenPicked ?? null;
  // L'altra gamba scelta su una chain non appartiene alla chain successiva
  // (un mint Solana non e' un contratto EVM): un cambio chain la azzera,
  // non la lascia appesa a un indirizzo della chain sbagliata.
  if (state.chain !== lastChain) otherToken = null;
  lastChain = state.chain;
  if (presetToken && presetToken.address.toLowerCase() !== NATIVE[state.chain].address.toLowerCase()) {
    otherToken = presetToken;
  }
  const chain = state.chain;
  const native = NATIVE[chain];
  paintForm(chain, native);
}

function paintForm(chain, native) {
  host().innerHTML = `
    <div class="mo-side-tabs" id="limit-direction-tabs">
      <button type="button" class="mo-side-tab mo-buy-tab mo-tab-active" data-direction="buy_below">Compra se scende</button>
      <button type="button" class="mo-side-tab mo-sell-tab" data-direction="sell_above">Vendi se sale</button>
    </div>

    <form class="form" id="limit-form">
      <label for="limit-other-token">Contro</label>
      <button type="button" class="swap-token-pick" id="limit-pick-other">
        ${otherToken ? otherToken.symbol : "Scegli il token"} ↕
      </button>

      <div class="form-pair">
        <span><label for="limit-amount">Size (${native.symbol})</label>
          <input id="limit-amount" name="amount" type="number" min="0" step="any" required /></span>
        <span><label for="limit-target">Prezzo target (USD)</label>
          <input id="limit-target" name="target" type="number" min="0" step="any" required /></span>
      </div>

      <p class="swap-detail">Controllato ogni 45 secondi, non istantaneo al tocco del prezzo — stessi tetti di rischio di un ordine reale.</p>
      <button type="submit" class="btn-primary" id="limit-submit">Piazza ordine limite</button>
    </form>
    <output class="outcome" id="limit-outcome" hidden></output>
  `;

  host().querySelectorAll("#limit-direction-tabs .mo-side-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      host().querySelectorAll("#limit-direction-tabs .mo-side-tab")
        .forEach((t) => t.classList.toggle("mo-tab-active", t === tab));
    });
  });

  host().querySelector("#limit-pick-other").addEventListener("click", () => {
    sheets.openTokenPicker(chain, {
      title: "Contro quale token",
      onPick: async ({ address }) => {
        const outcome = host().querySelector("#limit-outcome");
        if (address.toLowerCase() === native.address.toLowerCase()) {
          outcome.hidden = false;
          outcome.dataset.tone = "no";
          outcome.textContent = `${native.symbol} e' gia' un lato dell'ordine — scegli l'altro token.`;
          return;
        }
        try {
          const info = await api.getTokenInfo(chain, address);
          otherToken = { address, symbol: info.symbol || `${address.slice(0, 4)}…${address.slice(-4)}`, decimals: info.decimals };
          onTokenPickedCb?.(otherToken);
          paintForm(chain, native);
        } catch (error) {
          outcome.hidden = false;
          outcome.dataset.tone = "no";
          outcome.textContent = error instanceof ApiError ? error.message : "Token non riconosciuto.";
        }
      },
    });
  });

  host().querySelector("#limit-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitOrder(chain, native);
  });
}

async function submitOrder(chain, native) {
  const outcome = host().querySelector("#limit-outcome");
  const submitButton = host().querySelector("#limit-submit");
  if (!otherToken) {
    outcome.hidden = false;
    outcome.dataset.tone = "no";
    outcome.textContent = "Scegli prima il token.";
    return;
  }

  const direction = host().querySelector("#limit-direction-tabs .mo-tab-active").dataset.direction;
  const amount = Number(host().querySelector("#limit-amount").value);
  const target = Number(host().querySelector("#limit-target").value);
  if (!(amount > 0) || !(target > 0)) return;

  // "Compra se scende" vende il nativo per l'altro token (si spera che
  // valga di piu' quando il target e' toccato); "Vendi se sale" vende
  // l'altro token per il nativo — stessa gamba nativa richiesta dal
  // backend, solo il lato in vendita cambia.
  const tokenIn = direction === "buy_below" ? native : otherToken;
  const tokenOut = direction === "buy_below" ? otherToken : native;

  submitButton.disabled = true;
  outcome.hidden = false;
  outcome.dataset.tone = "";
  outcome.textContent = "Piazzo l'ordine…";

  try {
    await api.createLimitOrder({
      chain, token_in: tokenIn.address, token_out: tokenOut.address,
      token_in_symbol: tokenIn.symbol, token_out_symbol: tokenOut.symbol,
      token_in_decimals: tokenIn.decimals, token_out_decimals: tokenOut.decimals,
      amount_in: amount, direction, target_price_usd: target,
    });
    outcome.dataset.tone = "ok";
    outcome.textContent = "Ordine piazzato — resta in attesa finche' il prezzo non tocca il target.";
    host().querySelector("#limit-form").reset();
    onPlacedCb?.();
  } catch (error) {
    outcome.dataset.tone = "no";
    outcome.textContent = error instanceof ApiError ? error.message : "Errore inatteso.";
  } finally {
    submitButton.disabled = false;
  }
}

/* -------------------------------------------------- Ordini aperti (tab) */

export async function mountOpenOrders(container) {
  const chain = state.chain;
  container.innerHTML = "<p class='sheet-note'>Leggo…</p>";
  try {
    const orders = (await api.listLimitOrders(chain)).filter((o) => o.status === "open");
    if (!orders.length) { container.innerHTML = "<p class='sheet-note'>Nessun ordine limite aperto.</p>"; return; }
    container.innerHTML = `<ul class="tx-list">${orders.map((o) => `
      <li data-id="${o.id}">
        <span><span class="status-dot" data-status="${o.status}"></span>${o.direction === "buy_below" ? "Compra se scende a" : "Vendi se sale a"} ${formatUsd(o.target_price_usd)}
          <small>${o.amount_in} ${o.token_in_symbol} → ${o.token_out_symbol}</small></span>
        <button type="button" class="btn-quiet" data-cancel="${o.id}">Annulla</button>
      </li>`).join("")}</ul>`;
    container.querySelectorAll("[data-cancel]").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try { await api.cancelLimitOrder(button.dataset.cancel); await mountOpenOrders(container); }
        catch { button.disabled = false; }
      });
    });
  } catch (error) {
    container.innerHTML = `<p class="sheet-note is-error">${error.message}</p>`;
  }
}

/* ------------------------------------------------------------- Storico */

// Un pannello solo, non tre: "Storico" (transazioni RPC grezze) e
// "Diario" (ordini di questo sito) sembravano fare la stessa cosa da due
// bottoni diversi — Richiesta: "una copia delle transazioni per capire cosa
// e' accaduto in passato". Due viste dentro lo stesso pannello, non due
// pannelli, e ora sempre in vista nel pannello inferiore a tab.
export async function mountHistory(container) {
  const chain = state.chain;
  container.innerHTML = `
    <div class="mo-side-tabs" id="history-tabs">
      <button type="button" class="mo-side-tab mo-buy-tab mo-tab-active" data-view="orders">Ordini</button>
      <button type="button" class="mo-side-tab mo-sell-tab" data-view="raw">Transazioni on-chain</button>
    </div>
    <div id="history-body"><p class="sheet-note">Leggo…</p></div>
  `;
  container.querySelectorAll("#history-tabs .mo-side-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      container.querySelectorAll("#history-tabs .mo-side-tab").forEach((t) => t.classList.toggle("mo-tab-active", t === tab));
      renderHistoryView(container, chain, tab.dataset.view);
    });
  });
  await renderHistoryView(container, chain, "orders");
}

async function renderHistoryView(container, chain, view) {
  const body = container.querySelector("#history-body");
  if (!body) return;
  body.innerHTML = `<p class="sheet-note">Leggo…</p>`;

  if (view === "raw") {
    try {
      const data = await api.getTransactions(chain);
      if (!data.available) {
        body.innerHTML = `<p class="sheet-note">${data.reason}
          <a href="${encodeURI(data.explorer_url)}" target="_blank" rel="noopener">Apri l'explorer →</a></p>`;
        return;
      }
      if (!data.transactions.length) { body.innerHTML = "<p class='sheet-note'>Nessuna transazione ancora.</p>"; return; }
      body.innerHTML = `<ul class="tx-list">${data.transactions.map((tx) => `
        <li>
          <a href="${encodeURI(tx.explorer_url)}" target="_blank" rel="noopener">${tx.signature.slice(0, 12)}…</a>
          <span>${tx.status}${tx.block_time ? ` · ${new Date(tx.block_time * 1000).toLocaleString("it-IT")}` : ""}</span>
        </li>`).join("")}</ul>`;
    } catch (error) {
      body.innerHTML = `<p class="sheet-note is-error">${error.message}</p>`;
    }
    return;
  }

  try {
    const data = await api.getHistory(chain);
    const rows = [
      ...data.orders.map((o) => ({ ...o, at: o.updated_at })),
      ...data.limit_orders.map((o) => ({ ...o, at: o.updated_at })),
    ].sort((a, b) => b.at - a.at);

    if (!rows.length) { body.innerHTML = "<p class='sheet-note'>Nessun ordine ancora.</p>"; return; }

    body.innerHTML = `<ul class="tx-list">${rows.map((row) => row.kind === "swap" ? `
      <li>
        <span><span class="status-dot" data-status="${row.status}"></span><b>${row.side === "buy" ? "Compra" : "Vendi"} ${row.token}</b>
          <small>${formatUsd(row.size_usd)} · ${STATUS_LABEL[row.status] ?? row.status}${row.reason ? ` — ${row.reason}` : ""}</small></span>
        <span>${new Date(row.at * 1000).toLocaleString("it-IT")}</span>
      </li>` : `
      <li>
        <span><span class="status-dot" data-status="${row.status}"></span><b>Limite ${row.pair}</b>
          <small>${row.direction === "buy_below" ? "compra se scende a" : "vendi se sale a"} ${formatUsd(row.target_price_usd)}
          · ${STATUS_LABEL[row.status] ?? row.status}${row.reason ? ` — ${row.reason}` : ""}</small></span>
        <span>${new Date(row.at * 1000).toLocaleString("it-IT")}</span>
      </li>`).join("")}</ul>`;
  } catch (error) {
    body.innerHTML = `<p class="sheet-note is-error">${error.message}</p>`;
  }
}
