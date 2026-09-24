/**
 * Attivare le strategie dell'archivio (trend, RSI+BB, Accumulo/
 * Distribuzione) sui token di questo wallet — non un secondo motore:
 * stessa Watchlist, stessa API, stesso trading automatico on-chain che
 * gia' sa comprare e vendere da questa sessione. Sempre visibile nel
 * pannello inferiore a tab, filtrata ai soli token collegabili a QUESTA
 * chain — insieme a posizioni/ordini/storico, non in un pannello a se'.
 *
 * Aggiungere un NUOVO token resta nella tab Watchlist del sito: la
 * ricerca per ticker li' risolve gia' simbolo DEX completo (BASE@chain:
 * pool) e indirizzo insieme — duplicarla qui con solo l'indirizzo del
 * token (che il picker di questo wallet da') vorrebbe dire ricostruire a
 * mano un pool che non conosciamo, con un rischio reale di sbagliarlo.
 */
import * as api from "@onchain/api.js";
import { ApiError } from "@onchain/api.js";
import { state } from "@onchain/store.js";
import { escapeHtml, strategyLabel } from "@onchain/format.js";

function parseChainFromSymbol(symbol) {
  const m = /^.+@([a-z]+):.+$/.exec(symbol);
  return m ? m[1] : null;
}

export async function mountStrategies(container) {
  const chain = state.chain;
  container.innerHTML = "<p class='sheet-note'>Loading watchlist…</p>";

  let strategies = [];
  let entries = [];
  try {
    [strategies, entries] = await Promise.all([api.getStrategies(), api.getWatchlist()]);
  } catch (error) {
    container.innerHTML = `<p class="sheet-note is-error">${error instanceof ApiError ? error.message : "Unexpected error."}</p>`;
    return;
  }

  const onchainEntries = entries.filter((e) => e.base_token_address && parseChainFromSymbol(e.symbol) === chain);
  container.innerHTML = `
    <p class="swap-detail">To add a new token, use search in the site's Watchlist tab — here you can only toggle the ones already there.</p>
    <ul id="strategies-list" class="tx-list"></ul>
  `;
  paintList(container, onchainEntries, strategies);
}

function paintList(container, entries, strategies) {
  const list = container.querySelector("#strategies-list");
  if (!list) return;
  if (!entries.length) {
    list.innerHTML = "<li>No tokens from this chain in your watchlist yet.</li>";
    return;
  }
  list.innerHTML = entries.map((entry) => `
    <li data-entry="${entry.id}" style="flex-direction:column;align-items:stretch;gap:6px">
      <b>${entry.symbol.split("@")[0]}</b>
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        ${strategies.map((name) => `
          <label class="mo-radio-label" style="display:flex;align-items:center;gap:4px">
            <input type="checkbox" data-auto-trade="${entry.id}:${name}" ${entry.auto_trade?.[name] ? "checked" : ""} />
            ${escapeHtml(strategyLabel(name))}
          </label>`).join("")}
      </div>
      <p class="swap-detail" data-entry-error="${entry.id}" hidden></p>
    </li>`).join("");

  list.querySelectorAll("[data-auto-trade]").forEach((box) => {
    box.addEventListener("change", async () => {
      const [entryId, strategyName] = box.dataset.autoTrade.split(":");
      const errorEl = list.querySelector(`[data-entry-error="${entryId}"]`);
      box.disabled = true;
      if (errorEl) errorEl.hidden = true;
      try {
        // Attivare il trading automatico presuppone anche l'alert acceso
        // per quella strategia: senza segnali non parte niente.
        await api.setWatchlistStrategy(entryId, strategyName, box.checked);
        await api.setWatchlistAutoTrade(entryId, strategyName, box.checked);
      } catch (error) {
        box.checked = !box.checked;
        if (errorEl) {
          errorEl.hidden = false;
          errorEl.classList.add("is-warn");
          errorEl.textContent = error instanceof ApiError ? error.message : "Unexpected error.";
        }
      } finally {
        box.disabled = false;
      }
    });
  });
}
