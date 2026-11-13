import stat
from datetime import datetime, timezone

import pytest

from tgsearch.archive import read_archive, write_archive
from tgsearch.models import Message


def _m(mid, text, chat_id=1):
    return Message(
        chat="Deals",
        message_id=mid,
        date=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
        text=text,
        urls=("https://loja.com/a",),
        link=f"https://t.me/deals/{mid}",
        chat_id=chat_id,
        forward_ref="9:3" if mid == 2 else "",
    )


def test_round_trip_is_private_and_keeps_latest_edit(tmp_path):
    path = tmp_path / "a" / "fetch.jsonl"
    write_archive([_m(1, "RX 7600 R$ 1.499"), _m(2, "Cupom ÇÃO")], path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    write_archive([_m(1, "RX 7600 R$ 1.399 (edited)")], path, append=True)

    messages = sorted(read_archive(path), key=lambda m: m.message_id)
    assert [m.text for m in messages] == ["RX 7600 R$ 1.399 (edited)", "Cupom ÇÃO"]
    assert messages[1].forward_ref == "9:3"
    assert messages[0].date == datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    assert messages[0].urls == ("https://loja.com/a",)


def test_same_id_in_different_chats_is_kept(tmp_path):
    path = tmp_path / "f.jsonl"
    write_archive([_m(1, "a", chat_id=1), _m(1, "b", chat_id=2)], path)
    assert len(list(read_archive(path))) == 2


def test_bad_line_reports_position(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_text('{"chat": "x", "message_id": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=":2:"):
        list(read_archive(path))
