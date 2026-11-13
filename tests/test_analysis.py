import csv
import json
import stat
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from tgsearch.analysis import analyse, describe, method_values, store_of
from tgsearch.archive import write_archive
from tgsearch.cli import main
from tgsearch.models import Message

T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)

CARD = "Placa de Vídeo ASRock Radeon RX 7600 Challenger 8GB"


def _m(mid, text, *, chat="A", chat_id=1, hours=0, urls=(), fwd=""):
    return Message(
        chat=chat,
        message_id=mid,
        date=T0 + timedelta(hours=hours),
        text=text,
        urls=tuple(urls),
        link=f"https://t.me/{chat}/{mid}",
        chat_id=chat_id,
        forward_ref=fwd,
    )


def _by_id(analysis):
    return {(p.message.chat, p.message.message_id): p for p in analysis.posts}


def test_store_detection():
    assert store_of(["https://s.shopee.com.br/x"], "") == "shopee"
    assert store_of(["https://www.kabum.com.br/produto/1"], "") == "kabum"
    assert store_of(["https://short.ly/x"], "Oferta na Kabum") == "kabum"
    assert store_of([], "Kabum ou Pichau") == ""


def test_mirrored_text_is_merged_but_price_change_is_kept():
    text = f"{CARD}\n💰 R$ 1.799,00\nhttps://kabum.com.br/p/1"
    cheaper = f"{CARD}\n💰 R$ 1.699,00\nhttps://kabum.com.br/p/1"
    a = analyse(
        [
            _m(1, text),
            _m(2, text, chat="B", chat_id=2, hours=1),
            _m(3, cheaper, chat="B", chat_id=2, hours=2),
        ]
    )
    posts = _by_id(a)
    assert posts[("A", 1)].status == "included"
    assert posts[("B", 2)].status == "duplicate"
    assert posts[("B", 2)].reason == "same_text_and_prices"
    assert posts[("B", 2)].duplicate_of is posts[("A", 1)]
    assert posts[("B", 3)].status == "included"


def test_different_coupon_or_method_is_not_merged():
    base = f"{CARD}\n💰 R$ 1.799,00 com cupom: SAVE10\nhttps://kabum.com.br/p/1"
    other = f"{CARD}\n💰 R$ 1.799,00 com cupom: OUTRO10\nhttps://kabum.com.br/p/1"
    pix = f"{CARD}\n💰 R$ 1.799,00 no pix com cupom: SAVE10\nhttps://kabum.com.br/p/1"
    a = analyse([_m(1, base), _m(2, other, hours=1), _m(3, pix, hours=2)])
    assert {p.status for p in a.posts} == {"included"}


def test_forwards_of_the_same_post_are_merged():
    text = f"{CARD}\n💰 R$ 1.799,00"
    a = analyse(
        [
            _m(10, text, chat="Src", chat_id=555),
            _m(1, text + "\nvia Src", chat="B", chat_id=2, hours=1, fwd="555:10"),
        ]
    )
    posts = _by_id(a)
    assert posts[("B", 1)].reason == "same_forwarded_post"


def test_probable_reposts_are_grouped_not_merged():
    a_text = f"{CARD}\n💰 R$ 1.799,00\nhttps://s.shopee.com.br/aaa"
    b_text = "🔥 RX 7600 Challenger placa de vídeo\n💵 R$ 1.799,00\nhttps://s.shopee.com.br/bbb"
    late = "RX 7600 placa de vídeo 8GB top\n💵 R$ 1.799,00\nhttps://s.shopee.com.br/ccc"
    a = analyse(
        [
            _m(1, a_text),
            _m(2, b_text, chat="B", chat_id=2, hours=5),
            _m(3, late, chat="C", chat_id=3, hours=5 + 100),
        ]
    )
    posts = _by_id(a)
    assert all(p.status == "included" for p in a.posts)
    assert posts[("A", 1)].probable_group == posts[("B", 2)].probable_group == 1
    assert posts[("C", 3)].probable_group is None  # more than 72 h after the group
    assert method_values(a.posts, "rx7600", "unspecified") == [D("1799.00")] * 3
    assert len(method_values(a.posts, "rx7600", "unspecified", collapse_probable=True)) == 2


