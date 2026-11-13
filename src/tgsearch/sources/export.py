"""Read messages from a Telegram Desktop "Export chat history" JSON file (result.json).

Works fully offline. Both a single-chat export (top-level ``messages``) and a
full account export (``chats.list[*].messages``) are supported.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Message

_CHANNEL_TYPES = {"public_channel", "private_channel", "public_supergroup", "private_supergroup"}


def _flatten_text(text: Any) -> tuple[str, list[str]]:
    """Export text is either a string or a list of strings / entity dicts."""
    if isinstance(text, str):
        return text, []
    parts: list[str] = []
    hrefs: list[str] = []
    for item in text or []:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            parts.append(str(item.get("text", "")))
            href = item.get("href")
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                hrefs.append(href)
    return "".join(parts), hrefs


def _parse_date(raw: dict[str, Any]) -> datetime | None:
    unix = raw.get("date_unixtime")
    if unix not in (None, ""):
        try:
            return datetime.fromtimestamp(int(unix), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    date = raw.get("date")
    if isinstance(date, str):
        try:
            parsed = datetime.fromisoformat(date)
        except ValueError:
            return None
        # Exports without unixtime use local time; treat it as UTC for comparisons.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _link(chat: dict[str, Any], message_id: int) -> str:
    chat_id = chat.get("id")
    if chat.get("type") in _CHANNEL_TYPES and isinstance(chat_id, int):
        # The export stores the bare channel id; t.me/c/ links work for members.
        bare = str(chat_id).removeprefix("-100").lstrip("-")
        return f"https://t.me/c/{bare}/{message_id}"
    return ""


def _chat_messages(chat: dict[str, Any]) -> Iterator[Message]:
    name = str(chat.get("name") or chat.get("id") or "unknown")
    for raw in chat.get("messages") or []:
        if not isinstance(raw, dict) or raw.get("type", "message") != "message":
            continue
        text, hrefs = _flatten_text(raw.get("text"))
        caption, caption_hrefs = _flatten_text(raw.get("caption", ""))
        if caption and caption not in text:
            text = f"{text}\n{caption}" if text else caption
        if not text.strip():
            continue
        message_id = int(raw.get("id", 0))
        yield Message(
            chat=name,
            message_id=message_id,
            date=_parse_date(raw),
            text=text,
            urls=tuple(dict.fromkeys(hrefs + caption_hrefs)),
            link=_link(chat, message_id),
        )


def read_export(path: str | Path) -> Iterator[Message]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a Telegram export (expected a JSON object)")
    if "messages" in data:
        yield from _chat_messages(data)
    elif isinstance(data.get("chats"), dict):
        for chat in data["chats"].get("list") or []:
            if isinstance(chat, dict):
                yield from _chat_messages(chat)
    else:
        raise ValueError(f"{path}: no 'messages' or 'chats.list' found; is this result.json?")
