"""Query matching and the search pipeline."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .dedupe import dedupe
from .extract import extract_offer
from .models import Message, Offer
from .textutil import normalize


def _term_pattern(term: str) -> re.Pattern[str]:
    # Prefix match on word start: "note" matches "notebook" but not "keynote".
    words = normalize(term).split()
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words))


@dataclass
class Query:
    terms: Sequence[str] = ()
    exclude: Sequence[str] = ()
    any_term: bool = False
    _include: list[re.Pattern[str]] = field(init=False, repr=False)
    _exclude: list[re.Pattern[str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._include = [_term_pattern(t) for t in self.terms if t.strip()]
        self._exclude = [_term_pattern(t) for t in self.exclude if t.strip()]

    def matches(self, text: str) -> bool:
        norm = normalize(text)
        if any(p.search(norm) for p in self._exclude):
            return False
        if not self._include:
            return True
        hits = (p.search(norm) for p in self._include)
        return any(hits) if self.any_term else all(hits)


@dataclass
class Filters:
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_unpriced: bool = False

    def date_ok(self, message: Message) -> bool:
        if message.date is None:
            return self.since is None and self.until is None
        if self.since and message.date < self.since:
            return False
        return not (self.until and message.date >= self.until)

    def offer_ok(self, offer: Offer) -> bool:
        if not self.include_unpriced and not offer.has_pricing:
            return False
        if self.min_price is None and self.max_price is None:
            return True
        price = offer.best_price
        if price is None:
            return False
        if self.min_price is not None and price < self.min_price:
            return False
        return not (self.max_price is not None and price > self.max_price)


@dataclass
class SearchStats:
    scanned: int = 0
    matched: int = 0
    offers: int = 0
    merged: int = 0


def run_search(
    messages: Iterable[Message],
    query: Query,
    filters: Filters,
    *,
    deduplicate: bool = True,
    sort: str = "date",
) -> tuple[list[Offer], SearchStats]:
    stats = SearchStats()
    offers = []
    for message in messages:
        stats.scanned += 1
        if not message.text.strip() or not filters.date_ok(message):
            continue
        if not query.matches(message.text):
            continue
        stats.matched += 1
        offer = extract_offer(message)
        if filters.offer_ok(offer):
            offers.append(offer)

    if deduplicate:
        before = len(offers)
        offers = dedupe(offers)
        stats.merged = before - len(offers)
    stats.offers = len(offers)
    return sort_offers(offers, sort), stats


def sort_offers(offers: list[Offer], sort: str) -> list[Offer]:
    if sort == "price":
        return sorted(offers, key=lambda o: (o.best_price is None, o.best_price or 0))
    # newest first; undated messages last
    dated = sorted(
        (o for o in offers if o.message.date), key=lambda o: o.message.date, reverse=True
    )
    return dated + [o for o in offers if not o.message.date]
