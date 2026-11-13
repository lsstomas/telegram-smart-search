"""Read chats through the user's own Telegram account (Telethon, MTProto).

Only chats the account already belongs to are read, and only in read-only ways:
listing dialogs, Telegram's own message search, and reading message history.
Nothing is posted, joined or forwarded.

Credentials come from :mod:`tgsearch.credentials`. The Telethon session file
lives outside the project (``$XDG_DATA_HOME/tgsearch/``) in a ``0700`` directory.
Commands other than ``tgsearch auth`` never start a phone login: if the session
is not authorised they fail and say so, so a script cannot block on a prompt.

Private conversations are never read. Discovery keeps broadcast channels only,
unless groups are requested explicitly.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..credentials import Credentials, LiveConfigError, resolve
from ..models import Message

ENV_SESSION = "TGSEARCH_SESSION"

__all__ = [
    "ChatHit",
    "ChatInfo",
    "LiveConfigError",
    "check_session",
    "default_session_path",
    "discover_chats",
    "fetch_messages",
    "list_chats",
]


def default_session_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get(ENV_SESSION):
        return Path(env[ENV_SESSION]).expanduser()
    base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "tgsearch" / "tgsearch"  # Telethon appends ".session"


def coerce_chat(chat: str) -> str | int:
    """Numeric ids must be passed to Telethon as int; usernames and links as str."""
    stripped = chat.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


def chat_kind(entity: Any) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False) or getattr(entity, "gigagroup", False):
        return "supergroup"
    if hasattr(entity, "first_name") or hasattr(entity, "bot"):
        return "user"
    return "group"


def message_link(entity: Any, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if chat_kind(entity) in ("channel", "supergroup"):
        return f"https://t.me/c/{entity.id}/{message_id}"
    return ""


def chat_name(entity: Any) -> str:
    for attr in ("title", "username", "first_name"):
        value = getattr(entity, attr, None)
        if value:
            return str(value)
    return str(getattr(entity, "id", "unknown"))


def forward_ref(msg: Any) -> str:
    """Identify the original channel post of a forward, so reposts can be matched."""
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return ""
    channel_id = getattr(getattr(fwd, "from_id", None), "channel_id", None)
    post = getattr(fwd, "channel_post", None)
    if channel_id is None or post is None:
        return ""
    return f"{channel_id}:{post}"


def to_message(entity: Any, msg: Any) -> Message | None:
    text = getattr(msg, "message", None) or ""
    if not text.strip():
        return None
    urls = []
    for ent in getattr(msg, "entities", None) or []:
        url = getattr(ent, "url", None)  # MessageEntityTextUrl
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.append(url)
    entity_id = getattr(entity, "id", None)
    return Message(
        chat=chat_name(entity),
        message_id=int(msg.id),
        date=getattr(msg, "date", None),
        text=text,
        urls=tuple(dict.fromkeys(urls)),
        link=message_link(entity, int(msg.id)),
        chat_id=int(entity_id) if entity_id is not None else None,
        forward_ref=forward_ref(msg),
    )


def _allowed(entity: Any, include_groups: bool) -> bool:
    kind = chat_kind(entity)
    return kind == "channel" or (include_groups and kind in ("supergroup", "group"))


@dataclass(frozen=True)
class ChatInfo:
    id: int
    title: str
    username: str
    kind: str


@dataclass
class ChatHit:
    chat: ChatInfo
    entity: Any = field(repr=False, compare=False)
    hits: dict[str, int] = field(default_factory=dict)
    latest: datetime | None = None

    @property
    def total(self) -> int:
        return sum(self.hits.values())


def _info(entity: Any) -> ChatInfo:
    return ChatInfo(
        id=int(entity.id),
        title=chat_name(entity),
        username=getattr(entity, "username", None) or "",
        kind=chat_kind(entity),
    )


# --- client -------------------------------------------------------------------------------


ClientFactory = Callable[[str, int, str], Any]


def _telethon_factory(session: str, api_id: int, api_hash: str) -> Any:
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise LiveConfigError(
            "live mode needs Telethon: pip install 'telegram-smart-search[live]'"
        ) from exc
    return TelegramClient(session, api_id, api_hash)


@asynccontextmanager
async def open_client(
    creds: Credentials,
    session: Path,
    *,
    interactive: bool = False,
    factory: ClientFactory = _telethon_factory,
) -> AsyncIterator[Any]:
    """Connect with an existing session. Only ``interactive`` may start a phone login."""
    session.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = factory(str(session), creds.api_id, creds.api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if not interactive:
                raise LiveConfigError(
                    f"the session at {session}.session is not logged in; "
                    "run `tgsearch auth` in a terminal"
                )
            await client.start()  # Telethon asks for phone, code and 2FA password here
        yield client
    finally:
        await client.disconnect()


def _run(
    coro_fn: Callable[[Any], Any],
    session: Path | None,
    interactive: bool = False,
    creds: Credentials | None = None,
) -> Any:
    creds = creds or resolve()
    path = session or default_session_path()

    async def runner() -> Any:
        async with open_client(creds, path, interactive=interactive) as client:
            return await coro_fn(client)

    return asyncio.run(runner())


# --- operations ---------------------------------------------------------------------------


async def _check(client: Any) -> dict[str, int]:
    counts = {"channel": 0, "supergroup": 0, "group": 0, "user": 0}
    async for dialog in client.iter_dialogs():
        counts[chat_kind(dialog.entity)] += 1
    return counts


def check_session(
    session: Path | None = None,
    *,
    interactive: bool = False,
    creds: Credentials | None = None,
) -> dict[str, int]:
    """Connect, log in if allowed, and count dialogs by kind (no names are returned)."""
    return _run(_check, session, interactive, creds)


async def _list_chats(client: Any, include_groups: bool) -> list[ChatInfo]:
    out = []
    async for dialog in client.iter_dialogs():
        if _allowed(dialog.entity, include_groups):
            out.append(_info(dialog.entity))
    return out


def list_chats(session: Path | None = None, *, include_groups: bool = False) -> list[ChatInfo]:
    return _run(lambda c: _list_chats(c, include_groups), session)


async def _discover(
    client: Any,
    queries: Sequence[str],
    since: datetime | None,
    limit: int,
    include_groups: bool,
    accept: Callable[[str], bool] | None,
) -> list[ChatHit]:
    """Telegram's global search over every chat the account is in, grouped by chat."""
    found: dict[int, ChatHit] = {}
    for query in queries:
        async for msg in client.iter_messages(None, search=query, limit=limit, wait_time=1):
            if since and msg.date and msg.date < since:
                break  # global search returns newest first
            if getattr(msg, "is_private", False):
                continue
            entity = await msg.get_chat()
            if entity is None or not _allowed(entity, include_groups):
                continue
            if accept and not accept(getattr(msg, "message", None) or ""):
                continue
            hit = found.get(int(entity.id))
            if hit is None:
                hit = found[int(entity.id)] = ChatHit(_info(entity), entity)
            hit.hits[query] = hit.hits.get(query, 0) + 1
            if msg.date and (hit.latest is None or msg.date > hit.latest):
                hit.latest = msg.date
    return sorted(found.values(), key=lambda h: (-h.total, h.chat.title))


