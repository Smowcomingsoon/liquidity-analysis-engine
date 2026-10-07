/**
 * A row that loses its place in the Top breaks into particles (Lorenzo,
 * 29/09: "animazione molto bella di distruzione particellare del ticker che
 * occupava quella posizione, facendo spazio al nuovo").
 *
 * The row's own text (name, ticker, cap) and logo are drawn once into a
 * canvas laid over the row, sampled into a few hundred particles, and blown
 * away left to right. One canvas per burst, removed when done; nothing else
 * on the page repaints.
 */

const MAX_PARTICLES = 700;
const DURATION_MS = 1100;

const reduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

function drawText(ctx, el, origin) {
  if (!el || !el.textContent.trim()) return;
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
  ctx.fillStyle = cs.color;
  ctx.textBaseline = "middle";
  ctx.fillText(el.textContent.trim().replace(/\s+/g, " "), r.left - origin.left, r.top - origin.top + r.height / 2);
}

/** Snapshot `row` now (before it is replaced) and play the burst over its
 *  place. Resolves when the burst is over (at once when there is nothing to
 *  play), so a sequence can wait for each one. */
export function disintegrate(row, ms = DURATION_MS) {
  if (!row || reduced()) return Promise.resolve();
  // A hidden ghost (top.js) is measured and drawn, never shown itself.
  const rect = row.getBoundingClientRect();
  if (!rect.width || !rect.height || rect.bottom < 0 || rect.top > innerHeight) return Promise.resolve();
  const pad = 48;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = Math.ceil(rect.width + pad * 2);
  const h = Math.ceil(rect.height + pad * 2);
  const origin = { left: rect.left - pad, top: rect.top - pad };

  // 1. The row as it looks, text and logo, in an offscreen canvas.
  const src = document.createElement("canvas");
  src.width = w;
  src.height = h;
  const sctx = src.getContext("2d", { willReadFrequently: true });
  const logo = row.querySelector("img, .tile, .logo, svg");
  if (logo) {
    const lr = logo.getBoundingClientRect();
    sctx.fillStyle = getComputedStyle(row).color;
    sctx.beginPath();
    sctx.arc(lr.left - origin.left + lr.width / 2, lr.top - origin.top + lr.height / 2, Math.min(lr.width, lr.height) / 2, 0, Math.PI * 2);
    sctx.fill();
  }
  row.querySelectorAll(".trow-main b, .trow-main small, .trow-num b").forEach((el) => drawText(sctx, el, origin));

  // 2. Sample the drawn pixels into particles.
  const data = sctx.getImageData(0, 0, w, h).data;
  let step = 2;
  let parts = [];
  for (;;) {
    parts = [];
    for (let y = 0; y < h; y += step) {
      for (let x = 0; x < w; x += step) {
        const i = (y * w + x) * 4;
        if (data[i + 3] > 100) parts.push({ x, y, c: `rgba(${data[i]},${data[i + 1]},${data[i + 2]},` });
      }
    }
    if (parts.length <= MAX_PARTICLES || step >= 6) break;
    step += 1;
  }
  if (!parts.length) return Promise.resolve();
  for (const p of parts) {
    // Left to right, like ash blown off the row.
    p.delay = (p.x / w) * 0.45 + Math.random() * 0.12;
    p.vx = 40 + Math.random() * 120;
    p.vy = -30 - Math.random() * 90;
    p.s = step * (0.6 + Math.random() * 0.6);
  }

  // 3. Play it on a canvas laid over the row.
  const cv = document.createElement("canvas");
  cv.className = "fx-burst";
  cv.width = w * dpr;
  cv.height = h * dpr;
  Object.assign(cv.style, { left: `${origin.left}px`, top: `${origin.top}px`, width: `${w}px`, height: `${h}px` });
  document.body.append(cv);
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  const t0 = performance.now();
  return new Promise((done) => {
  const frame = (now) => {
    const t = (now - t0) / ms;
    ctx.clearRect(0, 0, w, h);
    for (const p of parts) {
      const k = Math.max(0, Math.min(1, (t - p.delay) / (1 - p.delay)));
      const a = 1 - k;
      if (a <= 0) continue;
      const e = k * k;
      ctx.fillStyle = `${p.c}${a.toFixed(3)})`;
      ctx.fillRect(p.x + p.vx * e, p.y + p.vy * e, p.s * (1 - k * 0.5), p.s * (1 - k * 0.5));
    }
    if (t < 1) requestAnimationFrame(frame);
    else { cv.remove(); done(); }
  };
  requestAnimationFrame(frame);
  });
}

