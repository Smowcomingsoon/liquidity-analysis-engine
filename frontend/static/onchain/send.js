/**
 * Invia — prelievo: manda crypto (nativo o un token) a un indirizzo
 * qualsiasi, non uno swap (nessun altro token in cambio). Richiesta:
 * "aggiungi funzione anche per prelevare le crypto e inviarle ad
 * indirizzi". Resta un dialog (non un pannello sempre visibile come
 * Swap/Limite): un'azione rara e ad alto rischio (un indirizzo sbagliato
 * non si annulla), non qualcosa da tenere aperto di continuo.
 *
 * Stesso principio del resto del wallet: nessuna conferma via
 * window.confirm() del browser, una checkbox esplicita prima del
 * submit; il token si scegli fra quelli che il wallet ha DAVVERO (stesso
 * picker con "I tuoi token" usato per lo swap), non solo per ticker.
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

const GAS_RESERVE = { solana: 0.003, robinhood: 0.0008 };

let token = null; // { address, symbol, decimals, priceUsd, isNative }
let onSentCb = null;

function availableToSend(chain) {
  if (!token?.isNative) return null;
  const native = state.balance?.native_balance;
  if (typeof native !== "number") return null;
  const reserve = GAS_RESERVE[chain] ?? 0;
  return Math.max(0, Math.round((native - reserve) * 1e8) / 1e8);
}

export async function openSend({ onSent } = {}) {
  onSentCb = onSent ?? null;
  const chain = state.chain;
  token = { ...NATIVE[chain], priceUsd: state.balance?.native_price_usd ?? null, isNative: true };

  sheets.openSheet("Send", "Send crypto to any address — double-check it, a wrong withdrawal can't be undone.");

  let armed = false;
  try { armed = Boolean((await api.getRiskStatus(chain)).execution_enabled); } catch { /* resta spento */ }

  if (state.balance?.chain !== chain) {
    try { state.balance = await api.getBalance(chain); } catch { /* resta quello che c'e' */ }
  }
  token.priceUsd = state.balance?.native_price_usd ?? null;

  paintForm(armed);
}

function paintForm(armed) {
  const chain = state.chain;
  const available = availableToSend(chain);
  sheets.setBody(`
    ${armed ? "" : `<p class="sheet-note is-warn">Live execution is off on this server: you can't send from here until it's turned on.</p>`}
    <form class="form" id="send-form">
      <label for="send-amount">Amount</label>
      <div class="swap-row">
        <input id="send-amount" name="amount" type="number" min="0" step="any" required />
        <button type="button" class="swap-max" id="send-max" ${token.isNative ? "" : "hidden"}>Max</button>
        <span class="swap-unit" id="send-symbol">${token.symbol}</span>
      </div>
      <button type="button" class="swap-token-pick" id="send-pick-token">Change token ↕</button>
      <p class="swap-available" id="send-available">
        ${token.isNative
          ? (available != null ? `Available: ${available.toLocaleString("en-US", { maximumFractionDigits: 6 })} ${token.symbol} (${GAS_RESERVE[chain]} kept for gas)` : "Balance unavailable.")
          : "Check this token's balance yourself before sending."}
      </p>
      <p class="swap-usd" id="send-usd"></p>

      <label for="send-to">To address</label>
      <input id="send-to" name="to" type="text" placeholder="${chain === "solana" ? "Solana address" : "0x…"}" autocomplete="off" required />
      <p class="swap-detail is-warn">Check the address twice: on-chain transactions are final.</p>

      <label class="swap-confirm">
        <input type="checkbox" id="send-confirm-box" />
        <span>I confirm: real money, <b id="send-confirm-summary">—</b></span>
      </label>

      <button type="submit" class="btn-primary" id="send-submit" disabled>Enter amount and address</button>
    </form>
    <output class="outcome" id="send-outcome" hidden></output>
  `);

  const amountInput = sheets.sheetBody().querySelector("#send-amount");
  const toInput = sheets.sheetBody().querySelector("#send-to");
  const maxButton = sheets.sheetBody().querySelector("#send-max");
  const confirmBox = sheets.sheetBody().querySelector("#send-confirm-box");
  const form = sheets.sheetBody().querySelector("#send-form");

  const refresh = () => { confirmBox.checked = false; updateState(armed); };
  amountInput.addEventListener("input", refresh);
  toInput.addEventListener("input", refresh);
  confirmBox.addEventListener("change", () => updateState(armed));
  maxButton.addEventListener("click", () => {
    const max = availableToSend(chain);
    if (max != null) { amountInput.value = max; refresh(); }
  });
  sheets.sheetBody().querySelector("#send-pick-token").addEventListener("click", () => pickToken(armed));
  form.addEventListener("submit", (event) => { event.preventDefault(); if (confirmBox.checked) confirmAndSend(armed); });

  updateState(armed);
}

