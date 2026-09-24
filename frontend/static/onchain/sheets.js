/**
 * I pannelli che salgono dal basso, su <dialog> nativo.
 *
 * Il browser porta gia' quello che una sheet fatta a mano deve reimplementare
 * male: top-layer (niente z-index da rincorrere), ::backdrop, trappola del
 * focus, chiusura con Escape, e il resto della pagina reso inerte. L'entrata
 * e l'uscita sono CSS (@starting-style + transition-behavior: allow-discrete),
 * non timer JS.
 *
 * Il contenuto si carica all'apertura, mai prima: tetti, transazioni e
 * posizioni cambiano, e una sheet riempita al caricamento della pagina
 * mostrerebbe lo stato di quando la pagina e' stata aperta.
 */
import * as api from "@onchain/api.js";
import { escapeHtml, formatToken, formatUsd, shortAddress } from "@onchain/format.js";
import { chainInfo, readCache, state } from "@onchain/store.js";

// Stesso indirizzo nativo duplicato in swap.js/limitOrders.js: qui serve
// solo per proporre il nativo come "token che hai" nel picker, non per
// firmare niente.
const NATIVE_ADDRESS = {
  solana: "So11111111111111111111111111111111111111112",
  robinhood: "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
};

const dialog = document.querySelector("#sheet");
const titleEl = dialog.querySelector(".sheet-title");
const descEl = dialog.querySelector(".sheet-desc");
const bodyEl = dialog.querySelector(".sheet-body");

// Sul backdrop di un <dialog> il click ha come target il dialog stesso:
// e' il modo standard di distinguerlo da un click sul contenuto.
dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
dialog.querySelector(".sheet-close").addEventListener("click", () => dialog.close());

export function openSheet(title, description) {
  titleEl.textContent = title;
  descEl.textContent = description;
  bodyEl.replaceChildren();
  if (!dialog.open) dialog.showModal();
}
const open = openSheet;

export const closeSheet = () => dialog.close();
export const sheetBody = () => bodyEl;

const html = (markup) => {
  const template = document.createElement("template");
  template.innerHTML = markup.trim();
  return template.content;
};

export const setBody = (markup) => bodyEl.replaceChildren(html(markup));
export const setMessage = (text, tone = "") =>
  setBody(`<p class="sheet-note ${tone}">${escapeHtml(text)}</p>`);

/* ---------------------------------------------------------------- Ricevi */

export function openReceive() {
  const { label, unit, suffix } = chainInfo();
  const address = state.wallets[state.chain]?.address ?? readCache(state.chain)?.address ?? "";
  open("Ricevi", `Indirizzo ${label}${suffix}. Manda solo asset di questa chain: un invio da un'altra rete e' perso.`);
  setBody(`
    <p class="address-block">${escapeHtml(address)}</p>
    <dl class="kv">
      <div><dt>Chain</dt><dd>${escapeHtml(label)}${escapeHtml(suffix)}</dd></div>
      <div><dt>Asset nativo</dt><dd>${escapeHtml(unit)}</dd></div>
    </dl>
    <button type="button" class="btn-quiet" data-copy>Copia indirizzo</button>
  `);
  bodyEl.querySelector("[data-copy]").addEventListener("click", async (event) => {
    await navigator.clipboard?.writeText(address);
    event.currentTarget.textContent = "Copiato";
  });
}

/* ---------------------------------------------------------------- Limiti */

export async function openRisk(onChange) {
  open("Limiti & sicurezza", "I tetti valgono su ogni ordine, simulato o reale. Il circuit breaker si riarma solo a mano.");
  setMessage("Leggo lo stato…");
  await renderRisk(onChange);
}

