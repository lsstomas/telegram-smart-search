"""One editable Telegram message per promotion, with durable delivery state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from html import escape
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .credentials import LiveConfigError
from .report_html import brl

DEDUP_SECONDS = 3600
MIN_PRICE_DROP = 1000  # centavos
MAX_VISIBLE_POSTS = 25
STORE_DISPLAY = {
    "amazon": "Amazon",
    "kabum": "KaBuM",
    "pichau": "Pichau",
    "terabyte": "Terabyte",
    "mercadolivre": "Mercado Livre",
    "aliexpress": "AliExpress",
    "shopee": "Shopee",
    "magalu": "Magalu",
    "casasbahia": "Casas Bahia",
    "girafa": "Girafa",
    "gkinfostore": "GK InfoStore",
    "fastshop": "Fast Shop",
    "carrefour": "Carrefour",
    "extra": "Extra",
    "pontofrio": "Ponto",
    "samsung": "Samsung",
    "apple": "Apple",
    "dell": "Dell",
    "asus": "ASUS",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS promo_outbox (
    id INTEGER PRIMARY KEY, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_notice (
    offer_key TEXT PRIMARY KEY, message_id INTEGER NOT NULL, touched_at INTEGER NOT NULL,
    notified_price INTEGER NOT NULL, previous_price INTEGER, data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_posts (
    offer_key TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY (offer_key, url)
);
"""


def configure_db(db: sqlite3.Connection) -> None:
    db.execute("PRAGMA busy_timeout = 30000")
    try:
        db.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass


class BotError(LiveConfigError):
    def __init__(self, description: str = "Falha ao acessar a API do Telegram") -> None:
        super().__init__(description)
        self.description = description


class Bot:
    def __init__(self, token: str, chat_id: int) -> None:
        self.token = token
        self.chat_id = chat_id

    def call(self, method: str, **payload):
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=max(15, payload.get("timeout", 0) + 5)) as response:
                data = json.load(response)
        except HTTPError as error:
            try:
                description = json.load(error).get("description", "")
            except (OSError, ValueError):
                description = ""
            raise BotError(description or "Falha ao acessar a API do Telegram") from None
        except (OSError, ValueError):
            raise BotError() from None
        if not data.get("ok"):
            raise BotError(data.get("description") or "Falha ao acessar a API do Telegram")
        return data["result"]

    def message(self, text: str, *, message_id: int | None = None, **options):
        args = {"chat_id": self.chat_id, "text": text, **options}
        if message_id is not None:
            return self.call("editMessageText", message_id=message_id, **args)
        return self.call("sendMessage", **args)


@dataclass
class Offer:
    key: str
    title: str
    store: str
    price: int
    payments: list[str]
    coupons: list[str]
    shipping: int | None
    buy_url: str
    posts: dict[str, str]
    seller: str = ""
    restocked: bool = False
    payment_method: str = ""
    installments: str = ""
    other_payments: list[str] = field(default_factory=list)
    item_name: str = ""


def cents(amount: Decimal) -> int:
    return int(amount * 100)


def money(amount: int) -> str:
    return brl(Decimal(amount) / 100)


def store_display(store: str) -> str:
    if not store:
        return ""
    return STORE_DISPLAY.get(store.casefold(), store)


def clean_post_label(title: str) -> str:
    cleaned = "".join(
        c
        for c in title
        if unicodedata.category(c) not in {"So", "Cf"} and c not in {"\ufe0f", "\u200d"}
    )
    cleaned = re.sub(r"--+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|/:·•[]")
    return (cleaned or title.strip())[:45]


def valid_url(url: str) -> bool:
    return urlsplit(url).scheme in {"https", "http"} and len(url) <= 1000


def link(url: str, label: str) -> str:
    if not valid_url(url):
        return ""
    return f'<a href="{escape(url, quote=True)}">{escape(label[:55])}</a>'


def buy_markup(offer: Offer) -> dict | None:
    if not offer.buy_url or not valid_url(offer.buy_url):
        return None
    return {"inline_keyboard": [[{"text": "Abrir oferta", "url": offer.buy_url}]]}


