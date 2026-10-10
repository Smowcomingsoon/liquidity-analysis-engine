// Elliott Waves for the Levels panel (segnalazione 7f9cd5ed).
//
// Rules from the reference indicator's description (LuxAlgo, TradingView
// KvrhsPTp), written from scratch here:
// - pivots: a high higher than the `len` highs before it and the bar after
//   it (lows mirrored); consecutive pivots of the same side collapse into the
//   more extreme one, giving a zigzag;
// - motive wave: six alternating zigzag points 0-5 in the trend direction,
//   where wave 2 does not retrace past the start of wave 1, wave 4 does not
//   enter wave 1's territory, wave 3 extends past wave 1 and wave 5 past
//   wave 3, and wave 3 is not the shortest of 1, 3 and 5;
// - corrective wave: the three zigzag points after a motive (a, b, c), b not
//   past the motive's end and c past a; it is invalid once c breaks the
//   motive's start;
// - Fibonacci retracements (0.5, 0.618, 0.764, 0.854) of the latest motive,
//   where the correction may bounce.
// Classic script: sets globalThis.ElliottWave, and module.exports for tests.
(function (root) {
  "use strict";

  const FIB_RATIOS = [0.5, 0.618, 0.764, 0.854];

  function zigzag(candles, len) {
    const pts = [];
    const push = (p) => {
      const last = pts[pts.length - 1];
      if (last && last.side === p.side) {
        const better = p.side === "high" ? p.price > last.price : p.price < last.price;
        if (better) pts[pts.length - 1] = p;
        return;
      }
      pts.push(p);
    };
    for (let i = len; i < candles.length - 1; i++) {
      const c = candles[i], next = candles[i + 1];
      let isHigh = next.high < c.high, isLow = next.low > c.low;
      for (let j = i - len; j < i && (isHigh || isLow); j++) {
        if (candles[j].high >= c.high) isHigh = false;
        if (candles[j].low <= c.low) isLow = false;
      }
      // An outside bar can be both: keep the side that continues the zigzag.
      if (isHigh && isLow) {
        const last = pts[pts.length - 1];
        if (last && last.side === "high") isHigh = false;
        else isLow = false;
      }
      if (isHigh) push({ i, time: c.time, price: c.high, side: "high" });
      if (isLow) push({ i, time: c.time, price: c.low, side: "low" });
    }
    return pts;
  }

  function isMotive(p, up) {
    const s = up ? 1 : -1;
    const v = p.map((x) => x.price * s);
    if (!(v[1] > v[0] && v[2] > v[0] && v[2] < v[1])) return false;
    if (!(v[3] > v[1] && v[4] > v[1] && v[4] < v[3] && v[5] > v[3])) return false;
    const w1 = v[1] - v[0], w3 = v[3] - v[2], w5 = v[5] - v[4];
    return !(w3 < w1 && w3 < w5);
  }

  function detect(candles, len) {
    const out = { motives: [], corrections: [], fib: null };
    if (!Array.isArray(candles) || candles.length < 2 * len + 6) return out;
    const z = zigzag(candles, len);
    const strip = ({ time, price }) => ({ time, price });
    let k = 5;
    while (k < z.length) {
      const p = z.slice(k - 5, k + 1);
      const up = p[0].side === "low";
      if (!isMotive(p, up)) { k += 1; continue; }
      out.motives.push({ dir: up ? "bull" : "bear", points: p.map(strip), tentative: k === z.length - 1 });
      const abc = z.slice(k + 1, k + 4);
      if (abc.length === 3) {
        const s = up ? 1 : -1;
        const [a, b, c] = abc.map((x) => x.price * s);
        if (a < p[5].price * s && b > a && b < p[5].price * s && c < a) {
          out.corrections.push({
            dir: up ? "bull" : "bear",
            points: [p[5], ...abc].map(strip),
            invalid: c < p[0].price * s,
            tentative: k + 3 === z.length - 1,
          });
          k += 3;
        }
      }
      k += 5;
    }
    const last = out.motives[out.motives.length - 1];
    if (last) {
      const start = last.points[0].price, end = last.points[5].price;
      const levels = {};
      FIB_RATIOS.forEach((r) => { levels[r] = end - (end - start) * r; });
      out.fib = { dir: last.dir, time: last.points[5].time, levels };
    }
    return out;
  }

  const api = { detect, zigzag, FIB_RATIOS };
  root.ElliottWave = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
