from datetime import datetime, timedelta, timezone

from tgsearch.dedupe import dedupe
from tgsearch.extract import extract_offer
from tgsearch.textutil import canonical_url

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def offers_from(make_message, *specs):
    out = []
    for i, spec in enumerate(specs):
        text, chat = spec if isinstance(spec, tuple) else (spec, "chan")
        out.append(extract_offer(make_message(text, chat=chat, date=T0 + timedelta(hours=i))))
    return out


def test_canonical_url_strips_tracking_but_keeps_affiliate():
    a = canonical_url("http://www.Loja.com/p/1/?utm_source=tg&fbclid=1&tag=aff-20#top")
    b = canonical_url("https://loja.com/p/1?tag=aff-20")
    assert a == b == "https://loja.com/p/1?tag=aff-20"
    assert canonical_url("https://loja.com/p/1?tag=other") != b
    assert canonical_url("https://loja.com/p/1?id=2&color=red") == canonical_url(
        "https://loja.com/p/1?color=red&id=2"
    )


def test_same_url_and_price_merge(make_message):
    offers = offers_from(
        make_message,
        ("SSD 1TB por R$ 349 https://loja.com/ssd?utm_source=a", "A"),
        ("Corre! SSD com preço baixo R$ 349 https://www.loja.com/ssd", "B"),
    )
    result = dedupe(offers)
    assert len(result) == 1
    assert result[0].message.chat == "A"  # earliest kept
    assert result[0].duplicates == 1
    assert result[0].also_seen == ["B#" + str(offers[1].message.message_id)]


def test_same_url_different_price_kept(make_message):
    offers = offers_from(
        make_message,
        "SSD 1TB por R$ 349 https://loja.com/ssd",
        "SSD 1TB por R$ 329 https://loja.com/ssd",
    )
    assert len(dedupe(offers)) == 2


def test_different_coupon_kept(make_message):
    offers = offers_from(
        make_message,
        "SSD 1TB por R$ 349 cupom AAA111 https://loja.com/ssd",
        "SSD 1TB por R$ 349 cupom BBB222 https://loja.com/ssd",
    )
    assert len(dedupe(offers)) == 2


def test_same_text_different_short_links_merge(make_message):
    text = "Air fryer Mondial 4L por apenas R$ 299 no Pix"
    offers = offers_from(
        make_message,
        (f"{text} https://bit.ly/aaa", "A"),
        (f"{text}  https://bit.ly/bbb", "B"),
    )
    assert len(dedupe(offers)) == 1


def test_similar_but_not_identical_text_kept(make_message):
    offers = offers_from(
        make_message,
        "Air fryer Mondial 4L por apenas R$ 299 https://bit.ly/aaa",
        "Air fryer Mondial 5L por apenas R$ 299 https://bit.ly/bbb",
    )
    assert len(dedupe(offers)) == 2


def test_short_texts_do_not_merge_without_url(make_message):
    offers = offers_from(make_message, "R$ 10", "R$ 10")
    assert len(dedupe(offers)) == 2


def test_unpriced_url_only_does_not_merge(make_message):
    offers = offers_from(
        make_message,
        "Olha isso https://loja.com/x",
        "Tem cupom? https://loja.com/x",
    )
    assert len(dedupe(offers)) == 2


def test_same_message_twice_is_not_counted(make_message):
    msg = make_message("SSD 1TB por R$ 349 https://loja.com/ssd", message_id=5)
    result = dedupe([extract_offer(msg), extract_offer(msg)])
    assert len(result) == 1
    assert result[0].duplicates == 0


def test_earliest_is_kept_regardless_of_input_order(make_message):
    late = make_message(
        "SSD 1TB por R$ 349 https://loja.com/ssd", chat="late", date=T0 + timedelta(days=2)
    )
    early = make_message("SSD 1TB por R$ 349 https://loja.com/ssd", chat="early", date=T0)
    result = dedupe([extract_offer(late), extract_offer(early)])
    assert [o.message.chat for o in result] == ["early"]
