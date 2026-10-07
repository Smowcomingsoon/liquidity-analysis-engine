/**
 * Morph the Why tab when a deep re-check replaces the old conclusions
 * (Lorenzo, 01/10: "vengono cancellati e barrati i concetti vecchi e
 * sostituiti da quelli nuovi con animazione particellare"): every text line
 * of a gone point is struck through, then dissolves into particles; the new
 * points assemble from particles. On DOM nodes only transform and opacity
 * move; particles live on one canvas, one rAF loop, frame-rate independent.
 * With prefers-reduced-motion the body is simply repainted.
 */

const STRIKE_MS = 260;
const STRIKE_STAGGER = 40;
const STRIKE_MAX = 8;
const SWEEP_MS = 320;
const FADE_MS = 320;
const FLY_MS = 420;
const ASSEMBLE_STAGGER = 50;
const ASSEMBLE_MAX = 10;
const PER_PX = 1 / 90;
const MAX_PER_NODE = 140;

const running = new WeakMap();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const easeOutQuad = (t) => 1 - (1 - t) * (1 - t);
const easeOutCubic = (t) => 1 - (1 - t) ** 3;
const UNITS = "li, p, h5";

function texts(root) {
  return new Set([...root.querySelectorAll(UNITS)].map((n) => n.textContent.trim()).filter(Boolean));
}

// The line boxes of a node's text: one strike and one particle band per line.
function lines(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const rects = [...range.getClientRects()].filter((r) => r.width > 2 && r.height > 2);
  const merged = [];
  for (const r of rects) {
    const row = merged.find((m) => Math.abs(m.top - r.top) < r.height / 2);
    if (row) { row.left = Math.min(row.left, r.left); row.right = Math.max(row.right, r.right); row.bottom = Math.max(row.bottom, r.bottom); }
    else merged.push({ left: r.left, right: r.right, top: r.top, bottom: r.bottom });
  }
  return merged.map((m) => ({ left: m.left, top: m.top, width: m.right - m.left, height: m.bottom - m.top }));
}

function colorOf(node) {
  const tone = node.getAttribute("data-tone");
  // The phone skin (classic.css) defines --c-up/--c-down; the desktop keeps the old tones.
  const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  if (tone === "good") return css("--c-up", "#34d399");
  if (tone === "bad") return css("--c-down", "#f87171");
  return getComputedStyle(node).color;
}

class Particles {
  constructor(host) {
    this.dpr = Math.min(2, window.devicePixelRatio || 1);
    this.canvas = document.createElement("canvas");
    this.canvas.className = "fx-canvas";
    this.canvas.width = Math.round(innerWidth * this.dpr);
    this.canvas.height = Math.round(innerHeight * this.dpr);
    host.append(this.canvas);
    // A transformed ancestor moves a fixed canvas: draw relative to where it really is.
    const r = this.canvas.getBoundingClientRect();
    this.ox = r.left; this.oy = r.top;
    this.ctx = this.canvas.getContext("2d");
    this.list = [];
    this.raf = 0;
    this.last = 0;
    this.clock = 0;
    this.frame = this.frame.bind(this);
  }

  // Drifting dust from a struck line, born along a left-to-right sweep.
  dissolve(rect, color, delay) {
    const n = Math.min(MAX_PER_NODE, Math.max(6, Math.round(rect.width * rect.height * PER_PX)));
    for (let i = 0; i < n; i++) {
      const x = rect.left + Math.random() * rect.width;
      this.list.push({ fly: false, x, y: rect.top + Math.random() * rect.height,
        vx: 20 + Math.random() * 70, vy: -10 - Math.random() * 60, jx: (Math.random() - 0.5) * 60, jy: (Math.random() - 0.5) * 40,
        born: this.clock + delay + ((x - rect.left) / rect.width) * SWEEP_MS, life: 450 + Math.random() * 300,
        size: 1 + Math.random() * 1.2, color });
    }
  }

  // Dust flying in from a ring around a new line to points inside it.
  assemble(rect, color, delay) {
    const n = Math.min(MAX_PER_NODE, Math.max(6, Math.round(rect.width * rect.height * PER_PX)));
    for (let i = 0; i < n; i++) {
      const a = Math.random() * Math.PI * 2, d = 30 + Math.random() * 40;
      const tx = rect.left + Math.random() * rect.width, ty = rect.top + Math.random() * rect.height;
      this.list.push({ fly: true, sx: tx + Math.cos(a) * d, sy: ty + Math.sin(a) * d,
        tx, ty, born: this.clock + delay, life: FLY_MS, size: 1 + Math.random() * 1.2, color });
    }
  }

