/**
 * Your profile (backend: profiles.py, wealth.py, GET /api/profile).
 *
 * Lorenzo, with a trading app's profile screenshot: "uno deve vedere cosa
 * detiene, l'andamento del wallet nel tempo con un grafico [...] l'account
 * deve rimanere tutto nel database, uno deve poter scegliere anche
 * l'immagine profilo [...] un controllo maggiore su tutta la piattaforma".
 * Top to bottom, like the reference: picture (tap to change), name, @handle,
 * bio; hold time, trades, joined; what the wallets are worth with the change
 * and a line over 24h / 7d / 30d / All; cash with Deposit and the wallet
 * menu; positions, open (what the wallets hold now) or closed (the
 * system's round trips).
 */
import * as api from "@onchain/api.js";
import { escapeHtml, logo, usdShort } from "@onchain/format.js";
import * as phantom from "@onchain/phantomlink.js";
import { loadLinked } from "@onchain/userswap.js";
import { register } from "@onchain/detail.js";
import { flash, liveDelta, liveKey, liveValue, priceOf, roll, watch } from "@onchain/live.js";

const RANGES = [["1h", "1H"], ["2h", "2H"], ["3h", "3H"], ["4h", "4H"], ["8h", "8H"], ["24h", "1D"], ["7d", "1W"],
  ["30d", "1M"], ["all", "All"]];
let root = null;
let data = null;
let range = "all";
let tab = "open";
let chainF = "all";
let showHidden = false;
let showAllPos = false;
const CHAIN_NAMES = { solana: "Solana", robinhood: "Robinhood", base: "Base", ethereum: "Ethereum", bsc: "BSC" };
let go = () => {};

const money = (v) => {
  if (v == null) return ["—", ""];
  const [i, d] = Math.abs(v).toFixed(2).split(".");
  return [`${v < 0 ? "−" : ""}$${Number(i).toLocaleString("en-US")}`, `.${d}`];
};
const signed = (v) => (v == null ? "" : `${v >= 0 ? "+" : "−"}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (v) => (v == null ? "—" : `${v >= 0 ? "▲" : "▼"} ${Math.abs(v * 100).toFixed(2)}%`);
const qty = (v) => (v == null ? "" : v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : v.toLocaleString("en-US", { maximumFractionDigits: 4 }));
const hold = (s) => (s == null ? "—" : s < 3600 ? `${Math.max(1, Math.round(s / 60))}m` : s < 86_400 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${Math.floor(s / 86_400)}d ${Math.round((s % 86_400) / 3600)}h`);
const since = (t) => (t ? new Date(t * 1000).toLocaleDateString("en-US", { month: "short", year: "numeric" }) : "—");

const PENCIL = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m13.5 6.5 4 4"/></svg>`;

function avatar(p, cls = "") {
  return p?.avatar_url
    ? `<img class="${cls}" src="${escapeHtml(p.avatar_url)}" alt="" decoding="async" />`
    : `<i class="${cls} me-empty" aria-hidden="true">${escapeHtml((p?.display_name ?? "?").trim().slice(0, 1).toUpperCase())}</i>`;
}

/** Your picture everywhere it stands for you: header and dock. */
function paintMe(p) {
  document.querySelectorAll("[data-me-avatar]").forEach((el) => { el.innerHTML = avatar(p); });
  const head = document.querySelector("#more-btn .avatar");
  if (head) head.innerHTML = p?.avatar_url ? `<img src="${escapeHtml(p.avatar_url)}" alt="" />` : "";
}

let scrub = null;

/* The simulated wallet is the Top 50 wallet (main._top_wallet): $10,000,
 * the same for every account, on every entry and exit of the strategy on
 * the Top 50 tokens, and on the next ones (Lorenzo, 30/09). */
