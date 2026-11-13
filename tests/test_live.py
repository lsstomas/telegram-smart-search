import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tgsearch.sources import live


def test_credentials_required():
    with pytest.raises(live.LiveConfigError):
        live.credentials_from_env({})
    with pytest.raises(live.LiveConfigError):
        live.credentials_from_env({"TELEGRAM_API_ID": "abc", "TELEGRAM_API_HASH": "x"})


def test_credentials_repr_hides_values():
    creds = live.credentials_from_env({"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "s3cr3t"})
    assert creds.api_id == 123
    assert "s3cr3t" not in repr(creds) and "123" not in repr(creds)


def test_default_session_path_is_outside_project(tmp_path):
    path = live.default_session_path({"XDG_DATA_HOME": str(tmp_path)})
    assert path == tmp_path / "tgsearch" / "tgsearch"
    override = live.default_session_path({"TGSEARCH_SESSION": str(tmp_path / "s")})
    assert override == tmp_path / "s"


def test_coerce_chat():
    assert live.coerce_chat("-1001234") == -1001234
    assert live.coerce_chat("@ofertas") == "@ofertas"
    assert live.coerce_chat("https://t.me/ofertas") == "https://t.me/ofertas"


def test_links_and_conversion():
    public = SimpleNamespace(id=1, username="ofertas", title="Ofertas")
    private = SimpleNamespace(id=99, username=None, title="Privado", broadcast=True)
    user = SimpleNamespace(id=5, username=None, first_name="Ana")
    assert live.message_link(public, 7) == "https://t.me/ofertas/7"
    assert live.message_link(private, 7) == "https://t.me/c/99/7"
    assert live.message_link(user, 7) == ""
    assert live.chat_name(user) == "Ana"

    msg = SimpleNamespace(
        id=7,
        date=datetime(2026, 10, 1, tzinfo=timezone.utc),
        message="SSD R$ 349",
        entities=[SimpleNamespace(url="https://loja.com/ssd"), SimpleNamespace(offset=0)],
    )
    converted = live.to_message(public, msg)
    assert converted.urls == ("https://loja.com/ssd",)
    assert converted.link == "https://t.me/ofertas/7"
    assert live.to_message(public, SimpleNamespace(id=8, message=None)) is None


class FakeClient:
    """Mimics the parts of TelethonClient used by _collect; no network."""

    def __init__(self, messages):
        self.messages = messages
        self.searches = []

    async def get_entity(self, chat):
        return SimpleNamespace(id=1, username=str(chat).lstrip("@"), title=str(chat))

    def iter_messages(self, entity, search=None, limit=None):
        self.searches.append(search)
        msgs = self.messages[:limit]

        async def gen():
            for m in msgs:
                yield m

        return gen()


def _msg(i, day, text):
    return SimpleNamespace(
        id=i, date=datetime(2026, 10, day, tzinfo=timezone.utc), message=text, entities=None
    )


def test_collect_uses_longest_term_and_stops_at_since():
    client = FakeClient([_msg(3, 5, "ssd nvme"), _msg(2, 3, "ssd"), _msg(1, 1, "old ssd")])
    since = datetime(2026, 10, 2, tzinfo=timezone.utc)
    out = asyncio.run(live._collect(client, ["@c"], ["ssd", "nvme"], False, 10, since))
    assert client.searches == ["nvme"]
    assert [m.message_id for m in out] == [3, 2]


def test_collect_any_mode_dedupes_ids():
    client = FakeClient([_msg(1, 5, "ssd hd")])
    out = asyncio.run(live._collect(client, ["@c"], ["ssd", "hd"], True, 10, None))
    assert client.searches == ["ssd", "hd"]
    assert len(out) == 1
