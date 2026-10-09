"""Long tasks of the app as jobs with a push at the end (jobs.py, web_push.py)."""
import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import jobs, main
from app.auth import get_current_user, get_current_user_optional
from app.notify import web_push

DEVICE = "dev0123456789abcd"
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}


def _sent(monkeypatch):
    out = []
    monkeypatch.setattr(web_push, "_deliver", lambda sub, payload: out.append(payload) or 201)
    monkeypatch.setattr(web_push.threading, "Thread",
                        lambda target, **kw: SimpleNamespace(start=target))
    return out


def test_vapid_keys_are_made_once_and_sign(tmp_path):
    from py_vapid import Vapid
    k = web_push.keys()
    assert web_push.keys() is k and json.loads(web_push._KEYS_FILE.read_text()) == k
    assert Vapid.from_string(k["private"]).public_key is not None
    assert len(web_push._b64(b"x" * 65)) == 87 and len(k["public"]) == 87


def test_subscribe_rejects_junk():
    for bad in ({}, {"endpoint": "http://x", "keys": {"p256dh": "k", "auth": "a"}}, {"endpoint": "https://x"}):
        try:
            web_push.subscribe(DEVICE, bad)
            raise AssertionError("accepted")
        except ValueError:
            pass
    web_push.subscribe(DEVICE, SUB, "u1")
    assert web_push.subscribed(DEVICE)


def test_one_push_when_done_and_none_while_watched(monkeypatch):
    sent = _sent(monkeypatch)
    web_push.subscribe(DEVICE, SUB)
    a = jobs.create("u1", "scan", "HYDX", device=DEVICE, view="meme&scan=1")
    jobs.finish(a, {"token": {"symbol": "HYDX"}})
    assert len(sent) == 1 and sent[0]["url"] == f"/onchain-app.html?job={a}#meme&scan=1"
    b = jobs.create("u1", "scan", "GRID", device=DEVICE)
    jobs.watch(b)                       # the page is on screen: no push
    jobs.finish(b, {})
    assert len(sent) == 1
    c = jobs.create("u1", "scan", "PFWA", device=DEVICE)
    jobs.watch(c)
    jobs.away(c)                        # the page left the screen
    jobs.fail(c, "boom")
    assert len(sent) == 2 and sent[1]["body"] == "boom"
    d = jobs.create("u1", "scan", "X")  # no device: never a push
    jobs.finish(d, {})
    assert len(sent) == 2


def test_gone_subscription_is_forgotten(monkeypatch):
    _sent(monkeypatch)
    monkeypatch.setattr(web_push, "_deliver", lambda sub, payload: 410)
    web_push.subscribe(DEVICE, SUB)
    web_push.send(DEVICE, "t", "b", "/")
    assert not web_push.subscribed(DEVICE)


def test_view_is_url_safe_only():
    assert jobs.clean_view("#gems&token=solana:AbC&tab=why") == "gems&token=solana:AbC&tab=why"
    assert jobs.clean_view('gems"><script>') == ""


def test_jobs_survive_a_restart_and_running_ones_are_told(monkeypatch):
    sent = _sent(monkeypatch)
    web_push.subscribe(DEVICE, SUB)
    done = jobs.create("u1", "ask", "Agent", device=DEVICE)
    jobs.watch(done)
    jobs.finish(done, {"answer": "hi"})
    running = jobs.create("u1", "ask", "Agent", device=DEVICE)
    monkeypatch.setattr(jobs, "_jobs", {})
    monkeypatch.setattr(jobs, "_loaded", False)
    assert jobs.get(done, owner_id="u1")["result"] == {"answer": "hi"}
    lost = jobs.get(running, owner_id="u1")
    assert lost["state"] == "fallito" and "run it again" in lost["error"]
    assert len(sent) == 1 and running in sent[0]["url"]
    assert [j["id"] for j in jobs.recent(DEVICE, "u1")] == [running, done]
    assert jobs.recent(DEVICE, "someone-else") == []


def test_recheck_as_a_job(monkeypatch):
    from app.trading.onchain import deep_recheck

    async def fake_run(chain, token, client, limited=True):
        return {"keep": True, "score": 58, "reason": "fine"}

    monkeypatch.setattr(deep_recheck, "run", fake_run)
    monkeypatch.setattr(deep_recheck, "wait_s", lambda *a, **k: 0)
    monkeypatch.setattr(main.onchain_projects_db, "get", lambda c, t: {"symbol": "HYDX"})
    monkeypatch.setattr(main, "_require_onchain_enabled", lambda: None)
    user = SimpleNamespace(id="u1", role="user", email="x@y")
    main.app.dependency_overrides[get_current_user] = lambda: user
    main.app.dependency_overrides[get_current_user_optional] = lambda: user
    monkeypatch.setattr(main.guest_links, "is_guest", lambda u: False)
    try:
        c = TestClient(main.app)
        r = c.post("/api/onchain/token/recheck?chain=base&token=0xabc", headers={"X-Device": DEVICE},
                   json={"view": "gems&token=base:0xabc&tab=why", "label": "HYDX"}).json()
        jid = r["job"]
        for _ in range(50):
            got = c.get(f"/api/jobs/{jid}?visible=1").json()
            if got["state"] != "in corso":
                break
            asyncio.run(asyncio.sleep(0.02))
        assert got["state"] == "finito" and got["result"]["deep"]["score"] == 58
        assert got["view"] == "gems&token=base:0xabc&tab=why"
        assert main._recheck_note(jobs._jobs[jid])[1].startswith("Kept · 58/100")
        assert c.get("/api/jobs", headers={"X-Device": DEVICE}).json()["jobs"][0]["id"] == jid
        assert c.post(f"/api/jobs/{jid}/away").json() == {"ok": True}
    finally:
        main.app.dependency_overrides.clear()


def test_push_endpoints(monkeypatch):
    user = SimpleNamespace(id="u1", role="user", email="x@y")
    main.app.dependency_overrides[get_current_user_optional] = lambda: user
    main.app.dependency_overrides[get_current_user] = lambda: user
    try:
        c = TestClient(main.app)
        assert len(c.get("/api/push/key").json()["key"]) == 87
        assert c.post("/api/push/subscribe", json={"subscription": SUB}).status_code == 400   # no device
        assert c.post("/api/push/subscribe", json={"subscription": SUB}, headers={"X-Device": DEVICE}).json() == {"ok": True}
        assert web_push.subscribed(DEVICE)
        c.post("/api/push/unsubscribe", headers={"X-Device": DEVICE})
        assert not web_push.subscribed(DEVICE)
        sw = c.get("/sw.js")
        assert sw.status_code == 200 and "notificationclick" in sw.text
        assert c.get("/manifest.webmanifest").json()["start_url"] == "/onchain-app.html"
    finally:
        main.app.dependency_overrides.clear()
