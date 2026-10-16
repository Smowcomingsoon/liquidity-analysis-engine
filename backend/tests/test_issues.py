"""Test della bacheca segnalazioni.

Il punto delicato è la separazione fra i due lati che scrivono: le segnalazioni
arrivano dall'app e vivono sul volume, le risoluzioni arrivano dal repo col
deploy. Se quella separazione si rompe, un deploy cancella le segnalazioni o
una segnalazione sovrascrive una risoluzione — e in entrambi i casi il danno è
silenzioso.
"""

from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Modulo issues isolato su file temporanei."""
    from app import issues as mod

    monkeypatch.setattr(mod, "_STORE_FILE", tmp_path / "issues.json")
    monkeypatch.setattr(mod, "_RESOLUTIONS_FILE", tmp_path / "resolutions.json")
    mod._issues.clear()
    return mod


def _write_resolutions(mod, data: dict) -> None:
    mod._RESOLUTIONS_FILE.write_text(json.dumps(data), encoding="utf-8")


# --- scrittura e lettura -----------------------------------------------------


def test_add_and_list(store):
    store.add_issue("Grafico rotto", "succede sul 4h", "alta", "tizio@x.it")
    items = store.list_issues()
    assert len(items) == 1
    assert items[0]["title"] == "Grafico rotto"
    assert items[0]["priority"] == "alta"
    assert items[0]["status"] == "aperto"
    assert items[0]["author"] == "tizio@x.it"


def test_empty_title_is_refused(store):
    with pytest.raises(ValueError):
        store.add_issue("   ")


def test_unknown_priority_falls_back(store):
    assert store.add_issue("x", priority="urgentissima")["priority"] == "media"


def test_ids_are_unique(store):
    ids = {store.add_issue(f"bug {i}")["id"] for i in range(30)}
    assert len(ids) == 30


def test_newest_first(store):
    a = store.add_issue("primo")
    a["created_at"] = 1000
    b = store.add_issue("secondo")
    b["created_at"] = 2000
    assert [i["title"] for i in store.list_issues()] == ["secondo", "primo"]


def test_update_and_delete(store):
    iid = store.add_issue("bug")["id"]
    assert store.update_issue(iid, status="risolto")["status"] == "risolto"
    with pytest.raises(ValueError):
        store.update_issue(iid, status="boh")
    assert store.update_issue("inesistente", status="risolto") is None
    assert store.delete_issue(iid) is True
    assert store.delete_issue(iid) is False


def test_survives_a_reload(store, tmp_path):
    store.add_issue("persiste")
    store._issues.clear()
    store._load()
    assert [i["title"] for i in store.list_issues()] == ["persiste"]


def test_cap_drops_the_oldest(store, monkeypatch):
    monkeypatch.setattr(store, "_MAX_ISSUES", 5)
    for i in range(9):
        store.add_issue(f"bug {i}")
    titles = {i["title"] for i in store.list_issues()}
    assert len(titles) == 5
    assert "bug 8" in titles and "bug 0" not in titles


# --- i due lati che scrivono -------------------------------------------------


def test_resolution_is_attached_by_id(store):
    iid = store.add_issue("bug")["id"]
    _write_resolutions(store, {iid: {"summary": "trovato", "status": "risolto"}})
    it = store.list_issues()[0]
    assert it["resolution"]["summary"] == "trovato"


def test_resolution_status_wins(store):
    """La risoluzione è la fonte di verità sullo stato: altrimenti una voce
    resta 'aperta' solo perché nessuno ha aggiornato il campo a mano."""
    iid = store.add_issue("bug")["id"]
    assert store.list_issues()[0]["status"] == "aperto"
    _write_resolutions(store, {iid: {"summary": "fatto", "status": "risolto"}})
    assert store.list_issues()[0]["status"] == "risolto"


def test_resolution_never_touches_the_stored_issue(store):
    """Il file delle segnalazioni non deve cambiare per effetto di un deploy:
    l'unione avviene solo in lettura."""
    iid = store.add_issue("bug")["id"]
    _write_resolutions(store, {iid: {"summary": "x", "status": "risolto"}})
    store.list_issues()
    raw = json.loads(store._STORE_FILE.read_text(encoding="utf-8"))
    assert raw[0]["status"] == "aperto"
    assert "resolution" not in raw[0]


