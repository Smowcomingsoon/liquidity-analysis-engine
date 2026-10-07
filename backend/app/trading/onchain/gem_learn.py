"""Which part of the Gem Finder's score predicts, measured on its own receipts.

Lorenzo, 30/09 ("applica tutto"): the board's weights were written by hand
and never checked against what happened. gem_track freezes, for every
project the board ranked, studied or (a sample) rejected, each input of the
score and follows the price for 90 days. Here, with no model calls:

- `report()`: per horizon and per input, the rank correlation with the return
  and the AUC on "went up", over every case that has a return. It says which
  pillar carries information and which is noise.
- `train()`: a small L2 logistic regression (outcomes.py's, same deterministic
  folds) on "doubled before it halved". It becomes a *candidate* only above
  the same bars as the meme model and only if it beats the hand score out of
  sample on the same cases.
- A candidate never changes the board by itself. It is used only once a copy
  is installed as `data/gem_model_approved.json` with `approved: true` and a
  reviewer, and its hash matches its content (as quant_approved.json for the
  pilot). Retraining never touches the approved copy.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import time

import numpy as np

from app.atomic_write import write_text_atomic
from app.trading.onchain import gem_track, outcomes
from app.trading.onchain.storage import data_file

logger = logging.getLogger(__name__)

CANDIDATE_FILE = data_file("gem_model_candidate.json")
APPROVED_FILE = data_file("gem_model_approved.json")
REPORT_FILE = data_file("gem_learn_report.json")

# 07/10: DREGG's likeness and the narrative bonus are labels, not points, so
# the learned model no longer reads them (the report still measures them);
# the usage blocks take their place. A model approved on the old keys stops
# applying (approved() checks the key list).
FEATURES = ("quality", "pillar_github", "pillar_site", "pillar_tokenomics", "closed_source",
            "traction", "value", "judge", "judge_confidence", "code", "review",
            "block_usage", "block_participation", "mcap", "liq", "age_d")
REPORT_KEYS = ("score",) + FEATURES + ("similarity", "narrative_bonus")
HORIZONS_D = (1, 3, 7, 30)
MIN_N = 40                   # same bars as the meme winners model (outcomes.WIN_*)
MIN_POS = 8
MIN_AUC = 0.60
MIN_LIFT = 0.03              # must beat the hand score by this much out of sample
LEARNED_WEIGHT = 0.3         # share of an approved model in the final score
REFRESH_S = 6 * 3600

_state: dict = {"at": 0.0, "report": None, "candidate": None}
_approved: dict = {"mtime": None, "model": None, "hash": None}


def fingerprint(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
                          .encode("utf-8")).hexdigest()


def reset_for_tests() -> None:
    _state.update(at=0.0, report=None, candidate=None)
    _approved.update(mtime=None, model=None, hash=None)


def _rows(case: dict) -> dict | None:
    parts = case.get("parts")
    if not isinstance(parts, dict):
        return None
    return {**parts, "score": case.get("score")}


def label(case: dict) -> bool | None:
    """Doubled before it halved: yes. Halved first, or a month without either: no."""
    if case.get("first") == "double":
        return True
    if case.get("first") == "halve":
        return False
    if "30" in (case.get("returns") or {}) or case.get("closed"):
        return False
    return None


def _rank(v: list[float]) -> np.ndarray:
    a = np.asarray(v, dtype=float)
    order = a.argsort(kind="mergesort")
    r = np.empty(len(a))
    r[order] = np.arange(len(a), dtype=float)
    for x in np.unique(a):          # ties share their mean rank
        m = a == x
        if m.sum() > 1:
            r[m] = r[m].mean()
    return r


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx, ry = _rank(x), _rank(y)
    if rx.std() == 0 or ry.std() == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def report(cases: list[dict] | None = None) -> dict:
    cases = list(gem_track._track.values()) if cases is None else cases
    out = {"at": time.time(), "horizons": []}
    for d in HORIZONS_D:
        done = [(r, c["returns"][str(d)]) for c in cases
                if (r := _rows(c)) is not None and str(d) in (c.get("returns") or {})]
        row = {"days": d, "n": len(done), "inputs": []}
        for k in REPORT_KEYS:
            pairs = [(v, ret) for r, ret in done if (v := _num(r.get(k))) is not None]
            if len(pairs) < 5:
                continue
            xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
            ic, a = spearman(xs, ys), outcomes.auc(xs, [y > 0 for y in ys])
            row["inputs"].append({"key": k, "n": len(pairs), "ic": round(ic, 3) if ic is not None else None,
                                  "auc_up": round(a, 3) if a is not None else None})
        row["inputs"].sort(key=lambda i: -abs(i["ic"] or 0))
        out["horizons"].append(row)
    return out


def train(cases: list[dict] | None = None) -> dict:
    cases = list(gem_track._track.values()) if cases is None else cases
    data = sorted(((r, y, c["at"]) for c in cases
                   if (r := _rows(c)) is not None and (y := label(c)) is not None), key=lambda t: t[2])
    n, pos = len(data), sum(y for _, y, _ in data)
    out = {"at": time.time(), "n": n, "pos": pos, "keys": list(FEATURES), "ready": False,
           "cv_auc": None, "baseline_auc": None, "why": ""}
    if n < MIN_N or pos < MIN_POS or n - pos < MIN_POS:
        out["why"] = (f"needs {MIN_N} finished cases with at least {MIN_POS} that doubled and {MIN_POS} that did not "
                      f"(now {n}: {pos} doubled)")
        return out
    rows, labels = [r for r, _, _ in data], [y for _, y, _ in data]
    cv, w, means = outcomes._cv_fit(rows, labels, FEATURES)
    base = outcomes.auc([_num(r.get("score")) or 0.0 for r in rows], labels)
    out.update(cv_auc=round(cv, 3) if cv is not None else None,
               baseline_auc=round(base, 3) if base is not None else None,
               weights=w.tolist(), means={k: list(v) for k, v in means.items()})
    if cv is None or cv < MIN_AUC:
        out["why"] = f"out of sample AUC {cv or 0:.2f}, needs {MIN_AUC}"
    elif base is not None and cv < base + MIN_LIFT:
        out["why"] = f"out of sample AUC {cv:.2f} does not beat the hand score ({base:.2f})"
    else:
        out.update(ready=True, why=f"AUC {cv:.2f} against {base or 0:.2f} for the hand score, over {n} cases")
    return out


def _model_body(m: dict) -> dict:
    return {k: m.get(k) for k in ("keys", "weights", "means", "n", "pos", "cv_auc", "baseline_auc", "at")}


def approved() -> dict | None:
    """The installed model, if its file says approved, names a reviewer and
    its hash matches its content. Anything else: hand weights only."""
    try:
        mtime = APPROVED_FILE.stat().st_mtime
    except OSError:
        _approved.update(mtime=None, model=None, hash=None)
        return None
    if mtime == _approved["mtime"]:
        return _approved["model"]
    _approved.update(mtime=mtime, model=None, hash=None)
    try:
        doc = json.loads(APPROVED_FILE.read_text(encoding="utf-8"))
        body = _model_body(doc["model"])
        if doc.get("approved") is not True or not doc.get("reviewer") or doc.get("hash") != fingerprint(body):
            return None
        if list(body["keys"]) != list(FEATURES) or len(body["weights"]) != 2 * len(FEATURES) + 2:
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    _approved.update(model=body, hash=doc["hash"])
    return body


def approved_hash() -> str | None:
    return _approved["hash"] if approved() else None


def chance(parts: dict) -> float | None:
    """Chance of doubling before halving, from the approved model only."""
    m = approved()
    if not m:
        return None
    means = {k: tuple(v) for k, v in m["means"].items()}
    x, _ = outcomes._matrix([parts], means, tuple(m["keys"]))
    return float(outcomes._predict(np.array(m["weights"]), x)[0])


def refresh(now: float | None = None) -> None:
    now = now or time.time()
    if now - _state["at"] < REFRESH_S:
        return
    _state["at"] = now
    rep, cand = report(), train()
    if cand.get("weights"):
        cand["hash"] = fingerprint(_model_body(cand))
    _state.update(report=rep, candidate=cand)
    try:
        write_text_atomic(REPORT_FILE, json.dumps(rep), encoding="utf-8")
        write_text_atomic(CANDIDATE_FILE, json.dumps({"model": cand, "hash": cand.get("hash")}), encoding="utf-8")
    except OSError:
        logger.warning("pagella gemme: file non scritti")


def status() -> dict:
    cand = _state["candidate"] or {}
    rep = _state["report"] or {}
    return {"at": _state["at"] or None, "approved": approved_hash(),
            "candidate": {k: cand.get(k) for k in ("n", "pos", "ready", "cv_auc", "baseline_auc", "why", "hash")},
            "horizons": [{"days": h["days"], "n": h["n"], "best": h["inputs"][:5]} for h in rep.get("horizons", [])]}
