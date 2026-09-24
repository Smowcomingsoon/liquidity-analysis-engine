"""
La ricerca stabile disegnata, nel pannello del laboratorio.

Non c'e' un canale nuovo verso il server: l'animazione legge le stesse righe
che la ricerca gia' scrive nel log (say(...) in plateau.py e pipeline.py),
con le espressioni regolari incollate a quel testo esatto. Se una frase li'
cambia forma, questi test la notano prima che l'animazione smetta di capirla
in silenzio.
"""

from __future__ import annotations

import re
from pathlib import Path

_APP_JS = Path(__file__).resolve().parent.parent.parent / "frontend" / "static" / "app.js"
_PLATEAU_PY = Path(__file__).resolve().parent.parent / "app" / "analysis" / "plateau.py"


def _js() -> str:
    return _APP_JS.read_text(encoding="utf-8")


def _real_lines() -> list[str]:
    """Le frasi vere che plateau.py scrive, con dati plausibili al posto delle
    f-string — sono quelle che le regex di app.js devono saper leggere."""
    return [
        "mapping boundaries on 5 axes: where the result changes, not where the parameter changes",
        "axis rsi_length: 8 boundaries → 9 intervals (70 backtests)",
        "axis bb_mult: 1 boundary → 2 intervals (22 backtests)",  # singolare
        "axis len: still working — 12 backtests so far",
        "grid: 9 × 5 × 2 × 8 × 6 = 4320 cells",
        "grid: 9 × 5 = 45 cells · inert on this series: bb_mult",  # con la coda
        "4320 cells over the budget of 400: instead of sampling 400 I open them in order of promise",
        "trying the center of 1080 cells (all enumerated)",
        "cells 50 — 32 distinct configurations so far",
        "128 distinct configurations over 400 cells tried: the others produce exactly the same trades",
        "4 candidates with at least 8 trades; shaking 4 with 14 random shifts of ±5%",
        "finalist 1/4: holds 1 time out of 14 — +17.4% over 8 trades",
        "finalist 4/12: holds 10 times out of 14 — -12.4% over 8 trades",  # negativo, plurale
        "picked: +17.4% over 8 trades, stable 7% of the time",
        "picked: +164.6% over 10 trades, stable 43% of the time (the raw maximum did +198.4% but holds up less)",
    ]


def _extract_regexes(js: str) -> list[re.Pattern]:
    """Le stesse regex di plateauAnimParseLine, tradotte in Python. Tenerle
    duplicate qui e' il prezzo di non avere un motore JS in questo progetto —
    vedi la convenzione gia' in uso per le altre verifiche su app.js."""
    start = js.index("function plateauAnimParseLine(m) {")
    end = js.index("function plateauAnimFeed(", start)
    body = js[start:end]
    # [^/] da solo si fermerebbe al primo "\/" (una barra dentro il pattern
    # JS dev'essere scritta cosi', o chiuderebbe il letterale) leggendolo come
    # fine del regex — (?:\\.|[^/]) tratta ogni coppia backslash+carattere
    # come opaca, cosi' "\/" resta dentro invece di troncare l'estrazione.
    literals = re.findall(r"/(\^(?:\\.|[^/])+)/\.exec", body)
    # 7 forme ancorate: confini, asse, budget-superato, enumerate-tutte,
    # checkpoint di apertura, apertura-finale, quanti-finalisti, finalista,
    # scelto — "griglia:" e' un'ottava forma ma non ancorata (si cerca
    # ovunque nella riga), gestita a parte nel test sotto.
    assert len(literals) >= 7, "il parser deve avere almeno le 7 forme di riga ancorate attese"
    return [re.compile(pat) for pat in literals]