def discover_chats(
    queries: Sequence[str],
    *,
    since: datetime | None = None,
    limit: int = 500,
    include_groups: bool = False,
    accept: Callable[[str], bool] | None = None,
    session: Path | None = None,
) -> list[ChatHit]:
    return _run(lambda c: _discover(c, queries, since, limit, include_groups, accept), session)


def _server_terms(terms: Sequence[str], any_term: bool) -> list[str | None]:
    # Telegram's search matches one string. For AND queries send the longest term and
    # filter the rest locally; for OR queries send each term.
    if not terms:
        return [None]
    if any_term:
        return list(terms)
    return [max(terms, key=len)]


Accept = Callable[[Message, str], bool]


@dataclass
class FetchStats:
    read: int = 0  # messages returned by Telegram's search
    kept: int = 0
    dropped: int = 0  # failed the local filter; discarded in memory, never stored


async def _collect(
    client: Any,
    chats: Sequence[Any],
    terms: Sequence[str],
    any_term: bool,
    limit: int,
    since: datetime | None,
    accept: Accept | None = None,
    stats: FetchStats | None = None,
) -> list[Message]:
    stats = stats if stats is not None else FetchStats()
    out: list[Message] = []
    for chat in chats:
        entity = await client.get_entity(coerce_chat(chat)) if isinstance(chat, str) else chat
        seen: set[int] = set()
        for term in _server_terms(terms, any_term):
            async for msg in client.iter_messages(entity, search=term, limit=limit):
                if since and msg.date and msg.date < since:
                    break  # results are newest first
                if msg.id in seen:
                    continue
                seen.add(msg.id)
                stats.read += 1
                message = to_message(entity, msg)
                if message is None or (accept and not accept(message, chat_kind(entity))):
                    stats.dropped += 1
                    continue
                stats.kept += 1
                out.append(message)
    return out


def fetch_messages(
    chats: Sequence[Any],
    terms: Sequence[str],
    *,
    any_term: bool = False,
    limit: int = 200,
    since: datetime | None = None,
    session: Path | None = None,
    interactive: bool = False,
    accept: Accept | None = None,
) -> list[Message]:
    """Search each chat (``@name``, link, id, or a Telethon entity) for ``terms``."""
    return _run(
        lambda c: _collect(c, chats, terms, any_term, limit, since, accept),
        session,
        interactive,
    )


async def _discover_and_collect(
    client: Any,
    queries: Sequence[str],
    since: datetime | None,
    discover_limit: int,
    limit: int,
    include_groups: bool,
    discover_accept: Callable[[str], bool] | None,
    accept: Accept | None,
) -> tuple[list[ChatHit], list[Message], FetchStats]:
    hits = await _discover(client, queries, since, discover_limit, include_groups, discover_accept)
    stats = FetchStats()
    entities = [h.entity for h in hits]
    messages = await _collect(client, entities, queries, True, limit, since, accept, stats)
    return hits, messages, stats


def discover_and_fetch(
    queries: Sequence[str],
    *,
    since: datetime | None = None,
    discover_limit: int = 500,
    limit: int = 300,
    include_groups: bool = False,
    discover_accept: Callable[[str], bool] | None = None,
    accept: Accept | None = None,
    session: Path | None = None,
) -> tuple[list[ChatHit], list[Message], FetchStats]:
    """Find the chats with hits, then read each one's own search results in full.

    Global search is a discovery step only: its results can be truncated, so every
    chat it finds is searched again on its own, for every query. ``accept`` runs on
    each message before it is kept; rejected messages are only counted.
    """
    return _run(
        lambda c: _discover_and_collect(
            c, queries, since, discover_limit, limit, include_groups, discover_accept, accept
        ),
        session,
    )
