"""Split an offer's prices by payment method: Pix, boleto, card in one payment,
installments, "à vista" without a named method, and prices with no payment cue.

Brazilian deal posts put one payment option per line ("💠 R$ 1.599 no Pix",
"💳 R$ 1.799 em até 10x sem juros"), so each amount is labelled from its own
line only. Installment totals are either stated ("R$ 1.799 em 10x") or computed
from the count and value ("10x de R$ 179,90" -> 1799.00); the ``source`` field
says which, so nothing computed is presented as quoted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from .extract import (
    _CUR,
    _NUM,
    _OTHER_AFTER,
    _OTHER_BEFORE,
    _SP,
    COUPON_RE,
    PRICE_RE,
    _coupon_ok,
    _currency,
    parse_amount,
)
from .textutil import normalize

METHODS = ("pix", "boleto", "card_1x", "installments", "upfront", "unspecified")
UPFRONT_METHODS = ("pix", "boleto", "card_1x", "upfront", "unspecified")

# "10x de R$ 179,90", "10 x R$179,90", "em 12x de R$ 150"
_INSTALLMENT_VALUE = re.compile(
    rf"(?<![\w.,])(?P<n>\d{{1,2}}){_SP}?[x×]{_SP}?(?:de{_SP}+)?"
    rf"(?P<cur>{_CUR}){_SP}{{0,2}}(?P<num>{_NUM})(?!\d)",
    re.IGNORECASE,
)
# An amount followed by "em até 10x", "parcelado em 12x", "/ 10x", "10x" (normalised text)
_INSTALLMENT_AFTER = re.compile(
    r"^\W*(?:(?:em|ate|parcelado|parcelados|no|cartao|credito|de|ou|/)\W+){0,4}"
    r"(?P<n>\d{1,2})\s?x(?![a-z0-9])"
)
_ORIGINAL = re.compile(r"(?:\bde|\bantes|\bera|\bnormal|\bpreco\s+cheio)\W*$")
_FROM = re.compile(r"\bpartir\W+de\W*$")
_PIX = re.compile(r"\bpix\b")
_BOLETO = re.compile(r"\bboleto\b")
_CARD = re.compile(r"\b(?:cartao|cartoes|credito|1x)\b|💳")
_UPFRONT = re.compile(r"\ba\s+vista\b")
_TOTAL_BEFORE = re.compile(r"(?:\btotal(?:\s+de)?|\bfinal|=)\W*$")
_SEM_JUROS = re.compile(r"\bsem\s+juros\b|\bs/\s*juros\b")
_COM_JUROS = re.compile(r"\bcom\s+juros\b|\bc/\s*juros\b")
_RATE = re.compile(r"(?P<r>\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*(?:a\.?\s?m\b|ao\s+mes)")
_WITH_COUPON = re.compile(
    r"\b(?:com|c/|usando|aplicando|aplique|use|utilizando|no)\s+(?:o\s+)?cupo[mn]\b"
)
_BARE_POR_PRICE = re.compile(
    rf"\bpor\s*:?\s*(?P<num>{_NUM})(?!\d)",
    re.IGNORECASE,
)
_BARE_UNIT_AFTER = re.compile(
    r"^\s*(?:x\b|×|gb\b|tb\b|mb\b|%|hz\b|w\b|mm\b|cm\b|m\b|kg\b|g\b"
    r"|horas?\b|dias?\b|mes(?:es)?\b|anos?\b|vezes?\b|unidades?\b)"
)
_NORM_COUPON_RE = re.compile(
    r"\b(?:cupo[mn]s?|coupon|codigo|code)\b\W{0,4}(?:de\W+desconto\W{0,3})?"
    r"(?P<code>[a-z0-9][a-z0-9_-]{2,30})(?:\s*\+\s*(?P<code2>[a-z0-9][a-z0-9_-]{2,30}))?",
)
_PCT_OFF = re.compile(r"(?P<p>\d{1,2})\s*%\s*(?:off|de\s+desconto|desconto)")


@dataclass(frozen=True)
class PricePoint:
    method: str
    amount: Decimal  # total paid
    currency: str
    line: int  # 1-based line of the message, for auditing
    installments: int | None = None
    installment_value: Decimal | None = None
    interest: str = ""  # "free", "charged" or "" (not stated)
    monthly_rate: Decimal | None = None
    coupon_applied: bool = False  # the line says the price needs the coupon
    # How the method and total were read:
    #   stated            amount and its payment cue are on the same line
    #   stated_cue_apart  the only payment method the post names is on a line with no amount
    #   computed          installment count x installment value
    #   price_with_terms  a lone "10x sem juros" line applies to the post's only price
    source: str = "stated"


@dataclass
class Terms:
    points: list[PricePoint] = field(default_factory=list)
    original_price: Decimal | None = None
    coupons: tuple[str, ...] = ()
    coupon_discounts: tuple[str, ...] = ()  # "R$ 50.00", "10%"
    interest_free_anywhere: bool = False
    interest_charged_anywhere: bool = False

    def by_method(self, method: str) -> list[PricePoint]:
        return [p for p in self.points if p.method == method]


def _context(norm_line: str, start: int, end: int, width: int = 40) -> tuple[str, str]:
    return norm_line[max(0, start - width) : start].rstrip(), norm_line[end : end + width]


def _line_interest(norm_line: str) -> str:
    if _SEM_JUROS.search(norm_line):
        return "free"
    if _COM_JUROS.search(norm_line) or _RATE.search(norm_line):
        return "charged"
    return ""


def _rate(norm_line: str) -> Decimal | None:
    m = _RATE.search(norm_line)
    return parse_amount(m.group("r")) if m else None


def _upfront_method(before: str, after: str, norm_line: str) -> str:
    near = f"{before} | {after}"
    if _PIX.search(near):
        return "pix"
    if _BOLETO.search(near):
        return "boleto"
    if _CARD.search(near):
        return "card_1x"
    if _UPFRONT.search(near):
        return "upfront"
    # Fall back to a single method named elsewhere on the line ("Pix: R$ 1.599").
    named = [
        m
        for m, rx in (("pix", _PIX), ("boleto", _BOLETO), ("card_1x", _CARD))
        if rx.search(norm_line)
    ]
    if len(named) == 1:
        return named[0]
    return "unspecified"


def _parse_line(raw: str, number: int, terms: Terms) -> None:
    # Normalisation keeps string length for Latin text, so spans line up; check anyway.
    norm = normalize(raw)
    if len(norm) != len(raw):
        norm = raw.lower()
    coupon_applied = bool(_WITH_COUPON.search(norm))
    interest = _line_interest(norm)
    rate = _rate(norm)
    taken: list[tuple[int, int]] = []

    for m in _INSTALLMENT_VALUE.finditer(raw):
        n = int(m.group("n"))
        value = parse_amount(m.group("num"))
        if not 2 <= n <= 48 or value is None or value <= 0:
            continue
        taken.append((m.start(), m.end()))
        terms.points.append(
            PricePoint(
                "installments",
                (value * n).quantize(Decimal("0.01")),
                _currency(m.group("cur")),
                number,
                n,
                value,
                interest,
                rate,
                coupon_applied,
                "computed",
            )
        )

    for m in PRICE_RE.finditer(raw):
        if any(s <= m.start() < e for s, e in taken):
            continue
        raw_num, symbol = (
            (m.group("num"), m.group("cur"))
            if m.group("num")
            else (m.group("num2"), m.group("cur2"))
        )
        amount = parse_amount(raw_num)
        if amount is None or amount <= 0:
            continue
        before, after = _context(norm, *m.span())
        if _OTHER_BEFORE.search(before) and not _WITH_COUPON.search(before[-25:]):
            _discount(terms, amount, before)
            continue
        if _OTHER_AFTER.search(after):
            _discount(terms, amount, before + " cupom" if "cupom" in after else before)
            continue
        if _ORIGINAL.search(before) and not _FROM.search(before):
            if terms.original_price is None or amount > terms.original_price:
                terms.original_price = amount
            continue
        currency = _currency(symbol)
        inst = _INSTALLMENT_AFTER.search(after)
        if inst and 2 <= int(inst.group("n")) <= 48:
            n = int(inst.group("n"))
            _stated_installments(terms, amount, currency, number, n, interest, rate, coupon_applied)
            continue
        if _TOTAL_BEFORE.search(before) and any(
            p.line == number and p.method == "installments" for p in terms.points
        ):
            _stated_installments(
                terms, amount, currency, number, None, interest, rate, coupon_applied
            )
            continue
        method = _upfront_method(before, after, norm)
        terms.points.append(
            PricePoint(method, amount, currency, number, coupon_applied=coupon_applied)
        )

    if not taken:
        for m in _BARE_POR_PRICE.finditer(raw):
            raw_num = m.group("num")
            if "." not in raw_num and "," not in raw_num and len(raw_num) < 3:
                continue
            amount = parse_amount(raw_num)
            if amount is None or amount <= 0:
                continue
            before, after = _context(norm, m.start("num"), m.end("num"))
            if _BARE_UNIT_AFTER.search(after) or _OTHER_AFTER.search(after):
                continue
            method = _upfront_method(before, after, norm)
            terms.points.append(
                PricePoint(method, amount, "BRL", number, coupon_applied=coupon_applied)
            )


def _stated_installments(
    terms: Terms,
    amount: Decimal,
    currency: str,
    line: int,
    n: int | None,
    interest: str,
    rate: Decimal | None,
    coupon_applied: bool,
) -> None:
    """A quoted installment total replaces the computed one from the same line."""
    computed = next(
        (p for p in terms.points if p.line == line and p.method == "installments"), None
    )
    if computed is not None:
        terms.points.remove(computed)
        n = n or computed.installments
        value = computed.installment_value
    else:
        value = None
    terms.points.append(
        PricePoint("installments", amount, currency, line, n, value, interest, rate, coupon_applied)
    )


def _discount(terms: Terms, amount: Decimal, before: str) -> None:
    if "cupo" in before:
        label = f"R$ {amount:.2f}"
        if label not in terms.coupon_discounts:
            terms.coupon_discounts += (label,)


_CUE_PIX = re.compile(r"\bpix\b")
_CUE_BOLETO = re.compile(r"\bboleto\b")
_TERMS_ONLY = re.compile(r"(?<![\w.,])(?P<n>\d{1,2})\s?x\s*(?:sem\s+juros|s/\s*juros)")


def _replace(p: PricePoint, **changes) -> PricePoint:
    from dataclasses import replace

    return replace(p, **changes)


def _apply_cues_from_other_lines(lines: list[str], terms: Terms) -> None:
    """Posts often put the price alone on a line and the payment terms on another.

    Only applied when the post has exactly one amount with no cue, so a cue can
    never be attached to the wrong price.
    """
    unspecified = [p for p in terms.points if p.method == "unspecified"]
    if len({p.amount for p in unspecified}) != 1 or len(unspecified) != len(
        [p for p in terms.points if p.method != "installments"]
    ):
        return
    cue_lines = [normalize(line) for line in lines if not PRICE_RE.search(line)]
    named = {
        m
        for m, rx in (("pix", _CUE_PIX), ("boleto", _CUE_BOLETO))
        for c in cue_lines
        if rx.search(c)
    }
    price = unspecified[0]
    if len(named) == 1:
        method = named.pop()
        terms.points = [
            _replace(p, method=method, source="stated_cue_apart")
            if p.method == "unspecified"
            else p
            for p in terms.points
        ]
    if any(p.method == "installments" for p in terms.points):
        return
    counts = {int(m.group("n")) for c in cue_lines for m in _TERMS_ONLY.finditer(c)}
    counts = {n for n in counts if 2 <= n <= 48}
    if len(counts) == 1:
        terms.points.append(
            PricePoint(
                "installments",
                price.amount,
                price.currency,
                price.line,
                counts.pop(),
                None,
                "free",
                None,
                price.coupon_applied,
                "price_with_terms",
            )
        )


def parse_terms(text: str) -> Terms:
    terms = Terms()
    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        _parse_line(line, number, terms)
    _apply_cues_from_other_lines(lines, terms)

    norm = normalize(text)
    terms.interest_free_anywhere = bool(_SEM_JUROS.search(norm))
    terms.interest_charged_anywhere = bool(_COM_JUROS.search(norm) or _RATE.search(norm))
    # "sem juros" on its own line applies to the post's installments when nothing says otherwise.
    if terms.interest_free_anywhere and not terms.interest_charged_anywhere:
        terms.points = [
            p if p.method != "installments" or p.interest else _with_interest(p, "free")
            for p in terms.points
        ]

    codes = []
    for m in COUPON_RE.finditer(text):
        code = _coupon_ok(m.group("code"))
        if code and code not in codes:
            codes.append(code)
    for m in _NORM_COUPON_RE.finditer(norm):
        for group in ("code", "code2"):
            raw_code = m.group(group)
            if not raw_code:
                continue
            code = _coupon_ok(raw_code.upper())
            if code and code not in codes:
                codes.append(code)
    terms.coupons = tuple(codes)
    for m in _PCT_OFF.finditer(norm):
        window = norm[max(0, m.start() - 40) : m.end() + 40]
        if "cupo" in window:
            label = f"{m.group('p')}%"
            if label not in terms.coupon_discounts:
                terms.coupon_discounts += (label,)
    # A price equal to or above the "de" price is not a discount; drop the original.
    if terms.original_price is not None and terms.points:
        if terms.original_price <= min(p.amount for p in terms.points):
            terms.original_price = None
    return terms


def _with_interest(p: PricePoint, interest: str) -> PricePoint:
    return _replace(p, interest=interest)
