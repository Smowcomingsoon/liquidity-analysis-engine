/**
 * Formattazione, tutta da Intl: valuta, quantita' e "quanto fa" non si
 * costruiscono a mano con toFixed e concatenazioni. Intl conosce gia' il
 * separatore delle migliaia italiano, i minimi/massimi decimali e i tempi
 * relativi — riscriverli a mano significa solo sbagliarli in un caso.
 */

// Locale en-US per i soli importi in dollari, non per distrazione: in it-IT
// Intl scrive "8,87 USD", con la sigla in coda, mentre tutto il resto di
// questa app (i tetti, il ledger, il price feed) parla di "$20" col simbolo
// davanti. Due forme per la stessa valuta nella stessa schermata sono un
// inciampo.
const usd = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
});

// I saldi nativi vivono su scale incompatibili fra loro: 0.00337 ETH e
// 142.6 SOL vogliono un numero di decimali diverso, e arrotondare il primo
// a due cifre lo azzererebbe.
const token = new Intl.NumberFormat("en-US", { maximumSignificantDigits: 6 });

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

export const formatUsd = (value) => usd.format(value);
export const formatToken = (value) => token.format(value);

/** "$8.87" spezzato in parti: il simbolo e i centesimi si compongono piu'
 *  piccoli del numero, e per farlo servono i pezzi separati. */
export function usdParts(value) {
  const parts = usd.formatToParts(value);
  const pick = (...types) => parts.filter((p) => types.includes(p.type)).map((p) => p.value).join("");
  return {
    currency: pick("currency"),
    integer: pick("integer", "group"),
    fraction: pick("decimal", "fraction"),
  };
}

export function freshness(atMs) {
  const seconds = Math.round((Date.now() - atMs) / 1000);
  if (seconds < 8) return "updated just now";
  if (seconds < 60) return relative.format(-seconds, "second");
  if (seconds < 3600) return relative.format(-Math.round(seconds / 60), "minute");
  return relative.format(-Math.round(seconds / 3600), "hour");
}

export const shortAddress = (address) =>
  address.length > 18 ? `${address.slice(0, 10)}…${address.slice(-6)}` : address;

export const escapeHtml = (value) =>
  String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

// Strategy names are storage keys on the backend (watchlist, auto-trade);
// only what the page shows is renamed to neutral, professional labels.
const STRATEGY_LABELS = [
  [/^Lorenzo HF\b[^·]*/i, "Trend HF"],
  [/^Lorenzo\b[^·]*/i, "Trend EMA"],
  [/^Gabriele 2\b[^·]*/i, "RSI+BB Long/Short"],
  [/^Gabriele\b[^·]*/i, "RSI+BB Reversal"],
];
export function strategyLabel(name) {
  const text = String(name ?? "");
  for (const [re, label] of STRATEGY_LABELS) if (re.test(text)) return text.replace(re, `${label} `).replace(/\s+$/, "").replace(/\s+·/, " ·");
  return text;
}

/* ------------------------------------------------------------ token rows */

// Compact dollars for lists: $510k, $3.5M, $420M.
// The strategy a Top 50 wallet trade names (sim_backtest.strategy_of): a
// label ("Trend EMA", "RSI+BB") or a family ("trend", "reversal", "counter").
const WALLET_STRATEGY = { trend: "", reversal: "RSI+BB", counter: "not trend follower", unknown: "strategy not recorded" };
export const walletStrategy = (s) => (s == null ? "" : WALLET_STRATEGY[s] ?? String(s));

