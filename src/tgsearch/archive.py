"""Local JSON Lines archive of fetched messages, so analysis can be re-run offline.

An archive holds raw message text, so it is written with ``0600`` permissions,
by default under ``$XDG_DATA_HOME/tgsearch/archives/`` (outside the project).
One JSON object per line; later lines for the same chat and message id replace
earlier ones, so appending a newer fetch keeps the latest edit of a post.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from .models import Message

FORMAT_VERSION = 1


def default_archive_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "tgsearch" / "archives"


def _record(message: Message) -> dict:
    return {
        "v": FORMAT_VERSION,
        "chat": message.chat,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "date": message.date.isoformat() if message.date else None,
        "text": message.text,
        "urls": list(message.urls),
        "link": message.link,
        "forward_ref": message.forward_ref,
    }


def write_archive(messages: Iterable[Message], path: Path, *, append: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    os.chmod(path, 0o600)
    count = 0
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for message in messages:
            fh.write(json.dumps(_record(message), ensure_ascii=False) + "\n")
            count += 1
    return count


def _message(data: dict) -> Message:
    date = data.get("date")
    return Message(
        chat=str(data["chat"]),
        message_id=int(data["message_id"]),
        date=datetime.fromisoformat(date) if date else None,
        text=str(data.get("text") or ""),
        urls=tuple(data.get("urls") or ()),
        link=str(data.get("link") or ""),
        chat_id=data.get("chat_id"),
        forward_ref=str(data.get("forward_ref") or ""),
    )


def read_archive(path: Path) -> Iterator[Message]:
    latest: dict[tuple, Message] = {}
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                message = _message(json.loads(line))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{number}: not a tgsearch archive line") from exc
            latest[(message.chat_id or message.chat, message.message_id)] = message
    yield from latest.values()
