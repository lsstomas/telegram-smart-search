"""Search profiles: what to look for, how to recognise it, and which prices are plausible.

A profile turns any search into a study: the Telegram queries to send, a local
classifier that decides whether a post is about the target (and why not), and
an optional plausible price band. ``terms_profile`` covers any product
("air fryer", "ssd 1tb"); ``gpu_profile`` adds strict graphics-card rules.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from . import gpu
from .gpu import Classification
from .models import Message
from .search import Query
from .textutil import extract_urls, is_telegram_url

_AMOUNT = re.compile(r"(?:R\$|US\$|\$)\s?\d|\bpor\s*:?\s*\d[\d.,]*", re.IGNORECASE)


@dataclass(frozen=True)
class Profile:
    name: str
    targets: tuple[str, ...]
    labels: dict[str, str]
    queries: tuple[str, ...]  # Telegram search strings (each one is searched)
    classify: Callable[[str], Classification]
    plausible: tuple[Decimal, Decimal] | None = None

    def label(self, key: str | None) -> str:
        return self.labels.get(key or "", key or "")


def terms_profile(
    terms: Sequence[str],
    *,
    exclude: Sequence[str] = (),
    any_term: bool = False,
    plausible: tuple[Decimal, Decimal] | None = None,
) -> Profile:
    """Any product: a post matches when the terms match (accent- and case-insensitive)."""
    terms = [t for t in terms if t.strip()]
    if not terms:
        raise ValueError("a search needs at least one term or a profile")
    query = Query(terms=terms, exclude=exclude, any_term=any_term)
    label = (" | " if any_term else " ").join(terms)

    def classify(text: str) -> Classification:
        if query.matches(text):
            return Classification("query", "any", (), "")
        return Classification(None, "any", (), "no_target_model")

    # Telegram matches one string per search: AND sends the longest term, OR sends each.
    queries = tuple(terms) if any_term else (max(terms, key=len),)
    return Profile("terms", ("query",), {"query": label}, queries, classify, plausible)


GPU_QUERIES = {
    "rx7600": ("7600", "RX7600"),
    "rx7600xt": ("7600", "RX7600XT"),
    "rtx5050": ("5050", "RTX5050"),
}


def gpu_profile(models: Sequence[str] = ("rx7600", "rtx5050")) -> Profile:
    targets = tuple(dict.fromkeys(models))
    queries = tuple(dict.fromkeys(q for m in targets for q in GPU_QUERIES[m]))
    return Profile(
        "gpu",
        targets,
        {k: gpu.MODEL_BY_KEY[k].label for k in targets},
        queries,
        lambda text: gpu.classify(text, targets),
        (Decimal("700"), Decimal("6000")),
    )


def offer_filter(profile: Profile) -> Callable[[Message, str], bool]:
    """Local filter run before anything is stored.

    The post must be about the target and carry an amount. Outside broadcast
    channels, where ordinary members write, it must also link to a store, so
    questions and chatter are dropped.
    """

    def accept(message: Message, kind: str) -> bool:
        if not _AMOUNT.search(message.text):
            return False
        if profile.classify(message.text).reason == "no_target_model":
            return False
        if kind == "channel":
            return True
        urls = list(message.urls) + extract_urls(message.text)
        return any(not is_telegram_url(u) for u in urls)

    return accept


def mentions(profile: Profile) -> Callable[[str], bool]:
    return lambda text: profile.classify(text).reason != "no_target_model"