/**
 * A scanned project going into the database (Lorenzo, 29/09: "un tasto che
 * mi permetta di aggiungere al database con delle animazioni fighe
 * particellari che mi fanno capire che e' stato registrato"). The token's
 * head (logo and name) breaks into particles that stream into the button,
 * which then lights up. Same canvas-over-the-page approach as disintegrate.
 */
export function absorb(from, into, ms = 1300) {
  if (!from || !into || reduced()) return Promise.resolve();
  const fr = from.getBoundingClientRect(), to = into.getBoundingClientRect();
  if (!fr.width || !to.width) return Promise.resolve();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const w = Math.ceil(fr.width), h = Math.ceil(fr.height);
  const src = document.createElement("canvas");
  src.width = w;
  src.height = h;
  const sctx = src.getContext("2d", { willReadFrequently: true });
  const origin = { left: fr.left, top: fr.top };
  const logo = from.querySelector("img, .tile, .logo, svg");
  if (logo) {
    const lr = logo.getBoundingClientRect();
    sctx.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--c-text-3").trim() || "#818cf8";
    sctx.beginPath();
    sctx.arc(lr.left - origin.left + lr.width / 2, lr.top - origin.top + lr.height / 2, Math.min(lr.width, lr.height) / 2, 0, Math.PI * 2);
    sctx.fill();
  }
  from.querySelectorAll("b, small").forEach((el) => drawText(sctx, el, origin));
  const data = sctx.getImageData(0, 0, w, h).data;
  let step = 2, parts = [];
  for (;;) {
    parts = [];
    for (let y = 0; y < h; y += step) {
      for (let x = 0; x < w; x += step) {
        const i = (y * w + x) * 4;
        if (data[i + 3] > 100) parts.push({ x: fr.left + x, y: fr.top + y, c: `rgba(${data[i]},${data[i + 1]},${data[i + 2]},` });
      }
    }
    if (parts.length <= MAX_PARTICLES || step >= 6) break;
    step += 1;
  }
  if (!parts.length) return Promise.resolve();
  const tx = to.left + to.width / 2, ty = to.top + to.height / 2;
  for (const p of parts) {
    p.delay = ((p.x - fr.left) / w) * 0.35 + Math.random() * 0.15;
    // A curved path: each particle swings out sideways before landing.
    p.cx = (p.x + tx) / 2 + (Math.random() - 0.5) * 220;
    p.cy = Math.min(p.y, ty) - 40 - Math.random() * 120;
    p.s = step * (0.7 + Math.random() * 0.6);
  }
  const cv = document.createElement("canvas");
  cv.className = "fx-burst";
  cv.width = innerWidth * dpr;
  cv.height = innerHeight * dpr;
  Object.assign(cv.style, { left: "0px", top: "0px", width: `${innerWidth}px`, height: `${innerHeight}px` });
  // The scanner is a modal dialog (top layer): a popover canvas sits above it.
  if (cv.showPopover) {
    cv.setAttribute("popover", "manual");
    Object.assign(cv.style, { margin: "0", padding: "0", border: "0", background: "transparent", inset: "0 auto auto 0", overflow: "visible" });
    document.body.append(cv);
    cv.showPopover();
  } else {
    (from.closest("dialog") ?? document.body).append(cv);
  }
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  from.animate?.([{ opacity: 1 }, { opacity: 0.2, offset: 0.4 }, { opacity: 1 }], { duration: ms, easing: "ease-in-out" });
  const t0 = performance.now();
  return new Promise((done) => {
    const frame = (now) => {
      const t = (now - t0) / ms;
      ctx.clearRect(0, 0, innerWidth, innerHeight);
      for (const p of parts) {
        const k = Math.max(0, Math.min(1, (t - p.delay) / (1 - p.delay)));
        if (k >= 1) continue;
        const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
        const u = 1 - e;
        const x = u * u * p.x + 2 * u * e * p.cx + e * e * tx;
        const y = u * u * p.y + 2 * u * e * p.cy + e * e * ty;
        ctx.fillStyle = `${p.c}${(0.35 + 0.65 * (1 - k * k)).toFixed(3)})`;
        const s = p.s * (1 - k * 0.6);
        ctx.fillRect(x, y, s, s);
      }
      if (t < 1) requestAnimationFrame(frame);
      else { cv.remove(); done(); }
    };
    requestAnimationFrame(frame);
  });
}
