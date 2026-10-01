"""The project's own Discord server, read from the public invite API without
an account. Never touches the network: responses are fixtures, the client
is fake."""
import asyncio

from app.trading.onchain import discord_public as dp
from app.trading.onchain import judge

NOW = 1_800_000_000.0


class _Resp:
    def __init__(self, status: int, data: dict | None = None):
        self.status_code = status
        self._data = data or {}

    def json(self):
        return self._data


class _Client:
    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    async def get(self, url, headers=None, **kw):
        self.calls.append(url)
        for key, resp in self.responses.items():
            if key in url:
                return resp
        return _Resp(404)


def _entry(**kw) -> dict:
    e = {"name": "ABC Protocol", "symbol": "ABC", "chain": "solana",
         "token_address": "abc", "socials": [], "links": []}
    e.update(kw)
    return e


def setup_function(_):
    dp.reset_for_tests()


def test_invite_code_from_typed_social():
    e = _entry(socials=[{"type": "discord", "url": "https://discord.gg/abc123"}])
    assert dp.invite_code(e) == "abc123"


def test_invite_code_from_discord_gg_link():
    e = _entry(links=[{"url": "https://discord.gg/xyz789"}])
    assert dp.invite_code(e) == "xyz789"


def test_invite_code_from_discord_com_invite():
    e = _entry(links=[{"url": "https://discord.com/invite/foo-bar"}])
    assert dp.invite_code(e) == "foo-bar"


def test_invite_code_none_for_channel_url():
    e = _entry(links=[{"url": "https://discord.com/channels/123/456"}])
    assert dp.invite_code(e) is None


def test_lookup_200_parsed():
    client = _Client({"invites/abc123": _Resp(200, {
        "approximate_member_count": 4200,
        "approximate_presence_count": 310,
        "guild": {"name": "ABC Protocol"},
    })})
    e = _entry(socials=[{"type": "discord", "url": "https://discord.gg/abc123"}])
    dc = asyncio.run(dp.lookup(e, now=NOW, client=client))
    assert dc["kind"] == "server"
    assert dc["code"] == "abc123"
    assert dc["name"] == "ABC Protocol"
    assert dc["members"] == 4200
    assert dc["online"] == 310


def test_lookup_404_missing():
    client = _Client({"invites/gone": _Resp(404)})
    e = _entry(links=[{"url": "https://discord.gg/gone"}])
    dc = asyncio.run(dp.lookup(e, now=NOW, client=client))
    assert dc == {"kind": "missing", "code": "gone"}


def test_lookup_cached_second_call_no_client():
    client = _Client({"invites/abc123": _Resp(200, {
        "approximate_member_count": 100, "approximate_presence_count": 5,
        "guild": {"name": "ABC"},
    })})
    e = _entry(socials=[{"type": "discord", "url": "https://discord.gg/abc123"}])
    asyncio.run(dp.lookup(e, now=NOW, client=client))
    assert len(client.calls) == 1
    dc = asyncio.run(dp.lookup(e, now=NOW + 60, client=client))
    assert len(client.calls) == 1
    assert dc["members"] == 100


def test_lookup_none_without_code():
    e = _entry()
    assert asyncio.run(dp.lookup(e, now=NOW)) is None


def test_dossier_contains_discord_line():
    e = _entry()
    dc = {"kind": "server", "code": "abc123", "name": "ABC Protocol",
          "members": 4200, "online": 310}
    text = judge.dossier(e, "", dc=dc)
    assert "Discord ABC Protocol: 4200 members, 310 online" in text


def test_dossier_discord_not_read():
    e = _entry()
    text = judge.dossier(e, "")
    assert "Discord: not read" in text
