"""Cache degli asset: il deploy era andato, la pagina no.

È successo davvero: l'ottimizzazione girava con il log vecchio — una riga e poi
silenzio — mentre in produzione c'era già la versione che racconta le fasi. Il
server aveva il codice nuovo, il browser no.

L'HTML è l'unico posto dove vive il numero di versione degli asset (`?v=hash`).
Se il browser tiene la pagina in cache tiene anche il vecchio numero, e continua
a chiedere il vecchio app.js all'infinito.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture(scope="module")
def client():
    return TestClient(main.app)


def test_the_page_is_never_served_from_cache(client):
    for path in ("/",):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-cache", path


def test_the_page_carries_the_asset_version(client):
    html = client.get("/").text
    assert f"/static/app.js?v={main._ASSET_VERSION}" in html
    assert f"/static/style.css?v={main._ASSET_VERSION}" in html


def test_a_versioned_asset_can_be_kept_forever(client):
    resp = client.get(f"/static/app.js?v={main._ASSET_VERSION}")
    assert resp.status_code == 200
    assert "immutable" in resp.headers.get("cache-control", "")


def test_an_unversioned_asset_is_revalidated(client):
    """È l'URL che qualcuno può aver salvato, o che arriva da una pagina
    vecchia: servirlo dalla cache significa servire codice di due deploy fa."""
    resp = client.get("/static/app.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


def test_the_version_follows_the_bytes_not_the_commit():
    """Nell'immagine Docker non c'è .git: legare la versione al commit la
    lascerebbe ferma."""
    v1 = main._compute_asset_version()
    assert v1 == main._ASSET_VERSION
    assert len(v1) == 10


# --- la pagina si accorge di essere vecchia --------------------------------
#
# Il `no-cache` sull'HTML vale dal deploy in poi. Chi aveva già la pagina in
# cache da prima può restarci per ore, ed è successo: grafico nero dopo che la
# correzione era in produzione da un pezzo, perché il browser continuava a
# chiedere il vecchio app.js.


def test_the_page_carries_the_version_it_was_built_with(client):
    html = client.get("/").text
    assert f'content="{main._ASSET_VERSION}"' in html
    assert 'name="lae-asset-version"' in html


def test_the_server_says_which_version_it_serves(client):
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json()["asset_version"] == main._ASSET_VERSION


def test_the_two_agree_so_a_fresh_page_never_reloads(client):
    """Se non coincidessero, ogni caricamento farebbe una ricarica in più."""
    html = client.get("/").text
    live = client.get("/api/version").json()["asset_version"]
    assert f'content="{live}"' in html
