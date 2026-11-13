"""Plain data types shared by sources, extraction, dedupe and output."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Message:
    """A Telegram message, independent of where it came from."""

    chat: str
    message_id: int
    date: datetime | None
    text: str
    urls: tuple[str, ...] = ()
    link: str = ""
    chat_id: int | None = None
    # "<channel id>:<post id>" of the original post when this message is a forward.
    forward_ref: str = ""

    @property
    def ref(self) -> str:
        return self.link or f"{self.chat}#{self.message_id}"


@dataclass
class Offer:
    """Structured data extracted from a single message."""

    message: Message
    title: str
    price: Decimal | None = None
    original_price: Decimal | None = None
    pix_price: Decimal | None = None
    currency: str | None = None
    installments: int | None = None
    installment_value: Decimal | None = None
    interest_free: bool = False
    payment_methods: tuple[str, ...] = ()
    coupon: str | None = None
    url: str | None = None
    duplicates: int = 0
    also_seen: list[str] = field(default_factory=list)

    @property
    def best_price(self) -> Decimal | None:
        """Lowest upfront price found (regular or Pix)."""
        prices = [p for p in (self.price, self.pix_price) if p is not None]
        return min(prices) if prices else None

    @property
    def has_pricing(self) -> bool:
        return self.best_price is not None or self.installment_value is not None
