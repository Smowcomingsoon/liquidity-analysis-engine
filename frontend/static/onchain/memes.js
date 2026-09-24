/**
 * The meme lane: tokens the serious scanner rejected for meme reasons
 * (name, website, GitHub) but that are alive, sellable and clean on
 * RugCheck/GoPlus, ranked by similarity to real pumps. Gambling, and the
 * section says so. Read-only: the autopilot never buys these.
 */
import * as api from "@onchain/api.js";
import { escapeHtml } from "@onchain/format.js";

const POLL_MS = 60_000;
const CHAIN = { solana: "Solana", robinhood: "Robinhood", ethereum: "Ethereum", base: "Base" };

let root = null;
let timer = null;

const usd = (n) => (n == null ? "—" : n >= 1e6 ? `$${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `$${Math.round(n / 1e3)}k` : `$${Math.round(n)}`);
const pct = (n) => (n == null ? "—" : `${n > 0 ? "+" : ""}${Math.round(n)}%`);

function row(m) {
  const flow = m.buys_h1 != null ? ` · ${m.buys_h1}↑ ${m.sells_h1 ?? 0}↓ 1h` : "";
  const cap = m.market_cap != null ? ` · cap ${usd(m.market_cap)}` : "";
  const chance = m.win_chance != null ? `${Math.round(m.win_chance * 100)}% chance` : "";
  const href = m.pair_url || "#";
  return `
    <li><a class="meme-row" href="${escapeHtml(href)}" target="_blank" rel="noopener">
      <span class="mm-avatar" aria-hidden="true">${escapeHtml(String(m.symbol).slice(0, 2).toUpperCase())}</span>
      <span class="mm-name"><b>${escapeHtml(m.symbol)}</b><small>${escapeHtml(CHAIN[m.chain] ?? m.chain)} · liq ${usd(m.liquidity_usd)}${cap}${flow}</small></span>
      <span class="mm-num"><b data-sign="${(m.ret_since_seen ?? 0) >= 0 ? "up" : "down"}">${pct(m.ret_since_seen)}</b><small>${escapeHtml(chance)}</small></span>
    </a></li>`;
}

async function refresh() {
  clearTimeout(timer);
  try {
    const data = await api.getRadarMemes();
    const items = data.items ?? [];
    delete root.dataset.loading;
    root.querySelector("[data-memes-list]").innerHTML = items.map(row).join("");
    root.querySelector("[data-memes-empty]").hidden = items.length > 0;
    root.querySelector("[data-memes-meta]").textContent = items.length ? `${items.length} on the runway` : "";
  } catch {
    // Like the radar: if the server does not answer, keep what is on screen.
  }
  if (document.visibilityState === "visible") timer = setTimeout(refresh, POLL_MS);
}

export function mountMemes() {
  root = document.getElementById("memes");
  if (!root) return;
  refresh();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
}