async function renderRisk(onChange) {
  try {
    const data = await api.getRiskStatus(state.chain);
    const breaker = data.circuit_breaker;
    const spentRatio = data.daily_cap_usd ? Math.min(1, data.daily_spent_usd / data.daily_cap_usd) : 0;

    setBody(`
      <dl class="kv">
        <div><dt>Per ordine</dt><dd>${formatUsd(data.per_trade_cap_usd)}</dd></div>
        <div><dt>Concentrazione max</dt><dd>${data.concentration_cap_pct}%</dd></div>
        <div><dt>Esecuzione reale</dt><dd>${data.execution_enabled ? "attiva" : "spenta"}</dd></div>
      </dl>

      <div class="meter" style="--fill:${(spentRatio * 100).toFixed(1)}%">
        <div class="meter-head"><span>Speso oggi</span>
          <b>${formatUsd(data.daily_spent_usd)} di ${formatUsd(data.daily_cap_usd)}</b></div>
        <div class="meter-track"><div class="meter-bar"></div></div>
      </div>

      <p class="breaker" data-tripped="${breaker.tripped}">
        <span class="breaker-dot"></span>
        ${breaker.tripped
          ? `Circuit breaker scattato — ${breaker.consecutive_losses} perdite di fila`
          : "Circuit breaker a riposo"}
      </p>
      ${breaker.tripped ? '<button type="button" class="btn-quiet" data-reset>Riattiva manualmente</button>' : ""}

      <div class="switch-row">
        <span><b>Wallet attivo</b><small>spento resta visibile, ma nessuna operazione lo tocca</small></span>
        <button type="button" class="switch" role="switch" data-toggle></button>
      </div>
    `);

    bodyEl.querySelector("[data-reset]")?.addEventListener("click", async () => {
      try {
        await api.resetCircuitBreaker(state.chain);
        await renderRisk(onChange);
        onChange?.();
      } catch (error) { setMessage(error.message, "is-error"); }
    });
    wireWalletSwitch(bodyEl.querySelector("[data-toggle]"));
  } catch (error) {
    setMessage(error.message, "is-error");
  }
}

// L'interruttore del wallet sta qui, fra i limiti, non in una schermata
// "impostazioni" a se': spegnerlo e' una misura di sicurezza, non una
// preferenza.
function wireWalletSwitch(button) {
  const wallet = state.wallets[state.chain];
  if (!button || !wallet) { button?.closest(".switch-row")?.remove(); return; }

  const paint = (enabled) => button.setAttribute("aria-checked", String(enabled));
  paint(wallet.enabled !== false);

  button.addEventListener("click", async () => {
    const next = button.getAttribute("aria-checked") !== "true";
    button.disabled = true;
    try {
      const updated = await api.setWalletEnabled(state.chain, next);
      state.wallets[state.chain] = { ...wallet, enabled: updated.enabled };
      paint(updated.enabled);
    } catch (error) {
      setMessage(error.message, "is-error");
    } finally {
      button.disabled = false;
    }
  });
}

/* ------------------------------------------------------------- Simulazione */

