import asyncio
import stat
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tgsearch import credentials
from tgsearch.credentials import Credentials, LiveConfigError
from tgsearch.sources import live

HASH = "0123456789abcdef0123456789abcdef"


# --- credentials --------------------------------------------------------------------------


def test_env_credentials_are_validated():
    assert credentials.credentials_from_env({}) is None
    with pytest.raises(LiveConfigError):
        credentials.credentials_from_env({"TELEGRAM_API_ID": "abc", "TELEGRAM_API_HASH": HASH})
    with pytest.raises(LiveConfigError, match="both"):
        credentials.credentials_from_env({"TELEGRAM_API_ID": "123"})
    with pytest.raises(LiveConfigError, match="32 hexadecimal"):
        credentials.credentials_from_env({"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "short"})


def test_credentials_repr_hides_values():
    creds = credentials.credentials_from_env({"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": HASH})
    assert creds.api_id == 123
    assert HASH not in repr(creds) and "123" not in repr(creds)


def test_saved_credentials_are_private_and_round_trip(tmp_path):
    path = tmp_path / "cfg" / "tgsearch" / "credentials.json"
    credentials.save(Credentials(42, HASH), path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert credentials.load_saved(path) == Credentials(42, HASH)

    path.chmod(0o644)
    with pytest.raises(LiveConfigError, match="readable by other users"):
        credentials.load_saved(path)


def test_resolve_prefers_env_then_file_and_never_prompts(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path)}
    with pytest.raises(LiveConfigError, match="tgsearch auth"):
        credentials.resolve(env)
    credentials.save(Credentials(7, HASH), credentials.credentials_path(env))
    assert credentials.resolve(env).api_id == 7
    other = "f" * 32
    env.update(TELEGRAM_API_ID="8", TELEGRAM_API_HASH=other)
    assert credentials.resolve(env) == Credentials(8, other)


def test_prompt_reads_hash_through_the_secret_reader():
    asked = []

    def secret(prompt):
        asked.append(prompt)
        return HASH.upper()

    creds = credentials.prompt(read_line=lambda _: " 99 ", read_secret=secret)
    assert creds == Credentials(99, HASH)
    assert "hidden" in asked[0]


# --- conversion ---------------------------------------------------------------------------


def test_default_session_path_is_outside_project(tmp_path):
    path = live.default_session_path({"XDG_DATA_HOME": str(tmp_path)})
    assert path == tmp_path / "tgsearch" / "tgsearch"
    override = live.default_session_path({"TGSEARCH_SESSION": str(tmp_path / "s")})
    assert override == tmp_path / "s"


def test_coerce_chat():
    assert live.coerce_chat("-1001234") == -1001234
    assert live.coerce_chat("@ofertas") == "@ofertas"
    assert live.coerce_chat("https://t.me/ofertas") == "https://t.me/ofertas"


def channel(id=1, username="ofertas", title="Ofertas", **kw):
    return SimpleNamespace(id=id, username=username, title=title, broadcast=True, **kw)


def test_links_kinds_and_conversion():
    public = channel()
    private = channel(id=99, username=None, title="Privado")
    group = SimpleNamespace(id=3, username=None, title="Chat", megagroup=True, broadcast=False)
    user = SimpleNamespace(id=5, username=None, first_name="Ana")
    assert live.message_link(public, 7) == "https://t.me/ofertas/7"
    assert live.message_link(private, 7) == "https://t.me/c/99/7"
    assert live.message_link(user, 7) == ""
    assert [live.chat_kind(e) for e in (public, group, user)] == ["channel", "supergroup", "user"]
    assert live.chat_name(user) == "Ana"

    msg = SimpleNamespace(
        id=7,
        date=datetime(2026, 10, 1, tzinfo=timezone.utc),
        message="SSD R$ 349",
        entities=[SimpleNamespace(url="https://loja.com/ssd"), SimpleNamespace(offset=0)],
        fwd_from=SimpleNamespace(from_id=SimpleNamespace(channel_id=555), channel_post=12),
    )
    converted = live.to_message(public, msg)
    assert converted.urls == ("https://loja.com/ssd",)
    assert converted.link == "https://t.me/ofertas/7"
    assert converted.chat_id == 1
    assert converted.forward_ref == "555:12"
    assert live.to_message(public, SimpleNamespace(id=8, message=None)) is None


def test_forward_from_a_user_has_no_ref():
    msg = SimpleNamespace(fwd_from=SimpleNamespace(from_id=SimpleNamespace(user_id=1)))
    assert live.forward_ref(msg) == ""


# --- client behaviour (fake Telethon client, no network) ----------------------------------


def _msg(i, day, text, chat=None, private=False):
    return SimpleNamespace(
        id=i,
        date=datetime(2026, 10, day, tzinfo=timezone.utc),
        message=text,
        entities=None,
        fwd_from=None,
        is_private=private,
        _chat=chat,
    )


class FakeClient:
    """Mimics the parts of TelegramClient this module uses."""

    def __init__(self, messages=(), authorized=True, dialogs=()):
        self.messages = list(messages)
        self.authorized = authorized
        self.dialogs = list(dialogs)
        self.searches = []
        self.started = False
        self.connected = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def start(self):
        self.started = True

    async def get_entity(self, chat):
        return channel(id=1, username=str(chat).lstrip("@"), title=str(chat))

    def iter_dialogs(self):
        async def gen():
            for d in self.dialogs:
                yield d

        return gen()

    def iter_messages(self, entity, search=None, limit=None, wait_time=None):
        self.searches.append((None if entity is None else entity.id, search))
        msgs = self.messages[:limit]

        async def gen():
            for m in msgs:
                yield m

        return gen()


def _patch_get_chat(messages):
    for m in messages:

        async def get_chat(m=m):
            return m._chat

        m.get_chat = get_chat


def test_non_interactive_client_refuses_to_log_in(tmp_path):
    fake = FakeClient(authorized=False)

    async def go():
        async with live.open_client(Credentials(1, HASH), tmp_path / "s", factory=lambda *a: fake):
            pass

    with pytest.raises(LiveConfigError, match="tgsearch auth"):
        asyncio.run(go())
    assert fake.started is False and fake.connected is False


def test_interactive_client_logs_in_and_disconnects(tmp_path):
    fake = FakeClient(authorized=False)
    session = tmp_path / "dir" / "s"

    async def go():
        async with live.open_client(
            Credentials(1, HASH), session, interactive=True, factory=lambda *a: fake
        ) as client:
            assert client is fake

    asyncio.run(go())
    assert fake.started is True and fake.connected is False
    assert stat.S_IMODE(session.parent.stat().st_mode) == 0o700


def test_collect_uses_longest_term_and_stops_at_since():
    client = FakeClient([_msg(3, 5, "ssd nvme"), _msg(2, 3, "ssd"), _msg(1, 1, "old ssd")])
    since = datetime(2026, 10, 2, tzinfo=timezone.utc)
    out = asyncio.run(live._collect(client, ["@c"], ["ssd", "nvme"], False, 10, since))
    assert client.searches == [(1, "nvme")]
    assert [m.message_id for m in out] == [3, 2]


def test_collect_any_mode_dedupes_ids():
    client = FakeClient([_msg(1, 5, "ssd hd")])
    out = asyncio.run(live._collect(client, ["@c"], ["ssd", "hd"], True, 10, None))
    assert client.searches == [(1, "ssd"), (1, "hd")]
    assert len(out) == 1


def test_discover_keeps_channels_skips_private_and_groups_and_old():
    deals = channel(id=10, title="Deals")
    other = channel(id=11, title="Other")
    group = SimpleNamespace(id=12, title="Chatty", megagroup=True, broadcast=False)
    person = SimpleNamespace(id=13, first_name="Ana")
    msgs = [
        _msg(1, 9, "rx 7600 R$ 1.499", deals),
        _msg(2, 8, "rx 7600 R$ 1.399", deals),
        _msg(3, 7, "rx 7600 R$ 1.599", other),
        _msg(4, 6, "rx 7600?", group),
        _msg(5, 5, "rx 7600 chegou", person, private=True),
        _msg(6, 1, "rx 7600 R$ 1.299", other),  # older than since
    ]
    _patch_get_chat(msgs)
    client = FakeClient(msgs)
    since = datetime(2026, 10, 2, tzinfo=timezone.utc)

    hits = asyncio.run(live._discover(client, ["7600"], since, 50, False, None))
    assert [(h.chat.title, h.total) for h in hits] == [("Deals", 2), ("Other", 1)]
    assert hits[0].latest.day == 9
    assert client.searches == [(None, "7600")]  # global search

    with_groups = asyncio.run(live._discover(client, ["7600"], since, 50, True, None))
    assert {h.chat.title for h in with_groups} == {"Deals", "Other", "Chatty"}


def test_discover_accept_filter():
    deals = channel(id=10, title="Deals")
    msgs = [_msg(1, 9, "Ryzen 5 7600", deals), _msg(2, 8, "RX 7600", deals)]
    _patch_get_chat(msgs)
    hits = asyncio.run(
        live._discover(FakeClient(msgs), ["7600"], None, 50, False, lambda t: "RX" in t)
    )
    assert hits[0].total == 1


def test_check_counts_dialogs_by_kind():
    dialogs = [
        SimpleNamespace(entity=channel()),
        SimpleNamespace(entity=SimpleNamespace(id=2, first_name="Ana")),
    ]
    counts = asyncio.run(live._check(FakeClient(dialogs=dialogs)))
    assert counts == {"channel": 1, "supergroup": 0, "group": 0, "user": 1}