export const usdShort = (v) => (v == null || !Number.isFinite(Number(v)) ? "—"
  : v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(v >= 1e8 ? 0 : 1)}M`
    : v >= 1e3 ? `$${(v / 1e3).toFixed(v >= 1e5 ? 0 : 1)}k` : `$${Math.round(v)}`);

// Age like the reference: 16m, 13h, 11d. Returns [text, fresh?].
export function age(ts) {
  if (!ts) return ["—", false];
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return [`${Math.max(1, Math.round(s / 60))}m`, s < 1800];
  if (s < 86400) return [`${Math.round(s / 3600)}h`, false];
  return [`${Math.round(s / 86400)}d`, false];
}

const DS_CHAIN = { solana: "solana", robinhood: "robinhood", base: "base", bsc: "bsc", ethereum: "ethereum" };
const CHAIN_MARK = {
  solana: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 16.5h12.5l2-2H7zM5 7.5h12.5l2 2H7zM5 12h12.5l2-2H7" /></svg>',
  robinhood: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 20 12 12 21 4 12z" /></svg>',
  base: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="5" width="14" height="14" rx="3.5" /></svg>',
  bsc: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.5 15 6.5 12 9.5 9 6.5zM12 14.5l3 3-3 3-3-3zM6.5 9l3 3-3 3-3-3zM17.5 9l3 3-3 3-3-3zM12 9.5l2.5 2.5-2.5 2.5L9.5 12z" /></svg>',
  ethereum: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.5 6 12.2 12 16l6-3.8zM6 13.6l6 8 6-8-6 3.8z" /></svg>',
};

/** The token's logo: DexScreener's image, else its public logo for the
 *  address. Underneath, never a letter (it reads as a placeholder): an
 *  emblem drawn from the address, two hues and a shape, unique per token. */
export function tile(symbol, image, chain, token) {
  const seed = String(token || symbol || "?");
  const h = [...seed].reduce((a, c) => (a * 33 + c.charCodeAt(0)) >>> 0, 5381);
  // DexScreener's picture URLs use lowercase EVM addresses (Solana mints are case-sensitive).
  const addr = token && String(token).startsWith("0x") ? String(token).toLowerCase() : token;
  const src = image || (addr && DS_CHAIN[chain] ? `https://dd.dexscreener.com/ds-data/tokens/${DS_CHAIN[chain]}/${addr}.png` : "");
  return `<span class="tile" style="--h1:${h % 360};--h2:${(h >> 9) % 360};--rot:${(h >> 5) % 360}deg" data-shape="${(h >> 3) % 4}"><i class="emblem" aria-hidden="true"></i>${src
    ? `<img src="${escapeHtml(src)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" />` : ""}</span>`;
}
if (typeof document !== "undefined") {
  document.addEventListener("error", (event) => {
    if (event.target instanceof HTMLImageElement && event.target.parentElement?.classList.contains("tile") || event.target.parentElement?.classList.contains("tok-av")) event.target.remove();
  }, true);
}

export function logo(symbol, image, chain, token) {
  return `<span class="logo">${tile(symbol, image, chain, token)}<i class="chain-mark" data-chain="${escapeHtml(chain ?? "")}">${CHAIN_MARK[chain] ?? ""}</i></span>`;
}

/** Change pill: ↑ 42% green / ↓ 7% red, from a fraction. */
export function changePill(f) {
  if (f == null || !Number.isFinite(Number(f))) return `<em class="chg">—</em>`;
  const up = f >= 0;
  const p = Math.abs(f * 100);
  return `<em class="chg" data-sign="${up ? "up" : "down"}">${up ? "↑" : "↓"} ${p >= 1000 ? Math.round(p).toLocaleString("en-US") : p >= 10 ? Math.round(p) : p.toFixed(1)}%</em>`;
}

/**
 * One row for every list (memes, gems, search, leaderboard): logo with the
 * chain, name with a HOT/EARLY pill, ticker and age, market cap and a pill.
 */