export function openSimulate({ onAccepted } = {}) {
  open("Simulazione", "Gli stessi guardrail dell'ordine reale, senza chiave e senza transazione: serve a vedere se passerebbe.");
  setBody(`
    <form class="form" id="simulate-form">
      <label for="sim-token">Token</label>
      <input id="sim-token" name="token" type="text" placeholder="simbolo o indirizzo" required autocomplete="off" />

      <label for="sim-side">Lato</label>
      <select id="sim-side" name="side">
        <option value="buy">Compra</option>
        <option value="sell">Vendi</option>
      </select>

      <div class="form-pair">
        <span><label for="sim-notional">Size (USD)</label>
          <input id="sim-notional" name="notional" type="number" min="0" step="0.01" value="10" required /></span>
        <span><label for="sim-price">Prezzo (USD)</label>
          <input id="sim-price" name="price" type="number" min="0" step="0.000001" value="1" required /></span>
      </div>

      <label for="sim-impact">Price impact stimato (%)</label>
      <input id="sim-impact" name="impact" type="number" min="0" step="0.01" value="0.5" required />

      <button type="submit" class="btn-primary">Simula</button>
    </form>
    <output class="outcome" id="simulate-outcome" hidden></output>
  `);

  const form = bodyEl.querySelector("#simulate-form");
  const outcome = bodyEl.querySelector("#simulate-outcome");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const fields = new FormData(form);
    const button = form.querySelector("button");
    button.disabled = true;
    outcome.hidden = false;
    outcome.dataset.tone = "";
    outcome.textContent = "Simulo…";
    try {
      const result = await api.simulateOrder({
        chain: state.chain,
        token: String(fields.get("token")).trim(),
        strategy_name: "manuale",
        side: fields.get("side"),
        notional_usd: Number(fields.get("notional")),
        price_impact_pct: Number(fields.get("impact")),
        price_usd: Number(fields.get("price")),
      });
      outcome.dataset.tone = result.accepted ? "ok" : "no";
      outcome.textContent = result.accepted ? "Passa tutti i guardrail." : result.reason;
      if (result.accepted) onAccepted?.();
    } catch (error) {
      outcome.dataset.tone = "no";
      outcome.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}

/* ------------------------------------------------------------ Chiave privata */

export function openKey({ onStart }) {
  open("Rivela chiave privata", "Serve il riconoscimento sul tuo telefono — Face ID, Touch ID o password. La sola sessione del browser non basta.");
  setBody(`
    <dl class="kv">
      <div><dt>Verifica</dt><dd>QR + WebAuthn o password</dd></div>
      <div><dt>Validita' del QR</dt><dd>2 minuti</dd></div>
    </dl>
    <button type="button" class="btn-danger" data-start>Rivela ora</button>
  `);
  bodyEl.querySelector("[data-start]").addEventListener("click", () => { dialog.close(); onStart(); });
}

/* ------------------------------------------------------- Righe delle posizioni */

export function positionRow(position) {
  return `
    <li class="asset">
      <span class="asset-mark is-token">${escapeHtml(position.token.slice(0, 1).toUpperCase())}</span>
      <span class="asset-name"><b>${escapeHtml(position.token)}</b>
        <small>${formatToken(position.quantity)} ${escapeHtml(position.token)}</small></span>
      <span class="asset-value"><b>${formatUsd(position.cost_usd)}</b><small>costo</small></span>
    </li>`;
}

/* --------------------------------------------------------- Ricerca token */
// Sostituisce il prompt() del browser: mai un dialog nativo dentro l'app
// (richiesta: "sta cosa va fatta all'interno della web app"). Cerca per
// ticker sulla stessa fonte del resto del sito — DexScreener, gia' usata
// per i grafici — non un indirizzo digitato a mano come unica via.
const pickerDialog = document.querySelector("#picker");
const pickerBody = pickerDialog.querySelector(".sheet-body");
pickerDialog.addEventListener("click", (event) => { if (event.target === pickerDialog) pickerDialog.close(); });
pickerDialog.querySelector(".sheet-close").addEventListener("click", () => pickerDialog.close());
pickerBody.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && event.target.matches?.("li[data-address]")) event.target.click();
});

let pickerDebounce = null;

export const isNativeAddress = (chain, address) =>
  Boolean(address) && address.toLowerCase() === NATIVE_ADDRESS[chain]?.toLowerCase();

// Una schermata sola per scegliere un token, a tutto schermo da telefono:
// campo in alto con "Annulla" accanto, sotto i token che hai; scrivere
// sostituisce la lista invece di allungarla. Richiesta: "quando scelgo un
// token appare sto casino, serve una schermata immersiva e semplice" e
// "immersivo, pulito e ben fatto".
//
// Il campo e' type=search con name "search": Safari su iOS non propone
// l'autocompilazione dei contatti (casa/ufficio sopra la tastiera) sui
// campi di ricerca, mentre autocomplete="off" da solo lo ignora.
const iconCache = new Map(); // indirizzo -> url, dalle ricerche gia' fatte

const hueOf = (text) => [...text].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) % 360, 7);