def test_exclusions_and_conflicts():
    a = analyse(
        [
            _m(1, "Notebook ASUS V16 RTX 5050\nR$ 5.999"),
            _m(2, "Processador Ryzen 5 7600 R$ 899"),
            _m(3, f"{CARD}\nsem preço"),
            _m(4, f"{CARD}\nR$ 1.799 no pix\nR$ 1.699 no pix"),
            _m(5, f"{CARD}\nR$ 99 no pix"),
        ]
    )
    posts = _by_id(a)
    assert ("A", 2) not in posts  # not a target model at all
    assert posts[("A", 1)].reason == "category_laptop"
    assert posts[("A", 3)].reason == "no_price_found"
    assert posts[("A", 4)].reason == "no_usable_price"
    assert posts[("A", 4)].method_notes == {"pix": "conflicting_amounts"}
    assert posts[("A", 5)].method_notes == {"pix": "implausible_amount"}


def test_window_filter():
    text = f"{CARD}\nR$ 1.799"
    a = analyse([_m(1, text), _m(2, text + " ", hours=-48)], since=T0 - timedelta(hours=1))
    assert [p.message.message_id for p in a.posts] == [1]


def test_describe_mean_and_median_are_exact():
    s = describe([D("1799.00"), D("1699.99"), D("1899.00"), D("1700.00")])
    assert (s.n, s.mean, s.median, s.minimum, s.maximum) == (
        4,
        D("1774.50"),
        D("1749.50"),
        D("1699.99"),
        D("1899.00"),
    )
    assert describe([]) is None


def test_gpu_report_end_to_end(tmp_path, capsys):
    archive = tmp_path / "a.jsonl"
    msgs = [
        _m(1, f"{CARD}\n💠 R$ 1.599,99 no Pix\n💳 ou R$ 1.799,99 em até 10x sem juros"),
        _m(2, f"{CARD}\n💠 R$ 1.599,99 no Pix\n💳 ou R$ 1.799,99 em até 10x sem juros", hours=1),
        _m(3, f"{CARD}\n💠 R$ 1.699,00 no Pix", hours=2),
        _m(4, "Placa de vídeo GeForce RTX 5050 8GB\n💵 R$ 2.115\n⭐ 10x sem juros", hours=3),
        _m(5, "=cmd() RTX 5050 notebook R$ 5.999", hours=4),
    ]
    write_archive(msgs, archive)
    out = tmp_path / "report"
    code = main(
        [
            "gpu-report",
            str(archive),
            "--since",
            "2026-10-30",
            "--until",
            "2026-11-02",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads((out / "summary.json").read_text())
    pix = data["statistics"]["rx7600"]["pix"]["posts"]
    assert pix == {
        "n": 2,
        "mean": "1649.50",
        "median": "1649.50",
        "min": "1599.99",
        "max": "1699.00",
    }
    inst = data["statistics"]["rx7600"]["installments_interest_free"]["posts"]
    assert inst["n"] == 1 and inst["median"] == "1799.99"
    rtx = data["statistics"]["rtx5050"]
    assert rtx["unspecified"]["posts"]["median"] == "2115.00"
    assert rtx["installments_interest_free"]["posts"]["median"] == "2115.00"
    assert data["counts"]["status"] == {"included": 3, "duplicate": 1, "excluded": 1}

    with open(out / "posts.csv", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    dup = next(r for r in rows if r["status"] == "duplicate")
    assert dup["duplicate_of"] == "https://t.me/A/1" and dup["hours_after_original"] == "1.0"
    assert next(r for r in rows if r["reason"] == "category_laptop")["text"].startswith("'=")
    with open(out / "price_points.csv", encoding="utf-8", newline="") as fh:
        points = list(csv.DictReader(fh))
    assert {p["source"] for p in points} >= {"stated", "price_with_terms"}
    assert all(p["used_in_statistics"] == "" for p in points if p["post_status"] != "included")
    for name in ("posts.csv", "price_points.csv", "summary.md", "summary.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    assert "Radeon RX 7600" in (out / "summary.md").read_text()
    assert str(out) in capsys.readouterr().out
