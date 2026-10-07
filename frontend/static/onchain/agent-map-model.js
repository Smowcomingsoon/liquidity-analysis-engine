/** Read-only projection of the legacy activity feed. No inferred causality. */
export const NODES = [
  { id: "scan", label: "Discovery", kinds: ["scan", "scanner", "manual"], role: "Find and check candidate tokens.", next: "Candidate checks and project research." },
  { id: "study", label: "Project study", kinds: ["study"], role: "Review GitHub, website and tokenomics pillars.", next: "Research and ranking evaluation." },
  { id: "research", label: "Research / judge", kinds: ["research", "judge", "review"], role: "Read project material and challenge the assessment.", next: "Validate findings before changing a decision." },
  { id: "code", label: "Code review", kinds: ["code"], role: "Examine repository evidence.", next: "Reuse or invalidate the review when its inputs change." },
  { id: "narratives", label: "Narratives", kinds: ["narratives"], role: "Associate projects with narrative categories.", next: "Update project context." },
  { id: "consistency", label: "Consistency", kinds: ["consistency"], role: "Look for contradictions in existing assessments.", next: "Confirm a finding before requesting another analysis." },
  { id: "memory", label: "Evidence / reuse", kinds: [], role: "Preserve evidence and reuse valid analysis.", next: "Connect versioned artifacts and dependency invalidation.", missing: "Artifact validity and reuse receipts are not exposed by this feed." },
  { id: "decision", label: "Decision", kinds: [], role: "Apply the ranking policy to available evidence.", next: "Connect the actual decision receipt and policy contributions.", missing: "A study note is not a ranking decision or an execution permission." },
  { id: "backtest", label: "Backtests", kinds: ["backtest"], role: "Evaluate strategies on historical data.", next: "Compare results while preserving historical data cutoffs." },
  { id: "outcomes", label: "Observed outcomes", kinds: [], role: "Compare an assessment with subsequent observations.", next: "Connect matured outcomes and data coverage.", missing: "Outcome labels and coverage are not exposed by this feed." },
  { id: "agent", label: "Agent objectives", kinds: [], role: "Prioritize open questions within a budget.", next: "Connect task objectives, priority reasons and stop conditions.", missing: "No structured objective or hypothesis is available in the current API." },
  { id: "other", label: "Other activity", kinds: null, role: "Keep activity without a mapped category visible.", next: "Classify new event kinds without losing their records." },
];
// Architecture edges, deliberately NOT receipt-backed traces.
export const EDGES = [["scan", "study"], ["study", "research"], ["study", "code"],
  ["research", "narratives"], ["research", "consistency"], ["code", "memory"],
  ["research", "memory"], ["memory", "decision"], ["consistency", "decision"],
  ["decision", "backtest"], ["backtest", "outcomes"], ["outcomes", "agent"],
  ["agent", "research"]];
// Trading off (Lorenzo, 07/10): no strategy backtests, decisions go straight to outcomes.
if (globalThis.document?.documentElement?.dataset.trading !== "on") {
  const n = NODES.findIndex((x) => x.id === "backtest");
  if (n >= 0) NODES.splice(n, 1);
  const kept = EDGES.filter(([a, b]) => a !== "backtest" && b !== "backtest");
  if (kept.length !== EDGES.length) EDGES.splice(0, EDGES.length, ...kept, ["decision", "outcomes"]);
}

const text = (v, max = 500) => typeof v === "string" ? v.slice(0, max) : "";
const number = (v) => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;

export function projectKey(row) {
  return typeof row?.chain === "string" && row.chain && typeof row.token === "string" && row.token
    ? JSON.stringify([row.chain, row.token]) : null;
}

export function normalizeEvents(items) {
  if (!Array.isArray(items)) return [];
  // Do not deduplicate by timestamp: distinct events may share one timestamp.
  return items.filter(e => e && typeof e === "object" && number(e.t) !== null)
    .map(e => ({ t: e.t, kind: text(e.kind, 80), text: text(e.text), outcome: text(e.outcome),
      key: projectKey(e), chain: text(e.chain, 80), token: text(e.token, 160),
      symbol: text(e.symbol, 80), steps: Array.isArray(e.steps) ? e.steps.slice(0, 8).map(s => text(s)).filter(Boolean) : [] }))
    .sort((a, b) => b.t - a.t).slice(0, 300);
}

export function projectsIn(items) {
  const map = new Map();
  for (const e of normalizeEvents(items)) if (e.key && !map.has(e.key)) {
    map.set(e.key, { key: e.key, label: `${e.symbol || e.token.slice(0, 12)} · ${e.chain} · ${e.token.slice(-6)}` });
  }
  return [...map.values()];
}

