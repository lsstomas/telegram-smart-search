"""CSV output."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from decimal import Decimal
from typing import TextIO

from .models import Offer
from .textutil import collapse_ws

COLUMNS = (
    "date",
    "chat",
    "message_id",
    "link",
    "title",
    "price",
    "original_price",
    "pix_price",
    "best_price",
    "currency",
    "installments",
    "installment_value",
    "interest_free",
    "payment_methods",
    "coupon",
    "url",
    "duplicates",
    "also_seen",
    "text",
)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: str) -> str:
    """Neutralise spreadsheet formula injection from untrusted message text."""
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}"


def offer_row(offer: Offer) -> dict[str, str]:
    msg = offer.message
    row = {
        "date": msg.date.isoformat() if msg.date else "",
        "chat": msg.chat,
        "message_id": str(msg.message_id),
        "link": msg.link,
        "title": offer.title,
        "price": _money(offer.price),
        "original_price": _money(offer.original_price),
        "pix_price": _money(offer.pix_price),
        "best_price": _money(offer.best_price),
        "currency": offer.currency or "",
        "installments": "" if offer.installments is None else str(offer.installments),
        "installment_value": _money(offer.installment_value),
        "interest_free": "yes" if offer.interest_free else "",
        "payment_methods": ";".join(offer.payment_methods),
        "coupon": offer.coupon or "",
        "url": offer.url or "",
        "duplicates": str(offer.duplicates),
        "also_seen": " ".join(offer.also_seen),
        "text": collapse_ws(msg.text),
    }
    return {k: _safe(v) for k, v in row.items()}


def write_csv(offers: Iterable[Offer], stream: TextIO, delimiter: str = ",") -> int:
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, delimiter=delimiter)
    writer.writeheader()
    count = 0
    for offer in offers:
        writer.writerow(offer_row(offer))
        count += 1
    return count
