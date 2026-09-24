"""The public project page: open without an account, rules taken from the radar."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import main


def test_page_is_public_and_leads_to_the_app():
    resp = TestClient(main.app).get("/engn")
    assert resp.status_code == 200
    assert "ENTER THE APP" in resp.text
    assert 'href="/onchain-app.html"' in resp.text
    assert "ENTER MEME SNIPER" in resp.text and "ENTER GEM FINDER" in resp.text
    assert 'id="why"' in resp.text and 'href="#why"' in resp.text


def test_page_names_no_person():
    html = TestClient(main.app).get("/engn").text.lower()
    assert "lorenzo" not in html and "gabriele" not in html


def test_placeholders_are_all_filled():
    assert "{{" not in TestClient(main.app).get("/engn").text


def test_nothing_launched_means_no_contract_address(monkeypatch):
    from app import token_launch
    monkeypatch.setattr(token_launch, "launched", lambda: None)
    assert "not launched yet" in TestClient(main.app).get("/engn").text


def test_contract_address_appears_only_after_launch(monkeypatch):
    from app import token_launch
    monkeypatch.setattr(token_launch, "launched", lambda: {"mint": "Mint1111"})
    html = TestClient(main.app).get("/engn").text
    assert "https://pump.fun/coin/Mint1111" in html
    assert "not launched yet" not in html


def test_launch_refuses_without_arming(monkeypatch):
    from app import token_launch
    monkeypatch.delenv("ENGN_LAUNCH_ARMED", raising=False)
    assert token_launch.main(["launch", "--confirm", "ENGN"]) == 2
    monkeypatch.setenv("ENGN_LAUNCH_ARMED", "1")
    assert token_launch.main(["launch", "--confirm", "engn"]) == 2