function avatar(token) {
  const icon = token.icon || iconCache.get(token.address.toLowerCase());
  const letter = escapeHtml((token.symbol || "?").replace(/^\$/, "").slice(0, 1).toUpperCase());
  return `<span class="tok-av" style="--h:${hueOf(token.address)}" aria-hidden="true">${
    icon ? `<img src="${escapeHtml(icon)}" alt="" loading="lazy" referrerpolicy="no-referrer" />` : ""}<span>${letter}</span></span>`;
}

const VERIFIED_MARK = `<svg class="tok-check" viewBox="0 0 16 16" aria-label="verificato"><path d="M8 1.5l1.7 1.2 2.1-.1.7 2 1.7 1.2-.6 2 .6 2-1.7 1.2-.7 2-2.1-.1L8 14.5l-1.7-1.2-2.1.1-.7-2L1.8 10.2l.6-2-.6-2 1.7-1.2.7-2 2.1.1z"/><path class="tick" d="M5.4 8.2l1.8 1.8 3.4-3.6"/></svg>`;

function tokenRow(token, side) {
  return `
    <li class="tok-row" data-address="${escapeHtml(token.address)}" data-symbol="${escapeHtml(token.symbol || "")}" role="button" tabindex="0">
      ${avatar(token)}
      <span class="tok-main">
        <span class="tok-sym">${escapeHtml(token.symbol || shortAddress(token.address))}${token.verified ? VERIFIED_MARK : ""}</span>
        <small>${escapeHtml(token.name || "")}</small>
      </span>
      <span class="tok-side">${side}</span>
    </li>`;
}

const skeleton = (n) => Array.from({ length: n }, () =>
  `<li class="tok-row is-skeleton" aria-hidden="true"><span class="tok-av"></span><span class="tok-main"><span></span><small></small></span></li>`).join("");

const note = (text) => `<li class="tok-note">${escapeHtml(text)}</li>`;

// Un'immagine che non carica lascia la lettera sotto, non un riquadro rotto.
function wireAvatars(list) {
  list.querySelectorAll(".tok-av img").forEach((img) => {
    img.addEventListener("error", () => img.remove(), { once: true });
  });
}