export function projectGraph({ items, info, key = null, receivedAt = null, failed = false, now = Date.now() }) {
  const events = normalizeEvents(items).filter(e => !key || e.key === key);
  const stale = failed || receivedAt === null || now - receivedAt > 45000;
  const known = new Set(NODES.flatMap(n => n.kinds || []));
  const nodes = NODES.map(n => {
    const matches = events.filter(e => n.kinds === null ? !known.has(e.kind) : n.kinds.includes(e.kind));
    return { ...n, events: matches.slice(0, 8), count: matches.length,
      status: n.kinds?.length === 0 ? "Not connected" : stale ? "Updates unavailable" : matches.length ? "Recorded activity" : "No event in loaded window" };
  });
  const values = info?.today && typeof info.today === "object" ? Object.values(info.today) : null;
  const calls = values && values.every(v => number(v) !== null) ? values.reduce((a, b) => a + b, 0) : null;
  return { nodes, edges: EDGES, stale, receivedAt, eventCount: events.length,
    // Global daily numbers must never appear as selected-project expenditure.
    usage: key ? null : { calls, tokens: number(info?.tokens), estimatedUsd: number(info?.spent_usd) } };
}

const KIND_NODE = new Map(NODES.flatMap(n => (n.kinds || []).map(k => [k, n.id])));
export const nodeFor = kind => KIND_NODE.get(kind) || "other";
export const eventId = e => `${e.t}|${e.kind}|${e.key || ""}|${e.text}`;

/** Most recent event that names a project: what the engine is looking at now. */
export function liveProject(items, now = Date.now()) {
  const e = normalizeEvents(items).find(x => x.key);
  if (!e) return null;
  return { key: e.key, symbol: e.symbol || e.token.slice(0, 12), chain: e.chain,
    node: nodeFor(e.kind), t: e.t, ageS: Math.max(0, Math.round(now / 1000 - e.t)) };
}

/** Recorded order of one project's events (oldest first), repeats of one node collapsed. */
export function cycleTrace(items, key, limit = 12) {
  const steps = [];
  for (const e of normalizeEvents(items).filter(x => x.key === key).reverse()) {
    const node = nodeFor(e.kind);
    if (steps.at(-1)?.node === node) steps[steps.length - 1] = { node, t: e.t, id: eventId(e) };
    else steps.push({ node, t: e.t, id: eventId(e) });
  }
  return steps.slice(-limit);
}

/** Events not seen before, oldest first. Marks them seen. */
export function freshEvents(seen, items) {
  const out = [];
  for (const e of normalizeEvents(items)) {
    const id = eventId(e);
    if (!seen.has(id)) { seen.add(id); out.push({ ...e, id, node: nodeFor(e.kind) }); }
  }
  return out.reverse();
}

/** Up to `limit` most recently active projects, each at the node of its latest recorded event. */
export function projectPositions(items, limit = 8) {
  const out = new Map();
  for (const e of normalizeEvents(items)) {
    if (!e.key || out.has(e.key)) continue;
    out.set(e.key, { key: e.key, symbol: e.symbol || e.token.slice(0, 6), node: nodeFor(e.kind), t: e.t });
    if (out.size >= limit) break;
  }
  return [...out.values()];
}

/** Recompute a journey's hash chain: id = sha256(record), link = sha256(prev + id).
 * `sha` is an async string -> hex digest. Returns the first broken step, if any. */
export async function verifyChain(journey, sha) {
  let link = journey?.genesis;
  const steps = Array.isArray(journey?.steps) ? journey.steps : [];
  if (typeof link !== "string") return { ok: false, brokenAt: 0, count: steps.length };
  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    if (typeof s?.record !== "string" || await sha(s.record) !== s.id) return { ok: false, brokenAt: i, count: steps.length };
    link = await sha(link + s.id);
    if (link !== s.link) return { ok: false, brokenAt: i, count: steps.length };
  }
  return { ok: link === journey.head, brokenAt: link === journey.head ? null : steps.length, count: steps.length, head: link };
}

export async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, "0")).join("");
}

/** Replay times proportional to the real gaps between steps: `speed` real seconds per screen second.
 * A pause longer than `capMs` on screen is shortened to `capMs` and marked. */
export function replaySchedule(steps, speed, { capMs = 3000, minMs = 80 } = {}) {
  let at = 0;
  return (steps || []).map((s, i) => {
    const gapS = i ? Math.max(0, s.t - steps[i - 1].t) : 0;
    const want = i ? gapS / speed * 1000 : 0;
    const capped = want > capMs;
    at += i ? Math.max(minMs, capped ? capMs : want) : 0;
    return { at, gapS, capped };
  });
}

export const SPEEDS = [[1, "Real time"], [60, "1 s = 1 min"], [600, "1 s = 10 min"], [3600, "1 s = 1 h"], [86400, "1 s = 1 day"]];

/** Slowest listed speed whose replay fits in `budgetMs` with at most a quarter of the pauses shortened. */
export function autoSpeed(steps, budgetMs = 60000, opts) {
  const gaps = Math.max(0, (steps || []).length - 1);
  for (const [speed] of SPEEDS) {
    const plan = replaySchedule(steps, speed, opts);
    const capped = plan.filter(p => p.capped).length;
    if (!plan.length || (plan.at(-1).at <= budgetMs && capped <= Math.ceil(gaps / 4))) return speed;
  }
  return SPEEDS.at(-1)[0];
}
