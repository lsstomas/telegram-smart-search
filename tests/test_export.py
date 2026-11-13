import json

import pytest

from tgsearch.sources.export import read_export


def test_single_chat_export(fixtures):
    msgs = list(read_export(fixtures / "export_single.json"))
    ids = [m.message_id for m in msgs]
    assert 1 not in ids  # service message
    assert 14 in ids
    first = next(m for m in msgs if m.message_id == 10)
    assert first.chat == "Ofertas Tech"
    assert first.link == "https://t.me/c/1234567890/10"
    assert first.urls == (
        "https://www.amazon.com.br/dp/B0TEST123?utm_source=telegram&tag=canal-20",
    )
    assert "Comprar" in first.text
    assert first.date.tzinfo is not None


def test_caption_is_used_for_media(fixtures):
    msgs = {m.message_id: m for m in read_export(fixtures / "export_single.json")}
    assert "Acer Aspire 5" in msgs[15].text


def test_full_export(fixtures):
    msgs = list(read_export(fixtures / "export_full.json"))
    assert {m.chat for m in msgs} == {"Promo Group", "Alice"}
    alice = next(m for m in msgs if m.chat == "Alice")
    assert alice.link == ""
    assert alice.date is not None  # falls back to the ISO "date" field


def test_invalid_export(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(ValueError):
        list(read_export(bad))