function simNote(d) {
  if (!d.sim) return `<div class="pf-simnote"><p>The Top 50 wallet is not ready yet.</p></div>`;
  const from = d.sim.from ? new Date(d.sim.from * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "";
  const ret = d.sim.ret == null ? "" : ` · ${d.sim.ret >= 0 ? "+" : ""}${(d.sim.ret * 100).toFixed(2)}%`;
  return `<div class="pf-simnote"><p>$${d.sim.start.toLocaleString("en-US")}${from ? ` on ${from}` : ""}${ret ? `${ret} since then` : ""}. Every strategy entry and exit on the Top 50 tokens, ${Math.round((d.sim.share ?? 0.1) * 100)}% per trade, same for every account. Nothing is sent.
    <button type="button" class="pf-link" data-sim-top>Details</button></p></div>`;
}

/* Touching the line: the big number and the change become those of the
 * reading under the finger, with its time; letting go shows now again. */
function bindScrub() {
  const svg = root.querySelector(".pf-line");
  if (!svg || !scrub) return;
  const totalEl = root.querySelector(".pf-total");
  const changeEl = root.querySelector(".pf-change");
  const idle = [totalEl.innerHTML, changeEl.innerHTML, changeEl.dataset.sign];
  const vx = svg.querySelector(".scrub-x"), vd = svg.querySelector(".scrub-dot");
  const at = (event) => {
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * scrub.W;
    let best = scrub.rows[0];
    for (const r of scrub.rows) if (Math.abs(scrub.x(r[0]) - px) < Math.abs(scrub.x(best[0]) - px)) best = r;
    const [whole, cents] = money(best[1]);
    totalEl.innerHTML = `<b>${whole}</b><span>${cents}</span>`;
    const diff = best[1] - scrub.rows[0][1];
    changeEl.dataset.sign = diff >= 0 ? "up" : "down";
    changeEl.innerHTML = `${signed(diff)} <span>${new Date(best[0] * 1000).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>`;
    const cx = scrub.x(best[0]).toFixed(1), cy = scrub.y(best[1]).toFixed(1);
    vx.setAttribute("x1", cx); vx.setAttribute("x2", cx); vd.setAttribute("cx", cx); vd.setAttribute("cy", cy);
    vx.removeAttribute("hidden"); vd.removeAttribute("hidden");
  };
  const leave = () => {
    [totalEl.innerHTML, changeEl.innerHTML, changeEl.dataset.sign] = idle;
    vx.setAttribute("hidden", ""); vd.setAttribute("hidden", "");
    applyLive();
  };
  svg.addEventListener("pointerdown", at);
  svg.addEventListener("pointermove", at);
  svg.addEventListener("pointerleave", leave);
  svg.addEventListener("pointerup", (e) => { if (e.pointerType !== "mouse") leave(); });
}

function chart(rows) {
  const W = 360, H = 170;
  scrub = null;
  if (!rows || rows.length < 2) return `<div class="pf-chart-empty">Not enough readings in this period yet: one a minute while the app is open, every 5 minutes otherwise.</div>`;
  const ys = rows.map((r) => r[1]);
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
  const t0 = rows[0][0], t1 = rows[rows.length - 1][0];
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (W - 12);
  const y = (v) => 12 + (1 - (v - lo) / (hi - lo)) * (H - 24);
  // Steps, not slopes: between two readings the value was the earlier one
  // (it is only known at readings), so the line never shows a price the
  // wallet did not have.
  const d = rows.map((r, i) => i ? `H${x(r[0]).toFixed(1)}V${y(r[1]).toFixed(1)}` : `M${x(r[0]).toFixed(1)},${y(r[1]).toFixed(1)}`).join("");
  scrub = { rows, x, y, W };
  const up = ys[ys.length - 1] >= ys[0];
  return `<svg class="pf-line" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" data-sign="${up ? "up" : "down"}" role="img" aria-label="Wallet value over time">
    <defs><linearGradient id="pf-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="currentColor" stop-opacity=".22"/><stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>
    <path class="area" d="${d}L${x(t1).toFixed(1)},${H}L0,${H}Z"/>
    <path class="ln" d="${d}"/>
    <circle class="halo" cx="${x(t1).toFixed(1)}" cy="${y(ys[ys.length - 1]).toFixed(1)}" r="11"/>
    <circle class="dot" cx="${x(t1).toFixed(1)}" cy="${y(ys[ys.length - 1]).toFixed(1)}" r="5"/>
    <line class="scrub-x" x1="0" x2="0" y1="0" y2="${H}" hidden/>
    <circle class="scrub-dot" r="5" hidden/>
  </svg>`;
}

/* ------------------------------------------------ connected wallets
 * Lorenzo: "dobbiamo dare la possibilita' di far collegare il wallet a
 * qualsiasi persona". The wallet signs a plain message (free, no
 * transaction); the server checks the signature; its holdings join the
 * profile, read-only. Browser wallets only (MetaMask, Rabby, Phantom):
 * on a phone without one, the wallet's own browser opens this page. */
const hexOf = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
const shortAddr = (a) => `${a.slice(0, 5)}…${a.slice(-4)}`;
const evmProvider = () => window.ethereum ?? null;
const solProvider = () => window.phantom?.solana ?? (window.solana?.isPhantom ? window.solana : null);
const here = () => `${location.host}${location.pathname}#profile`;

async function connect(kind) {
  let address, sign;
  if (kind === "evm") {
    const eth = evmProvider();
    if (!eth) { location.href = `https://metamask.app.link/dapp/${here()}`; return; }
    [address] = await eth.request({ method: "eth_requestAccounts" });
    sign = (msg) => eth.request({ method: "personal_sign", params: [`0x${hexOf(new TextEncoder().encode(msg))}`, address] });
  } else {
    const sol = solProvider();
    if (!sol) {
      // Safari on a phone: Phantom's deeplinks, this page stays ours
      // (phantomlink.js). Connect first; then one more tap signs.
      const s = phantom.session();
      if (!s) { phantom.connect({ op: "link" }); return; }
      await phantomStep(s.publicKey);
      return;
    }
    const res = await sol.connect();
    address = (res?.publicKey ?? sol.publicKey).toString();
    sign = async (msg) => hexOf((await sol.signMessage(new TextEncoder().encode(msg), "utf8")).signature);
  }
  const { message } = await api.walletChallenge({ kind, address });
  const signature = await sign(message);
  const res = await api.walletLink({ signature });
  data.profile = res.profile;
  paint();
  load();   // the new wallet's holdings
  loadLinked();   // the token pages offer "My wallet" from now on
}

/* Phantom by deeplink (phantomlink.js). iOS opens Phantom only from a tap,
 * so the challenge is fetched first and a second tap sends it to sign. */
let phantomSign = null;   // {message} waiting for the "Sign in Phantom" tap
// The answer can land before the profile has loaded: load() paints it then.
const repaint = () => { if (data && root) paint(); };

async function phantomStep(address) {
  const { message } = await api.walletChallenge({ kind: "solana", address });
  phantomSign = { message };
  go?.("profile");    // after the boot has picked its first view
  repaint();
  window.scrollTo({ top: 0 });
}

/** Phantom's answer on the way back (main.js): true when it was ours. */
export async function phantomAnswer(ans) {
  if (ans.pending?.op !== "link") return false;
  go?.("profile");
  const err = () => root?.querySelector("[data-wallet-err]");
  try {
    if (ans.error) throw new Error(ans.error);
    if (ans.op === "connect") { await phantom.preload(); await phantomStep(ans.data.public_key); return true; }
    if (ans.op === "signMessage") {
      const res = await api.walletLink({ signature: hexOf(phantom.b58dec(ans.data.signature)) });
      phantomSign = null;
      if (data) data.profile = res.profile;
      repaint();
      load();
      loadLinked();
    }
  } catch (e) {
    phantomSign = null;
    repaint();
    const el = err();
    if (el) el.textContent = e?.message ?? "Couldn't connect Phantom.";
  }
  return true;
}

function walletsBox(p) {
  const rows = (p.wallets ?? []).map((w) => `<li>
      <span class="pf-wchain" data-kind="${escapeHtml(w.kind)}" aria-hidden="true">${w.kind === "evm" ? "◆" : "◎"}</span>
      <span class="pf-main"><b>${escapeHtml(shortAddr(w.address))}</b><small>${w.kind === "evm" ? "EVM · Robinhood, Base, Ethereum, BSC" : "Solana"} · connected</small></span>
      <button type="button" class="pf-x" data-unlink="${escapeHtml(w.kind)}:${escapeHtml(w.address)}" aria-label="Disconnect">✕</button>
    </li>`).join("");
  return `<section class="pf-wallets">
    <h3>Wallets</h3>
    ${rows ? `<ul class="pf-wl">${rows}</ul>` : `<p class="pf-none">Connect your own wallet: you sign a message to prove it's yours (free, not a transaction). We never see its keys.</p>`}
    <div class="pf-connect">
      <button type="button" data-connect="evm"><b>MetaMask / Rabby</b><small>Robinhood Chain</small></button>
      <button type="button" data-connect="solana"><b>Phantom</b><small>Solana</small></button>
    </div>
    <p class="pf-err" data-wallet-err></p>
  </section>`;
}

// A position opens its token page: the chart with the strategy's arrows and
// the wallet's own entry (and exit, once closed). Lorenzo, 30/09: "le
// posizioni aperte e chiuse se ci clicco voglio vedere quali sono stati
// graficamente".
function openAttr(chain, token, symbol, opened, closed, ret, iv, strategy) {
  if (!chain || !token) return "";
  const id = `gem:${chain}:${token}`;
  const own = [...(opened ? [{ t: opened, side: "buy", iv, strategy }] : []), ...(closed ? [{ t: closed, side: "sell", ret, iv, strategy }] : [])];
  register(id, { kind: "gem", chain, token, symbol, own });
  return ` data-open="${escapeHtml(id)}" role="button" tabindex="0"`;
}

function positions(d) {
  if (tab === "closed") {
    const rows = d.closed ?? [];
    if (!rows.length) return `<p class="pf-none">No closed positions yet. The system's trades land here when it sells.</p>`;
    const shown = showAllPos ? rows : rows.slice(0, 4);
    const morePos = rows.length > 4 ? `<button type="button" class="pf-more" data-more-pos>${showAllPos ? "Show less" : `Show all (${rows.length})`}</button>` : "";
    return `<ul class="pf-list">${shown.map((c) => `<li${openAttr(c.chain, c.token, c.symbol, c.opened, c.closed, c.ret, c.interval, c.strategy)}>
      ${logo(c.symbol, null, c.chain, c.token)}
      <span class="pf-main"><b>${escapeHtml(c.symbol)}</b><small>${hold(c.closed - c.opened)} held${c.mode === "sim" ? " · simulated" : ""}${c.why ? ` · ${escapeHtml(c.why)}` : ""}</small></span>
      <span class="pf-num"><b>${c.proceeds_usdg == null ? "—" : `$${c.proceeds_usdg.toFixed(2)}`}</b><em data-sign="${(c.ret ?? 0) >= 0 ? "up" : "down"}">${pct(c.ret)}</em></span>
    </li>`).join("")}</ul>` + morePos;
  }
  const all = (d.positions ?? []).filter((p) => chainF === "all" || p.chain === chainF);
  const hidden = all.filter((p) => p.hidden);
  const rows = showHidden ? all : all.filter((p) => !p.hidden);
  const chips = Object.entries(d.by_chain ?? {}).sort((a, b) => b[1] - a[1]);
  const chipBar = chips.length > 1 ? `<nav class="pf-chains" aria-label="Chain">${[["all", d.value], ...chips].map(([c, v]) =>
    `<button type="button" data-chainf="${c}" aria-pressed="${c === chainF}">${c === "all" ? "All" : CHAIN_NAMES[c] ?? c}<small data-chipv="${c}">${usd2(v ?? 0)}</small></button>`).join("")}</nav>` : "";
  const more = hidden.length ? `<button type="button" class="pf-hidden" data-hidden>${showHidden ? "Hide" : "Show"} hidden (${hidden.length})</button>` : "";
  if (!all.length) return chipBar + `<p class="pf-none">${d.missing?.length ? `Couldn't read ${d.missing.join(" and ")} right now.` : "Nothing held yet."}</p>`;
  const shown = showAllPos ? rows : rows.slice(0, 4);
  const morePos = rows.length > 4 ? `<button type="button" class="pf-more" data-more-pos>${showAllPos ? "Show less" : `Show all (${rows.length})`}</button>` : "";
  return chipBar + `<ul class="pf-list">${shown.map((p) => `<li${p.native ? "" : openAttr(p.chain, p.address, p.symbol, p.opened, null, null, p.interval, p.strategy)} data-hidden-row="${Boolean(p.hidden)}"${liveKey(p) ? ` data-live="${escapeHtml(liveKey(p))}"` : ""}>
    ${logo(p.symbol, p.image, p.chain, p.address)}
    <span class="pf-main"><b>${escapeHtml(p.name || p.symbol)}</b><small>${qty(p.amount)} ${escapeHtml(p.symbol)} · ${CHAIN_NAMES[p.chain] ?? p.chain}${p.source === "connected" ? " · your wallet" : ""}</small></span>
    <span class="pf-num"><b data-lv>${p.value_usd == null ? "—" : usd2(p.value_usd)}</b>${p.cost_usd ? `<em data-pnl>${pnlText(p.value_usd, p.cost_usd)}</em>` : p.price_usd ? `<small data-lp>${unitPrice(p.price_usd)}</small>` : ""}</span>
  </li>`).join("")}</ul>` + morePos + more;
}

