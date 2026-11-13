"""Conservative deduplication of offers.

Two offers are merged only when there is strong evidence they are the same deal:

* the very same message (same chat and message id), or
* the same best price, currency and coupon AND either
  - the same canonical product URL, or
  - the same normalised message text (at least ``MIN_TEXT_KEY_LEN`` chars).

A price change, a different coupon, or a different link with different wording
keeps both rows.  False negatives (an unmerged duplicate) are preferred over
false positives (two distinct deals collapsed into one row).  The earliest
message is kept; later ones are counted in ``duplicates`` and listed in
``also_seen``.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterable
from datetime import datetime, timezone

from .models import Offer
from .textutil import URL_RE, canonical_url, collapse_ws, normalize

MIN_TEXT_KEY_LEN = 20
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def text_key(text: str) -> str:
    without_urls = URL_RE.sub(" ", text)
    return collapse_ws(re.sub(r"[\W_]+", " ", normalize(without_urls)))


def dedupe_keys(offer: Offer) -> list[Hashable]:
    msg = offer.message
    keys: list[Hashable] = [("msg", msg.chat, msg.message_id)]
    deal = (offer.best_price, offer.currency, offer.coupon)
    if offer.url and offer.best_price is not None:
        keys.append(("url", canonical_url(offer.url), *deal))
    tkey = text_key(msg.text)
    if len(tkey) >= MIN_TEXT_KEY_LEN:
        keys.append(("text", tkey, *deal))
    return keys


def _date_key(offer: Offer) -> datetime:
    return offer.message.date or _FAR_FUTURE


def dedupe(offers: Iterable[Offer]) -> list[Offer]:
    kept: list[Offer] = []
    index: dict[Hashable, Offer] = {}
    for offer in sorted(offers, key=_date_key):
        keys = dedupe_keys(offer)
        if keys[0] in index:
            continue  # the same message seen twice (overlapping inputs) is not a repost
        existing = next((index[k] for k in keys if k in index), None)
        if existing is None:
            kept.append(offer)
            for key in keys:
                index.setdefault(key, offer)
            continue
        existing.duplicates += 1
        existing.also_seen.append(offer.message.ref)
        for key in keys:
            index.setdefault(key, existing)
    return kept
