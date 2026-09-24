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
  container.innerHTML = "<p class='sheet-note'>Leggo il portafoglio…</p>";
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
        <span><b>${unit}</b><small>nativo · paga il gas · ${formatToken(balance.native_balance)} ${unit}</small></span>
        <span>${nativeUsd != null ? formatUsd(nativeUsd) : "prezzo n/d"}</span>
      </li>` : `<li>Saldo nativo non disponibile — riprova.</li>`;

    const otherRows = data.holdings.length
      ? data.holdings.map((h) => `
        <li>
          <span><b>${h.symbol}</b><small>${h.amount.toLocaleString("it-IT", { maximumFractionDigits: 6 })}</small></span>
          <span>${h.value_usd != null ? formatUsd(h.value_usd) : "prezzo n/d"}</span>
        </li>`).join("")
      : "";

    container.innerHTML = `
      ${data.partial ? `<p class="sheet-note is-warn">Elenco parziale su questa chain — solo i token che questo sito conosce (nessun indexer pubblico affidabile su Robinhood Chain): un token ricevuto da fuori quest'app potrebbe non comparire.</p>` : ""}
      ${unpriced.length ? `<p class="sheet-note is-warn">${unpriced.length} token senza prezzo noto, non contati nel totale.</p>` : ""}
      <dl class="kv"><div><dt>Valore stimato${data.partial || unpriced.length || nativeUsd == null ? " (parziale)" : ""}</dt><dd>${totalLabel}</dd></div></dl>
      <ul class="tx-list">${nativeRow}${otherRows}</ul>
    `;
  } catch (error) {
    container.innerHTML = `<p class="sheet-note is-error">${error instanceof ApiError ? error.message : "Errore inatteso."}</p>`;
  }
}