def test_every_real_log_line_is_matched_by_some_pattern():
    """"espansione guidata: ..." non e' in questa lista apposta: la sua
    informazione (celle aperte, finalisti) e' ridondante con la riga
    successiva "N configurazioni distinte su M celle provate", che invece
    l'animazione legge — non serve un secondo modo di saperlo."""
    js = _js()
    patterns = _extract_regexes(js)
    unmatched = []
    for line in _real_lines():
        if line.startswith("grid:"):
            if not re.search(r"= (\d+) cells(?:\s|$)", line):
                unmatched.append(line)
            continue
        if "shaking" in line:
            # Questa non e' ancorata a inizio riga (il numero di finalisti
            # arriva a meta' frase, dopo "N candidati con almeno..."), quindi
            # _extract_regexes() non la prende insieme alle altre.
            if not re.search(r"shaking (\d+) with (\d+) random shifts", line):
                unmatched.append(line)
            continue
        if not any(p.search(line) for p in patterns):
            unmatched.append(line)
    assert not unmatched, f"righe vere che l'animazione non riconosce: {unmatched}"


def test_the_animation_only_shows_for_the_stable_search():
    js = _js()
    assert 'job.mode === "plateau"' in js
    start = js.index("function plateauAnimShow(job)")
    body = js[start:start + 500]
    assert "isPlateau" in body


def test_the_job_records_its_own_mode_for_the_animation_to_read():
    js = _js()
    start = js.index("optJobs.set(started.job_id")
    block = js[start:start + 400]
    assert "mode: optMode" in block


def test_reopening_the_progress_window_replays_the_whole_log():
    """Il lavoro puo' essere gia' a meta' quando si riapre la finestra:
    l'animazione deve poter ricostruire tutte le fasi gia' passate, non solo
    quelle da qui in poi."""
    js = _js()
    start = js.index("function openOptProgress(jobId)")
    body = js[start:start + 1200]
    assert "plateauAnimShow(job)" in body


def test_new_log_lines_are_fed_incrementally_while_watching():
    js = _js()
    start = js.index("async function pollOnce(job)")
    body = js[start:start + 1200]
    assert 'plateauAnimFeed(data.log || [])' in body


def test_the_canvas_exists_and_is_hidden_by_default():
    html = Path(__file__).resolve().parent.parent.parent / "frontend" / "index.html"
    src = html.read_text(encoding="utf-8")
    assert 'id="opt-progress-anim"' in src
    m = re.search(r'<canvas id="opt-progress-anim"[^>]*class="([^"]+)"', src)
    assert m and "hidden" in m.group(1)


def test_plateau_py_still_says_the_five_things_the_animation_listens_for():
    """Se una di queste frasi sparisse da plateau.py, l'animazione perderebbe
    silenziosamente quella fase: meglio un test che se ne accorga qui."""
    src = _PLATEAU_PY.read_text(encoding="utf-8")
    needles = [
        "mapping boundaries on",
        "grid: ",
        "distinct configurations so far",
        "distinct configurations over",
        "random shifts of",
        "finalist {rank}",
        "picked: ",
        "still working",
    ]
    missing = [n for n in needles if n not in src]
    assert not missing, f"frasi che plateau.py non scrive piu': {missing}"


def test_an_axis_still_being_searched_shows_something_instead_of_nothing():
    """Segnalato dal vivo con uno screenshot: il pannello restava fermo su
    "asse 0/5" per una quindicina di secondi senza muovere nulla, ne' nel
    disegno ne' nel log — un asse pesante puo' metterci cosi' tanto prima
    della sua riga finale. Questo e' il contatore che nel frattempo si vede
    crescere."""
    js = _js()
    assert "axisBusy" in js
    start = js.index("function plateauActBoundaries(")
    body = js[start:start + 3000]
    assert "isBusy" in body and "working" in body


def test_finishing_an_axis_clears_its_own_busy_counter():
    """Altrimenti il contorno tratteggiato dell'asse appena chiuso resterebbe
    a schermo mentre il prossimo asse comincia, mostrando un numero vecchio."""
    js = _js()
    start = js.index("^axis (.+?): (\\d+) boundar")
    block = js[start:start + 300]
    assert "plateauAnim.axisBusy = 0" in block
