/**
 * Cosa detiene DAVVERO il wallet — saldo nativo (SOL/ETH, quello che paga
 * il gas) PIU' i saldi reali degli altri token letti dalla chain. Sempre
 * visibile nel pannello inferiore a tab ("Posizioni"), non piu' dietro
 * un tap — e mai senza il nativo: l'utente, "non visualizzo da nessuna
 * parte il mio saldo attuale nel wallet" — era vero, /api/onchain/portfolio
 * elenca solo gli ALTRI token, il nativo va preso a parte e unito qui.
 *
 * Certezza del dato, non un numero solo: un vero exchange non nasconde
 * mai l'incertezza di un totale — se l'elenco e' parziale (Robinhood
 * Chain, senza indexer pubblico affidabile) il totale e' "≥ $X", mai un
 * numero che sembra definitivo e non lo e'.
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { formatToken, formatUsd } from "@onchain/format.js";
import { chainInfo, state } from "@onchain/store.js";

export async function mountPortfolio(container) {
  const chain = state.chain;
  container.innerHTML = "<p class='sheet-note'>Loading portfolio…</p>";
  try {
    const [balance, data] = await Promise.all([
      api.getBalance(chain).catch(() => state.balance ?? null),
      api.getPortfolio(chain),
    ]);
    const { unit } = chainInfo();

    const priced = data.holdings.filter((h) => h.value_usd != null);
    const unpriced = data.holdings.filter((h) => h.value_usd == null);
    const nativeUsd = balance?.native_usd_value ?? null;
    const totalUsd = priced.reduce((sum, h) => sum + h.value_usd, 0) + (nativeUsd ?? 0);
    const totalLabel = (data.partial || unpriced.length || nativeUsd == null) ? `≥ ${formatUsd(totalUsd)}` : formatUsd(totalUsd);

    const nativeRow = balance ? `
      <li>
        <span><b>${unit}</b><small>native · pays gas · ${formatToken(balance.native_balance)} ${unit}</small></span>
        <span>${nativeUsd != null ? formatUsd(nativeUsd) : "no price"}</span>
      </li>` : `<li>Native balance unavailable — try again.</li>`;

    const otherRows = data.holdings.length
      ? data.holdings.map((h) => `
        <li>
          <span><b>${h.symbol}</b><small>${h.amount.toLocaleString("en-US", { maximumFractionDigits: 6 })}</small></span>
          <span>${h.value_usd != null ? formatUsd(h.value_usd) : "no price"}</span>
        </li>`).join("")
      : "";

    container.innerHTML = `
      ${data.partial ? `<p class="sheet-note is-warn">Partial list on this chain — only tokens this site knows about (no reliable public indexer on Robinhood Chain): a token received from outside this app may not show up.</p>` : ""}
      ${unpriced.length ? `<p class="sheet-note is-warn">${unpriced.length} token(s) with no known price, left out of the total.</p>` : ""}
      <dl class="kv"><div><dt>Estimated value${data.partial || unpriced.length || nativeUsd == null ? " (partial)" : ""}</dt><dd>${totalLabel}</dd></div></dl>
      <ul class="tx-list">${nativeRow}${otherRows}</ul>
    `;
  } catch (error) {
    container.innerHTML = `<p class="sheet-note is-error">${error instanceof ApiError ? error.message : "Unexpected error."}</p>`;
  }
}
