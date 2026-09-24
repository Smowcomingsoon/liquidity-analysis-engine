/**
 * La riga "cosa hai" sotto il saldo. Richiesta: "non e' chiaro che moneta io
 * abbia nel portafoglio [...] devo sapere subito cosa ho disponibile".
 *
 * Stessi dati e stesso aspetto della riga sopra lo swap (swap.js): la
 * moneta evidenziata e' quella da cui parte lo swap. Una riga sola che
 * scorre di lato, cosi' il radar resta subito sotto.
 */
import { formatUsd } from "@onchain/format.js";
import { state } from "@onchain/store.js";
import { walletHoldings } from "@onchain/swap.js";

const fmtAmount = (n) => n.toLocaleString("it-IT", { maximumFractionDigits: n >= 1 ? 4 : 6 });

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export async function refreshHoldings(container, { force = false, onPick } = {}) {
  if (!container) return;
  const chain = state.chain;
  let rows;
  try { rows = await walletHoldings(chain, force); } catch { return; }
  if (chain !== state.chain) return;
  container.hidden = false;
  if (!rows.length) {
    container.innerHTML = `<p class="swap-wallet-empty">Wallet vuoto · tocca l'indirizzo in alto per ricevere</p>`;
    return;
  }
  container.innerHTML = rows.map((h, i) => `
    <button type="button" class="wallet-chip${i === 0 ? " is-current" : ""}" data-i="${i}">
      <b>${fmtAmount(h.amount)} ${escapeHtml(h.symbol)}</b>${h.valueUsd != null ? `<small>${formatUsd(h.valueUsd)}</small>` : ""}
    </button>`).join("");
  container.onclick = (event) => {
    const chip = event.target.closest("[data-i]");
    if (chip) onPick?.(rows[Number(chip.dataset.i)]);
  };
}