def test_resolutions_are_reread_each_time(store):
    """Il file arriva col deploy; un processo che gli sopravvive servirebbe
    altrimenti le risoluzioni con cui era partito."""
    iid = store.add_issue("bug")["id"]
    assert "resolution" not in store.list_issues()[0]
    _write_resolutions(store, {iid: {"summary": "arrivata dopo"}})
    assert store.list_issues()[0]["resolution"]["summary"] == "arrivata dopo"


def test_missing_or_broken_resolutions_do_not_break_listing(store):
    store.add_issue("bug")
    assert len(store.list_issues()) == 1          # file assente
    store._RESOLUTIONS_FILE.write_text("{ non json", encoding="utf-8")
    assert len(store.list_issues()) == 1          # file illeggibile
    _write_resolutions(store, {"id-che-non-esiste": {"summary": "orfana"}})
    assert "resolution" not in store.list_issues()[0]


def test_shipped_resolutions_file_is_valid():
    """Il file vero nel repo: se è JSON rotto, in produzione ogni risoluzione
    sparisce senza errore, perché la lettura fallisce in silenzio."""
    from app import issues as mod

    importlib.reload(mod)
    path = mod._RESOLUTIONS_FILE
    assert path.exists(), f"manca {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    for key, val in data.items():
        if key.startswith("_"):
            continue
        assert isinstance(val, dict), f"{key} non è un oggetto"
        assert val.get("summary"), f"{key} senza summary"


def test_seed_resolutions_point_at_a_real_seed_entry():
    """Le risoluzioni si agganciano per id: un id `seed-` che non esiste fra le
    voci iniziali è una diagnosi scritta che nessuno vedrà mai."""
    from app import issues as mod

    importlib.reload(mod)
    seed_ids = {s[0] for s in mod._SEED}
    res_ids = {k for k in json.loads(
        mod._RESOLUTIONS_FILE.read_text(encoding="utf-8")) if not k.startswith("_")}
    orphans = {k for k in res_ids if k.startswith("seed-")} - seed_ids
    assert not orphans, f"risoluzioni senza voce corrispondente: {orphans}"


def test_non_seed_resolutions_use_a_server_generated_id():
    """Le altre risposte a segnalazioni scritte dall'app: il loro id vive sul
    volume, quindi da qui non si può verificare che esista.

    Quello che si può verificare è la *forma*: `add_issue` genera otto caratteri
    esadecimali. Un id inventato o storpiato non aggancia nulla e la diagnosi
    resta invisibile — che è lo stesso guasto del test sopra, per l'altra metà
    della bacheca.
    """
    import re

    from app import issues as mod

    importlib.reload(mod)
    res_ids = {k for k in json.loads(
        mod._RESOLUTIONS_FILE.read_text(encoding="utf-8")) if not k.startswith("_")}
    others = {k for k in res_ids if not k.startswith("seed-")}
    bad = {k for k in others if not re.fullmatch(r"[0-9a-f]{8}", k)}
    assert not bad, f"id che nessuna segnalazione può avere: {bad}"


# --- la lista iniziale --------------------------------------------------------
#
# Il guasto che questi test bloccano è arrivato davvero: `_seed` usciva subito
# se l'elenco non era vuoto, quindi seed-0011 — aggiunto alla lista dopo il
# primo avvio — dal vivo non è mai comparso, e la sua risoluzione, scritta e
# distribuita col deploy, restava senza voce a cui agganciarsi. Il test sopra
# non poteva accorgersene: confronta le due liste nel sorgente, non ciò che
# c'è sul volume.


@pytest.fixture()
def seeding(tmp_path, monkeypatch):
    from app import issues as mod

    monkeypatch.setattr(mod, "_STORE_FILE", tmp_path / "issues.json")
    monkeypatch.setattr(mod, "_SEEDED_FILE", tmp_path / "seeded.json")
    monkeypatch.setattr(mod, "_RESOLUTIONS_FILE", tmp_path / "resolutions.json")
    mod._issues.clear()
    mod._seeded_ids.clear()
    return mod


