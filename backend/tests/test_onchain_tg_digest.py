"""The Telegram digest: paging back through a channel's posts and asking
DeepSeek for the concrete developments. No network: fake client, fake ask."""
import asyncio
import json
from datetime import datetime, timezone

from app.trading.onchain import judge, tg_digest, telegram_public as tp

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc).timestamp()


def _post(n: int, iso: str, text: str) -> str:
    return f"""
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="abcproto/{n}">
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>
    <div class="tgme_widget_message_footer compact js-message_footer">
      <div class="tgme_widget_message_info short js-message_info">
        <span class="tgme_widget_message_views">1K</span>
        <span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/abcproto/{n}"><time datetime="{iso}" class="time">12:00</time></a></span>
      </div>
    </div>
  </div>
</div></div>"""


def _page(posts: list[str]) -> str:
    return ('<!DOCTYPE html><html><body><section class="tgme_channel_history js-message_history">'
            + "".join(posts) + "</section></body></html>")


PAGE1 = _page([
    _post(30, "2026-09-30T09:00:00+00:00", "v1.0 released"),
    _post(29, "2026-09-29T09:00:00+00:00", "Partnership with Ondo"),
])
PAGE2 = _page([
    _post(28, "2026-09-28T09:00:00+00:00", "Audit passed"),
    _post(27, "2026-09-27T09:00:00+00:00", "Roadmap update"),
])
PAGE_OLD = _page([
    _post(5, "2026-01-01T09:00:00+00:00", "Ancient post"),
])


class _Resp:
    def __init__(self, status: int, text: str = ""):
        self.status_code, self.text = status, text


class _Client:
    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    async def get(self, url, headers=None, follow_redirects=False):
        self.calls.append(url)
        for key, page in self.pages.items():
            if key in url:
                return _Resp(200, page)
        return _Resp(404, "")


def _entry():
    return {"name": "ABC", "symbol": "ABC", "chain": "solana", "token_address": "abc",
            "socials": [{"type": "telegram", "url": "https://t.me/abcproto"}]}


def _ask(events):
    async def ask(messages, **kw):
        return json.dumps({"events": events})
    return ask


def test_history_pages_back_and_stops_at_max_pages():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE2, "t.me/s/abcproto": PAGE1})
    posts = asyncio.run(tp.history("abcproto", NOW, client, max_pages=2))
    ids = [p["id"] for p in posts]
    assert ids == [27, 28, 29, 30]
    assert len(client.calls) == 2


def test_history_stops_at_days():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE2, "?before=27": PAGE_OLD, "t.me/s/abcproto": PAGE1})
    posts = asyncio.run(tp.history("abcproto", NOW, client, max_pages=5, days=90))
    ids = [p["id"] for p in posts]
    assert 5 not in ids
    assert ids == [27, 28, 29, 30]


def test_history_stops_when_page_adds_nothing():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE1, "t.me/s/abcproto": PAGE1})
    posts = asyncio.run(tp.history("abcproto", NOW, client, max_pages=5))
    assert [p["id"] for p in posts] == [29, 30]


def test_update_extracts_events_and_dedupes():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE2, "t.me/s/abcproto": PAGE1})
    ask = _ask([
        {"date": "2026-09-30", "type": "release", "text": "v1.0 released", "post_id": 30},
        {"date": "2026-09-29", "type": "partnership", "text": "Partnership with Ondo", "post_id": 29},
        {"date": "2026-09-30", "type": "release", "text": "v1.0 released", "post_id": 30},
    ])
    out = asyncio.run(tg_digest.update(_entry(), NOW, client=client, ask=ask))
    assert out["last_id"] == 30
    assert len(out["events"]) == 2
    assert out["events"][0]["date"] == "2026-09-29"


def test_update_second_time_only_sends_new_posts():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE2, "t.me/s/abcproto": PAGE1})
    ask = _ask([{"date": "2026-09-30", "type": "release", "text": "v1.0 released", "post_id": 30}])
    asyncio.run(tg_digest.update(_entry(), NOW, client=client, ask=ask))
    sent: list[str] = []

    async def ask2(messages, **kw):
        sent.append(messages[-1]["content"])
        return json.dumps({"events": []})

    later = _page([_post(31, "2026-10-02T09:00:00+00:00", "Mainnet live"),
                   _post(30, "2026-09-30T09:00:00+00:00", "v1.0 released")])
    client2 = _Client({"?before=30": PAGE1, "t.me/s/abcproto": later})
    asyncio.run(tg_digest.update(_entry(), NOW + 25 * 3600, client=client2, ask=ask2))
    assert sent and "Mainnet live" in sent[0] and "v1.0 released" not in sent[0]
    assert len(client2.calls) == 1   # stops at the post already read


def test_update_caps_events():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE2, "t.me/s/abcproto": PAGE1})
    many = [{"date": f"2026-09-{(i % 28) + 1:02d}", "type": "other", "text": f"event {i}", "post_id": 100 + i}
            for i in range(60)]
    out = asyncio.run(tg_digest.update(_entry(), NOW, client=client, ask=_ask(many)))
    assert len(out["events"]) == tg_digest.MAX_EVENTS


def test_dossier_contains_telegram_developments():
    tp.reset_for_tests()
    tg_digest.reset_for_tests()
    client = _Client({"?before=29": PAGE2, "t.me/s/abcproto": PAGE1})
    ask = _ask([{"date": "2026-09-30", "type": "release", "text": "v1.0 released", "post_id": 30}])
    asyncio.run(tg_digest.update(_entry(), NOW, client=client, ask=ask))
    tg = {"handle": "abcproto", "kind": "channel", "subs": 100, "posts_7d": 2, "posts_30d": 4,
          "last_post_days": 1, "median_views": 1000, "recent_posts": []}
    text = judge.dossier(_entry(), "", None, tg, None)
    assert "Telegram developments: 2026-09-30 release: v1.0 released" in text
