"""Extract pricing, installments, payment methods and coupons from offer text.

The heuristics target the typical Brazilian deal-channel format
("De R$ 1.299 por R$ 999 no Pix ou 10x de R$ 109,90 sem juros") but also
understand US-style numbers ("$1,299.99").  Ambiguous amounts are classified
conservatively: a value only becomes ``price`` when nothing marks it as a
discount, shipping threshold, or installment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import Message, Offer
from .textutil import extract_urls, is_telegram_url, normalize

_CUR = r"R\$|US\$|U\$|\$|€|£"
_NUM = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_SP = r"[  ]"

PRICE_RE = re.compile(
    rf"(?P<cur>{_CUR}){_SP}{{0,2}}(?P<num>{_NUM})(?!\d)"
    rf"|(?<![\d.,])(?P<num2>{_NUM}){_SP}?(?P<cur2>reais|brl|usd|eur)\b",
    re.IGNORECASE,
)

INSTALLMENT_RE = re.compile(
    rf"(?<![\w.,])(?P<n>\d{{1,2}}){_SP}?[x×](?!\d){_SP}?(?:de{_SP}+)?"
    rf"(?:(?P<cur>{_CUR}){_SP}{{0,2}}(?P<num>{_NUM})(?!\d))?"
    rf"(?P<sj>[  ,]*sem{_SP}+juros)?",
    re.IGNORECASE,
)

CURRENCY_CODES = {
    "r$": "BRL",
    "reais": "BRL",
    "brl": "BRL",
    "us$": "USD",
    "u$": "USD",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
}

# Context patterns run against normalised (accent-free, casefolded) text.
_OTHER_BEFORE = re.compile(
    r"(?:desconto|off|cashback|economi\w*|frete|cupom|ganhe|bonus|acima|minimo|compras?|limite)"
    r"\W*(?:de\W*)?$"
)
_OTHER_AFTER = re.compile(
    r"^\W*(?:off\b|de desconto"
    r"|desconto(?!\s+(?:na\s+finalizacao|no\s+carrinho|automatico|no\s+checkout))"
    r"|de cashback|cashback|em compras|de frete|de economia)"
)
_FROM_BEFORE = re.compile(r"\bpartir\W+de\W*$")
_ORIGINAL_BEFORE = re.compile(r"(?:\bde|\bantes|\bera|\bnormal)\W*$")
_POR_BEFORE = re.compile(r"(?:\bpor|\bapenas|\bso|\bsai|\bagora|\bfica)\W*$")
_PIX_AFTER = re.compile(r"^\W*(?:(?:a|vista|no|na|via|pelo|com|pagando|pagamento)\W+){0,4}pix\b")
_PIX_BEFORE = re.compile(r"\bpix\W*(?:(?:por|de|a)\W*)?$")
_INSTALLMENT_LEAD = re.compile(r"(?:\bate|\bem|\bparcelad[oa] em|\bparcele em)\W*$")
_SEM_JUROS = re.compile(r"\bsem juros\b")

PAYMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pix", re.compile(r"\bpix\b")),
    ("boleto", re.compile(r"\bboleto\b")),
    (
        "credit_card",
        re.compile(r"\bcartao(?! de debito)\b|\bcredito\b|\bcredit card\b"),
    ),
    ("debit_card", re.compile(r"\bdebito\b|\bdebit card\b")),
    ("paypal", re.compile(r"\bpaypal\b")),
    ("mercado_pago", re.compile(r"\bmercado ?pago\b")),
    ("picpay", re.compile(r"\bpicpay\b")),
)

COUPON_RE = re.compile(
    r"\b(?:cupom|cupon|coupon|c[oó]digo|code)\b\W{0,3}(?:de\W+desconto\W{0,3})?"
    r"(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{2,30})",
    re.IGNORECASE,
)
_COUPON_STOPWORDS = frozenset(
    {
        "DESCONTO",
        "NO",
        "NA",
        "DE",
        "PARA",
        "APLICADO",
        "ABAIXO",
        "OFF",
        "RESGATE",
        "ANUNCIO",
        "LINK",
        "SITE",
        "APP",
        "LOJA",
        "APPLE",
        "PIX",
        "CARTAO",
        "BOLETO",
        "AQUI",
        "VER",
        "CLIQUE",
        "SELECIONE",
        "MOEDAS",
        "CARRINHO",
        "CHECKOUT",
        "PAGINA",
        "PRODUTO",
        "OFERTA",
        "CUPOM",
    }
)

_CONTEXT = 30


@dataclass(frozen=True)
class _Candidate:
    amount: Decimal
    currency: str
    start: int
    end: int
    kind: str  # "price", "por", "original", "pix", "other"


def parse_amount(raw: str) -> Decimal | None:
    """Parse '1.299,90', '1,299.90', '1299', '12,5' into a Decimal with 2 places."""
    s = raw.strip()
    if "." in s and "," in s:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        s = s.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        head, _, tail = s.rpartition(sep)
        if s.count(sep) == 1 and len(tail) in (1, 2):
            s = f"{head}.{tail}"
        else:
            s = s.replace(sep, "")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _currency(symbol: str) -> str:
    return CURRENCY_CODES.get(symbol.lower(), symbol.upper())


def _line_context(text: str, start: int, end: int) -> tuple[str, str]:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    before = text[max(line_start, start - _CONTEXT) : start]
    after = text[end : min(line_end, end + _CONTEXT)]
    return normalize(before).rstrip(), normalize(after)


def _classify(text: str, start: int, end: int) -> str:
    before, after = _line_context(text, start, end)
    if _OTHER_BEFORE.search(before) or _OTHER_AFTER.search(after):
        return "other"
    if _FROM_BEFORE.search(before):
        return "price"
    if _ORIGINAL_BEFORE.search(before):
        return "original"
    if _PIX_AFTER.search(after) or _PIX_BEFORE.search(before):
        return "pix"
    if _POR_BEFORE.search(before):
        return "por"
    return "price"


def _installments(text: str) -> tuple[int | None, Decimal | None, bool, list[tuple[int, int]]]:
    """Return (count, value, interest_free, value spans to exclude from price candidates)."""
    count: int | None = None
    value: Decimal | None = None
    interest_free = False
    spans: list[tuple[int, int]] = []
    for m in INSTALLMENT_RE.finditer(text):
        n = int(m.group("n"))
        has_value = m.group("num") is not None
        lead = normalize(text[max(0, m.start() - _CONTEXT) : m.start()]).rstrip()
        if not (has_value or m.group("sj") or _INSTALLMENT_LEAD.search(lead)):
            continue
        if not 2 <= n <= 48:
            continue
        if has_value:
            spans.append((m.start("cur"), m.end("num")))
        if m.group("sj"):
            interest_free = True
        if count is None:
            count = n
            value = parse_amount(m.group("num")) if has_value else None
        elif value is None and has_value and n == count:
            value = parse_amount(m.group("num"))
    return count, value, interest_free, spans


def _candidates(text: str, exclude: list[tuple[int, int]]) -> list[_Candidate]:
    found = []
    for m in PRICE_RE.finditer(text):
        if any(s <= m.start() < e for s, e in exclude):
            continue
        raw, symbol = (
            (m.group("num"), m.group("cur"))
            if m.group("num")
            else (
                m.group("num2"),
                m.group("cur2"),
            )
        )
        amount = parse_amount(raw)
        if amount is None or amount <= 0:
            continue
        found.append(
            _Candidate(amount, _currency(symbol), m.start(), m.end(), _classify(text, *m.span()))
        )
    return found


def _title(text: str) -> str:
    for line in text.splitlines():
        cleaned = re.sub(r"^[\W_]+", "", line).strip().strip("*_~").strip()
        if re.search(r"[^\W\d_]", cleaned):
            return cleaned[:120]
    return ""


def _coupon_ok(code: str) -> str | None:
    """Accept a code written in capitals or containing a digit; reject plain words."""
    upper = code.upper()
    if upper in _COUPON_STOPWORDS:
        return None
    if code == upper or any(ch.isdigit() for ch in code):
        return upper
    return None


def _coupon(text: str) -> str | None:
    for m in COUPON_RE.finditer(text):
        code = _coupon_ok(m.group("code"))
        if code:
            return code
    return None


def payment_methods(text: str) -> tuple[str, ...]:
    norm = normalize(text)
    return tuple(name for name, pattern in PAYMENT_PATTERNS if pattern.search(norm))


def _offer_url(message: Message) -> str | None:
    urls = list(message.urls) + [u for u in extract_urls(message.text) if u not in message.urls]
    for url in urls:
        if not is_telegram_url(url):
            return url
    return None


def extract_offer(message: Message) -> Offer:
    text = message.text
    count, inst_value, interest_free, inst_spans = _installments(text)
    cands = _candidates(text, inst_spans)

    def first(*kinds: str) -> _Candidate | None:
        return next((c for c in cands if c.kind in kinds), None)

    pix = first("pix")
    chosen = first("por") or first("price") or pix
    price = chosen.amount if chosen else None
    currency = chosen.currency if chosen else None

    original = first("original")
    original_price = None
    if original and price is not None and original.amount > price:
        original_price = original.amount

    if currency is None and inst_value is not None:
        inst_cur = INSTALLMENT_RE.search(text)
        currency = _currency(inst_cur.group("cur")) if inst_cur and inst_cur.group("cur") else None

    norm = normalize(text)
    return Offer(
        message=message,
        title=_title(text),
        price=price,
        original_price=original_price,
        pix_price=pix.amount if pix else None,
        currency=currency,
        installments=count,
        installment_value=inst_value,
        interest_free=interest_free or bool(_SEM_JUROS.search(norm)),
        payment_methods=payment_methods(text),
        coupon=_coupon(text),
        url=_offer_url(message),
    )