def _ids(mod) -> set[str]:
    return {it["id"] for it in mod._issues}


def test_seeding_from_scratch_inserts_everything(seeding):
    seeding._seed()
    assert _ids(seeding) == {s[0] for s in seeding._SEED}


def test_a_new_seed_entry_appears_on_a_store_that_already_exists(seeding):
    """Il caso reale: volume già popolato, e una voce nuova nel sorgente."""
    mancante = seeding._SEED[-1][0]
    seeding._issues.extend(
        {"id": s[0], "title": s[1], "detail": s[2], "priority": s[3],
         "status": "aperto", "author": "lista iniziale", "created_at": 1}
        for s in seeding._SEED[:-1]
    )
    assert mancante not in _ids(seeding)

    seeding._seed()
    assert mancante in _ids(seeding), "una voce aggiunta dopo il primo avvio non compare mai"


def test_seeding_twice_does_not_duplicate(seeding):
    seeding._seed()
    seeding._seed()
    assert len(seeding._issues) == len(seeding._SEED)


def test_a_deleted_seed_entry_stays_deleted(seeding):
    """L'altra metà: inserire per id non deve resuscitare ciò che è stato
    buttato via a mano. È la ragione per cui esiste `seeded_ids.json`."""
    seeding._seed()
    vittima = seeding._SEED[0][0]
    assert seeding.delete_issue(vittima)

    seeding._seed()
    assert vittima not in _ids(seeding)


def test_the_record_of_what_was_seeded_survives_a_restart(seeding):
    seeding._seed()
    seeding.delete_issue(seeding._SEED[0][0])

    seeding._issues.clear()          # come un riavvio
    seeding._seeded_ids.clear()
    seeding._load()
    seeding._load_seeded()
    seeding._seed()

    assert seeding._SEED[0][0] not in _ids(seeding)
    assert len(seeding._issues) == len(seeding._SEED) - 1


def test_seeding_does_not_touch_reports_written_from_the_app(seeding):
    seeding._seed()
    mia = seeding.add_issue("segnalazione mia", author="io@x.it")
    seeding._seed()
    assert mia["id"] in _ids(seeding)


def test_edits_to_a_seed_entry_are_not_overwritten(seeding):
    seeding._seed()
    iid = seeding._SEED[0][0]
    seeding.update_issue(iid, status="in corso", detail="con dettagli aggiunti")
    seeding._seed()
    voce = next(it for it in seeding._issues if it["id"] == iid)
    assert voce["status"] == "in corso"
    assert voce["detail"] == "con dettagli aggiunti"


# --- risposte -------------------------------------------------------------
#
# "Risolto" lo decide chi ha scritto la segnalazione, non chi ha scritto la
# correzione: senza un modo di rispondere, una voce chiusa per decreto resta
# chiusa anche se il problema si vede ancora.


@pytest.fixture()
def board(tmp_path, monkeypatch):
    from app import issues as mod

    monkeypatch.setattr(mod, "_STORE_FILE", tmp_path / "issues.json")
    monkeypatch.setattr(mod, "_SEEDED_FILE", tmp_path / "seeded.json")
    monkeypatch.setattr(mod, "_RESOLUTIONS_FILE", tmp_path / "res.json")
    monkeypatch.setattr(mod, "_REPLIES_FILE", tmp_path / "replies.json")
    monkeypatch.setattr(mod, "_CLAUDE_REPLIES_FILE", tmp_path / "claude.json")
    mod._issues.clear()
    mod._replies.clear()
    mod._seeded_ids.clear()
    return mod


def test_a_reply_lands_on_its_issue(board):
    iid = board.add_issue("bug")["id"]
    board.add_reply(iid, "succede ancora", author="io@x.it")
    it = board.list_issues()[0]
    assert [r["text"] for r in it["replies"]] == ["succede ancora"]
    assert it["replies"][0]["side"] == "loro"


