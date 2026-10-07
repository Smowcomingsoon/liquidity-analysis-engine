"""Arenas: the Top by narrative, pound for pound, and the nightly ladder."""
from app.trading.onchain import arenas

CAT = {"ai_agents": {"key": "ai_agents", "label": "AI Agents", "kind": None, "heat": 3},
       "privacy": {"key": "privacy", "label": "Privacy", "kind": None, "heat": 3},
       "rwa": {"key": "rwa", "label": "RWA", "kind": None, "heat": 2},
       "meme_dogs": {"key": "meme_dogs", "label": "Dog Memes", "kind": "meme", "heat": 3}}


def _e(sym, score, narr=(), chain="solana", blocks=None):
    return {"chain": chain, "token_address": sym, "symbol": sym, "score": score,
            "narratives": list(narr), "blocks": blocks}


def test_candidates_skip_memes_and_route_layers():
    assert arenas.candidates(_e("A", 50, ["meme_dogs", "privacy"]), CAT) == ["privacy"]
    assert arenas.candidates(_e("B", 50, ["meme_dogs"]), CAT) == [arenas.OTHER]
    assert arenas.candidates(_e("C", 50, ["privacy"], chain="layer"), CAT) == [arenas.CHAINS]
    assert arenas.candidates(_e("D", 50, [{"key": "rwa", "label": "RWA"}]), CAT) == ["rwa"]


def test_project_goes_where_it_leads_its_peers_most():
    es = [_e("X", 80, ["ai_agents", "privacy"]), _e("A1", 90, ["ai_agents"]), _e("A2", 88, ["ai_agents"]),
          _e("P1", 60, ["privacy"]), _e("P2", 55, ["privacy"])]
    where = arenas.assign(es, CAT)
    assert where["solana:X"] == "privacy"      # first among weak peers beats third among strong ones


def test_lonely_arena_is_dropped():
    es = [_e("R", 70, ["rwa", "privacy"]), _e("P1", 60, ["privacy"]), _e("P2", 50, ["privacy"]),
          _e("Z", 40, ["rwa"])]
    es.pop()                                   # RWA would hold R alone
    where = arenas.assign(es, CAT)
    assert where["solana:R"] == "privacy"
    lone = arenas.assign([_e("Q", 50, ["rwa"]), _e("P1", 60, ["privacy"]), _e("P2", 50, ["privacy"])], CAT)
    assert lone["solana:Q"] == arenas.OTHER


def test_build_marks_bench_and_pound_for_pound(monkeypatch):
    monkeypatch.setattr(arenas, "_catalog", lambda: CAT)
    es = [_e(f"A{i}", s, ["ai_agents"]) for i, s in enumerate([90, 70, 65, 60])] + \
         [_e(f"P{i}", s, ["privacy"]) for i, s in enumerate([62, 61, 60])]
    out = arenas.build(es, ladder={})
    ai = next(a for a in out["arenas"] if a["key"] == "ai_agents")
    assert [e["symbol"] for e in ai["entries"]] == ["A0", "A1", "A2", "A3"]
    assert [e["in"] for e in ai["entries"]] == [True, True, False, False]
    assert out["p4p"][0]["symbol"] == "A0" and out["p4p"][0]["arena_label"] == "AI Agents"
    # Three near-equal scores: the spread floor keeps the lead small.
    assert next(e for e in out["p4p"] if e["symbol"] == "P0")["z"] <= 0.4
    assert out["ladder"] == {"nights": 0, "ready": False}


def test_duel_counts_blocks_both_measured():
    a = {"blocks": {"usage": 50, "product": 80, "token": None}}
    b = {"blocks": {"usage": 60, "product": 70, "token": 90}}
    assert arenas.duel(a, b) == 0.5
    assert arenas.duel({"blocks": {"product": 90}}, b) == 1.0
    assert arenas.duel({"blocks": {}}, b) is None


def test_record_night_moves_ratings_once_per_night(monkeypatch):
    monkeypatch.setattr(arenas, "_catalog", lambda: CAT)
    strong = {k: 80 for k in arenas.BLOCKS}
    weak = {k: 40 for k in arenas.BLOCKS}
    es = [_e("S", 80, ["ai_agents"], blocks=strong), _e("W", 40, ["ai_agents"], blocks=weak)]
    state = {}
    assert arenas.record_night(es, now=1e6, state=state) == 1
    r = state["ratings"]
    assert r["solana:S"]["r"] > arenas.RATING_START > r["solana:W"]["r"]
    assert r["solana:S"]["nights"] == ["w"] and r["solana:W"]["nights"] == ["l"]
    assert arenas.record_night(es, now=1e6 + 3600, state=state) == 0     # same evening
    assert arenas.record_night(es, now=1e6 + 86400, state=state) == 1
    assert state["nights"] == 2


def test_privacy_holds_zk_and_tee_and_comes_first(monkeypatch):
    cat = {**CAT, "zero_knowledge": {"key": "zero_knowledge", "label": "Zero Knowledge", "kind": None, "heat": 3},
           "fhe_mpc_tee": {"key": "fhe_mpc_tee", "label": "FHE / MPC / TEE", "kind": None, "heat": 3}}
    monkeypatch.setattr(arenas, "_catalog", lambda: cat)
    es = [_e("DREGG", 70, ["zero_knowledge"]), _e("ROUTER", 75, ["fhe_mpc_tee", "ai_agents"]),
          _e("A1", 90, ["ai_agents"]), _e("A2", 88, ["ai_agents"]), _e("A3", 95, ["ai_agents"])]
    out = arenas.build(es, ladder={})
    assert out["arenas"][0]["key"] == "privacy"
    assert {e["symbol"] for e in out["arenas"][0]["entries"]} == {"DREGG", "ROUTER"}


def test_filter_keeps_projects_in_the_arena_asked(monkeypatch):
    monkeypatch.setattr(arenas, "_catalog", lambda: CAT)
    es = [_e("X", 95, ["ai_agents", "privacy"]), _e("P1", 60, ["privacy"]), _e("P2", 50, ["privacy"])]
    out = arenas.build(es, ladder={}, prefer={"privacy"})
    assert [a["key"] for a in out["arenas"]] == ["privacy"]