/* ------------------------------------------------ live value
 * Lorenzo, 29/09, on pump.fun: "il saldo [...] si aggiorna in modo
 * frenetico costantemente [...] vorrei vedere anche quanto sta perdendo o
 * guadagnando". Every couple of seconds (live.js) each position is marked
 * at the latest price, the total and the change follow, digits roll, rows
 * flash green or red. The server's numbers stay the base: a position the
 * price source does not know keeps its value. */
const usd2 = (v) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const unitPrice = (v) => `$${v < 0.01 ? v.toPrecision(3) : v.toFixed(v < 1 ? 4 : 2)}`;
const pnlText = (value, cost) => (value == null || !cost ? "" : `${signed(value - cost)} · ${value >= cost ? "+" : "−"}${Math.abs((value / cost - 1) * 100).toFixed(2)}%`);
const shownPx = new Map();   // key -> price last painted, to flash only on a move
let lastTotal = null;

function applyLive() {
  const d = data;
  if (!d || !root || root.querySelector(".scrub-x:not([hidden])")) return;
  const delta = liveDelta(d.positions);
  const top = root.querySelector(".pf-top");
  if (d.value != null && top) {
    const total = d.value + delta;
    const [whole, cents] = money(total);
    roll(top.querySelector(".pf-total b"), whole);
    roll(top.querySelector(".pf-total > span"), cents);
    if (lastTotal != null && Math.abs(total - lastTotal) >= 0.005) flash(top, total > lastTotal ? "up" : "down");
    lastTotal = total;
    // the line's last point follows the live total (clamped to the chart)
    if (scrub?.rows?.length) {
      const base = scrub.rows[scrub.rows.length - 1][1];
      const dy = Math.max(-scrub.y(base) + 6, Math.min(170 - 6 - scrub.y(base), scrub.y(total) - scrub.y(base)));
      root.querySelectorAll(".pf-line .dot, .pf-line .halo").forEach((c) => { c.style.transform = `translateY(${dy.toFixed(1)}px)`; });
    }
    const ch = d.change?.[range];
    const changeEl = top.querySelector(".pf-change");
    if (ch != null && changeEl) {
      changeEl.dataset.sign = ch + delta >= 0 ? "up" : "down";
      roll(changeEl.querySelector("[data-rl]"), signed(ch + delta));
    }
  }
  // chain chips: the same re-mark as the total, so All = the big number
  root.querySelectorAll("[data-chipv]").forEach((el) => {
    const c = el.dataset.chipv;
    const base = c === "all" ? d.value : d.by_chain?.[c];
    if (base == null) return;
    roll(el, usd2(base + (c === "all" ? delta : liveDelta((d.positions ?? []).filter((p) => p.chain === c)))));
  });
  const byKey = new Map((d.positions ?? []).map((p) => [liveKey(p), p]));
  root.querySelectorAll(".pf-list li[data-live]").forEach((li) => {
    const p = byKey.get(li.dataset.live);
    const px = priceOf(li.dataset.live);
    if (!p || !px) return;
    const v = liveValue(p);
    roll(li.querySelector("[data-lv]"), usd2(v));
    const pnl = li.querySelector("[data-pnl]");
    if (pnl) { pnl.dataset.sign = v >= p.cost_usd ? "up" : "down"; roll(pnl, pnlText(v, p.cost_usd)); }
    roll(li.querySelector("[data-lp]"), unitPrice(px));
    const before = shownPx.get(li.dataset.live);
    if (before != null && before !== px) flash(li, px > before ? "up" : "down");
    shownPx.set(li.dataset.live, px);
  });
}