def test_an_empty_reply_is_refused(board):
    iid = board.add_issue("bug")["id"]
    with pytest.raises(ValueError):
        board.add_reply(iid, "   ")


def test_replies_survive_a_reload(board):
    iid = board.add_issue("bug")["id"]
    board.add_reply(iid, "ancora")
    board._replies.clear()
    board._load_replies()
    assert len(board.list_issues()[0]["replies"]) == 1


def test_the_two_sides_are_merged_in_time_order(board):
    """Le risposte di Claude arrivano col deploy, le vostre dal volume: nessuna
    delle due parti può riscrivere le parole dell'altra."""
    iid = board.add_issue("bug")["id"]
    board.add_reply(iid, "seconda")
    board._replies[-1]["created_at"] = 200
    _write_resolutions(board, {})
    board._CLAUDE_REPLIES_FILE.write_text(
        json.dumps({iid: [{"text": "prima", "created_at": 100}]}), encoding="utf-8")
    thread = board.list_issues()[0]["replies"]
    assert [r["text"] for r in thread] == ["prima", "seconda"]
    assert [r["side"] for r in thread] == ["claude", "loro"]


def test_reopening_beats_a_resolution(board):
    """Il caso che conta: Claude l'ha segnata risolta, il problema si vede
    ancora."""
    iid = board.add_issue("bug")["id"]
    _write_resolutions(board, {iid: {"summary": "fatto", "status": "risolto"}})
    assert board.list_issues()[0]["status"] == "risolto"
    board.add_reply(iid, "non è risolto", reopen=True)
    assert board.list_issues()[0]["status"] == "riaperto"


def test_a_plain_reply_does_not_reopen(board):
    iid = board.add_issue("bug")["id"]
    _write_resolutions(board, {iid: {"summary": "fatto", "status": "risolto"}})
    board.add_reply(iid, "grazie")
    assert board.list_issues()[0]["status"] == "risolto"


def test_a_newer_resolution_closes_a_reopened_issue(board):
    """Altrimenti una voce riaperta resterebbe rossa per sempre, anche dopo la
    correzione vera."""
    iid = board.add_issue("bug")["id"]
    board.add_reply(iid, "non è risolto", reopen=True)
    later = board._replies[-1]["created_at"] + 10
    _write_resolutions(board, {iid: {"summary": "ora sì", "status": "risolto", "at": later}})
    assert board.list_issues()[0]["status"] == "risolto"


def test_a_resolution_without_a_date_never_wins_over_a_reopen(board):
    iid = board.add_issue("bug")["id"]
    board.add_reply(iid, "no", reopen=True)
    _write_resolutions(board, {iid: {"summary": "vecchia", "status": "risolto"}})
    assert board.list_issues()[0]["status"] == "riaperto"


def test_closing_a_reopened_issue_by_hand_sticks(board, monkeypatch):
    """1f8f7246: "Close manually" su una voce riaperta deve chiuderla."""
    iid = board.add_issue("bug")["id"]
    _write_resolutions(board, {iid: {"summary": "fatto", "status": "risolto", "at": 100}})
    board.add_reply(iid, "non è risolto", reopen=True)
    assert board.list_issues()[0]["status"] == "riaperto"
    later = board._replies[-1]["created_at"] + 5
    monkeypatch.setattr(board.time, "time", lambda: later)
    board.update_issue(iid, status="risolto")
    assert board.list_issues()[0]["status"] == "risolto"
    board.update_issue(iid, status="aperto")
    assert board.list_issues()[0]["status"] == "riaperto"


def test_public_board_masks_author_emails(store):
    import asyncio

    from app.routes import issues as route

    store.add_issue(title="t", author="someone@mail.example")
    store.add_reply(store.list_issues()[0]["id"], "ok", author="other@mail.example")
    out = asyncio.run(route.list_issues(user=None))
    text = str(out)
    assert "@" not in text.replace("DeepSeek", "")
    assert out["items"][0]["author"] == "so•••"
    assert out["items"][0]["replies"][0]["author"] == "ot•••"
