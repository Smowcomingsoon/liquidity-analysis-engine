"""Fermare una ricerca a meta'.

Una ricerca stabile puo' durare dieci minuti. Senza un modo di fermarla, chi
l'ha lanciata per sbaglio — coppia sbagliata, modalita' sbagliata — deve
aspettare che finisca, e nel frattempo la macchina e' occupata.

Il controllo e' una funzione passata dall'alto invece di una variabile globale:
la ricerca gira in un thread suo, e chi la ferma sta nel processo web. Un
booleano condiviso funzionerebbe, ma legherebbe questo modulo a chi lo usa,
mentre cosi' i test possono fermarla senza inventarsi un lavoro finto.
"""

from __future__ import annotations

from collections.abc import Callable


class SearchCancelled(Exception):
    """Sollevata dal punto in cui la ricerca si accorge di essere stata fermata.

    E' un'eccezione e non un ritorno anticipato perche' i punti da interrompere
    sono in fondo a tre livelli di chiamate: propagarla e' l'unico modo di non
    riempire ogni anello di controlli sul valore restituito.
    """


def stop_if_asked(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise SearchCancelled("Search stopped")