export function openTokenPicker(chain, { onPick, title = "Scegli token" } = {}) {
  pickerDialog.querySelector(".sheet-title").textContent = title;
  pickerDialog.setAttribute("aria-label", title);
  pickerBody.innerHTML = `
    <div class="picker-top">
      <p class="picker-title">${escapeHtml(title)}</p>
      <div class="picker-search">
        <label class="picker-field">
          <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="6"/><path d="M13.5 13.5L17 17"/></svg>
          <input type="search" id="picker-query" name="search" placeholder="Cerca per nome o ticker"
            aria-label="Cerca token" autocomplete="off" autocorrect="off" autocapitalize="off"
            spellcheck="false" enterkeyhint="search" data-1p-ignore data-lpignore="true" data-form-type="other" />
        </label>
        <button type="button" class="picker-cancel">Annulla</button>
      </div>
    </div>
    <div class="picker-scroll">
      <p class="picker-label" id="picker-label">I tuoi token</p>
      <ul id="picker-held" class="picker-list">${skeleton(2)}</ul>
      <ul id="picker-results" class="picker-list" hidden></ul>
      <p class="picker-hint">Non lo trovi? Incolla il contratto del token nel campo in alto.</p>
    </div>
  `;
  // Da telefono la tastiera della ricerca sposta la pagina sotto il foglio,
  // e chiuso il foglio ci si ritrovava piu' in basso (richiesta: "la
  // schermata mi va in basso e mi tocca tornare su"). Si torna dov'eri.
  if (!pickerDialog.open) {
    const scrollTop = window.scrollY;
    pickerDialog.addEventListener("close", () => {
      const back = () => window.scrollTo({ top: scrollTop, behavior: "instant" });
      requestAnimationFrame(back);
      setTimeout(back, 350);  // dopo che la tastiera di iOS ha finito di chiudersi
    }, { once: true });
    pickerDialog.showModal();
  }

  const input = pickerBody.querySelector("#picker-query");
  const results = pickerBody.querySelector("#picker-results");
  const held = pickerBody.querySelector("#picker-held");
  const label = pickerBody.querySelector("#picker-label");
  pickerBody.querySelector(".picker-cancel").addEventListener("click", () => pickerDialog.close());
  // Schermata di ricerca: si scrive subito. I tuoi token restano sopra la
  // tastiera, perche' la lista parte appena sotto il campo.
  input.focus();

  const pick = (token) => { pickerDialog.close(); onPick?.(token); };
  const wirePicks = (list) => {
    wireAvatars(list);
    list.querySelectorAll("[data-address]").forEach((li) => {
      li.addEventListener("click", () => pick({ address: li.dataset.address, symbolHint: li.dataset.symbol || null }));
    });
  };

  // I token che il wallet ha DAVVERO (saldo nativo + portafoglio reale via
  // RPC), non solo la ricerca per ticker — Richiesta: "quando seleziono cosa
  // vendere mi devi far selezionare dalle crypto disponibili nel wallet,
  // qualunque esse siano".
  (async () => {
    try {
      const { unit } = chainInfo();
      const [balance, portfolio] = await Promise.all([
        api.getBalance(chain).catch(() => null),
        api.getPortfolio(chain).catch(() => ({ holdings: [] })),
      ]);
      const rows = [];
      if (balance) {
        rows.push({ address: NATIVE_ADDRESS[chain], symbol: unit, name: "Nativo · paga il gas", amount: balance.native_balance, verified: true });
      }
      for (const h of portfolio.holdings) {
        if (h.amount > 0) rows.push({ address: h.address, symbol: h.symbol, name: h.name || "", amount: h.amount });
      }
      held.innerHTML = rows.length
        ? rows.map((r) => tokenRow(r, `<b>${formatToken(r.amount)}</b>`)).join("")
        : note("Nessun token con saldo in questo wallet.");
      wirePicks(held);
    } catch (error) {
      held.innerHTML = note(error.message);
    }
  })();

  const showHeld = (yes) => {
    held.hidden = !yes;
    results.hidden = yes;
    label.textContent = yes ? "I tuoi token" : "Risultati";
  };

  input.addEventListener("input", () => {
    clearTimeout(pickerDebounce);
    const q = input.value.trim();
    if (q.length < 2) { showHeld(true); return; }
    showHeld(false);
    // Un indirizzo incollato si usa cosi' com'e': niente ricerca per
    // ticker, niente secondo campo nascosto in un "non lo trovi?".
    if (looksLikeAddress(q)) {
      results.innerHTML = tokenRow({ address: q, symbol: "Contratto incollato", name: shortAddress(q) }, "Usa ›");
      wirePicks(results);
      return;
    }
    results.innerHTML = skeleton(3);
    pickerDebounce = setTimeout(async () => {
      try {
        const found = await api.searchTokens(chain, q);
        if (input.value.trim() !== q) return;
        if (!found.length) { results.innerHTML = note("Nessun token con questo nome su questa rete."); return; }
        for (const t of found) if (t.icon) iconCache.set(t.address.toLowerCase(), t.icon);
        results.innerHTML = found.map((t) => tokenRow(t, `${
          t.liquidity_usd ? `<span>liq. ${compactUsd.format(t.liquidity_usd)}</span>` : ""}${
          t.verified ? "" : "<i>non verificato</i>"}`)).join("");
        wirePicks(results);
      } catch (error) {
        results.innerHTML = note(error.message);
      }
    }, 300);
  });

  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    results.querySelector("[data-address]")?.click();
  });
}

const compactUsd = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1,
});

// Mint Solana (base58, 32-44 caratteri) o contratto EVM (0x + 40 hex).
const looksLikeAddress = (text) =>
  /^0x[0-9a-fA-F]{40}$/.test(text) || /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(text);

export { shortAddress };
