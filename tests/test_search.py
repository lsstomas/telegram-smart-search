from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from tgsearch.search import Filters, Query, run_search

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_all_terms_required_accent_and_case_insensitive():
    q = Query(terms=["cartao", "NOTEBOOK"])
    assert q.matches("Notebook Dell 12x no Cartão")
    assert not q.matches("Notebook Dell no Pix")


def test_prefix_word_match():
    q = Query(terms=["note"])
    assert q.matches("Notebooks em promoção")
    assert not q.matches("Keynote license")


def test_phrase_term():
    q = Query(terms=["air fryer"])
    assert q.matches("Air  Fryer Mondial")
    assert not q.matches("Air purifier and fryer")


def test_any_and_exclude():
    q = Query(terms=["ssd", "hd"], any_term=True, exclude=["usado"])
    assert q.matches("HD externo 2TB")
    assert q.matches("SSD 1TB")
    assert not q.matches("SSD usado")
    assert not q.matches("Memória RAM")


def test_empty_query_matches_everything():
    assert Query().matches("qualquer coisa")


def test_pipeline_filters(make_message):
    msgs = [
        make_message("SSD 1TB por R$ 349", date=T0),
        make_message("SSD 2TB por R$ 699", date=T0 + timedelta(days=1)),
        make_message("SSD chegando amanhã", date=T0 + timedelta(days=2)),
        make_message("SSD 500GB por R$ 199", date=T0 + timedelta(days=3)),
    ]
    offers, stats = run_search(msgs, Query(terms=["ssd"]), Filters(max_price=D("500")))
    assert [o.price for o in offers] == [D("199.00"), D("349.00")]  # newest first
    assert stats.scanned == 4 and stats.matched == 4 and stats.offers == 2

    offers, _ = run_search(msgs, Query(terms=["ssd"]), Filters(include_unpriced=True), sort="price")
    assert [o.price for o in offers][-1] is None
    assert offers[0].price == D("199.00")

    offers, _ = run_search(
        msgs,
        Query(terms=["ssd"]),
        Filters(since=T0 + timedelta(days=1), until=T0 + timedelta(days=2)),
    )
    assert [o.price for o in offers] == [D("699.00")]
