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