export function tokenRow({ id, symbol, name, chain, token, image, cap, change, pill, badge, ageTs, sub, sign, score }) {
  const [a, fresh] = age(ageTs);
  const tone = sign ?? (change == null ? "" : change >= 0 ? "up" : "down");
  return `<li class="trow" data-sign="${tone}"><button type="button" data-open="${escapeHtml(id)}">
    ${logo(symbol, image, chain, token)}
    <span class="trow-main">
      <b>${escapeHtml(name || symbol)}${badge === "hot" ? ' <em class="hot">HOT</em>' : badge === "early" ? ' <em class="hot early">EARLY</em>' : ""}</b>
      <small>${score != null ? `<span class="score" data-band="${score >= 60 ? "hi" : score >= 40 ? "mid" : "lo"}" title="Engine score">${Math.round(score)}</span>` : ""}${escapeHtml(symbol)}${ageTs ? ` <span class="age${fresh ? " fresh" : ""}"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 14V7M8 9c0-3 2-4.5 5-4.5 0 3-2 4.5-5 4.5ZM8 8C8 5.5 6.3 4 3.5 4c0 2.6 1.7 4 4.5 4Z"/></svg>${a}</span>` : ""}${sub ? ` <span class="sub">${sub}</span>` : ""}</small>
    </span>
    <span class="trow-num"><b>${usdShort(cap)}</b>${pill ?? changePill(change)}</span>
  </button></li>`;
}

/** One strategy's trade on one alert: entry, exit with the reason, and the
 *  return after fees. Shared by the alert cards and the token page, so the
 *  two cannot drift apart. */
const WHY = { signal: "trend turned", trail: "30% off the peak", crash: "crash inside one 5-min candle", "48h": "48h window over", rug: "pool vanished" };

function afterAlert(t, alertAt) {
  const m = Math.max(0, Math.round((t - alertAt) / 60));
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h}h ${rest}m` : `${h}h`;
}

export function strategyTrade(s, alertAt, chosen) {
  const ev = s.events ?? [];
  const buy = ev.find((v) => v.side === "buy");
  const sell = ev.filter((v) => v.side === "sell" || v.side === "rug").slice(-1)[0];
  const entry = buy
    ? `Bought ${afterAlert(buy.t, alertAt)} after the alert`
    : s.done ? "Did not enter" : "No entry yet";
  let exit = "";
  if (sell) {
    const why = WHY[sell.why] ?? "sold";
    exit = `<small>Sold ${afterAlert(sell.t, alertAt)} after · ${escapeHtml(why)}</small>`;
  } else if (s.state === "in") {
    exit = `<small>Still in the trade</small>`;
  }
  // No trade is no result: "—", never a green 0% (Lorenzo: "non voglio vedere 0%").
  const traded = (s.trades ?? 0) > 0 || s.state === "in";
  const ret = !traded || s.ret == null ? "—" : `${s.ret > 0 ? "+" : ""}${Math.round(s.ret * 100)}%`;
  const sign = !traded || s.ret == null ? "" : s.ret >= 0 ? "up" : "down";
  const many = (s.trades ?? 0) > 1 ? ` · ${s.trades} trades` : "";
  return `<div class="strat"${chosen && chosen === s.label ? " data-chosen" : ""}>
    <b>${escapeHtml(s.label)}</b>
    <b data-sign="${sign}">${ret}</b>
    <small>${escapeHtml(entry)}</small>
    <small>${!traded ? "" : s.state === "in" ? "open" : `after fees${many}`}</small>
    ${exit}
  </div>`;
}

/** The system's result on one alert, as shown: the better of its two
 *  internal versions, never named (Lorenzo, 28/09: "l'importante è proporre
 *  la migliore delle due, internamente, proponendo sempre il miglior
 *  risultato"). An alert where neither traded shows the one in use. */
export function bestSystem(a) {
  // The sniper's rule (Lorenzo, 28/09): in at the alert, out once on the
  // trend follower's exit. When that result exists it is the system.
  if (a.on_alert) return { ...a.on_alert, label: "System" };
  const traded = (a.systems ?? []).filter((s) => s && ((s.trades ?? 0) > 0 || s.state === "in") && s.ret != null);
  const best = traded.sort((x, y) => y.ret - x.ret)[0] ?? a.system;
  return best ? { ...best, label: "System" } : null;
}

