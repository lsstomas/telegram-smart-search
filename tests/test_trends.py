import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from tgsearch import trends
from tgsearch.analysis import analyse, top_offers
from tgsearch.archive import write_archive
from tgsearch.cli import main
from tgsearch.models import Message
from tgsearch.report_html import render

CARD = "Placa de Vídeo ASRock Radeon RX 7600 Challenger 8GB"
# 2026-11-02 is a Monday; 15:00 UTC is 12:00 in São Paulo.
MON = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)


def _m(mid, day_offset, price, *, chat="A", store="kabum", extra=""):
    return Message(
        chat=chat,
        message_id=mid,
        date=MON + timedelta(days=day_offset),
        text=f"{CARD} {mid}\n💰 R$ {price}\n{extra}\nhttps://{store}.com.br/p/{mid}",
        link=f"https://t.me/{chat}/{mid}",
        chat_id=1 if chat == "A" else 2,
    )


def test_calendar_helpers():
    assert trends.black_friday(2026) == date(2026, 11, 27)
    assert trends.black_friday(2025) == date(2025, 11, 28)
    assert trends.event_for(date(2026, 11, 10)) == "11.11"
    assert trends.event_for(date(2026, 11, 27)) == "Black Friday"
    assert trends.event_for(date(2026, 11, 5)) == ""
    assert trends.percentile([D("1"), D("2"), D("3"), D("4")], 0.25) == D("1.75")


def test_late_night_utc_post_counts_for_the_brazilian_day():
    msg = _m(1, 0, "1.799")
    late = Message(**{**msg.__dict__, "date": datetime(2026, 11, 3, 1, tzinfo=timezone.utc)})
    post = analyse([late]).posts[0]
    assert trends.local_day(post) == date(2026, 11, 2)  # 22:00 on Monday in São Paulo


def _analysis():
    msgs = [
        _m(1, 0, "1.799"),  # Mon
        _m(2, 0, "1.749"),  # Mon, the day's low
        _m(3, 3, "1.699", extra="com cupom: SAVE10"),  # Thu
        _m(4, 4, "1.650,00", store="pichau"),  # Fri
        _m(5, 9, "1.899"),  # Wed 11.11
        # The same deal reposted in another chat with other wording: grouped, not merged
        _m(6, 9, "1.899", chat="B"),
    ]
    since = MON - timedelta(hours=15)  # midnight UTC on Monday
    return analyse(msgs, since=since, until=since + timedelta(days=14))


def test_compute_groups_and_thresholds():
    a = _analysis()
    t = trends.compute(a, "rx7600")
    assert t.start == date(2026, 11, 1) and t.end == date(2026, 11, 15)
    assert t.daily[date(2026, 11, 2)] == D("1749.00")
    assert len(t.deal_prices) == 5  # posts 5 and 6 are one deal
    mon = next(g for g in t.weekdays if g.label == "Mon")
    assert (mon.days_with_posts, mon.posts, mon.median_daily_low) == (1, 2, D("1749.00"))
    eleven = next(g for g in t.events if g.label == "11.11")
    assert eleven.best == D("1899.00") and eleven.days == 3
    first = {p: (n, gap) for p, n, gap in t.thresholds}
    assert first[D("1700.00")] == (2, 9)  # Thu and Fri; then none from Nov 7 to Nov 15
    # "com cupom" on its own line: a code is mentioned, the price is not said to need it
    assert t.coupon["coupon code mentioned"]["n"] == 1
    assert t.stores["pichau"]["min"] == D("1650.00")


def test_top_offers_one_per_listing_and_day():
    offers = top_offers(_analysis(), "rx7600", 10)
    # Mon has two kabum posts (1.799 and 1.749): the cheaper one stands for the day.
    assert [o.price.amount for o in offers] == [
        D("1650.00"),
        D("1699.00"),
        D("1749.00"),
        D("1899.00"),
    ]
    assert offers[2].reposts == 1  # the 1.799 post the same day
    assert offers[-1].reposts == 1  # the repost in chat B
    only_pichau = top_offers(_analysis(), "rx7600", 10, stores={"pichau"})
    assert [o.post.store for o in only_pichau] == ["pichau"]


def test_html_escapes_telegram_text_and_has_no_scripts():
    msgs = [
        _m(1, 0, "1.799", extra=""),
        Message(
            "A<b>",
            9,
            MON,
            "<script>alert(1)</script> Placa de vídeo RX 7600 8GB\nR$ 1.700",
            link="javascript:alert(1)",
            chat_id=1,
        ),
    ]
    a = analyse(msgs, since=MON - timedelta(days=1), until=MON + timedelta(days=1))
    t = {"rx7600": trends.compute(a, "rx7600")}
    html = render(
        a, {"rx7600": {}}, {"target_posts": 2, "chats": 2, "status": {}, "reasons": {}}, t, {}
    )
    assert "<script" not in html
    offers = {"rx7600": top_offers(a, "rx7600")}
    from tgsearch.analysis import statistics_table

    html = render(
        a,
        statistics_table(a),
        {"target_posts": 2, "chats": 2, "status": {}, "reasons": {}},
        t,
        offers,
    )
    assert "<script" not in html and "&lt;script&gt;" in html
    assert 'href="javascript' not in html
    assert "A&lt;b&gt;" in html


def test_generic_report_merges_overlapping_archives(tmp_path):
    a1, a2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    msgs = [
        Message("Deals", 1, MON, "Air Fryer Mondial 4L\nR$ 299,90 no pix", chat_id=1),
        Message("Deals", 2, MON, "Air Fryer Mondial usada R$ 150", chat_id=1),
        Message("Deals", 3, MON, "Liquidificador R$ 99", chat_id=1),
    ]
    write_archive(msgs, a1)
    write_archive(msgs[:1], a2)
    out = tmp_path / "r"
    code = main(
        [
            "report",
            "air fryer",
            "-x",
            "usada",
            "-a",
            str(a1),
            "-a",
            str(a2),
            "--since",
            "2026-11-01",
            "--until",
            "2026-11-03",
            "--out",
            str(out),
        ]
    )
    assert code == 0
    data = json.loads((out / "summary.json").read_text())
    assert data["counts"]["status"] == {"included": 1}
    assert data["statistics"]["query"]["pix"]["posts"]["median"] == "299.90"
    assert data["top_offers"]["query"][0]["method"] == "pix"
    assert (out / "report.html").read_text().startswith("<!doctype html>")


def test_report_without_terms_or_profile_fails_cleanly(tmp_path, capsys):
    archive = tmp_path / "a.jsonl"
    write_archive([], archive)
    assert main(["report", "-a", str(archive)]) == 1
    assert "at least one term" in capsys.readouterr().err
