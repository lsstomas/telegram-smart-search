"""Search chats live through the user's own Telegram account (Telethon, MTProto).

Credentials come only from the environment (``TELEGRAM_API_ID`` and
``TELEGRAM_API_HASH``); they are never written anywhere by this tool. The
Telethon session file is stored outside the project by default
(``$XDG_DATA_HOME/tgsearch/``), with ``0700`` directory permissions.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Message

ENV_API_ID = "TELEGRAM_API_ID"
ENV_API_HASH = "TELEGRAM_API_HASH"
ENV_SESSION = "TGSEARCH_SESSION"


class LiveConfigError(RuntimeError):
    """Raised when live mode cannot start (missing credentials or dependency)."""


@dataclass(frozen=True)
class Credentials:
    api_id: int
    api_hash: str

    def __repr__(self) -> str:  # never leak the hash into logs or tracebacks
        return "Credentials(api_id=<hidden>, api_hash=<hidden>)"


def credentials_from_env(env: dict[str, str] | None = None) -> Credentials:
    env = os.environ if env is None else env
    api_id = env.get(ENV_API_ID, "").strip()
    api_hash = env.get(ENV_API_HASH, "").strip()
    if not api_id or not api_hash:
        raise LiveConfigError(
            f"live mode needs {ENV_API_ID} and {ENV_API_HASH} in the environment "
            "(get them at https://my.telegram.org)"
        )
    if not api_id.isdigit():
        raise LiveConfigError(f"{ENV_API_ID} must be numeric")
    return Credentials(int(api_id), api_hash)


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


def message_link(entity: Any, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        return f"https://t.me/c/{entity.id}/{message_id}"
    return ""


def chat_name(entity: Any) -> str:
    for attr in ("title", "username", "first_name"):
        value = getattr(entity, attr, None)
        if value:
            return str(value)
    return str(getattr(entity, "id", "unknown"))


def to_message(entity: Any, msg: Any) -> Message | None:
    text = getattr(msg, "message", None) or ""
    if not text.strip():
        return None
    urls = []
    for ent in getattr(msg, "entities", None) or []:
        url = getattr(ent, "url", None)  # MessageEntityTextUrl
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.append(url)
    return Message(
        chat=chat_name(entity),
        message_id=int(msg.id),
        date=getattr(msg, "date", None),
        text=text,
        urls=tuple(dict.fromkeys(urls)),
        link=message_link(entity, int(msg.id)),
    )


async def _collect(
    client: Any,
    chats: Sequence[str],
    terms: Sequence[str],
    any_term: bool,
    limit: int,
    since: datetime | None,
) -> list[Message]:
    # Telegram's server-side search matches one string. For AND queries send the
    # longest term and filter the rest locally; for OR queries send each term.
    if not terms:
        server_terms: list[str | None] = [None]
    elif any_term:
        server_terms = list(terms)
    else:
        server_terms = [max(terms, key=len)]

    out: list[Message] = []
    for chat in chats:
        entity = await client.get_entity(coerce_chat(chat))
        seen: set[int] = set()
        for term in server_terms:
            async for msg in client.iter_messages(entity, search=term, limit=limit):
                if since and msg.date and msg.date < since:
                    break  # results are newest first
                if msg.id in seen:
                    continue
                seen.add(msg.id)
                message = to_message(entity, msg)
                if message:
                    out.append(message)
    return out


def fetch_messages(
    chats: Sequence[str],
    terms: Sequence[str],
    *,
    any_term: bool = False,
    limit: int = 200,
    since: datetime | None = None,
    session: Path | None = None,
    credentials: Credentials | None = None,
) -> list[Message]:
    creds = credentials or credentials_from_env()
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise LiveConfigError(
            "live mode needs Telethon: pip install 'telegram-smart-search[live]'"
        ) from exc

    session_path = session or default_session_path()
    session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def runner() -> list[Message]:
        client = TelegramClient(str(session_path), creds.api_id, creds.api_hash)
        async with client:  # prompts for phone/code on first run only
            return await _collect(client, chats, terms, any_term, limit, since)

    return asyncio.run(runner())