def render(offer: Offer, previous: int | None) -> str:
    if previous is not None:
        header = f"📉 <b>{escape(offer.title)} · caiu {money(previous - offer.price)}</b>"
    elif offer.restocked:
        header = f"🔄 <b>{escape(offer.title)} · voltou</b>"
    else:
        header = f"⚡ <b>{escape(offer.title)}</b>"

    head_lines = [header]
    if offer.item_name and offer.item_name.casefold() != offer.title.casefold():
        head_lines.append(f"📦 {escape(offer.item_name[:110])}")
    sections: list[list[str]] = [head_lines]

    store_lines: list[str] = []
    if offer.store:
        store_lines.append(f"🏪 Loja: {escape(store_display(offer.store))}")
    if offer.seller:
        store_lines.append(f"🏷️ Vendido por: {escape(offer.seller[:80])}")
    if store_lines:
        sections.append(store_lines)

    price_lines: list[str] = []
    price_text = f"💰 Preço: <b>{money(offer.price)}</b>"
    if previous is not None:
        price_text += f" (antes {money(previous)})"
    price_lines.append(price_text)

    if offer.payment_method:
        price_lines.append(f"💳 Pagamento: {escape(offer.payment_method)}")
    elif offer.payments:
        price_lines.append(f"💳 {escape(offer.payments[0])}")
        for extra in offer.payments[1:]:
            price_lines.append(f"• {escape(extra)}")

    if offer.installments:
        price_lines.append(f"🧾 Parcelas: {escape(offer.installments)}")

    for other in offer.other_payments:
        price_lines.append(f"💳 Outra opção: {escape(other)}")

    if offer.coupons:
        codes = ", ".join(f"<code>{escape(c)}</code>" for c in offer.coupons)
        price_lines.append(f"🎟️ Cupom: {codes}")

    if offer.shipping is not None:
        freight = f"Frete {money(offer.shipping)}" if offer.shipping else "Frete grátis"
        price_lines.append(f"🚚 {freight}")

    sections.append(price_lines)

    references = [link(url, clean_post_label(title)) for url, title in offer.posts.items()]
    references = [reference for reference in references if reference]
    if references:
        visible = references[:MAX_VISIBLE_POSTS]
        post_lines = [f"<b>Posts ({len(references)}):</b>"]
        post_lines.extend(f"• {ref}" for ref in visible)
        remainder = len(references) - len(visible)
        if remainder:
            post_lines.append(f"• <i>+{remainder} posts</i>")
        sections.append(post_lines)

    return "\n\n".join("\n".join(section) for section in sections)


def enqueue(db: sqlite3.Connection, offer: Offer) -> None:
    db.execute("INSERT INTO promo_outbox (data) VALUES (?)", (json.dumps(asdict(offer)),))


def deliver(db: sqlite3.Connection, bot: Bot) -> None:
    while True:
        with db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("SELECT id, data FROM promo_outbox ORDER BY id LIMIT 1").fetchone()
            if entry is None:
                return
            offer = Offer(**json.loads(entry[1]))
            now = db.execute("SELECT unixepoch()").fetchone()[0]
            deliver_offer(db, bot, offer, now)
            db.execute("DELETE FROM promo_outbox WHERE id = ?", (entry[0],))


def deliver_offer(db: sqlite3.Connection, bot: Bot, offer: Offer, now: int) -> None:
    row = db.execute(
        "SELECT message_id, touched_at, notified_price, previous_price, data "
        "FROM promo_notice WHERE offer_key = ?",
        (offer.key,),
    ).fetchone()
    fresh_posts = {
        url: title
        for url, title in offer.posts.items()
        if not db.execute(
            "SELECT 1 FROM promo_posts WHERE offer_key = ? AND url = ?",
            (offer.key, url),
        ).fetchone()
    }
    if offer.posts and not fresh_posts:
        return
    db.executemany(
        "INSERT INTO promo_posts VALUES (?, ?)",
        ((offer.key, url) for url in fresh_posts),
    )
    if row is None:
        save_notice(db, bot, offer, now)
        return

    message_id, touched_at, notified_price, previous_price, data = row
    current = Offer(**json.loads(data))
    shipping_changed = (
        current.shipping is not None
        and offer.shipping is not None
        and current.shipping != offer.shipping
    )
    price_dropped = notified_price - offer.price >= MIN_PRICE_DROP
    repost = bool(fresh_posts) and now - touched_at >= DEDUP_SECONDS
    restocked = offer.restocked and bool(fresh_posts) and not current.restocked
    if shipping_changed or price_dropped or repost or restocked:
        save_notice(
            db,
            bot,
            offer,
            now,
            previous=notified_price if price_dropped else None,
        )
        return
    if offer.price != current.price or not fresh_posts:
        return

    current.posts.update(fresh_posts)
    current.buy_url = current.buy_url or offer.buy_url
    current.store = current.store or offer.store
    current.seller = current.seller or offer.seller
    if len(offer.item_name) > len(current.item_name):
        current.item_name = offer.item_name
    if offer.shipping is not None:
        current.shipping = offer.shipping
    current.payment_method = current.payment_method or offer.payment_method
    current.installments = current.installments or offer.installments
    if not current.other_payments and offer.other_payments:
        current.other_payments = list(offer.other_payments)
    save_notice(db, bot, current, now, message_id=message_id, previous=previous_price)