function pickToken(armed) {
  const chain = state.chain;
  sheets.openTokenPicker(chain, {
    onPick: async ({ address }) => {
      const submitButton = sheets.sheetBody().querySelector("#send-submit");
      if (address.toLowerCase() === NATIVE[chain].address.toLowerCase()) {
        token = { ...NATIVE[chain], priceUsd: state.balance?.native_price_usd ?? null, isNative: true };
        paintForm(armed);
        return;
      }
      if (submitButton) { submitButton.disabled = true; submitButton.textContent = "Checking token…"; }
      try {
        const info = await api.getTokenInfo(chain, address);
        token = { address, decimals: info.decimals, symbol: info.symbol || `${address.slice(0, 4)}…${address.slice(-4)}`, priceUsd: info.price_usd, isNative: false };
        paintForm(armed);
      } catch (error) {
        if (submitButton) submitButton.textContent = "Retry";
        const detailEl = sheets.sheetBody().querySelector("#send-amount")?.closest("form")?.querySelector(".swap-detail");
        if (detailEl) detailEl.textContent = error instanceof ApiError ? error.message : "Token not recognized.";
      }
    },
  });
}

function updateState(armed) {
  const amount = Number(sheets.sheetBody().querySelector("#send-amount")?.value);
  const to = sheets.sheetBody().querySelector("#send-to")?.value.trim();
  const usdEl = sheets.sheetBody().querySelector("#send-usd");
  const summaryEl = sheets.sheetBody().querySelector("#send-confirm-summary");
  const submitButton = sheets.sheetBody().querySelector("#send-submit");
  const confirmBox = sheets.sheetBody().querySelector("#send-confirm-box");
  if (usdEl) usdEl.textContent = amount > 0 && token.priceUsd != null ? `≈ ${formatUsd(amount * token.priceUsd)}` : "";
  const ready = amount > 0 && to && to.length > 3;
  if (summaryEl) summaryEl.textContent = ready ? `${amount} ${token.symbol} → ${to}` : "—";
  if (submitButton) {
    submitButton.disabled = !ready || !armed || !confirmBox?.checked;
    submitButton.textContent = !ready ? "Enter amount and address" : (armed ? "Send" : "Execution off on server");
  }
}

async function confirmAndSend(armed) {
  if (!armed) return;
  const chain = state.chain;
  const amount = Number(sheets.sheetBody().querySelector("#send-amount").value);
  const to = sheets.sheetBody().querySelector("#send-to").value.trim();
  const outcome = sheets.sheetBody().querySelector("#send-outcome");
  const submitButton = sheets.sheetBody().querySelector("#send-submit");

  submitButton.disabled = true;
  submitButton.textContent = "Sending…";
  outcome.hidden = false;
  outcome.dataset.tone = "";
  outcome.textContent = "Signing and sending…";

  // Il tetto di rischio lato server e' notional_usd = amount * price_usd:
  // un fallback a 0 lo azzererebbe (bypass verso l'alto, mai accettabile).
  // Se il prezzo non e' noto, 1 USD/unita' e' una stima dichiarata — quasi
  // certamente sbagliata, ma nella direzione prudente solo se il token
  // vale MENO di un dollaro; per questo il campo resta visibile e chi
  // invia vede comunque "prezzo n/d" nel form prima di firmare.
  const priceUsd = token.priceUsd != null ? token.priceUsd : 1;

  try {
    const result = await api.sendToken({
      chain, to_address: to, token_address: token.isNative ? null : token.address,
      amount, price_usd: priceUsd,
    });
    outcome.dataset.tone = result.accepted ? "ok" : "no";
    outcome.textContent = result.accepted ? `Sent. Signature: ${result.tx_signature}` : result.reason;
    if (result.accepted) onSentCb?.();
  } catch (error) {
    outcome.dataset.tone = "no";
    outcome.textContent = error instanceof ApiError ? error.message : "Unexpected error.";
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Send";
  }
}
