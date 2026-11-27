"""On-demand Telegram history search (24h / 7d) and monitored channel listing for PromoBot."""

from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from . import promo_delivery, promo_menu, watch
from .credentials import resolve
from .sources.live import (
    chat_kind,
    chat_name,
    coerce_chat,
    default_session_path,
    open_client,
    to_message,
)

BRT = ZoneInfo("America/Sao_Paulo")
MAX_HTML_CHARS = 3700
MAX_UNDER_CEILING = 12
ABOVE_PAGE_SIZE = 5
MAX_POSTS_PER_CARD = 15


@dataclass
class HistoryEntry:
    offer: promo_delivery.Offer
    seen_at: datetime
    within_ceiling: bool
    alert_ceiling: int

    def to_dict(self) -> dict:
        return {
            "offer": asdict(self.offer),
            "seen_at": self.seen_at.isoformat(),
            "within_ceiling": self.within_ceiling,
            "alert_ceiling": self.alert_ceiling,
        }

    @classmethod
    def from_dict(cls, data: dict) -> HistoryEntry:
        return cls(
            offer=promo_delivery.Offer(**data["offer"]),
            seen_at=datetime.fromisoformat(data["seen_at"]),
            within_ceiling=bool(data["within_ceiling"]),
            alert_ceiling=int(data["alert_ceiling"]),
        )


@dataclass(frozen=True)
class ChannelEntry:
    raw: str
    title: str
    username: str
    kind: str


def clean_channel_label(title: str) -> str:
    """Strip noisy decorative emojis and repeated dashes from channel titles."""
    cleaned = "".join(
        c
        for c in title
        if unicodedata.category(c) not in {"So", "Cf"} and c not in {"\ufe0f", "\u200d"}
    )
    cleaned = re.sub(r"--+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|/:·•[]")
    return (cleaned or title.strip())[:45]


def resolve_sources_file(sources_file: Path | None) -> Path | None:
    if sources_file is not None:
        return sources_file
    env_sources = os.getenv("TGSEARCH_SOURCES", "").strip()
    if env_sources:
        return Path(env_sources).expanduser()
    default_sources = Path.home() / ".config/tgsearch-watch/sources"
    return default_sources if default_sources.is_file() else None


def read_sources(sources_file: Path | None) -> list[str]:
    path = resolve_sources_file(sources_file)
    if path is None or not path.is_file():
        return []
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def search_queries(alert: promo_menu.Alert) -> tuple[str, ...]:
    if alert.product == "rx7600":
        return ("rx 7600", "rx7600")
    if alert.product == "rx7600xt":
        return ("7600 xt", "7600xt")
    if alert.product == "17e":
        return ("iphone 17e",)
    if alert.product == "17":
        return ("iphone 17",)
    return (alert.label,)


def load_source_ids(sources_file: Path | None) -> set[int]:
    ids: set[int] = set()
    for raw in read_sources(sources_file):
        if raw.lstrip("-").isdigit():
            if raw.startswith("-100"):
                ids.add(int(raw[4:]))
            else:
                ids.add(abs(int(raw)))
    return ids


