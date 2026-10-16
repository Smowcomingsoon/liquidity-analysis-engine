"""La pagina del wallet carica moduli ES veri attraverso un import map
generato dal server. Il modo in cui puo' rompersi e' silenzioso: un modulo
rinominato, o un import con un nome che nella mappa non c'e', danno un 404
a runtime che nessun test del backend vedrebbe — la pagina si apre bianca.

Questi test chiudono proprio quel buco, dal lato che possiamo controllare
senza un browser: che la mappa copra ogni import scritto nei moduli, e che
ogni voce della mappa punti a un file che esiste davvero.
"""

from __future__ import annotations

import json
import re

from app import main

_IMPORTMAP_RE = re.compile(r'<script type="importmap">(.*?)</script>', re.S)
_IMPORT_RE = re.compile(r"""from\s+["'](@onchain/[^"']+)["']""")


def _importmap() -> dict[str, str]:
    match = _IMPORTMAP_RE.search(main._ONCHAIN_APP_HTML)
    assert match, "l'import map non e' stata iniettata nella pagina"
    return json.loads(match.group(1))["imports"]


def test_page_ships_an_importmap_for_every_module_on_disk():
    imports = _importmap()
    on_disk = {f"@onchain/{m.name}" for m in main._onchain_modules()}
    assert on_disk, "nessun modulo trovato in static/onchain/"
    assert set(imports) == on_disk


def test_every_import_written_in_a_module_is_in_the_map():
    imports = _importmap()
    for module in main._onchain_modules():
        for specifier in _IMPORT_RE.findall(module.read_text(encoding="utf-8")):
            assert specifier in imports, f"{module.name} importa {specifier}, che l'import map non risolve"


def test_mapped_urls_point_at_files_that_exist_and_carry_the_version():
    for specifier, url in _importmap().items():
        path, _, query = url.partition("?")
        assert query == f"v={main._ASSET_VERSION}", f"{specifier} senza cache-busting"
        served = main.FRONTEND_DIR / path.removeprefix("/")
        assert served.is_file(), f"{specifier} punta a {path}, che non esiste"


def test_entry_point_script_is_versioned_too():
    # L'import map versiona gli import interni, ma il <script type="module">
    # iniziale ha un URL suo: se resta senza ?v= il browser puo' servire da
    # cache il vecchio entry point e ignorare tutto il resto.
    assert f'src="/static/onchain/main.js?v={main._ASSET_VERSION}"' in main._ONCHAIN_APP_HTML


def test_asset_version_changes_when_a_module_changes(tmp_path, monkeypatch):
    before = main._compute_asset_version()
    real_dir = main.FRONTEND_DIR / "static" / "onchain"
    target = real_dir / "format.js"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n// tocco per il test\n")
        assert main._compute_asset_version() != before
    finally:
        target.write_bytes(original)
    assert main._compute_asset_version() == before


_BOOT_RE = re.compile(r'<script type="application/json" id="onchain-boot">(.*?)</script>', re.S)


def _boot(html: str) -> dict:
    match = _BOOT_RE.search(html)
    assert match, "i dati di avvio non sono stati iniettati nella pagina"
    return json.loads(match.group(1))


def test_every_module_is_modulepreloaded_with_the_version():
    # Senza modulepreload gli import di main.js partono solo dopo main.js:
    # un giro di rete in fila in piu' prima che la pagina faccia qualcosa.
    # Trading-only modules are the exception: main.js imports them on demand.
    for module in main._onchain_modules():
        link = f'<link rel="modulepreload" href="/static/onchain/{module.name}?v={main._ASSET_VERSION}" />'
        if module.name in main._ONCHAIN_LAZY_MODULES:
            assert link not in main._ONCHAIN_APP_HTML, f"{module.name} e' solo trading, non va precaricato"
        else:
            assert link in main._ONCHAIN_APP_HTML, f"{module.name} non e' precaricato"


def test_lazy_modules_are_imported_only_on_demand():
    # A static import anywhere would pull a lazy module back into the graph.
    static = re.compile(r'^import [^;]*from "@onchain/([\w-]+\.js)"', re.M)
    for module in main._onchain_modules():
        for name in static.findall(module.read_text(encoding="utf-8")):
            assert name not in main._ONCHAIN_LAZY_MODULES, f"{module.name} importa {name} staticamente"


def test_chart_library_is_versioned_and_async():
    assert (
        f'<script async src="/static/lightweight-charts.standalone.production.js?v={main._ASSET_VERSION}">'
        in main._ONCHAIN_APP_HTML
    )


def test_page_uses_no_third_party_fonts():
    assert "fonts.googleapis.com" not in main._ONCHAIN_APP_HTML
    for href in re.findall(r'href="(/static/fonts/[^"?]+)', main._ONCHAIN_APP_HTML):
        assert (main.FRONTEND_DIR / href.removeprefix("/")).is_file(), f"{href} non esiste"


def _tag(html: str, element_id: str) -> str:
    return re.search(rf'<[^>]*\bid="{element_id}"[^>]*>', html).group(0)


def test_signed_out_page_shows_login_and_boots_without_user():
    html = main._render_onchain_app(None)
    assert _boot(html) == {"user": None}
    assert not _tag(html, "login-screen").endswith(" hidden>")
    assert _tag(html, "app-shell").endswith(" hidden>")


def test_signed_in_page_ships_user_and_wallets_but_no_key(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(main.settings, "onchain_enabled", True)
    wallet = SimpleNamespace(address="So1ana</script>", enabled=True, created_at=1, private_key="SEGRETO")
    user = SimpleNamespace(id="u1", email="a@b.c", telegram_chat_id=None, onchain_wallets={"solana": wallet})
    html = main._render_onchain_app(user)

    boot = _boot(html)
    assert boot["user"]["id"] == "u1"
    assert boot["wallets"] == [{"chain": "solana", "address": "So1ana</script>", "enabled": True, "created_at": 1}]
    assert "SEGRETO" not in html
    assert not _tag(html, "app-shell").endswith(" hidden>")
    assert _tag(html, "login-screen").endswith(" hidden>")


def test_page_response_is_not_shared_by_caches():
    from fastapi.testclient import TestClient

    response = TestClient(main.app).get("/onchain-app.html")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert _boot(response.text) == {"user": None}


def test_app_skin_is_versioned_and_counted_in_the_asset_hash():
    from app import main
    assert f'href="/static/onchain/app.css?v={main._ASSET_VERSION}"' in main._ONCHAIN_APP_HTML
    assert "app.css" not in main._INDEX_HTML            # the embed keeps its own stylesheet
