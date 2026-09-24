// Simple Mode — a guided wizard that orchestrates the SAME real backend
// endpoints the normal cockpit UI uses (/api/symbols, /api/analyze,
// /api/strategies, /api/optimize/start, /api/jobs, /api/auth/trading,
// /api/watchlist, /api/archive). No new backend endpoints. See HANDOVER.md
// for the approved prototype this follows.
(function () {
  "use strict";

  const LS_KEY = "simpleModeActive";
  const root = document.getElementById("simple-mode-root");
  // Un solo punto d'ingresso voluto: la voce nel menu "Altro" (mobile).
  // Modalita' Semplice e' pensata per il telefono; su desktop non deve
  // esistere ne' essere raggiungibile. Il primo id (bottone nel vecchio
  // .toolbar desktop) e' stato rimosso da index.html — resta cercato qui
  // solo per compatibilita', .filter(Boolean) lo scarta se assente.
  const toggleBtns = [
    document.getElementById("simple-mode-toggle-btn"),
    document.getElementById("altro-simple-mode-item"),
  ].filter(Boolean);
  if (!root || !toggleBtns.length) return;

  // Trading exchanges Simple Mode offers for live execution — the full set
  // backend/app/auth.py's set_trading_credential() accepts
  // (binance/mexc/kucoin/kraken/coinbase/bitstamp), not just the first
  // three: seed-0064 asked explicitly for all of them here, same as the
  // desktop "Trading automatico" panel already offers. Spot only: futures
  // leverage is capped at 1x server-wide, so the wizard never touches
  // /api/auth/futures.
  const TRADING_EXCHANGES = [
    { id: "binance", name: "Binance", color: "#f0b90b", letter: "B",
      steps: ["Log in to Binance → API Management in your profile.", "Create a new key and enable only “Reading” and “Spot Trading”.", "Copy the key and secret key below — don't share them anywhere else."] },
    { id: "mexc", name: "MEXC", color: "#1b73e8", letter: "M",
      steps: ["Log in to MEXC → API Management from your profile.", "Create a key with read and spot trading permissions, never withdrawal.", "Copy the key and secret key below."] },
    { id: "kucoin", name: "KuCoin", color: "#24ae8f", letter: "K",
      steps: ["Go to KuCoin → API Management from your profile.", "Create a key with “General” and “Trade” permissions, never “Withdraw”.", "Paste the key and secret here: they stay encrypted on our system."] },
    { id: "kraken", name: "Kraken", color: "#5741d9", letter: "K",
      steps: ["On Kraken go to Settings → Security → API Keys.", "Create a key with “Query Funds” and “Create & Modify Orders” enabled.", "Enter both values below to complete the connection."] },
    { id: "coinbase", name: "Coinbase", color: "#0052ff", letter: "C",
      steps: ["On Coinbase Advanced Trade go to Settings → API.", "Create a key with “View” and “Trade” permissions, never “Transfer”.", "Paste the key and secret below."] },
    { id: "bitstamp", name: "Bitstamp", color: "#2e3a59", letter: "B",
      steps: ["On Bitstamp go to Account → Security → API Access.", "Create a key with balance-read and order-placement permissions.", "Paste the key and secret below."] },
  ];

  // Timeframe scelto una volta sola allo step Strategia e riusato ovunque
  // dopo — backtest, ottimizzazione, export, watchlist. Prima ogni passo
  // aveva il suo timeframe (il backtest usava il default del server, il
  // grafico "1d" fisso, l'ottimizzazione "1d" fisso, la watchlist "1d"
  // fisso): esattamente il tipo di disallineamento fra cio' che si vede e
  // cio' che gira davvero di cui l'utente si e' lamentato piu' volte
  // (seed-0007, backtest/ottimizzazione su feed diversi senza dirlo).
  const SIMPLE_INTERVALS = [
    { id: "15m", label: "15m" },
    { id: "1h", label: "1h" },
    { id: "4h", label: "4h" },
    { id: "1d", label: "1d" },
    { id: "1w", label: "1w" },
  ];
  function tfLabel(id) {
    return (SIMPLE_INTERVALS.find((t) => t.id === id) || {}).label || id;
  }

  const state = {
    step: 0,
    query: "",
    ticker: null, // {symbol, name, exchange, asset_type, provider}
    lastCandles: null,
    archiveEntry: null, // chosen "già pronto" archive item, or null
    archiveList: [],
    strategies: [],
    strategy: null, // strategy name string
    interval: "1d", // timeframe su cui la strategia lavora — vedi sopra
    analysis: null, // last /api/analyze response used for the backtest step
    optMode: "stable",
    optJobId: null,
    optPollTimer: null,
    optResult: null, // {params, backtest:{...}, overfitted}
    tradingExchange: TRADING_EXCHANGES[0].id,
    connTested: false, // esito dell'ultimo test di connessione in questa sessione del wizard
    watchlistEntry: null,
  };

  function h(strings, ...vals) {
    return strings.reduce((acc, s, i) => acc + s + (vals[i] !== undefined ? vals[i] : ""), "");
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  // Strategy names are API keys; users read a neutral label (same mapping
  // as strategyLabel() in app.js, kept local so load order doesn't matter).
  function stratLabel(name) {
    if (name == null) return "";
    const s = String(name);
    const i = s.indexOf(" · ");
    const head = i < 0 ? s : s.slice(0, i);
    const tail = i < 0 ? "" : s.slice(i);
    let label = head;
    if (/^Lorenzo HF\b/.test(head)) label = "Trend HF";
    else if (/^Lorenzo\b/.test(head)) label = "Trend EMA";
    else if (/^Gabriele 2\b/.test(head)) label = "RSI+BB Long/Short";
    else if (/^Gabriele\b/.test(head)) label = "RSI+BB Reversal";
    else if (/^Accumulo\b/.test(head)) label = "Accumulation / Distribution";
    return label + tail;
  }
  function fmtPct(v, digits) {
    if (v == null || Number.isNaN(v)) return "—";
    digits = digits == null ? 1 : digits;
    return (v >= 0 ? "+" : "") + v.toFixed(digits) + "%";
  }
  function fmtNum(v) {
    if (v == null) return "—";
    return Number(v).toLocaleString("en-US", { maximumFractionDigits: v >= 1000 ? 0 : 2 });
  }

  async function api(path, opts) {
    const resp = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    if (resp.status === 401) {
      const err = new Error("auth");
      err.needsAuth = true;
      throw err;
    }
    let body = null;
    try { body = await resp.json(); } catch (_e) { /* no body */ }
    if (!resp.ok) {
      const detail = (body && body.detail) ? body.detail : `Error ${resp.status}`;
      throw new Error(detail);
    }
    return body;
  }

  function requireLogin() {
    const btn = document.getElementById("auth-toggle-btn");
    if (btn) btn.click();
  }

  // ---- CSS injected once, scoped under #simple-mode-root -------------------
  function injectStyle() {
    if (document.getElementById("simple-mode-style")) return;
    const style = document.createElement("style");
    style.id = "simple-mode-style";
    style.textContent = `
#simple-mode-root{--sm-accent:var(--accent,#0b8f68);--sm-bull:#0b8f68;--sm-bear:#cf3a2a;--sm-warn:#a86a06;
  --sm-bg:var(--bg,#f4f6f5);--sm-panel:var(--panel-bg,#fff);--sm-raised:#eef1ef;--sm-sunk:#e6eae7;
  --sm-border:#dfe4e1;--sm-border-strong:#c9d1cc;--sm-ink:var(--text,#0e1513);--sm-ink-dim:#54615c;--sm-ink-faint:#84918c;
  position:fixed;inset:0;z-index:500;overflow-y:auto;background:var(--sm-bg);font-family:-apple-system,BlinkMacSystemFont,"Instrument Sans",sans-serif;}
@media (prefers-color-scheme:dark){#simple-mode-root:not([data-sm-theme="light"]){--sm-bg:#080b0c;--sm-panel:#0f1416;--sm-raised:#141b1e;--sm-sunk:#1a2327;--sm-border:#1e282c;--sm-border-strong:#2c393e;--sm-ink:#e6eeec;--sm-ink-dim:#93a49f;--sm-ink-faint:#63736f;--sm-accent:#4fd8a8;--sm-bull:#3ecf8e;--sm-bear:#ff6b5e;}}
#simple-mode-root[data-sm-theme="dark"]{--sm-bg:#080b0c;--sm-panel:#0f1416;--sm-raised:#141b1e;--sm-sunk:#1a2327;--sm-border:#1e282c;--sm-border-strong:#2c393e;--sm-ink:#e6eeec;--sm-ink-dim:#93a49f;--sm-ink-faint:#63736f;--sm-accent:#4fd8a8;--sm-bull:#3ecf8e;--sm-bear:#ff6b5e;}
#simple-mode-root *{box-sizing:border-box;}
#simple-mode-root.hidden{display:none;}
#simple-mode-root .sm-app{max-width:460px;margin:0 auto;min-height:100vh;display:flex;flex-direction:column;position:relative;color:var(--sm-ink);}
@media (min-width:860px){#simple-mode-root .sm-app{max-width:640px;}}
#simple-mode-root .sm-topbar{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--sm-border);position:sticky;top:0;background:var(--sm-bg);z-index:20;}
#simple-mode-root .sm-brand{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13px;white-space:nowrap;}
#simple-mode-root .sm-brand .dot{width:8px;height:8px;border-radius:50%;background:var(--sm-accent);}
#simple-mode-root .sm-progress-track{flex:1;height:3px;background:var(--sm-sunk);border-radius:2px;overflow:hidden;}
#simple-mode-root .sm-progress-fill{height:100%;width:100%;background:var(--sm-accent);border-radius:2px;transition:transform .3s;transform:scaleX(0);transform-origin:left;}
#simple-mode-root .sm-stepcount{font-family:monospace;font-size:11px;color:var(--sm-ink-faint);white-space:nowrap;}
#simple-mode-root .sm-exit{border:1px solid var(--sm-border);background:var(--sm-panel);color:var(--sm-ink-dim);border-radius:20px;padding:5px 10px;font-size:11px;cursor:pointer;white-space:nowrap;}
#simple-mode-root main.sm-step{flex:1;padding:20px 16px 110px;display:none;flex-direction:column;}
#simple-mode-root main.sm-step.active{display:flex;}
#simple-mode-root .sm-eyebrow{font-family:monospace;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--sm-ink-faint);margin-bottom:8px;}
#simple-mode-root .sm-title{font-size:22px;font-weight:600;letter-spacing:-.01em;margin:0 0 8px;}
#simple-mode-root .sm-sub{font-size:14px;color:var(--sm-ink-dim);line-height:1.5;margin:0 0 18px;}
#simple-mode-root .sm-ctabar{position:fixed;left:0;right:0;bottom:0;z-index:30;background:linear-gradient(to top,var(--sm-bg) 65%,transparent);padding:16px 16px 18px;}
#simple-mode-root .sm-ctabar-inner{max-width:460px;margin:0 auto;display:flex;gap:10px;}
@media (min-width:860px){#simple-mode-root .sm-ctabar-inner{max-width:640px;}}
#simple-mode-root .sm-btn{font-size:15px;font-weight:600;border-radius:8px;padding:14px 16px;border:1px solid var(--sm-border-strong);background:var(--sm-panel);color:var(--sm-ink);cursor:pointer;flex:1;}
#simple-mode-root .sm-btn:disabled{opacity:.4;cursor:not-allowed;}
#simple-mode-root .sm-btn.primary{background:var(--sm-accent);border-color:var(--sm-accent);color:#04140e;}
#simple-mode-root .sm-btn.ghost{flex:0 0 auto;background:transparent;}
#simple-mode-root .sm-search-box{display:flex;align-items:center;gap:8px;background:var(--sm-panel);border:1px solid var(--sm-border);border-radius:8px;padding:12px 14px;margin-bottom:12px;}
#simple-mode-root .sm-search-box input{border:none;outline:none;background:transparent;color:var(--sm-ink);font-size:16px;width:100%;}
#simple-mode-root .sm-list{display:flex;flex-direction:column;gap:6px;margin-bottom:16px;}
#simple-mode-root .sm-row{display:flex;align-items:center;justify-content:space-between;padding:11px 14px;border:1px solid var(--sm-border);border-radius:8px;background:var(--sm-panel);cursor:pointer;transition:transform .1s,background .15s,border-color .15s;}
#simple-mode-root .sm-row:active{transform:scale(0.97);background:var(--sm-raised);}
#simple-mode-root .sm-row.active{border-color:var(--sm-accent);box-shadow:0 0 0 1px var(--sm-accent) inset;background:var(--sm-raised);}
#simple-mode-root .sm-row-check{display:none;color:var(--sm-accent);font-weight:700;font-size:16px;}
#simple-mode-root .sm-row.active:not(.loading) .sm-row-check{display:inline-block;}
#simple-mode-root .sm-row-spinner{display:none;width:15px;height:15px;border-radius:50%;border:2px solid var(--sm-border-strong);border-top-color:var(--sm-accent);animation:sm-spin .6s linear infinite;flex:0 0 auto;}
#simple-mode-root .sm-row.loading .sm-row-spinner{display:inline-block;}
@keyframes sm-spin{to{transform:rotate(360deg);}}
#simple-mode-root .sm-row .sym{font-weight:600;font-size:14px;}
#simple-mode-root .sm-row .name{color:var(--sm-ink-faint);font-size:12px;}
#simple-mode-root .sm-card{background:var(--sm-panel);border:1px solid var(--sm-border);border-radius:8px;padding:14px;margin-bottom:14px;}
#simple-mode-root .sm-price{font-size:28px;font-style:italic;letter-spacing:-.01em;font-family:Georgia,serif;}
#simple-mode-root .up{color:var(--sm-bull);} #simple-mode-root .down{color:var(--sm-bear);}
#simple-mode-root .sm-strat-grid,#simple-mode-root .sm-opt-grid,#simple-mode-root .sm-archive-grid{display:flex;flex-direction:column;gap:10px;margin-bottom:16px;}
#simple-mode-root .sm-strat-card,#simple-mode-root .sm-opt-card,#simple-mode-root .sm-archive-card{border:1px solid var(--sm-border);background:var(--sm-panel);border-radius:8px;padding:14px;cursor:pointer;}
#simple-mode-root .sm-strat-card.active,#simple-mode-root .sm-opt-card.active{border-color:var(--sm-accent);box-shadow:0 0 0 1px var(--sm-accent) inset;}
#simple-mode-root .sm-strat-name{font-weight:600;font-size:15px;}
#simple-mode-root .sm-strat-tag{font-family:monospace;font-size:10px;text-transform:uppercase;color:var(--sm-ink-faint);background:var(--sm-sunk);padding:2px 7px;border-radius:20px;margin-left:8px;}
#simple-mode-root .sm-strat-desc{font-size:13px;color:var(--sm-ink-dim);line-height:1.45;margin-top:6px;}
#simple-mode-root .sm-summary{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;}
#simple-mode-root .sm-stat{background:var(--sm-raised);border-radius:8px;padding:10px;text-align:center;}
#simple-mode-root .sm-stat .lbl{font-family:monospace;font-size:10px;text-transform:uppercase;color:var(--sm-ink-faint);margin-bottom:4px;}
#simple-mode-root .sm-stat .val{font-size:18px;font-style:italic;font-family:Georgia,serif;}
#simple-mode-root .sm-legend{display:flex;gap:14px;justify-content:center;margin-top:8px;font-size:11px;color:var(--sm-ink-dim);}
#simple-mode-root .sm-legend i{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:5px;}
#simple-mode-root .sm-exch-grid{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px;}
#simple-mode-root .sm-exch-opt{flex:1 1 28%;min-width:80px;border:1px solid var(--sm-border);background:var(--sm-panel);border-radius:8px;padding:12px 6px;text-align:center;cursor:pointer;}
#simple-mode-root .sm-exch-opt.active{border-color:var(--sm-accent);box-shadow:0 0 0 1px var(--sm-accent) inset;}
#simple-mode-root .sm-exch-badge{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;color:#0e1513;margin:0 auto 6px;}
#simple-mode-root .sm-field{margin-bottom:10px;}
#simple-mode-root .sm-field label{display:block;font-size:11px;font-family:monospace;color:var(--sm-ink-faint);margin-bottom:5px;text-transform:uppercase;}
#simple-mode-root .sm-field input{width:100%;padding:11px 12px;border:1px solid var(--sm-border);border-radius:8px;background:var(--sm-raised);color:var(--sm-ink);font-family:monospace;font-size:13px;}
#simple-mode-root .sm-exch-steps{background:var(--sm-panel);border:1px solid var(--sm-border);border-radius:8px;padding:12px 14px;margin-bottom:14px;}
#simple-mode-root .sm-exch-steps ol{margin:0;padding-left:18px;font-size:13px;color:var(--sm-ink-dim);line-height:1.6;}
#simple-mode-root .sm-review-card{background:var(--sm-panel);border:1px solid var(--sm-border);border-radius:8px;padding:2px 14px;margin-bottom:14px;}
#simple-mode-root .sm-review-row{display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--sm-border);font-size:13px;}
#simple-mode-root .sm-review-row:last-child{border-bottom:none;}
#simple-mode-root .sm-review-row .k{color:var(--sm-ink-faint);}
#simple-mode-root .sm-review-row .v{font-weight:600;font-family:monospace;}
#simple-mode-root .sm-explain{background:var(--sm-raised);border-radius:8px;padding:12px;font-size:12.5px;color:var(--sm-ink-dim);line-height:1.6;margin-bottom:10px;}
#simple-mode-root .sm-error{background:rgba(207,58,42,.12);border:1px solid var(--sm-bear);color:var(--sm-bear);border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:12px;}
#simple-mode-root .sm-pulse-wrap{display:flex;flex-direction:column;align-items:center;text-align:center;padding:26px 10px 10px;}
#simple-mode-root .sm-pulse{width:60px;height:60px;border-radius:50%;background:rgba(11,143,104,.14);display:flex;align-items:center;justify-content:center;margin-bottom:18px;}
#simple-mode-root .sm-pulse .core{width:13px;height:13px;border-radius:50%;background:var(--sm-accent);}
#simple-mode-root .sm-status-card{width:100%;background:var(--sm-panel);border:1px solid var(--sm-border);border-radius:8px;padding:2px 14px;margin-top:18px;}
#simple-mode-root .sm-status-row{display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid var(--sm-border);font-size:13px;}
#simple-mode-root .sm-status-row:last-child{border-bottom:none;}
#simple-mode-root .sm-live-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--sm-bull);margin-right:6px;}
#simple-mode-root .sm-welcome{flex:1;display:flex;flex-direction:column;justify-content:center;padding:0 16px 100px;}
#simple-mode-root .sm-welcome .kicker{font-family:monospace;font-size:12px;color:var(--sm-accent);text-transform:uppercase;margin-bottom:12px;}
#simple-mode-root .sm-welcome h1{font-size:28px;line-height:1.2;margin-bottom:12px;}
#simple-mode-root .sm-welcome p{font-size:14px;color:var(--sm-ink-dim);line-height:1.6;margin-bottom:18px;}
#simple-mode-root .sm-feat{display:flex;gap:8px;font-size:13px;color:var(--sm-ink-dim);margin-bottom:8px;}
#simple-mode-root .sm-feat b{color:var(--sm-ink);}
#simple-mode-root .sm-badge{display:inline-flex;align-items:center;gap:5px;font-family:monospace;font-size:10px;text-transform:uppercase;color:var(--sm-accent);background:rgba(11,143,104,.12);padding:3px 8px;border-radius:20px;margin-bottom:8px;}
#simple-mode-root .sm-muted{color:var(--sm-ink-faint);font-size:12px;}
#simple-mode-root .sm-deploy-info{text-align:center;font-family:monospace;font-size:10.5px;color:var(--sm-ink-faint);padding:6px 0 90px;}
/* La finestra reale di avanzamento ottimizzazione (#opt-progress, vedi
   window.LiquidityOptBridge in app.js) vive fuori da #simple-mode-root e
   normalmente sta sotto (z-index:60) — qui, mentre la Modalita' Semplice
   e' aperta (z-index:500), deve stare sopra invece che sparire dietro. */
body:has(#simple-mode-root:not(.hidden)) #opt-progress,
body:has(#simple-mode-root:not(.hidden)) #opt-pill{z-index:600;}
#simple-mode-root .sm-tf-row{display:flex;gap:8px;flex-wrap:wrap;}
#simple-mode-root .sm-tf-btn{border:1px solid var(--sm-border);background:var(--sm-panel);color:var(--sm-ink);border-radius:20px;padding:7px 14px;font-size:13px;font-weight:600;cursor:pointer;}
#simple-mode-root .sm-tf-btn.active{border-color:var(--sm-accent);background:var(--sm-accent);color:#04140e;}
#simple-mode-root .sm-real-chart-mount{height:440px;border:1px solid var(--sm-border);border-radius:8px;overflow:hidden;margin-bottom:10px;background:var(--sm-panel);display:flex;flex-direction:column;}
#simple-mode-root .sm-trades-panel{max-height:260px;overflow-y:auto;border:1px solid var(--sm-border);border-radius:8px;margin-bottom:10px;background:var(--sm-panel);}
#simple-mode-root .sm-trade-row{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:9px 12px;border-bottom:1px solid var(--sm-border);font-size:12.5px;transition:background .3s;}
#simple-mode-root .sm-trade-row:last-child{border-bottom:none;}
#simple-mode-root .sm-trade-row .dir{font-weight:600;flex:0 0 auto;}
#simple-mode-root .sm-trade-row .dates{color:var(--sm-ink-dim);font-family:monospace;font-size:11px;flex:1;text-align:center;}
#simple-mode-root .sm-trade-row .pnl{font-weight:600;font-family:monospace;flex:0 0 auto;}
#simple-mode-root .sm-trade-row.sm-trade-flash{background:rgba(11,143,104,.18);}
#simple-mode-root .sm-real-chart-mount .chart-area{flex:1;min-height:0;}
`;
    document.head.appendChild(style);
  }

  // ---- Skeleton shell --------------------------------------------------
  function renderShell() {
    root.innerHTML = `
<div class="sm-app">
  <div class="sm-topbar" id="sm-topbar" style="display:none;">
    <div class="sm-brand"><span class="dot"></span>Liquidity Engine</div>
    <div class="sm-progress-track"><div class="sm-progress-fill" id="sm-progfill"></div></div>
    <div class="sm-stepcount" id="sm-stepcount">1/6</div>
    <button type="button" class="sm-exit" id="sm-exit-btn">Exit</button>
  </div>
  <div id="sm-steps"></div>
  <div class="sm-deploy-info" id="sm-deploy-info"></div>
</div>`;
    document.getElementById("sm-exit-btn").addEventListener("click", () => setActive(false));
    loadDeployInfo();
  }

  // Stessa domanda del piede della sidebar desktop ("quando e' stato
  // distribuito davvero"), qui perche' Modalita' Semplice non condivide
  // quel DOM — vedi renderDeployInfo() in app.js.
  async function loadDeployInfo() {
    const el2 = document.getElementById("sm-deploy-info");
    if (!el2) return;
    try {
      const resp = await fetch("/api/version", { cache: "no-store" });
      if (!resp.ok) return;
      const { deployed_at } = await resp.json();
      if (!deployed_at) return;
      const stamp = new Date(deployed_at * 1000).toLocaleString("en-US", {
        day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit",
      });
      el2.textContent = `Updated ${stamp}`;
    } catch (_e) { /* offline: non mostrare nulla piuttosto che un dato falso */ }
  }

  const TOTAL_STEPS = 6;

  function setTopbar(stepLabel, frac) {
    const bar = document.getElementById("sm-topbar");
    if (frac == null) { bar.style.display = "none"; return; }
    bar.style.display = "flex";
    document.getElementById("sm-progfill").style.transform = `scaleX(${frac})`;
    document.getElementById("sm-stepcount").textContent = stepLabel;
  }

  // ------------------------------------------------------------------
  // STEP 0 — welcome
  // ------------------------------------------------------------------
  function stepWelcome() {
    setTopbar("", null);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-welcome">
    <div class="kicker">Simple Mode</div>
    <h1>Find a ticker,<br>pick a strategy,<br>let the algorithm work.</h1>
    <p>The same Liquidity Engine, without the technical panels. Every step of this guide calls the same real APIs as the regular mode.</p>
    <div class="sm-feat"><b>1.</b> Search the ticker and watch the live chart.</div>
    <div class="sm-feat"><b>2.</b> Pick one of the available strategies.</div>
    <div class="sm-feat"><b>3.</b> Check entries and exits on the real backtest.</div>
    <div class="sm-feat"><b>4.</b> Optimize — Quick or Stable.</div>
    <div class="sm-feat"><b>5.</b> Connect the exchange and activate with one tap.</div>
  </div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner"><button class="sm-btn primary" id="sm-start-btn">Get started</button></div></div>
</main>`;
    document.getElementById("sm-start-btn").addEventListener("click", () => go(1));
  }

  // ------------------------------------------------------------------
  // STEP 1 — ticker search (real /api/symbols)
  // ------------------------------------------------------------------
  let searchDebounce = null;
  function stepTicker() {
    setTopbar("1/" + TOTAL_STEPS, 1 / TOTAL_STEPS);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Step 1 of ${TOTAL_STEPS}</div>
  <h1 class="sm-title">What do you want to track?</h1>
  <p class="sm-sub">Search a ticker — stocks or crypto. Same search as the regular mode.</p>
  <div class="sm-search-box"><input id="sm-ticker-input" placeholder="BTC, ETH, AAPL…" autocomplete="off"></div>
  <div id="sm-ticker-error"></div>
  <div class="sm-list" id="sm-ticker-list"></div>
  <div id="sm-chart-card"></div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-continue-btn" disabled>Continue</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(0));
    document.getElementById("sm-continue-btn").addEventListener("click", onFromTicker);
    const input = document.getElementById("sm-ticker-input");
    input.value = state.query;
    input.addEventListener("input", (e) => {
      state.query = e.target.value;
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(() => searchTickers(state.query), 250);
    });
    if (state.ticker) {
      renderChartCard();
      document.getElementById("sm-continue-btn").disabled = false;
    }
    if (state.query) searchTickers(state.query);
  }

  async function searchTickers(query) {
    const list = document.getElementById("sm-ticker-list");
    if (!query || query.trim().length < 1) { list.innerHTML = ""; return; }
    try {
      const items = await api(`/api/symbols?query=${encodeURIComponent(query.trim())}`);
      list.innerHTML = "";
      (items || []).slice(0, 12).forEach((item) => {
        const row = document.createElement("div");
        row.className = "sm-row" + (state.ticker && state.ticker.symbol === item.symbol && state.ticker.provider === item.provider ? " active" : "");
        row.innerHTML = `<div><div class="sym">${esc(item.symbol)}</div><div class="name">${esc(item.name)} · ${esc(item.exchange)}</div></div><span class="sm-row-check">✓</span><span class="sm-row-spinner"></span>`;
        // Il tocco deve rispondere SUBITO, non solo quando il backtest
        // reale (qualche secondo, rete vera) finisce e la lista viene
        // ridisegnata — a l'utente sembrava che il click non avesse fatto
        // niente. Evidenzio la riga e mostro uno spinner al volo, prima
        // ancora di chiamare selectTicker().
        row.addEventListener("click", () => {
          list.querySelectorAll(".sm-row").forEach((r) => r.classList.remove("active", "loading"));
          row.classList.add("active", "loading");
          selectTicker(item);
        });
        list.appendChild(row);
      });
      if (!items || !items.length) list.innerHTML = `<div class="sm-muted">No results.</div>`;
    } catch (e) {
      list.innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
    }
  }

  function tickerExchangeForAnalyze(item) {
    // /api/analyze's `exchange` query only matters for asset_type=crypto and
    // only accepts mexc/binance/kucoin (the *data* providers) — unrelated to
    // which exchange the user later connects for live execution.
    if (item.asset_type !== "crypto") return "mexc";
    return ["mexc", "binance", "kucoin"].includes(item.provider) ? item.provider : "mexc";
  }

  async function selectTicker(item) {
    state.ticker = {
      symbol: item.symbol,
      name: item.name,
      // Serve solo per far combaciare la riga giusta quando la lista viene
      // ridisegnata (vedi il confronto in searchTickers) — prima non era
      // mai salvato, quindi quel confronto falliva sempre e la riga
      // scelta perdeva l'evidenziazione non appena il backtest reale
      // finiva e la lista si ridisegnava.
      provider: item.provider,
      asset_type: item.asset_type,
      exchange: tickerExchangeForAnalyze(item),
    };
    state.archiveEntry = null;
    document.getElementById("sm-continue-btn").disabled = true;
    document.getElementById("sm-chart-card").innerHTML = `<div class="sm-card sm-muted">Loading chart…</div>`;
    try {
      const strategies = await ensureStrategies();
      const defaultStrategy = strategies[0];
      const analysis = await api(`/api/analyze?symbol=${encodeURIComponent(item.symbol)}&asset_type=${item.asset_type}&exchange=${state.ticker.exchange}&strategy_name=${encodeURIComponent(defaultStrategy)}`);
      state.lastCandles = (analysis.timeframes.ltf && analysis.timeframes.ltf.candles) || [];
      renderChartCard();
      document.getElementById("sm-continue-btn").disabled = false;
    } catch (e) {
      document.getElementById("sm-chart-card").innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
    }
    searchTickers(state.query);
  }

  function renderChartCard() {
    const card = document.getElementById("sm-chart-card");
    if (!card || !state.ticker) return;
    const candles = state.lastCandles || [];
    const last = candles[candles.length - 1];
    const first = candles.length > 24 ? candles[candles.length - 24] : candles[0];
    const price = last ? last.close : null;
    const chg = last && first ? ((last.close - first.close) / first.close) * 100 : null;
    card.innerHTML = `
<div class="sm-card">
  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;">
    <div><div class="sm-eyebrow" style="margin-bottom:2px;">${esc(state.ticker.symbol)}</div><div class="sm-price">${price != null ? fmtNum(price) : "—"}</div></div>
    <div class="${chg != null && chg >= 0 ? "up" : "down"}" style="font-family:monospace;font-size:12px;">${chg != null ? fmtPct(chg) : "—"}</div>
  </div>
  <svg viewBox="0 0 400 130" width="100%" height="130" preserveAspectRatio="none" id="sm-mini-chart"></svg>
</div>`;
    drawSparkline(document.getElementById("sm-mini-chart"), candles.map((c) => c.close));
  }

  function drawSparkline(svg, closes) {
    if (!svg || !closes.length) return;
    const w = 400, h = 130, pad = 6;
    const min = Math.min(...closes), max = Math.max(...closes);
    const x = (i) => pad + i * ((w - 2 * pad) / Math.max(1, closes.length - 1));
    const y = (v) => h - pad - ((v - min) / (max - min || 1)) * (h - 2 * pad);
    let path = "";
    closes.forEach((v, i) => { path += (i === 0 ? "M" : "L") + x(i) + " " + y(v) + " "; });
    const up = closes[closes.length - 1] >= closes[0];
    const col = up ? "var(--sm-bull)" : "var(--sm-bear)";
    svg.innerHTML = `<path d="${path}" fill="none" stroke="${col}" stroke-width="2"/>`;
  }

  async function onFromTicker() {
    try {
      const items = await api("/api/archive");
      const norm = (s) => (s || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
      const matches = (items.items || []).filter((it) =>
        norm(it.symbol) === norm(state.ticker.symbol) && it.state === "pronto"
      );
      state.archiveList = matches;
      if (matches.length) go(1.5); else go(2);
    } catch (e) {
      if (e.needsAuth) { requireLogin(); return; }
      // Archive lookup failing shouldn't block the wizard — fall through.
      go(2);
    }
  }

  // ------------------------------------------------------------------
  // STEP 1.5 — archive (already-vetted strategies for this ticker)
  // ------------------------------------------------------------------
  function stepArchive() {
    setTopbar("Archive", 1.2 / TOTAL_STEPS);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Already in your archive</div>
  <h1 class="sm-title">You've already worked on ${esc(state.ticker.symbol)}</h1>
  <p class="sm-sub">These configurations have already been optimized and measured on this ticker (real data from /api/archive). Pick one up as-is, or set up a new one from scratch.</p>
  <div class="sm-archive-grid" id="sm-archive-grid"></div>
  <button class="sm-btn ghost" style="margin-bottom:90px;" id="sm-archive-fresh">Test a new strategy instead</button>
  <div class="sm-ctabar"><div class="sm-ctabar-inner"><button class="sm-btn ghost" id="sm-back-btn">Back</button></div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(1));
    document.getElementById("sm-archive-fresh").addEventListener("click", () => { state.archiveEntry = null; go(2); });
    const grid = document.getElementById("sm-archive-grid");
    state.archiveList.forEach((entry) => {
      const m = entry.metrics || {};
      const retUp = (m.total_return_pct || 0) >= 0;
      // created_at e' epoca in secondi (vedi archive.py), non una stringa
      // ISO: uno .slice() qui interrompeva silenziosamente il rendering
      // dell'intera lista al primo elemento con voci in archivio.
      const when = entry.created_at ? new Date(entry.created_at * 1000).toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "2-digit" }) : "—";
      const params = Object.entries(entry.params || {}).map(([k, v]) => `${esc(k)}=${esc(String(v))}`).join(" · ");
      const card = document.createElement("div");
      card.className = "sm-archive-card";
      card.innerHTML = `
        <div style="display:flex;justify-content:space-between;"><b>${esc(stratLabel(entry.strategy_name))}</b><span class="sm-muted">${esc(when)}</span></div>
        <div class="sm-summary" style="margin-top:10px;">
          <div class="sm-stat"><div class="lbl">Return</div><div class="val ${retUp ? "up" : "down"}">${fmtPct(m.total_return_pct)}</div></div>
          <div class="sm-stat"><div class="lbl">Drawdown</div><div class="val down">${fmtPct(m.max_drawdown_pct)}</div></div>
          <div class="sm-stat"><div class="lbl">Trades</div><div class="val">${m.num_trades != null ? m.num_trades : "—"}</div></div>
        </div>
        <div class="sm-muted" style="margin-top:6px;">Profit factor ${fmtNum(m.profit_factor)} · ${m.candles != null ? m.candles + " candles · " : ""}${esc(entry.interval || "")}</div>
        ${params ? `<div class="sm-muted" style="margin-top:4px;">${params}</div>` : ""}
        <div style="display:flex;gap:8px;margin-top:10px;">
          <button class="sm-btn primary" data-act="use" style="flex:1;">Use this</button>
          <button class="sm-btn ghost" data-act="export" style="flex:0 0 auto;">⤓ Excel</button>
        </div>`;
      card.querySelector('[data-act="use"]').addEventListener("click", () => {
        state.archiveEntry = entry;
        state.strategy = entry.strategy_name;
        go(3);
      });
      card.querySelector('[data-act="export"]').addEventListener("click", () => exportArchiveEntryToExcel(entry));
      grid.appendChild(card);
    });
  }

  // Stesso meccanismo del pulsante "⤓ Excel" sul sito: il server ririsulta
  // il backtest dai parametri della voce (mai da numeri gia' in pagina) e
  // costruisce il file — vedi /api/export/backtest.xlsx in main.py.
  function exportArchiveEntryToExcel(entry) {
    const params = new URLSearchParams();
    params.set("symbol", entry.symbol);
    params.set("asset_type", entry.asset_type);
    params.set("exchange", entry.exchange);
    params.set("ltf_interval", entry.interval);
    params.set("strategy_name", entry.strategy_name);
    Object.entries(entry.params || {}).forEach(([k, v]) => params.set(k, v));
    Object.entries(entry.backtest_params || {}).forEach(([k, v]) => params.set(k, v));
    const link = document.createElement("a");
    link.href = `/api/export/backtest.xlsx?${params.toString()}`;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  // ------------------------------------------------------------------
  // STEP 2 — strategy (real /api/strategies)
  // ------------------------------------------------------------------
  const STRAT_DESC = [
    { match: /lorenzo/i, tag: "Trend", desc: "EMA crossover on the derivative of a moving average — rides the continuation of the dominant trend." },
    { match: /gabriele/i, tag: "Mean-reversion", desc: "RSI + Bollinger Band — looks for price extremes and trades the snap-back." },
    { match: /accumulo|distribuzione/i, tag: "Counter-trend", desc: "RSI divergences on swings — enters against the move at the extremes." },
  ];
  function describeStrategy(name) {
    const hit = STRAT_DESC.find((d) => d.match.test(name));
    return hit || { tag: "Strategy", desc: "Proprietary logic of the analysis engine." };
  }

  let strategiesCache = null;
  async function ensureStrategies() {
    if (strategiesCache) return strategiesCache;
    strategiesCache = await api("/api/strategies");
    return strategiesCache;
  }

  async function stepStrategy() {
    setTopbar("2/" + TOTAL_STEPS, 2 / TOTAL_STEPS);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Step 2 of ${TOTAL_STEPS}</div>
  <h1 class="sm-title">Pick the strategy</h1>
  <p class="sm-sub">Each strategy reads <b>${esc(state.ticker.symbol)}</b> differently. You'll see it at work on the real chart in the next step.</p>
  <div class="sm-eyebrow" style="margin-top:4px;">Which timeframe should it trade?</div>
  <div class="sm-tf-row" id="sm-tf-row"></div>
  <div class="sm-strat-grid" id="sm-strat-grid" style="margin-top:14px;"><div class="sm-muted">Loading strategies…</div></div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-to-backtest-btn" disabled>See the backtest</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(1));
    document.getElementById("sm-to-backtest-btn").addEventListener("click", () => go(3));
    renderTfRow();
    try {
      const strategies = await ensureStrategies();
      state.strategies = strategies;
      renderStrategyGrid();
    } catch (e) {
      document.getElementById("sm-strat-grid").innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
    }
  }

  function renderTfRow() {
    const row = document.getElementById("sm-tf-row");
    if (!row) return;
    row.innerHTML = SIMPLE_INTERVALS.map((tf) =>
      `<button type="button" class="sm-tf-btn${tf.id === state.interval ? " active" : ""}" data-tf="${tf.id}">${esc(tf.label)}</button>`
    ).join("");
    row.querySelectorAll(".sm-tf-btn").forEach((btn) => {
      btn.addEventListener("click", () => { state.interval = btn.dataset.tf; renderTfRow(); });
    });
  }

  function renderStrategyGrid() {
    const grid = document.getElementById("sm-strat-grid");
    grid.innerHTML = "";
    state.strategies.forEach((name) => {
      const info = describeStrategy(name);
      const card = document.createElement("div");
      card.className = "sm-strat-card" + (state.strategy === name ? " active" : "");
      card.innerHTML = `<div><span class="sm-strat-name">${esc(stratLabel(name))}</span><span class="sm-strat-tag">${info.tag}</span></div><div class="sm-strat-desc">${info.desc}</div>`;
      card.addEventListener("click", () => { state.strategy = name; renderStrategyGrid(); document.getElementById("sm-to-backtest-btn").disabled = false; });
      grid.appendChild(card);
    });
  }

  // ------------------------------------------------------------------
  // STEP 3 — backtest (real /api/analyze -> ltf backtest + candles)
  // ------------------------------------------------------------------
  async function stepBacktest() {
    setTopbar("3/" + TOTAL_STEPS, 3 / TOTAL_STEPS);
    const fromArchive = !!state.archiveEntry;
    // Un solo timeframe per tutta la pipeline: dall'archivio si eredita
    // quello con cui la voce e' stata misurata (non ha senso "sceglierne"
    // un altro senza rifare il backtest), da zero e' quello scelto allo
    // step Strategia.
    if (fromArchive) state.interval = state.archiveEntry.interval || state.interval;
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Step 3 of ${TOTAL_STEPS}</div>
  ${fromArchive ? '<span class="sm-badge">◆ From archive</span>' : ""}
  <h1 class="sm-title">Backtest — ${esc(stratLabel(state.strategy))}</h1>
  <p class="sm-sub">On <b>${esc(state.ticker.symbol)}</b> · timeframe <b>${esc(tfLabel(state.interval))}</b>. Every entry and exit generated by the strategy is marked on the real chart.</p>
  <div id="sm-bt-body"><div class="sm-muted">Running backtest…</div></div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-bt-continue" disabled>${fromArchive ? "Continue" : "Optimize the strategy"}</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(fromArchive ? 1.5 : 2));
    document.getElementById("sm-bt-continue").addEventListener("click", () => go(fromArchive ? 5 : 4));

    try {
      const params = fromArchive ? (state.archiveEntry.params || {}) : {};
      const qs = new URLSearchParams({
        symbol: state.ticker.symbol, asset_type: state.ticker.asset_type,
        exchange: state.ticker.exchange, strategy_name: state.strategy,
        ltf_interval: state.interval,
      });
      Object.keys(params).forEach((k) => qs.set(`strategy_params.${k}`, params[k]));
      const analysis = await api(`/api/analyze?${qs.toString()}`);
      state.analysis = analysis;
      const bt = analysis.backtests.ltf;
      const body = document.getElementById("sm-bt-body");
      const m = fromArchive ? (state.archiveEntry.metrics || {}) : {
        total_return_pct: bt.total_return_pct, max_drawdown_pct: bt.max_drawdown_pct, num_trades: bt.num_trades,
      };
      body.innerHTML = `
        <div class="sm-summary">
          <div class="sm-stat"><div class="lbl">Return</div><div class="val ${(m.total_return_pct || 0) >= 0 ? "up" : "down"}">${fmtPct(m.total_return_pct)}</div></div>
          <div class="sm-stat"><div class="lbl">Max drawdown</div><div class="val down">${fmtPct(m.max_drawdown_pct)}</div></div>
          <div class="sm-stat"><div class="lbl">Trades</div><div class="val">${m.num_trades != null ? m.num_trades : "—"}</div></div>
        </div>
        <div class="sm-real-chart-mount" id="sm-real-chart-mount"><div class="sm-muted">Loading real chart…</div></div>
        <p class="sm-muted" style="margin:4px 0 10px;">Tap a point on the chart to open the nearest trade below.</p>
        <button type="button" class="sm-btn ghost" id="sm-trades-toggle" style="width:100%;margin-bottom:8px;">▸ ${m.num_trades != null ? m.num_trades : (bt.trades || []).length} trades</button>
        <div id="sm-trades-panel" class="sm-trades-panel" hidden></div>
        <button class="sm-btn ghost" id="sm-bt-export" style="width:100%;margin:10px 0;">⤓ Export trades to Excel</button>`;
      document.getElementById("sm-bt-export").addEventListener("click", () => exportBacktestToExcel(params));
      renderTradesPanel(bt);
      // Grafico reale: il VERO .chart-area del sito (LightweightCharts,
      // indicatori, marker di ingresso/uscita), spostato qui invece di
      // ridisegnato — vedi window.LiquidityChartBridge in app.js. Se il
      // ponte non e' disponibile (script non ancora caricato) la pagina
      // resta comunque utilizzabile, solo senza quel riquadro.
      if (window.LiquidityChartBridge) {
        // Come applyArchiveEntry() sul sito: le voci vecchie hanno
        // backtest_params={} (mai registrato niente, non e' un difetto) —
        // va trattato come "nessun override", mai passato cosi' com'e'.
        const bp = fromArchive && state.archiveEntry.backtest_params && Object.keys(state.archiveEntry.backtest_params).length
          ? state.archiveEntry.backtest_params : null;
        const mount = document.getElementById("sm-real-chart-mount");
        window.LiquidityChartBridge.mount(mount);
        await window.LiquidityChartBridge.show({
          assetType: state.ticker.asset_type,
          exchange: state.ticker.exchange,
          symbol: state.ticker.symbol,
          interval: state.interval,
          strategyName: state.strategy,
          params,
          backtestParams: bp,
        }, state.ticker.symbol);
        // Un tocco sul grafico reale apre l'operazione piu' vicina nella
        // lista qui sotto — "ogni ingresso se ci clicco mi deve rimandare
        // alla transazione effettuata dalla strategia" (seed-0068). Una
        // sola sottoscrizione alla volta: onChartClick() sostituisce da
        // sola quella di un montaggio precedente.
        if (window.LiquidityChartBridge.onChartClick) {
          window.LiquidityChartBridge.onChartClick((time) => selectTradeNear(bt, time));
        }
      } else {
        document.getElementById("sm-real-chart-mount").innerHTML = `<div class="sm-muted">Chart not available right now.</div>`;
      }
      document.getElementById("sm-bt-continue").disabled = false;
    } catch (e) {
      document.getElementById("sm-bt-body").innerHTML = e.needsAuth
        ? `<div class="sm-error">Log in to use this feature.</div>`
        : `<div class="sm-error">${esc(e.message)}</div>`;
      if (e.needsAuth) requireLogin();
    }
  }

  // Stessi dati del pulsante "N trade" del Laboratorio (bt.trades/oos_trades
  // del backtest reale) — una tabella propria invece di spostare il
  // pannello del Laboratorio, che e' pensato per stare incollato ai suoi
  // stessi risultati e non per essere spostato altrove.
  function renderTradesPanel(bt) {
    const toggle = document.getElementById("sm-trades-toggle");
    const panel = document.getElementById("sm-trades-panel");
    if (!toggle || !panel) return;
    const trades = (bt.trades || []).concat(bt.oos_trades || []);
    const fmtTs = (s) => new Date(s * 1000).toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "2-digit" });
    panel.innerHTML = trades.length
      ? `<div class="sm-trades-list">${trades.map((t, i) => {
          const isWin = t.pnl_abs > 0;
          const dir = t.direction === "long" ? "▲ Long" : "▼ Short";
          return `<div class="sm-trade-row" id="sm-trade-${i}">
            <span class="dir ${t.direction === "long" ? "up" : "down"}">${dir}</span>
            <span class="dates">${fmtTs(t.entry_time)} → ${t.exit_time ? fmtTs(t.exit_time) : "open"}</span>
            <span class="pnl ${isWin ? "up" : "down"}">${t.pnl_pct >= 0 ? "+" : ""}${t.pnl_pct.toFixed(2)}%</span>
          </div>`;
        }).join("")}</div>`
      : `<p class="sm-muted">No trades in this period.</p>`;
    toggle.addEventListener("click", () => {
      const hidden = panel.hidden;
      panel.hidden = !hidden;
      toggle.textContent = toggle.textContent.replace(hidden ? "▸" : "▾", hidden ? "▾" : "▸");
    });
  }

  // Trova l'operazione il cui ingresso o uscita e' piu' vicino al tempo
  // toccato sul grafico (stessa unita' — secondi — di entry_time/exit_time),
  // apre il pannello se era chiuso e ci scorre sopra con un lampeggio.
  function selectTradeNear(bt, time) {
    const trades = (bt.trades || []).concat(bt.oos_trades || []);
    if (!trades.length) return;
    let best = 0, bestDist = Infinity;
    trades.forEach((t, i) => {
      const d = Math.min(Math.abs(t.entry_time - time), Math.abs((t.exit_time || t.entry_time) - time));
      if (d < bestDist) { bestDist = d; best = i; }
    });
    const panel = document.getElementById("sm-trades-panel");
    const toggle = document.getElementById("sm-trades-toggle");
    if (panel && panel.hidden) { panel.hidden = false; if (toggle) toggle.textContent = toggle.textContent.replace("▸", "▾"); }
    const row = document.getElementById(`sm-trade-${best}`);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("sm-trade-flash");
      setTimeout(() => row.classList.remove("sm-trade-flash"), 1500);
    }
  }

  function exportBacktestToExcel(strategyParams) {
    const qs = new URLSearchParams({
      symbol: state.ticker.symbol, asset_type: state.ticker.asset_type,
      exchange: state.ticker.exchange, ltf_interval: state.interval, strategy_name: state.strategy,
    });
    Object.entries(strategyParams || {}).forEach(([k, v]) => qs.set(k, v));
    const link = document.createElement("a");
    link.href = `/api/export/backtest.xlsx?${qs.toString()}`;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function drawCandleChart(svg, candles, trades) {
    if (!svg || !candles || !candles.length) return;
    const n = candles.length, w = 400, h = 240, padT = 8, padB = 8;
    const vals = candles.flatMap((c) => [c.high, c.low]);
    const min = Math.min(...vals), max = Math.max(...vals);
    const cw = w / n;
    const x = (i) => i * cw + cw / 2;
    const y = (v) => padT + (h - padT - padB) - ((v - min) / (max - min || 1)) * (h - padT - padB);
    let candlesSvg = "";
    candles.forEach((c, i) => {
      const up = c.close >= c.open;
      const col = up ? "var(--sm-bull)" : "var(--sm-bear)";
      const cx = x(i);
      candlesSvg += `<line x1="${cx}" y1="${y(c.high)}" x2="${cx}" y2="${y(c.low)}" stroke="${col}" stroke-width="1"/>`;
      const bTop = y(Math.max(c.open, c.close)), bBot = y(Math.min(c.open, c.close));
      candlesSvg += `<rect x="${cx - cw * 0.32}" y="${bTop}" width="${cw * 0.64}" height="${Math.max(1, bBot - bTop)}" fill="${col}"/>`;
    });
    const timeToIdx = new Map(candles.map((c, i) => [c.time, i]));
    let markers = "";
    (trades || []).forEach((t) => {
      const entryIdx = timeToIdx.get(t.entry_time);
      const exitIdx = t.exit_time != null ? timeToIdx.get(t.exit_time) : null;
      if (entryIdx != null) {
        const cx = x(entryIdx), cy = y(t.entry_price);
        markers += `<path d="M${cx - 5} ${cy + 9} L${cx + 5} ${cy + 9} L${cx} ${cy - 2} Z" fill="var(--sm-bull)"/>`;
      }
      if (exitIdx != null && t.exit_price != null) {
        const cx = x(exitIdx), cy = y(t.exit_price);
        markers += `<path d="M${cx - 5} ${cy - 9} L${cx + 5} ${cy - 9} L${cx} ${cy + 2} Z" fill="var(--sm-bear)"/>`;
      }
    });
    svg.innerHTML = candlesSvg + markers;
  }

  // ------------------------------------------------------------------
  // STEP 4 — optimize (real /api/optimize/start + /api/jobs polling)
  // ------------------------------------------------------------------
  function stepOptimize() {
    setTopbar("4/" + TOTAL_STEPS, 4 / TOTAL_STEPS);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Step 4 of ${TOTAL_STEPS}</div>
  <h1 class="sm-title">Optimize the parameters</h1>
  <p class="sm-sub">Find the best parameter combination for <b>${esc(stratLabel(state.strategy))}</b> on <b>${esc(state.ticker.symbol)}</b> · timeframe <b>${esc(tfLabel(state.interval))}</b>.</p>
  <div id="sm-opt-body"></div>
  <div id="sm-opt-cta" class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-opt-start">Start optimization</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(3));
    document.getElementById("sm-opt-start").addEventListener("click", startOptimization);
    renderOptChoice();
  }

  function renderOptChoice() {
    document.getElementById("sm-opt-body").innerHTML = `
<div class="sm-opt-grid">
  <div class="sm-opt-card${state.optMode === "fast" ? " active" : ""}" data-mode="fast">
    <b>Quick</b> <span class="sm-muted">· 200 random trials · 10–25s (est.)</span>
    <div class="sm-strat-desc">Quickly explores the parameter space. The result may vary on every run.</div>
  </div>
  <div class="sm-opt-card${state.optMode === "stable" ? " active" : ""}" data-mode="stable">
    <b>Stable</b> <span class="sm-muted">· exhaustive guided search · 1–5 min (est.)</span>
    <div class="sm-strat-desc">Finds the configuration that holds up under changing markets, not the lucky peak. Deterministic: same result on every run.</div>
  </div>
</div>`;
    document.querySelectorAll("#simple-mode-root .sm-opt-card").forEach((card) => {
      card.classList.toggle("active", card.dataset.mode === state.optMode);
      card.addEventListener("click", () => { state.optMode = card.dataset.mode; renderOptChoice(); });
    });
  }

  async function startOptimization() {
    document.getElementById("sm-opt-cta").style.display = "none";
    const backtestParams = new URLSearchParams({
      symbol: state.ticker.symbol, asset_type: state.ticker.asset_type,
      exchange: state.ticker.exchange, strategy_name: state.strategy,
      mode: state.optMode === "fast" ? "random" : "plateau",
      ltf_interval: state.interval,
    });
    document.getElementById("sm-opt-body").innerHTML = `<p class="sm-muted">Starting…</p>`;
    try {
      const resp = await api(`/api/optimize/start?${backtestParams.toString()}`, { method: "POST" });
      state.optJobId = resp.job_id;
      // La ricerca gira nella STESSA finestra del sito (animazione della
      // ricerca stabile, log reale, pulsante "Ferma" gia' cablato) invece
      // di una riscritta qui — vedi window.LiquidityOptBridge in app.js.
      if (window.LiquidityOptBridge) {
        window.LiquidityOptBridge.start(resp.job_id, {
          symbol: state.ticker.symbol,
          strategy: state.strategy,
          mode: state.optMode === "fast" ? "random" : "plateau",
        });
        document.getElementById("sm-opt-body").innerHTML = `<p class="sm-muted">The search runs in the window above. You can stop it from there at any time.</p>`;
        watchOptBridge();
      } else {
        document.getElementById("sm-opt-body").innerHTML = `<div class="sm-error">Progress window not available right now — try again.</div>`;
        document.getElementById("sm-opt-cta").style.display = "flex";
      }
    } catch (e) {
      document.getElementById("sm-opt-body").innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
      document.getElementById("sm-opt-cta").style.display = "flex";
      if (e.needsAuth) requireLogin();
    }
  }

  // Nessuna seconda chiamata di rete: LiquidityOptBridge.getState() legge
  // dalla stessa mappa che il polling reale del sito gia' aggiorna da
  // solo — qui si controlla solo se e' il momento di far avanzare la guida.
  function watchOptBridge() {
    clearTimeout(state.optPollTimer);
    const tick = () => {
      // L'utente ha lasciato questo passo (Indietro, Esci): il lavoro
      // prosegue sul server per conto suo, ma non c'e' piu' nessuna guida
      // qui ad aspettarlo — fermare il controllo invece di scrivere in un
      // #sm-opt-body che non esiste piu'.
      if (!document.getElementById("sm-opt-body")) return;
      const st = window.LiquidityOptBridge.getState(state.optJobId);
      if (st && st.state === "finito") { window.LiquidityOptBridge.close(); onOptDone({ result: st.result }); return; }
      if (st && st.state === "fallito") { window.LiquidityOptBridge.close(); onOptFailed(st.error || "Optimization failed"); return; }
      if (st && st.state === "annullato") { window.LiquidityOptBridge.close(); onOptFailed("Optimization cancelled."); return; }
      state.optPollTimer = setTimeout(tick, 800);
    };
    tick();
  }

  function onOptFailed(msg) {
    document.getElementById("sm-opt-body").innerHTML = `<div class="sm-error">${esc(msg)}</div>`;
    document.getElementById("sm-opt-cta").style.display = "flex";
  }

  function onOptDone(job) {
    const result = job.result;
    state.optResult = result;
    const bt = result.backtest || {};
    document.getElementById("sm-opt-body").innerHTML = `
<div class="sm-summary">
  <div class="sm-stat"><div class="lbl">Optimized return</div><div class="val ${(bt.total_return_pct || 0) >= 0 ? "up" : "down"}">${fmtPct(bt.total_return_pct)}</div></div>
  <div class="sm-stat"><div class="lbl">Max drawdown</div><div class="val down">${fmtPct(bt.max_drawdown_pct)}</div></div>
  <div class="sm-stat"><div class="lbl">Out-of-sample</div><div class="val ${result.overfitted ? "down" : "up"}">${result.overfitted ? "overfit" : "holds"}</div></div>
</div>
<div class="sm-explain">Parameters found: <code>${esc(JSON.stringify(result.params))}</code>. They'll be applied to the strategy when you activate it. ${result.overfitted ? "Warning: the out-of-sample test suggests these parameters may not hold on future data." : "The out-of-sample test confirms it holds beyond the data used to find them."}</div>
<div class="sm-ctabar"><div class="sm-ctabar-inner">
  <button class="sm-btn ghost" id="sm-opt-review">Review chart</button>
  <button class="sm-btn primary" id="sm-opt-next">${state.ticker.asset_type === "crypto" ? "Connect exchange" : "Continue"}</button>
</div></div>`;
    document.getElementById("sm-opt-review").addEventListener("click", () => go(3));
    document.getElementById("sm-opt-next").addEventListener("click", () => go(state.ticker.asset_type === "crypto" ? 5 : 6));
  }

  // ------------------------------------------------------------------
  // STEP 5 — exchange (real PATCH /api/auth/trading). Skipped for stocks:
  // place_order_for_signal only executes on the crypto trading exchanges.
  // ------------------------------------------------------------------
  let savedCredsCache = null;
  async function ensureSavedCreds() {
    if (savedCredsCache) return savedCredsCache;
    try { savedCredsCache = await api("/api/auth/trading"); } catch (_e) { savedCredsCache = []; }
    return savedCredsCache;
  }

  async function stepExchange() {
    if (state.ticker.asset_type !== "crypto") { go(6); return; }
    setTopbar("5/" + TOTAL_STEPS, 5 / TOTAL_STEPS);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Step 5 of ${TOTAL_STEPS}</div>
  <h1 class="sm-title">Connect your exchange</h1>
  <p class="sm-sub">All you need is a spot API key with trading permissions — never withdrawal access. Spot only: leverage is capped at 1x across the app. Keys are encrypted and never shown again once saved.</p>
  <div class="sm-exch-grid" id="sm-exch-grid"></div>
  <div id="sm-exch-body"></div>
  <div id="sm-exch-error"></div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-exch-continue">Continue</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(4));
    document.getElementById("sm-exch-continue").addEventListener("click", onExchangeContinue);
    const grid = document.getElementById("sm-exch-grid");
    const saved = await ensureSavedCreds();
    TRADING_EXCHANGES.forEach((ex) => {
      const opt = document.createElement("div");
      const isSaved = saved.some((c) => c.exchange === ex.id && c.api_key_set);
      opt.className = "sm-exch-opt" + (state.tradingExchange === ex.id ? " active" : "");
      opt.innerHTML = `<div class="sm-exch-badge" style="background:${ex.color}">${ex.letter}</div><div style="font-size:12px;font-weight:600;">${ex.name}</div>${isSaved ? '<div class="sm-muted" style="margin-top:2px;">🔒 saved</div>' : ""}`;
      opt.addEventListener("click", () => { state.tradingExchange = ex.id; state.connTested = false; renderExchGrid(); });
      grid.appendChild(opt);
    });
    renderExchGrid();
  }
  function renderExchGrid() {
    document.querySelectorAll("#simple-mode-root .sm-exch-opt").forEach((opt, i) => opt.classList.toggle("active", TRADING_EXCHANGES[i].id === state.tradingExchange));
    const ex = TRADING_EXCHANGES.find((e) => e.id === state.tradingExchange);
    const saved = (savedCredsCache || []).find((c) => c.exchange === ex.id);
    const body = document.getElementById("sm-exch-body");
    if (saved && saved.api_key_set) {
      const when = saved.created_at ? new Date(saved.created_at * 1000).toLocaleDateString("en-US", { day: "2-digit", month: "short", year: "numeric" }) : null;
      body.innerHTML = `
<div class="sm-card">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <span>🔒 ${esc(ex.name)} key already saved${when ? " — added on " + esc(when) : ""}</span>
  </div>
  <p class="sm-sub" style="margin:8px 0 0;">No need to re-enter it: continue and the connected one will be used. Need to change it? Go to Trading on desktop.</p>
</div>`;
    } else {
      body.innerHTML = `
<div class="sm-exch-steps"><ol>${ex.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol></div>
<div class="sm-field"><label>API key</label><input id="sm-api-key" placeholder="••••••••••••••••"></div>
<div class="sm-field"><label>Secret key</label><input id="sm-api-secret" type="password" placeholder="••••••••••••••••"></div>
${ex.id === "kucoin" ? '<div class="sm-field"><label>Passphrase</label><input id="sm-api-passphrase" type="password" placeholder="••••••••••••••••"></div>' : ""}`;
    }
  }
  async function onExchangeContinue() {
    const saved = (savedCredsCache || []).find((c) => c.exchange === state.tradingExchange);
    if (saved && saved.api_key_set) { go(5.5); return; }
    const apiKey = document.getElementById("sm-api-key").value.trim();
    const apiSecret = document.getElementById("sm-api-secret").value.trim();
    const passphrase = document.getElementById("sm-api-passphrase")?.value.trim() || "";
    if (!apiKey || !apiSecret) {
      document.getElementById("sm-exch-error").innerHTML = `<div class="sm-error">Enter key and secret, or go back if you only want the Telegram alert without automatic execution.</div>`;
      return;
    }
    try {
      // Il processo resta quello ufficiale: la chiave si salva qui, per
      // davvero, con lo stesso endpoint che usa Trading — il passo
      // successivo la testa SENZA richiederla di nuovo (vedi
      // stepTestConnection: api_key/api_secret vuoti = usa quella salvata).
      await api("/api/auth/trading", {
        method: "PATCH",
        body: JSON.stringify({ exchange: state.tradingExchange, api_key: apiKey, api_secret: apiSecret, passphrase, enabled: true, size_mode: "fixed_usdt", size_value: 50 }),
      });
      savedCredsCache = null;
      state.connTested = false;
      go(5.5);
    } catch (e) {
      document.getElementById("sm-exch-error").innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
      if (e.needsAuth) requireLogin();
    }
  }

  // ------------------------------------------------------------------
  // STEP 5.5 — test the just-saved connection: balance + an optional real
  // manual order, before committing to automatic activation. l'utente,
  // verbatim: "voglio semplicemente... una volta che le ho inserite [le
  // API], vuoi testarle subito? ... mi permetti sia di visualizzare il
  // saldo... e di poter fare un ordine quindi manuale". The keys used here
  // are the ones just saved by onExchangeContinue via the official
  // PATCH /api/auth/trading flow — never re-entered, never re-asked.
  // ------------------------------------------------------------------
  function stepTestConnection() {
    setTopbar("Test", 5.5 / TOTAL_STEPS);
    const ex = TRADING_EXCHANGES.find((e) => e.id === state.tradingExchange);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Before activating</div>
  <h1 class="sm-title">Test ${esc(ex.name)} now?</h1>
  <p class="sm-sub">A connection attempt with the key you just saved: you see the real balance, and if you want you can also try a manual order — so you know it works before letting the strategy trade on its own.</p>
  <div id="sm-conn-body"></div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-conn-continue">Continue</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(5));
    document.getElementById("sm-conn-continue").addEventListener("click", () => go(6));
    renderConnPrompt();
  }

  function renderConnPrompt() {
    const ex = TRADING_EXCHANGES.find((e) => e.id === state.tradingExchange);
    document.getElementById("sm-conn-body").innerHTML = `
<div class="sm-card" style="text-align:center;">
  <p style="margin:0 0 12px;">Test the connection to <b>${esc(ex.name)}</b> now?</p>
  <div style="display:flex;gap:10px;justify-content:center;">
    <button class="sm-btn primary" id="sm-conn-yes">Yes, test it now</button>
    <button class="sm-btn ghost" id="sm-conn-no">Skip the test</button>
  </div>
</div>`;
    document.getElementById("sm-conn-yes").addEventListener("click", runConnectionTest);
    document.getElementById("sm-conn-no").addEventListener("click", () => go(6));
  }

  async function runConnectionTest() {
    const ex = TRADING_EXCHANGES.find((e) => e.id === state.tradingExchange);
    const body = document.getElementById("sm-conn-body");
    body.innerHTML = `
<div class="sm-pulse-wrap" style="padding:20px 10px 6px;">
  <div class="sm-pulse"><div class="core"></div></div>
  <p class="sm-muted">Testing connection to ${esc(ex.name)}…</p>
</div>`;
    try {
      // api_key/api_secret vuoti = il server usa la chiave appena salvata,
      // mai una reinserita qui — stesso endpoint gia' usato da Diagnosi.
      const res = await api("/api/diagnostics/test-connection", {
        method: "POST",
        body: JSON.stringify({ exchange: state.tradingExchange, market: "spot", api_key: "", api_secret: "", passphrase: "" }),
      });
      if (!res.ok) {
        state.connTested = false;
        body.innerHTML = `
<div class="sm-card">
  <p class="sm-error" style="margin:0;">✕ ${esc(res.error)}</p>
  <div style="display:flex;gap:10px;margin-top:10px;">
    <button class="sm-btn primary" id="sm-conn-retry">Retry</button>
    <button class="sm-btn ghost" id="sm-conn-skip">Continue anyway</button>
  </div>
</div>`;
        document.getElementById("sm-conn-retry").addEventListener("click", runConnectionTest);
        document.getElementById("sm-conn-skip").addEventListener("click", () => go(6));
        return;
      }
      state.connTested = true;
      const rows = res.balances || [];
      body.innerHTML = `
<div class="sm-card">
  <p class="sm-ok" style="margin:0 0 10px;color:var(--sm-bull);">✓ Connected to ${esc(ex.name)} — real balance:</p>
  ${rows.length
      ? `<div class="sm-summary" style="grid-template-columns:repeat(${Math.min(3, rows.length)},1fr);">${rows.slice(0, 6).map((b) => `<div class="sm-stat"><div class="lbl">${esc(b.asset)}</div><div class="val">${fmtNum(b.free)}</div></div>`).join("")}</div>`
      : `<p class="sm-muted" style="margin:0;">No positive balance on this account.</p>`}
</div>
<div class="sm-card" id="sm-order-prompt">
  <p style="margin:0 0 10px;">Want to try a manual test order too? <span class="sm-muted">(real money, requires explicit confirmation)</span></p>
  <button class="sm-btn ghost" id="sm-conn-order-btn">Try a manual order</button>
</div>`;
      document.getElementById("sm-conn-order-btn").addEventListener("click", renderTestOrderForm);
    } catch (e) {
      state.connTested = false;
      body.innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
      if (e.needsAuth) requireLogin();
    }
  }

  let testOrderSide = "BUY";
  function renderTestOrderForm() {
    const box = document.getElementById("sm-order-prompt");
    box.innerHTML = `
  <p style="margin:0 0 10px;">Manual test order — <b>${esc(state.ticker.asset_type === "crypto" ? state.ticker.symbol : "")}</b></p>
  <div class="sm-field"><label>Pair</label><input id="sm-test-order-symbol" placeholder="e.g. BTCUSDT" value="${esc(state.ticker.asset_type === "crypto" ? state.ticker.symbol : "")}"></div>
  <div style="display:flex;gap:8px;margin-bottom:10px;">
    <button type="button" class="sm-tf-btn active" id="sm-test-side-buy" style="flex:1;">BUY</button>
    <button type="button" class="sm-tf-btn" id="sm-test-side-sell" style="flex:1;">SELL</button>
  </div>
  <div class="sm-field"><label>USDT</label><input id="sm-test-order-usdt" type="number" value="10" min="1" step="1"></div>
  <label style="display:flex;gap:8px;align-items:flex-start;font-size:12.5px;color:var(--sm-ink-dim);margin-bottom:10px;">
    <input type="checkbox" id="sm-test-order-confirm" style="margin-top:3px;">
    <span>I confirm: this is real money, a test order on ${esc(TRADING_EXCHANGES.find((e) => e.id === state.tradingExchange).name)}.</span>
  </label>
  <button class="sm-btn primary" id="sm-test-order-exec" disabled style="width:100%;">Execute test order</button>
  <div id="sm-test-order-result" style="margin-top:8px;"></div>`;
    testOrderSide = "BUY";
    const buyBtn = document.getElementById("sm-test-side-buy");
    const sellBtn = document.getElementById("sm-test-side-sell");
    const execBtn = document.getElementById("sm-test-order-exec");
    const confirmBox = document.getElementById("sm-test-order-confirm");
    buyBtn.addEventListener("click", () => { testOrderSide = "BUY"; buyBtn.classList.add("active"); sellBtn.classList.remove("active"); execBtn.textContent = "Execute test order (BUY)"; });
    sellBtn.addEventListener("click", () => { testOrderSide = "SELL"; sellBtn.classList.add("active"); buyBtn.classList.remove("active"); execBtn.textContent = "Execute test order (SELL)"; });
    confirmBox.addEventListener("change", () => { execBtn.disabled = !confirmBox.checked; });
    execBtn.addEventListener("click", async () => {
      const symbol = document.getElementById("sm-test-order-symbol").value.trim().toUpperCase();
      const out = document.getElementById("sm-test-order-result");
      if (!symbol) { out.innerHTML = `<div class="sm-error">Missing pair</div>`; return; }
      execBtn.disabled = true;
      out.innerHTML = `<p class="sm-muted">Sending…</p>`;
      try {
        await api("/api/trading/order", {
          method: "POST",
          body: JSON.stringify({
            exchange: state.tradingExchange, symbol, side: testOrderSide,
            size_mode: "fixed_usdt", size_value: parseFloat(document.getElementById("sm-test-order-usdt").value) || 0,
          }),
        });
        out.innerHTML = `<p style="color:var(--sm-bull);">✓ Order sent — see the order log in Trading (desktop) for details.</p>`;
        confirmBox.checked = false;
      } catch (e) {
        out.innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
        if (e.needsAuth) requireLogin();
      } finally {
        execBtn.disabled = !confirmBox.checked;
      }
    });
  }

  // ------------------------------------------------------------------
  // STEP 6 — review + activate (real sequence of watchlist/archive calls)
  // ------------------------------------------------------------------
  function stepReview() {
    setTopbar("6/" + TOTAL_STEPS, 6 / TOTAL_STEPS);
    const isCrypto = state.ticker.asset_type === "crypto";
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-eyebrow">Step 6 of ${TOTAL_STEPS}</div>
  <h1 class="sm-title">Ready to activate</h1>
  <p class="sm-sub">Review the summary, then press Activate.</p>
  <div class="sm-review-card">
    <div class="sm-review-row"><span class="k">Ticker</span><span class="v">${esc(state.ticker.symbol)}</span></div>
    <div class="sm-review-row"><span class="k">Timeframe</span><span class="v">${esc(tfLabel(state.interval))}</span></div>
    <div class="sm-review-row"><span class="k">Strategy</span><span class="v">${esc(stratLabel(state.strategy))}</span></div>
    <div class="sm-review-row"><span class="k">Optimization</span><span class="v">${state.archiveEntry ? "from archive" : (state.optResult ? (state.optMode === "fast" ? "Quick" : "Stable") : "default parameters")}</span></div>
    <div class="sm-review-row"><span class="k">Execution</span><span class="v">${isCrypto ? esc(TRADING_EXCHANGES.find((e) => e.id === state.tradingExchange).name) + (state.connTested ? " — connection tested ✓" : "") : "alert only (no exchange)"}</span></div>
  </div>
  <div class="sm-explain">${isCrypto
      ? `When the strategy detects a signal on <b>${esc(state.ticker.symbol)}</b>, you get a Telegram alert and, if you've connected exchange keys, the app automatically executes the spot order with the parameters chosen here. You can turn it off anytime from the watchlist.`
      : `${esc(state.ticker.symbol)} is a stock: automatic execution is available for spot crypto only. Once activated you'll only get the Telegram alert when the strategy generates a signal.`}</div>
  <div id="sm-activate-error"></div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner">
    <button class="sm-btn ghost" id="sm-back-btn">Back</button>
    <button class="sm-btn primary" id="sm-activate-btn">Activate strategy</button>
  </div></div>
</main>`;
    document.getElementById("sm-back-btn").addEventListener("click", () => go(isCrypto ? 5.5 : 4));
    document.getElementById("sm-activate-btn").addEventListener("click", onActivate);
  }

  async function onActivate() {
    const btn = document.getElementById("sm-activate-btn");
    btn.disabled = true;
    btn.textContent = "Activating…";
    try {
      const isCrypto = state.ticker.asset_type === "crypto";
      const wantsAutoTrade = isCrypto && !!state.tradingExchange;
      let entry;
      if (state.archiveEntry) {
        // Single real call: apply-to-alert does watchlist add + params +
        // strategy toggle server-side, atomically, from the archived
        // configuration — this is the exact 3-step sequence described below,
        // already implemented on the backend for the archive path.
        const res = await api(`/api/archive/${state.archiveEntry.id}/apply-to-alert`, {
          method: "POST",
          body: JSON.stringify({ auto_trade: wantsAutoTrade }),
        });
        entry = res.watchlist_entry;
      } else {
        // Real 3-call sequence: add to watchlist if missing, then push any
        // optimized params, then flip the strategy toggle — the toggle is
        // what makes the background watcher call place_order_for_signal()
        // once trading credentials for this exchange are enabled.
        const existing = await api("/api/watchlist");
        entry = existing.find((e) => e.symbol.toUpperCase() === state.ticker.symbol.toUpperCase() && e.asset_type === state.ticker.asset_type);
        if (!entry) {
          entry = await api("/api/watchlist", {
            method: "POST",
            body: JSON.stringify({ symbol: state.ticker.symbol, asset_type: state.ticker.asset_type, exchange: state.ticker.exchange, interval: state.interval, on_close: true }),
          });
        }
        if (state.optResult && state.optResult.params) {
          entry = await api(`/api/watchlist/${entry.id}/strategy_params`, {
            method: "PATCH",
            body: JSON.stringify({ strategy_name: state.strategy, params: state.optResult.params }),
          });
        }
        entry = await api(`/api/watchlist/${entry.id}/strategy`, {
          method: "PATCH",
          body: JSON.stringify({ strategy_name: state.strategy, enabled: true }),
        });
        if (wantsAutoTrade) {
          entry = await api(`/api/watchlist/${entry.id}/auto_trade`, {
            method: "PATCH",
            body: JSON.stringify({ strategy_name: state.strategy, enabled: true }),
          });
        }
      }
      state.watchlistEntry = entry;
      go(7);
    } catch (e) {
      document.getElementById("sm-activate-error").innerHTML = `<div class="sm-error">${esc(e.message)}</div>`;
      btn.disabled = false;
      btn.textContent = "Activate strategy";
      if (e.needsAuth) requireLogin();
    }
  }

  // ------------------------------------------------------------------
  // STEP 7 — listening (real watchlist entry state, not a placeholder)
  // ------------------------------------------------------------------
  function stepListening() {
    setTopbar("", null);
    const entry = state.watchlistEntry || {};
    const isListening = (entry.strategies || []).includes(state.strategy);
    document.getElementById("sm-steps").innerHTML = `
<main class="sm-step active">
  <div class="sm-pulse-wrap">
    <div class="sm-pulse"><div class="core"></div></div>
    <h1 style="font-size:20px;margin:0;">${isListening ? "Strategy listening" : "Activation recorded"}</h1>
    <p class="sm-muted" style="max-width:38ch;margin-top:8px;"><b>${esc(stratLabel(state.strategy))}</b> is ${isListening ? "active" : "being activated"} on <b>${esc(state.ticker.symbol)}</b>. You'll get a Telegram alert on the next signal${state.ticker.asset_type === "crypto" ? ", and the order will go out on its own if exchange keys are connected." : "."}</p>
    <div class="sm-status-card">
      <div class="sm-status-row"><span class="k">Status</span><span class="v"><span class="sm-live-dot"></span>${isListening ? "Listening" : "To be verified"}</span></div>
      <div class="sm-status-row"><span class="k">Watchlist entry</span><span class="v">${esc(entry.id || "—")}</span></div>
      <div class="sm-status-row"><span class="k">Timeframe</span><span class="v">${esc(entry.interval || "—")}</span></div>
    </div>
  </div>
  <div class="sm-ctabar"><div class="sm-ctabar-inner"><button class="sm-btn primary" id="sm-restart-btn">New strategy</button></div></div>
</main>`;
    document.getElementById("sm-restart-btn").addEventListener("click", () => {
      Object.assign(state, { step: 0, ticker: null, archiveEntry: null, strategy: null, interval: "1d", analysis: null, optResult: null, optJobId: null, connTested: false, watchlistEntry: null, query: "" });
      go(0);
    });
  }

  // ------------------------------------------------------------------
  // Router
  // ------------------------------------------------------------------
  function go(step) {
    clearTimeout(state.optPollTimer);
    state.step = step;
    window.scrollTo(0, 0);
    // Il grafico reale (vedi stepBacktest) e' il vero nodo .chart-area del
    // sito, spostato qui dentro — questo step riscrive #sm-steps con
    // innerHTML, che distruggerebbe quel nodo (e i chart LightweightCharts
    // dentro) se non lo si riporta al suo posto prima. Idempotente: se non
    // era montato qui, restore() non fa nulla.
    if (step !== 3 && window.LiquidityChartBridge) window.LiquidityChartBridge.restore();
    if (step === 0) return stepWelcome();
    if (step === 1) return stepTicker();
    if (step === 1.5) return stepArchive();
    if (step === 2) return stepStrategy();
    if (step === 3) return stepBacktest();
    if (step === 4) return stepOptimize();
    if (step === 5) return stepExchange();
    if (step === 5.5) return stepTestConnection();
    if (step === 6) return stepReview();
    if (step === 7) return stepListening();
  }

  // ------------------------------------------------------------------
  // Mode toggle + persistence
  // ------------------------------------------------------------------
  function setActive(active) {
    if (!active && window.LiquidityChartBridge) {
      window.LiquidityChartBridge.restore();
      window.LiquidityChartBridge.offChartClick?.();
    }
    try { localStorage.setItem(LS_KEY, active ? "1" : "0"); } catch (_e) { /* ignore */ }
    root.classList.toggle("hidden", !active);
    document.querySelectorAll("header.toolbar, main.cockpit, nav.mobile-nav, footer.disclaimer, #mobile-drawer-backdrop").forEach((elm) => {
      elm.classList.toggle("sm-force-hidden", active);
    });
    if (active) {
      renderShell();
      go(state.step || 0);
    } else {
      clearTimeout(state.optPollTimer);
    }
  }

  function initHideRule() {
    if (document.getElementById("simple-mode-hide-rule")) return;
    const style = document.createElement("style");
    style.id = "simple-mode-hide-rule";
    style.textContent = ".sm-force-hidden{display:none!important;}";
    document.head.appendChild(style);
  }

  toggleBtns.forEach((btn) => btn.addEventListener("click", () => {
    // Sull'Altro mobile il click deve anche richiudere quel pannello,
    // altrimenti resta aperto sopra la modalita' semplice appena attivata.
    document.getElementById("altro-panel")?.classList.remove("mobile-open");
    setActive(root.classList.contains("hidden"));
  }));

  injectStyle();
  initHideRule();
  let startActive = false;
  try { startActive = localStorage.getItem(LS_KEY) === "1"; } catch (_e) { /* ignore */ }
  if (startActive) setActive(true);
})();
