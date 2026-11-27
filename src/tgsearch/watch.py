"""Poll selected chats once and deliver qualifying offers to a private bot chat."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sqlite3
import sys
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from . import promo_delivery, promo_menu
from .analysis import Post, _assign_method_prices, _strong_keys, build_post
from .credentials import LiveConfigError, resolve
from .extract import parse_amount
from .gpu import Classification
from .models import Message
from .payment import PricePoint
from .profiles import Profile, gpu_profile, offer_filter, terms_profile
from .report_html import brl
from .search import Query
from .sources.live import chat_kind, coerce_chat, default_session_path, open_client, to_message
from .textutil import normalize

CEILING = Decimal("1700")
PROFILE = gpu_profile(("rx7600", "rx7600xt"))
PHONE_LABELS = {"17e": "iPhone 17e 256 GB", "17": "iPhone 17 256 GB"}
PHONE_CEILINGS = {"17e": Decimal("3700"), "17": Decimal("4800")}
PHONE_MODEL = re.compile(r"\biphone[ \t-]*(\d+[ \t-]*e?)(?:[ \t-]*(pro|max|plus|air|mini))?\b")
PHONE_STORAGE = re.compile(r"\b(64|128|256|512)\s*gb\b|\b[12]\s*tb\b")
PHONE_SEALED = re.compile(r"\blacrad[oa]s?\b")
PHONE_CONDITION = re.compile(
    r"\b(?:usad[oa]s?|semi[ -]?nov[oa]s?|recondicionad[oa]s?|open[ -]?box|vitrine"
    r"|deslacrad[oa]s?|caixa aberta|recertificad[oa]s?|refurbished|sem lacre"
    r"|nao lacrad[oa]s?)\b"
)
PHONE_ACCESSORY = re.compile(
    r"\b(?:capinha|capa|case|pelicula|suporte|carregador|powerbank|cabo\s+usb)\b"
)
PHONE_RESTRICTION = re.compile(
    r"\b(?:cashback|trade[ -]?in|na troca|com troca|plano de operadora"
    r"|plano (?:vivo|claro|tim)|retirada obrigatoria)\b"
)
ENDED_OFFER = re.compile(r"\b(?:esgotad[oa]s?|encerrad[oa]s?)\b")
PAID_SHIPPING = re.compile(r"\bfrete\s*(?:de|fixo|:)?\s*r\$")
FREIGHT = re.compile(r"\bfrete\s*(?:(?:de|fixo)\s*)?:?\s*(?:r\$\s*([\d.,]+)|(gratis)\b)")
FREIGHT_CONDITIONAL = re.compile(
    r"\b(?:cep|ceps|regiao|regioes|selecionad\w*|acima|a partir|checkout|calcular|consulte"
    r"|depend\w*|prime|meli\+|assinante\w*)\b"
)
RESTOCK = re.compile(
    r"\b(?:voltou(?:\s+(?:ao\s+estoque|a\s+funcionar|o\s+estoque|o\s+cupom|a\s+oferta))?"
    r"|estoque\s+reposto|reposicao\s+de\s+estoque|disponivel\s+novamente"
    r"|oferta\s+(?:voltou|retornou|reativada)|cupom\s+(?:voltou|reativado))\b"
)
SELLER = re.compile(
    r"(?im)^\W*(?:vendido(?:\s+e\s+(?:entregue|enviado))?\s+por|vendedor)\s*:?\s*([^\r\n]{2,80})"
)
EXTRA_STORE_HOSTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("amazon", ("link.amazon",)),
    ("mercadolivre", ("meli.la",)),
    ("fastshop", ("fastshop.com.br",)),
    ("carrefour", ("carrefour.com.br",)),
    ("extra", ("extra.com.br",)),
    ("pontofrio", ("pontofrio.com.br",)),
    ("samsung", ("samsung.com",)),
    ("apple", ("apple.com",)),
    ("dell", ("dell.com",)),
    ("asus", ("asus.com",)),
)


def classify_phone(
    text: str,
    *,
    allow_paid_shipping: bool = False,
    require_sealed: bool = True,
    block_used: bool = True,
    block_restrictions: bool = True,
) -> Classification:
    plain = normalize(text)
    models = {
        re.sub(r"[ \t-]+", "", number) + variant for number, variant in PHONE_MODEL.findall(plain)
    }
    target_models = models.intersection(PHONE_LABELS)
    has_other_17 = any(m.startswith("17") and m not in target_models for m in models)
    if len(target_models) != 1 or has_other_17 or len(models) > 2:
        return Classification(None, "phone", (), "no_target_model")
    storage = {match.group() for match in PHONE_STORAGE.finditer(plain)}
    restocked = allow_paid_shipping and bool(RESTOCK.search(plain))
    if (
        {re.sub(r"\s+", "", size) for size in storage} != {"256gb"}
        or (require_sealed and not PHONE_SEALED.search(plain))
        or (block_used and PHONE_CONDITION.search(plain))
        or PHONE_ACCESSORY.search(plain)
        or (block_restrictions and PHONE_RESTRICTION.search(plain))
        or (not restocked and ENDED_OFFER.search(plain))
        or (not allow_paid_shipping and PAID_SHIPPING.search(plain))
    ):
        return Classification(None, "phone", (), "ineligible_phone_offer")
    return Classification(target_models.pop(), "phone", (), "")


PHONE_PROFILE = Profile("phone", tuple(PHONE_LABELS), PHONE_LABELS, (), classify_phone)
PROMO_PHONE_PROFILE = Profile(
    "phone",
    tuple(PHONE_LABELS),
    PHONE_LABELS,
    (),
    lambda text: classify_phone(text, allow_paid_shipping=True, require_sealed=False),
)
OFFER_FILTER = offer_filter(PROFILE)
PHONE_OFFER_FILTER = offer_filter(PHONE_PROFILE)
CARD_METHODS = {"card_1x", "installments"}
METHOD_LABELS = {
    "card_1x": "cartão à vista",
    "pix": "Pix",
    "boleto": "boleto",
    "upfront": "à vista, forma de pagamento não informada",
    "unspecified": "forma de pagamento não informada",
}
PROMO_METHOD_LABELS = {
    "pix": "Pix",
    "boleto": "Boleto",
    "card_1x": "Cartão à vista",
    "upfront": "À vista",
    "installments": "Cartão parcelado",
    "unspecified": "Não informada",
}
HEADER = "ALERTAS URGENTES"
SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (chat TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, body TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Match:
    priority: int
    amount: Decimal
    body: str
    keys: tuple[str, ...]


def payment_label(point: PricePoint) -> str:
    if point.method != "installments":
        return METHOD_LABELS[point.method]
    interest = {
        "free": "sem juros",
        "charged": "com juros",
    }.get(point.interest, "juros não informados")
    return (
        f"cartão em {point.installments}× ({interest})"
        if point.installments
        else f"cartão parcelado ({interest})"
    )


def match_offer(message: Message, kind: str) -> Match | None:
    if OFFER_FILTER(message, kind):
        profile = PROFILE
    elif PHONE_OFFER_FILTER(message, kind):
        profile = PHONE_PROFILE
    else:
        return None
    post = build_post(message, profile)
    model = post.cls.model
    if not post.cls.usable or model is None:
        return None
    is_phone = profile is PHONE_PROFILE
    if is_phone and not post.url:
        return None
    ceiling = PHONE_CEILINGS[model] if is_phone else CEILING
    _assign_method_prices(post, (Decimal("2500"), Decimal("15000")) if is_phone else None)
    eligible = [
        point
        for method, point in post.method_prices.items()
        if method != "lowest_upfront" and point.amount <= ceiling
    ]
    if not eligible:
        return None

    card = [point for point in eligible if point.method in CARD_METHODS]
    best = min(eligible if is_phone else card or eligible, key=lambda point: point.amount)
    label = payment_label(best)
    if best.method == "pix" and all(point.method == "pix" for point in eligible):
        label = "só Pix dentro do teto"
    title = profile.labels[model]
    lines = [f"{title} · {brl(best.amount)} · {label}"]
    alternates = [
        f"{brl(point.amount)} no {payment_label(point)}"
        for point in eligible
        if point.method in {"pix", "boleto"} and point is not best
    ]
    if alternates:
        lines.append(" · ".join(alternates))
    if post.terms.coupons:
        lines.append("Cupom " + ", ".join(post.terms.coupons))
    if is_phone:
        if post.store:
            lines.append(f"Loja: {post.store}")
        lines.append("Preço anunciado no Telegram. Confira lacre, estoque e vendedor na loja.")
        lines.append(f"Frete: até {brl(ceiling - best.amount)} cabe no teto.")
    if post.url:
        lines.append(post.url)
    if message.link:
        lines.append(message.link)
    keys = tuple(
        repr((model, key)) for key in _strong_keys(post) if key[0] != "post" or message.forward_ref
    )
    return Match(0 if best.method in CARD_METHODS else 1, best.amount, "\n".join(lines), keys)


def summaries(matches: Iterable[Match]) -> Iterator[str]:
    body = HEADER
    for match in sorted(matches, key=lambda match: (match.priority, match.amount)):
        # Count UTF-16 units conservatively and leave room below the API's 4096 limit.
        block = "\n".join(
            line for line in match.body.splitlines() if len(line.encode("utf-16-le")) <= 1200
        )
        if len(f"{body}\n\n{block}".encode("utf-16-le")) > 8000:
            yield body
            body = HEADER
        body = f"{body}\n\n{block}"
    if body != HEADER:
        yield body


def promo_shipping(text: str) -> int | None:
    shipping = set()
    for line in normalize(text).splitlines():
        if FREIGHT_CONDITIONAL.search(line):
            continue
        for amount, free in FREIGHT.findall(line):
            price = Decimal(0) if free else parse_amount(amount.rstrip(".,"))
            if price is not None:
                shipping.add(promo_delivery.cents(price))
    return shipping.pop() if len(shipping) == 1 else None


def promo_installment_text(point: PricePoint) -> str:
    interest = {"free": " sem juros", "charged": " com juros"}.get(point.interest, "")
    amount = brl(point.amount)
    if point.installments and point.installment_value:
        return f"{point.installments}× de {brl(point.installment_value)}{interest} · {amount}"
    if point.installments:
        return f"{point.installments}×{interest} · {amount}"
    return f"Parcelado{interest} · {amount}"


def promo_payment(point: PricePoint) -> str:
    amount = brl(point.amount)
    if point.method == "installments":
        return f"💳 {promo_installment_text(point)}"
    method = {
        "pix": "no Pix",
        "boleto": "no boleto",
        "card_1x": "no cartão à vista",
        "upfront": "à vista",
    }.get(point.method, "")
    return f"{amount} {method}".strip()


def promo_store(post: Post) -> str:
    if post.url:
        parts = urlsplit(post.url)
        host = (parts.hostname or "").lower().removeprefix("www.")
        for name, hosts in EXTRA_STORE_HOSTS:
            if any(host == h or host.endswith("." + h) for h in hosts):
                return name
        path = parts.path.lower()
        for prefix, name in (
            ("/amz/", "amazon"),
            ("/ml/", "mercadolivre"),
            ("/kabum/", "kabum"),
            ("/kbm/", "kabum"),
            ("/magalu/", "magalu"),
            ("/shopee/", "shopee"),
            ("/ali/", "aliexpress"),
        ):
            if path.startswith(prefix):
                return name
        for suffix, name in (("-amazon", "amazon"), ("-mercadolivre", "mercadolivre")):
            if path.endswith(suffix):
                return name
        if post.store:
            return post.store
        return host
    return post.store or ""


def profile_for_alert(alert: promo_menu.Alert) -> Profile:
    cfg = alert.filter_config()
    if cfg["mode"] == "broad":
        return terms_profile(alert.label.split())
    if alert.product in PROFILE.targets:
        return PROFILE
    if alert.product in PHONE_LABELS:
        return Profile(
            "phone",
            tuple(PHONE_LABELS),
            PHONE_LABELS,
            (),
            lambda text, c=cfg: classify_phone(
                text,
                allow_paid_shipping=True,
                require_sealed=bool(c["require_sealed"]),
                block_used=bool(c["block_used"]),
                block_restrictions=bool(c["block_restrictions"]),
            ),
        )
    return terms_profile(alert.label.split())


def _extract_promo_payments(
    post: Post, plausible: tuple[Decimal, Decimal] | None
) -> list[PricePoint]:
    _assign_method_prices(post, plausible)
    payments = [point for method, point in post.method_prices.items() if method != "lowest_upfront"]
    if not payments and post.method_notes.get("unspecified") == "conflicting_amounts":
        # Common pattern: line 1 quotes the title with regular price ("R$ 1.989") and line 2
        # quotes the deal price ("💵 R$ 1.751") without "De ... por ...". Keep the lower one.
        lo, hi = plausible or (Decimal("0.01"), Decimal("1e12"))
        unspec = [
            p
            for p in post.terms.points
            if p.currency == "BRL" and p.method == "unspecified" and lo <= p.amount <= hi
        ]
        if len({p.line for p in unspec}) >= 2:
            payments = [min(unspec, key=lambda p: p.amount)]
    payments.sort(key=lambda point: (point.amount, point.method != "pix"))
    return payments


def promo_matches(
    message: Message,
    kind: str,
    targets: Sequence[tuple[promo_menu.Alert, Profile]],
) -> Iterator[promo_delivery.Offer]:
    shipping = promo_shipping(message.text)
    plain = normalize(message.text)
    restocked = bool(RESTOCK.search(plain))
    if not restocked and ENDED_OFFER.search(plain):
        return
    for alert, profile in targets:
        cfg = alert.filter_config()
        if cfg["block_used"] and PHONE_CONDITION.search(plain):
            continue
        if cfg["include"] and not Query(terms=cfg["include"]).matches(message.text):
            continue
        if cfg["exclude"] and not Query(exclude=cfg["exclude"]).matches(message.text):
            continue
        if not offer_filter(profile)(message, kind):
            continue
        post = build_post(message, profile)
        if not post.cls.usable or post.cls.model not in {alert.product, "query"}:
            continue
        if cfg["require_url"] and not post.url:
            continue
        is_phone = alert.product in PHONE_LABELS and cfg["mode"] == "smart"
        plausible = (Decimal("2500"), Decimal("15000")) if is_phone else profile.plausible
        payments = _extract_promo_payments(post, plausible)
        if not payments:
            continue
        best = payments[0]
        price = promo_delivery.cents(best.amount)
        min_price = int(cfg.get("min_price") or 0)
        if min_price and price < min_price:
            continue
        if price + (shipping or 0) > alert.ceiling:
            continue
        store_name = promo_store(post)
        store_key = normalize(store_name) or post.url or message.link
        coupons = list(dict.fromkeys(coupon.upper() for coupon in post.terms.coupons))
        key = json.dumps([alert.product, store_key, sorted(coupons)], ensure_ascii=False)
        posts = {message.link: message.chat} if message.link else {}
        seller_match = SELLER.search(message.text)
        seller = seller_match.group(1).strip(" .:-") if seller_match else ""

        payment_method = PROMO_METHOD_LABELS.get(best.method, "")
        installment_points = [p for p in payments if p.method == "installments"]
        installments = promo_installment_text(installment_points[0]) if installment_points else ""
        other_payments = [promo_payment(p) for p in payments[1:] if p.method != "installments"]

        yield promo_delivery.Offer(
            key,
            alert.label,
            store_name,
            price,
            [promo_payment(p) for p in payments],
            coupons,
            shipping,
            post.url,
            posts,
            seller,
            restocked,
            payment_method,
            installments,
            other_payments,
        )


async def scan(
    client: Any,
    db: sqlite3.Connection,
    chats: Sequence[str],
    *,
    promo: bool = False,
) -> None:
    matches = []
    processed = set()
    targets = []
    if promo:
        for alert in promo_menu.alerts(db, active_only=True):
            targets.append((alert, profile_for_alert(alert)))
    with db:
        db.execute("DELETE FROM seen WHERE expires_at <= strftime('%s', 'now')")
    for chat in chats:
        entity = await client.get_entity(coerce_chat(chat))
        kind = chat_kind(entity)
        if kind == "user":
            raise LiveConfigError("--chat must identify a Telegram group or channel")
        chat_key = f"{'group' if kind == 'group' else 'channel'}:{entity.id}"
        if chat_key in processed:
            continue
        processed.add(chat_key)
        row = db.execute("SELECT last_id FROM cursors WHERE chat = ?", (chat_key,)).fetchone()
        chat_offers: list[promo_delivery.Offer] = []
        chat_matches: list[Match] = []
        if row is None:
            latest = await client.get_messages(entity, limit=1)
            last_id = latest[0].id if latest else 0
        else:
            last_id = row[0]
            async for item in client.iter_messages(entity, min_id=last_id, reverse=True):
                last_id = max(last_id, item.id)
                message = to_message(entity, item)
                if promo:
                    if message:
                        chat_offers.extend(promo_matches(message, kind, targets))
                    continue
                match = match_offer(message, kind) if message else None
                if match:
                    chat_matches.append(match)
        with db:
            for offer in chat_offers:
                promo_delivery.enqueue(db, offer)
            for match in chat_matches:
                duplicate = any(
                    db.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone()
                    for key in match.keys
                )
                if not duplicate:
                    matches.append(match)
                db.executemany(
                    "INSERT OR IGNORE INTO seen VALUES (?, strftime('%s', 'now') + 86400)",
                    ((key,) for key in match.keys),
                )
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (chat_key, last_id))
    if matches:
        with db:
            db.executemany(
                "INSERT INTO outbox (body) VALUES (?)", ((body,) for body in summaries(matches))
            )


def send_summary(token: str, chat_id: str, body: str) -> None:
    payload = json.dumps(
        {
            "chat_id": int(chat_id),
            "text": body,
            "link_preview_options": {"is_disabled": True},
        }
    ).encode()
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            success = json.load(response).get("ok")
    except (OSError, ValueError):
        success = False
    if not success:
        raise LiveConfigError("Telegram bot delivery failed; the summary is still queued")


def deliver(db: sqlite3.Connection, token: str, chat_id: str) -> None:
    while True:
        with db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("SELECT id, body FROM outbox ORDER BY id LIMIT 1").fetchone()
            if not entry:
                return
            send_summary(token, chat_id, entry[1])
            db.execute("DELETE FROM outbox WHERE id = ?", (entry[0],))


@contextmanager
def _scan_lock(state: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    lock_path = state.with_suffix(state.suffix + ".lock")
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


async def run(chats: Sequence[str], state: Path, session: Path | None) -> None:
    promo_token = os.environ.get("TGSEARCH_PROMOBOT_TOKEN", "")
    token = promo_token or os.environ.get("TGSEARCH_BOT_TOKEN", "")
    chat_id = os.environ.get("TGSEARCH_BOT_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(
        r"[1-9][0-9]*", chat_id
    ):
        raise LiveConfigError("set a bot token and TGSEARCH_BOT_CHAT_ID (private chat ID)")
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _scan_lock(state), closing(sqlite3.connect(state, timeout=30)) as db:
        promo_delivery.configure_db(db)
        db.executescript(SCHEMA)
        bot = None
        if promo_token:
            db.executescript(promo_delivery.SCHEMA)
            promo_menu.init(db)
            bot = promo_delivery.Bot(token, int(chat_id))
            promo_delivery.deliver(db, bot)
        else:
            deliver(db, token, chat_id)
        async with open_client(resolve(), session or default_session_path()) as client:
            await scan(client, db, chats, promo=bot is not None)
        if bot is not None:
            promo_delivery.deliver(db, bot)
        else:
            deliver(db, token, chat_id)


RECONCILE_SECONDS = 600
HEARTBEAT_INTERVAL = 15


def _event_chat_id(message_obj: Any) -> int | None:
    peer = getattr(message_obj, "peer_id", None)
    if peer is None:
        return None
    for attr in ("channel_id", "chat_id"):
        val = getattr(peer, attr, None)
        if val is not None:
            return int(val)
    return None


def _process_realtime_item(
    db: sqlite3.Connection,
    bot: promo_delivery.Bot,
    entity: Any,
    kind: str,
    chat_key: str,
    item: Any,
) -> int:
    """Process one incoming Telethon message against active alerts and deliver immediately."""
    item_id = int(item.id)
    row = db.execute("SELECT last_id FROM cursors WHERE chat = ?", (chat_key,)).fetchone()
    last_id = row[0] if row else 0
    if item_id <= last_id:
        return 0
    message = to_message(entity, item)
    targets = [(a, profile_for_alert(a)) for a in promo_menu.alerts(db, active_only=True)]
    offers = list(promo_matches(message, kind, targets)) if message else []
    with db:
        for offer in offers:
            promo_delivery.enqueue(db, offer)
        db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (chat_key, item_id))
    if offers:
        promo_delivery.deliver(db, bot)
        for offer in offers:
            print(
                f"watch-live delivered: {offer.title} ({promo_delivery.money(offer.price)}) "
                f"from {message.chat if message else chat_key}",
                flush=True,
            )
    return len(offers)


async def _catchup_monitored(
    client: Any,
    db: sqlite3.Connection,
    monitored: dict[int, tuple[Any, str, str]],
    bot: promo_delivery.Bot,
) -> None:
    targets = [(a, profile_for_alert(a)) for a in promo_menu.alerts(db, active_only=True)]
    enqueued = 0
    for entity, kind, chat_key in monitored.values():
        row = db.execute("SELECT last_id FROM cursors WHERE chat = ?", (chat_key,)).fetchone()
        chat_offers: list[promo_delivery.Offer] = []
        if row is None:
            latest = await client.get_messages(entity, limit=1)
            last_id = latest[0].id if latest else 0
        else:
            last_id = row[0]
            async for item in client.iter_messages(entity, min_id=last_id, reverse=True):
                last_id = max(last_id, item.id)
                message = to_message(entity, item)
                if message:
                    chat_offers.extend(promo_matches(message, kind, targets))
        with db:
            for offer in chat_offers:
                promo_delivery.enqueue(db, offer)
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?, ?)", (chat_key, last_id))
        enqueued += len(chat_offers)
    if enqueued or db.execute("SELECT 1 FROM promo_outbox LIMIT 1").fetchone():
        promo_delivery.deliver(db, bot)


async def _handle_job(
    client: Any,
    db: sqlite3.Connection,
    job_id: int,
    kind: str,
    payload: dict,
) -> None:
    from . import promo_history, promo_jobs

    try:
        raw_src = payload.get("sources_file") or ""
        sources_file = Path(raw_src) if raw_src else None
        if kind == "history":
            wanted_ids = set(payload.get("alert_ids") or [])
            selected = [a for a in promo_menu.alerts(db) if a.id in wanted_ids]
            hours = int(payload.get("hours") or 24)
            entries = await promo_history.fetch_history_with_client(
                client, selected, hours, sources_file
            )
            promo_jobs.complete(db, job_id, {"entries": [e.to_dict() for e in entries]})
        elif kind == "channels":
            chans = await promo_history.fetch_channels_with_client(client, sources_file)
            promo_jobs.complete(db, job_id, {"channels": [asdict(c) for c in chans]})
        else:
            promo_jobs.fail(db, job_id, f"Tipo de job desconhecido: {kind}")
    except Exception as exc:
        print(f"watch-live job {job_id} ({kind}) error: {exc}", file=sys.stderr, flush=True)
        promo_jobs.fail(db, job_id, str(exc))


async def run_live(chats: Sequence[str], state: Path, session: Path | None) -> None:
    """Keep one Telethon connection open and deliver qualifying offers in real time (1-3s)."""
    from telethon import events

    from . import promo_jobs

    token = os.environ.get("TGSEARCH_PROMOBOT_TOKEN", "")
    chat_id = os.environ.get("TGSEARCH_BOT_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(
        r"[1-9][0-9]*", chat_id
    ):
        raise LiveConfigError("set TGSEARCH_PROMOBOT_TOKEN and TGSEARCH_BOT_CHAT_ID")
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    with _scan_lock(state), closing(sqlite3.connect(state, timeout=30)) as db:
        promo_delivery.configure_db(db)
        db.executescript(SCHEMA)
        db.executescript(promo_delivery.SCHEMA)
        promo_jobs.init(db)
        bot = promo_delivery.Bot(token, int(chat_id))
        promo_delivery.deliver(db, bot)

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        async with open_client(resolve(), session or default_session_path()) as client:
            monitored: dict[int, tuple[Any, str, str]] = {}
            for chat in chats:
                entity = await client.get_entity(coerce_chat(chat))
                kind = chat_kind(entity)
                if kind == "user":
                    raise LiveConfigError("--chat must identify a Telegram group or channel")
                chat_key = f"{'group' if kind == 'group' else 'channel'}:{entity.id}"
                monitored[int(entity.id)] = (entity, kind, chat_key)

            print(
                f"watch-live online: monitoring {len(monitored)} chats in real time",
                flush=True,
            )
            await _catchup_monitored(client, db, monitored, bot)
            promo_jobs.touch_heartbeat(db)

            delivery_lock = asyncio.Lock()

            @client.on(events.NewMessage())
            async def _on_new_message(event: Any) -> None:
                cid = _event_chat_id(event.message)
                if cid is None or cid not in monitored:
                    return
                entity, kind, chat_key = monitored[cid]
                async with delivery_lock:
                    try:
                        _process_realtime_item(db, bot, entity, kind, chat_key, event.message)
                    except Exception as exc:
                        print(f"watch-live event error: {exc}", file=sys.stderr, flush=True)

            async def _background_loop() -> None:
                last_beat = time.monotonic()
                last_reconcile = time.monotonic()
                while not stop_event.is_set():
                    now_mono = time.monotonic()
                    if now_mono - last_beat >= HEARTBEAT_INTERVAL:
                        try:
                            promo_jobs.touch_heartbeat(db)
                        except sqlite3.OperationalError as exc:
                            print(f"watch-live heartbeat error: {exc}", file=sys.stderr, flush=True)
                        last_beat = now_mono

                    job = promo_jobs.claim_next(db)
                    if job is not None:
                        job_id, job_kind, payload = job
                        await _handle_job(client, db, job_id, job_kind, payload)
                        continue

                    if now_mono - last_reconcile >= RECONCILE_SECONDS:
                        async with delivery_lock:
                            try:
                                await _catchup_monitored(client, db, monitored, bot)
                            except Exception as exc:
                                print(
                                    f"watch-live reconcile error: {exc}",
                                    file=sys.stderr,
                                    flush=True,
                                )
                        last_reconcile = time.monotonic()

                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=0.2)
                    except asyncio.TimeoutError:
                        pass

            bg_task = asyncio.create_task(_background_loop())
            disconnect_task = asyncio.create_task(client.run_until_disconnected())
            stop_task = asyncio.create_task(stop_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {bg_task, disconnect_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stop_event.set()
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if not task.cancelled():
                        exc = task.exception()
                        if exc is not None:
                            raise exc
            finally:
                promo_jobs.clear_heartbeat(db)