function paint() {
  const d = data;
  const p = d.profile;
  const [whole, cents] = d.complete === false && d.missing?.length && !d.value ? ["$—", ""] : money(d.value);
  const ch = d.change?.[range];
  const wboxOpen = root.querySelector(".pf-wbox")?.open;   // a repaint must not fold what the user opened
  root.innerHTML = `${phantomSign ? `<div class="pf-phantom"><p>Phantom is connected. One more tap: a free signature proves the wallet is yours.</p>
      <button type="button" data-phantom-sign>Sign in Phantom</button></div>` : ""}
    <header class="pf-head">
      <label class="pf-pic" title="Change picture">
        ${avatar(p, "pf-img")}
        <input type="file" accept="image/png,image/jpeg,image/webp" data-avatar hidden />
      </label>
      <div class="pf-id">
        <h2 class="pf-name">${escapeHtml(p.display_name)}</h2>
        <p class="pf-handle">@${escapeHtml(p.handle)}</p>
        ${p.bio ? `<p class="pf-bio">${escapeHtml(p.bio)}</p>` : `<button type="button" class="pf-add" data-edit>+ Add a bio</button>`}
      </div>
      <button type="button" class="pf-edit" data-edit aria-label="Edit profile">${PENCIL}</button>
      <ul class="pf-stats">
        <li><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>${hold(d.stats?.avg_hold_s)} avg. hold</li>
        <li data-trading-only><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9h12l-3-3M19 15H7l3 3"/></svg>${d.stats?.trades ?? 0} trades</li>
        <li><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5.5" width="16" height="14" rx="2.5"/><path d="M4 10h16M9 3.5v4M15 3.5v4"/></svg>Joined ${since(p.joined)}</li>
      </ul>
    </header>
    <section class="pf-value">
      <nav class="pf-wallet" aria-label="Wallet" data-trading-only>
        <button type="button" data-wallet="real" aria-pressed="${d.wallet !== "sim"}">Real wallet</button>
        <button type="button" data-wallet="sim" aria-pressed="${d.wallet === "sim"}">Simulated wallet</button>
      </nav>
      ${d.wallet === "sim" ? simNote(d) : ""}
      <div class="pf-top">
        <p class="pf-total"><i class="pf-live" aria-hidden="true"></i><b>${whole}</b><span>${cents}</span></p>
        <p class="pf-change" data-sign="${(ch ?? 0) >= 0 ? "up" : "down"}">${ch == null ? "" : `<b data-rl>${signed(ch)}</b> <span>${RANGES.find(([k]) => k === range)[1]}</span>`}</p>
      </div>
      ${d.complete === false && d.missing?.length ? `<p class="pf-since">Not read right now: ${escapeHtml(d.missing.join(", "))} — shown at the last reading, or left out.</p>` : ""}
      <div class="pf-chart">${chart(d.history?.[range])}</div>
      <nav class="pf-range" aria-label="Period">${RANGES.map(([k, l]) => `<button type="button" data-range="${k}" aria-pressed="${k === range}">${l}</button>`).join("")}</nav>
    </section>
    <section class="pf-cash">
      <span class="pf-cash-ic" aria-hidden="true">$</span>
      <p><small>Total cash</small><b>${d.cash == null ? "—" : `$${d.cash.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`}</b></p>
      <button type="button" class="pf-sq" data-sheet="receive" aria-label="Deposit"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg></button>
      <button type="button" class="pf-sq" data-go="wallet" aria-label="Swap" data-trading-only><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 9h12l-3-3M19 15H7l3 3"/></svg></button>
      <button type="button" class="pf-sq" data-wallet-menu aria-label="Wallet"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="6" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="18" cy="12" r="1.6"/></svg></button>
    </section>
    <section class="pf-pos">
      <div class="pf-pos-head">
        <h3>Positions <span>(${tab === "open" ? (d.positions ?? []).length : (d.closed ?? []).length})</span></h3>
        <nav class="pf-seg" aria-label="Positions">
          <button type="button" data-tab="open" aria-pressed="${tab === "open"}">Open${tab === "open" ? ' <i aria-hidden="true"></i>' : ""}</button>
          <button type="button" data-tab="closed" aria-pressed="${tab === "closed"}">Closed</button>
        </nav>
      </div>
      ${positions(d)}
    </section>
    <details class="pf-wbox"${wboxOpen ? " open" : ""}>
      <summary>Connected wallets (${(p.wallets ?? []).length})</summary>
      ${walletsBox(p)}
    </details>
    <dialog class="sheet pf-sheet" data-edit-sheet aria-label="Edit profile">
      <form method="dialog" class="pf-form">
        <h3>Edit profile</h3>
        <label>Name<input name="display_name" maxlength="40" value="${escapeHtml(p.display_name)}" required /></label>
        <label>Handle<span class="pf-at"><i>@</i><input name="handle" maxlength="20" pattern="[A-Za-z0-9_]{3,20}" value="${escapeHtml(p.handle)}" required /></span></label>
        <label>Bio<textarea name="bio" maxlength="160" rows="3" placeholder="What you trade, how you trade it">${escapeHtml(p.bio)}</textarea></label>
        <p class="pf-err" data-err></p>
        <div class="pf-actions"><button type="button" data-cancel>Cancel</button><button type="submit" value="save" class="pf-save">Save</button></div>
      </form>
    </dialog>`;
  delete root.dataset.loading;
  paintMe(p);
  bindScrub();
  applyLive();
}

// The last profile of each wallet mode, so a switch paints at once instead of
// waiting for the chain read. loadSeq drops answers that arrive after a newer
// switch: real -> sim -> real must not end up showing the sim wallet.
const byMode = { real: null, sim: null };
let loadSeq = 0;

async function load() {
  const mode = api.walletMode();
  const seq = ++loadSeq;
  try {
    const p = await api.getProfile();
    if (seq !== loadSeq || mode !== api.walletMode()) return;
    byMode[mode] = p;
    data = p;
    delete root.dataset.switching;
    paint();
  } catch (err) {
    if (seq !== loadSeq || mode !== api.walletMode()) return;
    if (!data) root.innerHTML = `<p class="pf-none">Couldn't load your profile: ${escapeHtml(err?.message ?? "try again")}</p>`;
  }
}

/** The switch itself: the buttons flip now, the numbers follow. */
function switchWallet(mode) {
  lastTotal = null;
  api.setWalletMode(mode);
  if (byMode[mode]) { data = byMode[mode]; paint(); }
  else {
    // Nothing cached yet: flip the buttons and dim the numbers until it lands.
    root.querySelectorAll("[data-wallet]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.wallet === mode)));
    root.dataset.switching = "";
  }
  load();
}

export function refreshProfile() {
  if (root) load();
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

export function mountProfile({ go: goTo } = {}) {
  root = document.getElementById("profile");
  if (!root) return;
  if (phantom.needed()) phantom.preload().catch(() => {});   // taps must reach Phantom synchronously
  go = goTo ?? go;
  watch({
    keys: () => (data?.positions ?? []).filter((p) => !p.hidden).map(liveKey).filter(Boolean),
    active: () => Boolean(data && root.offsetParent),
    onPrices: applyLive,
  });
  // The value and its line stay live while the profile is on screen: a new
  // reading a minute, as the header's. Not while editing or touching the line.
  setInterval(() => {
    if (document.visibilityState !== "visible" || !root.offsetParent || !data) return;
    if (root.querySelector("dialog[open]") || root.querySelector(".scrub-x:not([hidden])")) return;
    load();
  }, 60_000);
  root.addEventListener("click", (event) => {
    const wb = event.target.closest("[data-wallet]");
    if (wb) { switchWallet(wb.dataset.wallet); return; }
    if (event.target.closest("[data-sim-top]")) { import("@onchain/sim-backtest.js").then((m) => m.openSimBacktest()); return; }
    const r = event.target.closest("[data-range]");
    if (r) { range = r.dataset.range; paint(); return; }
    const cf = event.target.closest("[data-chainf]");
    if (cf) { chainF = cf.dataset.chainf; paint(); return; }
    if (event.target.closest("[data-hidden]")) { showHidden = !showHidden; paint(); return; }
    if (event.target.closest("[data-more-pos]")) { showAllPos = !showAllPos; paint(); return; }
    const t = event.target.closest("[data-tab]");
    if (t) { tab = t.dataset.tab; showAllPos = false; paint(); return; }
    const sh = event.target.closest("[data-sheet]");
    if (sh) { document.dispatchEvent(new CustomEvent("lae:sheet", { detail: sh.dataset.sheet })); return; }
    if (event.target.closest("[data-wallet-menu]")) { document.dispatchEvent(new CustomEvent("lae:wallet-menu")); return; }
    if (event.target.closest("[data-edit]")) { root.querySelector("[data-edit-sheet]")?.showModal(); return; }
    if (event.target.closest("[data-cancel]")) root.querySelector("[data-edit-sheet]")?.close();
    if (event.target.closest("[data-phantom-sign]") && phantomSign) {
      try { phantom.signMessage(phantomSign.message, { op: "link" }); } catch (e) {
        const el = root.querySelector("[data-wallet-err]");
        if (el) el.textContent = e?.message ?? "Couldn't open Phantom.";
      }
      return;
    }
    const c = event.target.closest("[data-connect]");
    if (c) {
      connect(c.dataset.connect).catch((e) => {
        const el = root.querySelector("[data-wallet-err]");
        if (el) el.textContent = e?.code === 4001 ? "You cancelled it in the wallet." : (e?.message ?? "Couldn't connect.");
      });
      return;
    }
    const u = event.target.closest("[data-unlink]");
    if (u) {
      const [kind, ...rest] = u.dataset.unlink.split(":");
      api.walletUnlink(kind, rest.join(":")).then((res) => { data.profile = res.profile; paint(); load(); loadLinked(); }, () => {});
    }
  });
  root.addEventListener("submit", async (event) => {
    event.preventDefault();
    const f = new FormData(event.target);
    const err = root.querySelector("[data-err]");
    try {
      const res = await api.updateProfile({ display_name: f.get("display_name"), handle: f.get("handle"), bio: f.get("bio") });
      data.profile = res.profile;
      root.querySelector("[data-edit-sheet]")?.close();
      paint();
    } catch (e) { err.textContent = e?.message ?? "Couldn't save."; }
  });
  root.addEventListener("change", async (event) => {
    const input = event.target.closest("[data-avatar]");
    const file = input?.files?.[0];
    if (!file) return;
    root.querySelector(".pf-pic")?.setAttribute("data-busy", "");
    try {
      const res = await api.uploadAvatar({ data_url: await readFile(file) });
      data.profile = res.profile;
      paint();
    } catch (e) {
      alert(e?.message ?? "Couldn't change the picture.");
      root.querySelector(".pf-pic")?.removeAttribute("data-busy");
    }
  });
  // Your picture in the header and the dock, before the profile is opened.
  api.getProfile().then((d) => { data = d; paint(); }, () => {});
}