  start() { if (!this.raf) { this.last = 0; this.raf = requestAnimationFrame(this.frame); } }

  frame(ts) {
    const dt = this.last ? Math.min(50, ts - this.last) : 16;
    this.last = ts;
    this.clock += dt;
    const { ctx, dpr, list } = this;
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    for (let i = list.length - 1; i >= 0; i--) {
      const p = list[i];
      const age = this.clock - p.born;
      if (age < 0) continue;
      if (age >= p.life) { list[i] = list[list.length - 1]; list.pop(); continue; }
      const t = age / p.life;
      let x, y, alpha;
      if (p.fly) {
        const k = easeOutCubic(t);
        x = p.sx + (p.tx - p.sx) * k; y = p.sy + (p.ty - p.sy) * k;
        alpha = Math.min(1, t * 4, (p.life - age) / 120);
      } else {
        const s = age / 1000;
        x = p.x + p.vx * s + p.jx * s * s; y = p.y + p.vy * s + p.jy * s * s;
        alpha = 1 - easeOutQuad(t);
      }
      ctx.globalAlpha = alpha;
      ctx.fillStyle = p.color;
      ctx.fillRect((x - this.ox) * dpr, (y - this.oy) * dpr, p.size * dpr, p.size * dpr);
    }
    ctx.globalAlpha = 1;
    this.raf = list.length ? requestAnimationFrame(this.frame) : 0;
  }

  stop() { cancelAnimationFrame(this.raf); this.raf = 0; this.list.length = 0; this.canvas.remove(); }
}

export async function morph(body, html) {
  running.get(body)?.finish();
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const oldTexts = texts(body), newTexts = texts(tpl.content);
  const gone = [...body.querySelectorAll(UNITS)].filter((n) => n.textContent.trim() && !newTexts.has(n.textContent.trim()));
  const fresh = [...newTexts].filter((t) => !oldTexts.has(t));
  if (matchMedia("(prefers-reduced-motion: reduce)").matches || (!gone.length && !fresh.length)) {
    body.innerHTML = html;
    return;
  }

  const fx = new Particles(body.closest("dialog") ?? document.body);
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    fx.stop();
    if (body.innerHTML !== html) body.innerHTML = html;
    running.delete(body);
  };
  running.set(body, { finish });

  // 1. Strike: a line grows across every text line of each gone point.
  const struck = gone.map((node, i) => {
    const delay = Math.min(i, STRIKE_MAX - 1) * STRIKE_STAGGER;
    node.dataset.fx = "strike";
    const box = node.getBoundingClientRect();
    const rows = lines(node);
    for (const r of rows) {
      const bar = document.createElement("i");
      bar.className = "fx-strike";
      bar.style.cssText = `left:${r.left - box.left}px;top:${r.top - box.top + r.height / 2}px;width:${r.width}px`;
      node.append(bar);
      bar.animate([{ transform: "scaleX(0)" }, { transform: "scaleX(1)" }],
                  { duration: STRIKE_MS, delay, easing: "cubic-bezier(.2,.7,.2,1)", fill: "both" });
    }
    return { node, rows, delay, color: colorOf(node) };
  });
  await sleep(STRIKE_MS + Math.min(gone.length, STRIKE_MAX) * STRIKE_STAGGER);
  if (done) return;

  // 2. Dissolve: dust sweeps off each line while the point fades away.
  for (const { node, rows, delay, color } of struck) {
    for (const r of rows) fx.dissolve(r, color, delay);
    node.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateX(6px)" }],
                 { duration: FADE_MS, delay: delay + SWEEP_MS / 3, easing: "ease-out", fill: "forwards" });
  }
  fx.start();
  await sleep(SWEEP_MS / 3 + FADE_MS + Math.min(struck.length, STRIKE_MAX) * STRIKE_STAGGER);
  if (done) return;

  // 3. Assemble: the new markup, the new points fly in from dust.
  const keep = body.scrollTop;
  body.innerHTML = html;
  body.scrollTop = keep;
  const added = [...body.querySelectorAll(UNITS)].filter((n) => fresh.includes(n.textContent.trim()));
  added.forEach((node, i) => {
    const delay = Math.min(i, ASSEMBLE_MAX - 1) * ASSEMBLE_STAGGER;
    for (const r of lines(node)) fx.assemble(r, colorOf(node), delay);
    node.animate([{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }],
                 { duration: 220, delay: delay + FLY_MS - 120, easing: "ease-out", fill: "backwards" });
  });
  fx.start();
  await sleep(FLY_MS + 220 + Math.min(added.length, ASSEMBLE_MAX) * ASSEMBLE_STAGGER);
  finish();
}