NTFY_PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5, "max": 5}


def _ntfy_priority(raw: str) -> int:
    val = raw.strip().casefold()
    if val.isdigit():
        return max(1, min(5, int(val)))
    return NTFY_PRIORITIES.get(val, 4)


def build_ntfy_payload(
    offer: Offer,
    previous: int | None,
    *,
    topic: str,
    priority: int = 5,
) -> dict:
    if previous is not None:
        title = f"Caiu {money(previous - offer.price)}: {offer.title} · {money(offer.price)}"
        tags = ["chart_with_downwards_trend", "zap"]
    elif offer.restocked:
        title = f"Voltou: {offer.title} · {money(offer.price)}"
        tags = ["arrows_counterclockwise", "zap"]
    else:
        title = f"{offer.title} · {money(offer.price)}"
        tags = ["zap", "moneybag"]

    lines: list[str] = []
    if offer.item_name and offer.item_name.casefold() != offer.title.casefold():
        lines.append(offer.item_name[:120])
    if offer.store:
        lines.append(f"Loja: {store_display(offer.store)}")
    if offer.seller:
        lines.append(f"Vendido por: {offer.seller[:80]}")
    if previous is not None:
        lines.append(f"Antes: {money(previous)}")
    if offer.payment_method and offer.payment_method != "Não informada":
        lines.append(f"Pagamento: {offer.payment_method}")
    if offer.installments:
        lines.append(f"Parcelas: {offer.installments}")
    if offer.coupons:
        lines.append(f"Cupom: {', '.join(offer.coupons)}")
    if offer.shipping is not None:
        lines.append(f"Frete: {money(offer.shipping) if offer.shipping else 'grátis'}")

    actions: list[dict] = []
    click_url = ""
    if offer.buy_url and valid_url(offer.buy_url):
        click_url = offer.buy_url
        actions.append({"action": "view", "label": "Abrir oferta", "url": offer.buy_url})
    first_post = next((u for u in offer.posts if valid_url(u)), "")
    if first_post:
        if not click_url:
            click_url = first_post
        actions.append({"action": "view", "label": "Ver post", "url": first_post})

    payload: dict = {
        "topic": topic,
        "title": title[:140],
        "message": "\n".join(lines) or money(offer.price),
        "priority": priority,
        "tags": tags,
    }
    if click_url:
        payload["click"] = click_url
    if actions:
        payload["actions"] = actions
    return payload


def notify_ntfy(offer: Offer, previous: int | None) -> None:
    """Send an optional best-effort ntfy push notification; never raises or leaks secrets."""
    if os.getenv("NTFY_ENABLED", "").strip() != "1":
        return
    base_url = os.getenv("NTFY_URL", "").strip().rstrip("/")
    topic = (os.getenv("TGSEARCH_NTFY_TOPIC") or os.getenv("NTFY_TOPIC") or "").strip()
    if not base_url or not topic or not valid_url(base_url):
        return
    priority = _ntfy_priority(os.getenv("TGSEARCH_NTFY_PRIORITY", "urgent"))
    payload = build_ntfy_payload(offer, previous, topic=topic, priority=priority)
    request = Request(
        base_url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=8) as response:
            response.read(256)
    except Exception as exc:
        print(f"promo_delivery ntfy warning: {type(exc).__name__}", file=sys.stderr, flush=True)


def save_notice(
    db: sqlite3.Connection,
    bot: Bot,
    offer: Offer,
    now: int,
    *,
    message_id: int | None = None,
    previous: int | None = None,
) -> None:
    original_message_id = message_id
    text = render(offer, previous)
    options: dict = {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    markup = buy_markup(offer)
    if markup is not None:
        options["reply_markup"] = markup
    try:
        result = bot.message(text, message_id=message_id, **options)
    except BotError as error:
        description = error.description.casefold()
        if message_id is not None and "message is not modified" in description:
            pass  # Telegram already has this edit, including after a retry.
        elif message_id is not None and "message to edit not found" in description:
            result = bot.message(text, **options)
            message_id = result["message_id"]
        else:
            raise
    else:
        if message_id is None:
            message_id = result["message_id"]
    is_new_notice = original_message_id is None
    db.execute(
        "INSERT OR REPLACE INTO promo_notice VALUES (?, ?, ?, ?, ?, ?)",
        (offer.key, message_id, now, offer.price, previous, json.dumps(asdict(offer))),
    )
    if is_new_notice:
        notify_ntfy(offer, previous)