def group_history_entries(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """Merge duplicate reposts of the same (offer_key, rounded_price) keeping all source links."""
    grouped: dict[tuple[str, int], HistoryEntry] = {}
    for entry in entries:
        price_bucket = (
            entry.offer.price if entry.within_ceiling else (entry.offer.price // 100) * 100
        )
        gkey = (entry.offer.key, price_bucket)
        existing = grouped.get(gkey)
        if existing is None:
            grouped[gkey] = HistoryEntry(
                offer=promo_delivery.Offer(
                    key=entry.offer.key,
                    title=entry.offer.title,
                    store=entry.offer.store,
                    price=entry.offer.price,
                    payments=list(entry.offer.payments),
                    coupons=list(entry.offer.coupons),
                    shipping=entry.offer.shipping,
                    buy_url=entry.offer.buy_url,
                    posts=dict(entry.offer.posts),
                    seller=entry.offer.seller,
                    restocked=entry.offer.restocked,
                    payment_method=entry.offer.payment_method,
                    installments=entry.offer.installments,
                    other_payments=list(entry.offer.other_payments),
                ),
                seen_at=entry.seen_at,
                within_ceiling=entry.within_ceiling,
                alert_ceiling=entry.alert_ceiling,
            )
            continue
        existing.offer.posts.update(entry.offer.posts)
        existing.offer.buy_url = existing.offer.buy_url or entry.offer.buy_url
        existing.offer.seller = existing.offer.seller or entry.offer.seller
        existing.offer.installments = existing.offer.installments or entry.offer.installments
        if not existing.offer.payment_method or existing.offer.payment_method == "Não informada":
            existing.offer.payment_method = entry.offer.payment_method
        if existing.offer.shipping is None and entry.offer.shipping is not None:
            existing.offer.shipping = entry.offer.shipping
        if entry.offer.price < existing.offer.price:
            existing.offer.price = entry.offer.price
        if entry.seen_at > existing.seen_at:
            existing.seen_at = entry.seen_at
        existing.within_ceiling = existing.within_ceiling or entry.within_ceiling
    return sorted(
        grouped.values(),
        key=lambda e: (not e.within_ceiling, e.offer.price, -e.seen_at.timestamp()),
    )


def format_offer_card(entry: HistoryEntry, *, best: bool, show_title: bool) -> str:
    """Render one clean, airy blockquote card with balanced emojis and full post list."""
    offer = entry.offer
    local_dt = entry.seen_at.astimezone(BRT)
    date_str = local_dt.strftime("%d/%m")
    time_str = local_dt.strftime("%H:%M")
    store = promo_delivery.store_display(offer.store) or "Loja não identificada"
    badge = "🏆 " if best else "💰 "
    if show_title:
        head = (
            f"{badge}<b>{escape(offer.title)}</b>\n"
            f"<b>{promo_delivery.money(offer.price)}</b> — {escape(store)}"
        )
    else:
        head = f"{badge}<b>{promo_delivery.money(offer.price)}</b> — {escape(store)}"

    details: list[str] = []
    if offer.payment_method and offer.payment_method != "Não informada":
        details.append(f"💳 Pagamento: {escape(offer.payment_method)}")
    if offer.installments:
        details.append(f"🧾 Parcelas: {escape(offer.installments)}")
    if offer.coupons:
        codes = ", ".join(f"<code>{escape(c)}</code>" for c in offer.coupons)
        details.append(f"🎟️ Cupom: {codes}")
    if offer.shipping is not None:
        freight = (
            f"🚚 Frete: {promo_delivery.money(offer.shipping)}"
            if offer.shipping
            else "🚚 Frete grátis"
        )
        details.append(freight)
    if offer.seller:
        details.append(f"🏷️ Vendedor: {escape(offer.seller[:60])}")
    details.append(f"📅 {date_str} · 🕒 {time_str}")

    sections = [head, "\n".join(details)]

    if offer.buy_url and promo_delivery.valid_url(offer.buy_url):
        sections.append(f"🛒 {promo_delivery.link(offer.buy_url, 'Abrir oferta')}")

    valid_posts = [
        (url, clean_channel_label(title))
        for url, title in offer.posts.items()
        if promo_delivery.valid_url(url)
    ]
    if valid_posts:
        visible = valid_posts[:MAX_POSTS_PER_CARD]
        post_lines = [f"<b>Posts ({len(valid_posts)}):</b>"]
        for url, label in visible:
            post_lines.append(f"• {promo_delivery.link(url, label)}")
        extra = len(valid_posts) - len(visible)
        if extra:
            post_lines.append(f"• <i>+{extra} posts</i>")
        sections.append("\n".join(post_lines))

    return "<blockquote>" + "\n\n".join(sections) + "</blockquote>"


def _paginate_blocks(blocks: Sequence[str]) -> list[str]:
    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > MAX_HTML_CHARS and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def render_digest(
    title: str,
    period_label: str,
    entries: Sequence[HistoryEntry],
    *,
    ceiling: int | None = None,
    multi_product: bool = False,
) -> tuple[list[str], list[HistoryEntry]]:
    """Return (main_messages, above_ceiling_entries) for clean Option A layout."""
    grouped = group_history_entries(entries)
    under = [e for e in grouped if e.within_ceiling]
    above = [e for e in grouped if not e.within_ceiling]

    header_lines = [
        "<b>HISTÓRICO DE OFERTAS</b>",
        "",
        f"📦 <b>Produto:</b> {escape(title)}",
        f"🗓️ <b>Período:</b> {escape(period_label.capitalize())}",
    ]
    if ceiling is not None and not multi_product:
        header_lines.append(f"🎯 <b>Teto:</b> {promo_delivery.money(ceiling)}")
    header = "\n".join(header_lines)

    if not grouped:
        msg = (
            f"{header}\n\n"
            "<blockquote>Nenhuma promoção encontrada nos canais monitorados "
            "nesse período.</blockquote>"
        )
        return [msg], []

    blocks: list[str] = [header]

    under_count = len(under)
    under_word = "oferta" if under_count == 1 else "ofertas"
    above_count = len(above)
    above_word = "oferta" if above_count == 1 else "ofertas"

    summary_lines = [f"✅ <b>DENTRO DO TETO</b> · {under_count} {under_word}"]
    if above:
        lowest = promo_delivery.money(above[0].offer.price)
        summary_lines.append(
            f"❌ <b>FORA DO TETO</b> · {above_count} {above_word} (menor: <b>{lowest}</b>)"
        )
    else:
        summary_lines.append("❌ <b>FORA DO TETO</b> · 0 ofertas")
    blocks.append("\n".join(summary_lines))

    if under:
        for idx, entry in enumerate(under[:MAX_UNDER_CEILING]):
            blocks.append(format_offer_card(entry, best=(idx == 0), show_title=multi_product))
        if under_count > MAX_UNDER_CEILING:
            extra = under_count - MAX_UNDER_CEILING
            blocks.append(f"<i>… e mais {extra} ofertas dentro do teto.</i>")
    else:
        blocks.append(
            "<blockquote>Nenhuma oferta ficou dentro do seu teto nesse período.</blockquote>"
        )

    return _paginate_blocks(blocks), above


def render_above_page(
    title: str,
    period_label: str,
    above: Sequence[HistoryEntry],
    *,
    offset: int = 0,
    ceiling: int | None = None,
    multi_product: bool = False,
) -> tuple[list[str], int]:
    """Render a clean, dedicated page of above-ceiling offers as full cards."""
    page = above[offset : offset + ABOVE_PAGE_SIZE]
    remaining = max(0, len(above) - (offset + len(page)))
    start_num = offset + 1
    end_num = offset + len(page)

    header_lines = [
        "❌ <b>OFERTAS FORA DO TETO</b>",
        "",
        f"📦 <b>Produto:</b> {escape(title)}",
        f"🗓️ <b>Período:</b> {escape(period_label.capitalize())}",
    ]
    if ceiling is not None and not multi_product:
        header_lines.append(f"🎯 <b>Teto:</b> {promo_delivery.money(ceiling)}")
    header_lines.append(f"📄 <b>Mostrando:</b> {start_num} a {end_num} de {len(above)}")

    blocks = ["\n".join(header_lines)]
    for idx, entry in enumerate(page):
        blocks.append(
            format_offer_card(entry, best=(offset == 0 and idx == 0), show_title=multi_product)
        )
    return _paginate_blocks(blocks), remaining


def render_channels(channels: Sequence[ChannelEntry]) -> list[str]:
    header = (
        f"📡 <b>CANAIS MONITORADOS ({len(channels)})</b>\n\n"
        "Estes são os canais e grupos consultados nas buscas:"
    )
    if not channels:
        return [f"{header}\n\n<blockquote>Nenhum canal configurado no momento.</blockquote>"]

    lines: list[str] = []
    for idx, ch in enumerate(channels, start=1):
        label = escape(clean_channel_label(ch.title))
        if ch.username:
            href = f"https://t.me/{escape(ch.username, quote=True)}"
            item = f'{idx}. <a href="{href}">{label}</a> · @{escape(ch.username)}'
        else:
            item = f"{idx}. <b>{label}</b> <i>(privado)</i>"
        lines.append(item)

    messages: list[str] = []
    chunk: list[str] = []
    prefix = f"{header}\n\n"
    for line in lines:
        candidate = prefix + "\n".join(chunk + [line])
        if len(candidate) > MAX_HTML_CHARS and chunk:
            messages.append(prefix + "\n".join(chunk))
            prefix = "<b>CANAIS MONITORADOS (continuação)</b>\n\n"
            chunk = [line]
        else:
            chunk.append(line)
    if chunk:
        messages.append(prefix + "\n".join(chunk))
    return messages


async def _fetch_history(
    alerts: Sequence[promo_menu.Alert],
    hours: int,
    sources_file: Path | None,
    session: Path | None,
) -> list[HistoryEntry]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    source_ids = load_source_ids(sources_file)
    limit = 120 if hours <= 24 else 250

    high_targets: list[tuple[promo_menu.Alert, watch.Profile]] = []
    real_ceilings: dict[str, int] = {}
    queries: list[str] = []
    for alert in alerts:
        real_ceilings[alert.product] = alert.ceiling
        high_alert = promo_menu.Alert(
            alert.id, alert.product, alert.label, 10**9, alert.paused, alert.filters
        )
        high_targets.append((high_alert, watch.profile_for_alert(alert)))
        for q in search_queries(alert):
            if q not in queries:
                queries.append(q)

    seen_msgs: set[tuple[int, int]] = set()
    entries: list[HistoryEntry] = []
    async with open_client(resolve(), session or default_session_path()) as client:
        for query in queries:
            async for item in client.iter_messages(None, search=query, limit=limit):
                if item.date and item.date < since:
                    break
                if getattr(item, "is_private", False):
                    continue
                entity = await item.get_chat()
                if entity is None:
                    continue
                kind = chat_kind(entity)
                if kind == "user":
                    continue
                if source_ids and entity.id not in source_ids:
                    continue
                uid = (entity.id, item.id)
                if uid in seen_msgs:
                    continue
                seen_msgs.add(uid)
                message = to_message(entity, item)
                if not message:
                    continue
                seen_at = item.date or datetime.now(timezone.utc)
                for offer in watch.promo_matches(message, kind, high_targets):
                    product = json.loads(offer.key)[0]
                    ceiling = real_ceilings.get(product, 0)
                    total = offer.price + (offer.shipping or 0)
                    entries.append(
                        HistoryEntry(
                            offer=offer,
                            seen_at=seen_at,
                            within_ceiling=total <= ceiling,
                            alert_ceiling=ceiling,
                        )
                    )
    return entries


async def _fetch_channels(
    sources_file: Path | None,
    session: Path | None,
) -> list[ChannelEntry]:
    raw_sources = read_sources(sources_file)
    if not raw_sources:
        return []
    result: list[ChannelEntry] = []
    async with open_client(resolve(), session or default_session_path()) as client:
        for raw in raw_sources:
            try:
                entity = await client.get_entity(coerce_chat(raw))
                title = chat_name(entity)
                username = getattr(entity, "username", None) or ""
                kind = chat_kind(entity)
            except Exception:
                title = raw
                username = ""
                kind = "channel"
            result.append(ChannelEntry(raw=raw, title=title, username=username, kind=kind))
    return sorted(result, key=lambda c: clean_channel_label(c.title).casefold())


def search_history(
    state: Path,
    alerts: Sequence[promo_menu.Alert],
    hours: int,
    *,
    sources_file: Path | None = None,
    session: Path | None = None,
) -> list[HistoryEntry]:
    """Run an on-demand Telegram history search safely serialized with watch.py."""
    resolved = resolve_sources_file(sources_file)
    with watch._scan_lock(state):
        return asyncio.run(_fetch_history(alerts, hours, resolved, session))


def list_monitored_channels(
    state: Path,
    *,
    sources_file: Path | None = None,
    session: Path | None = None,
) -> list[ChannelEntry]:
    """Resolve names and links for all monitored channels, serialized with watch.py."""
    resolved = resolve_sources_file(sources_file)
    with watch._scan_lock(state):
        return asyncio.run(_fetch_channels(resolved, session))
