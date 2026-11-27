"""On-demand Telegram history search (24h / 7d) and monitored channel listing for PromoBot."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
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
MAX_ABOVE_CEILING = 5


@dataclass
class HistoryEntry:
    offer: promo_delivery.Offer
    seen_at: datetime
    within_ceiling: bool
    alert_ceiling: int


@dataclass(frozen=True)
class ChannelEntry:
    raw: str
    title: str
    username: str
    kind: str


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
        # Bucket by whole reais when above ceiling so R$ 5.030,00 and R$ 5.030,28 merge cleanly.
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


def _format_card(entry: HistoryEntry, *, best: bool, show_title: bool) -> str:
    offer = entry.offer
    dt_str = entry.seen_at.astimezone(BRT).strftime("%d/%m às %H:%M")
    store = promo_delivery.store_display(offer.store) or "Loja não identificada"
    badge = "🏆 " if best else ""
    if show_title:
        head = (
            f"{badge}<b>{escape(offer.title)}</b>\n"
            f"💰 <b>{promo_delivery.money(offer.price)}</b> · {escape(store)}"
        )
    else:
        head = f"{badge}<b>{promo_delivery.money(offer.price)}</b> · {escape(store)}"

    details: list[str] = []
    if offer.payment_method and offer.payment_method != "Não informada":
        details.append(f"💳 {escape(offer.payment_method)}")
    if offer.installments:
        details.append(f"🧾 {escape(offer.installments)}")
    if offer.coupons:
        codes = ", ".join(f"<code>{escape(c)}</code>" for c in offer.coupons)
        details.append(f"🎟️ Cupom: {codes}")
    if offer.shipping is not None:
        freight = (
            f"Frete {promo_delivery.money(offer.shipping)}" if offer.shipping else "Frete grátis"
        )
        details.append(f"🚚 {freight}")
    if offer.seller:
        details.append(f"🏷️ {escape(offer.seller[:60])}")
    details.append(f"🕒 {dt_str}")

    action_parts: list[str] = []
    buy = promo_delivery.link(offer.buy_url, "Abrir oferta") if offer.buy_url else ""
    if buy:
        action_parts.append(f"🛒 {buy}")
    valid_posts = [url for url in offer.posts if promo_delivery.valid_url(url)]
    if valid_posts:
        count = len(valid_posts)
        post_label = "1 post" if count == 1 else f"{count} posts"
        action_parts.append(f"🔗 {promo_delivery.link(valid_posts[0], post_label)}")

    sections = [head, "\n".join(details)]
    if action_parts:
        sections.append(" · ".join(action_parts))
    return "<blockquote>" + "\n\n".join(sections) + "</blockquote>"


def _format_above_line(entry: HistoryEntry, *, show_title: bool) -> str:
    offer = entry.offer
    store = promo_delivery.store_display(offer.store) or "Loja"
    parts = [f"<b>{promo_delivery.money(offer.price)}</b>"]
    if show_title:
        parts.insert(0, f"<b>{escape(offer.title)}</b>")
    store_str = escape(store)
    if offer.payment_method and offer.payment_method != "Não informada":
        store_str += f" ({escape(offer.payment_method)})"
    parts.append(store_str)
    if offer.coupons:
        parts.append(", ".join(f"<code>{escape(c)}</code>" for c in offer.coupons))
    if offer.buy_url and promo_delivery.valid_url(offer.buy_url):
        parts.append(promo_delivery.link(offer.buy_url, "Abrir"))
    else:
        valid_posts = [url for url in offer.posts if promo_delivery.valid_url(url)]
        if valid_posts:
            parts.append(promo_delivery.link(valid_posts[0], "Post"))
    return "• " + " · ".join(parts)


def render_digest(
    title: str,
    period_label: str,
    entries: Sequence[HistoryEntry],
    *,
    ceiling: int | None = None,
    multi_product: bool = False,
) -> list[str]:
    grouped = group_history_entries(entries)
    under = [e for e in grouped if e.within_ceiling]
    above = [e for e in grouped if not e.within_ceiling]

    sub = f"🗓️ {escape(period_label.capitalize())}"
    if ceiling is not None and not multi_product:
        sub += f" · 🎯 Teto: <b>{promo_delivery.money(ceiling)}</b>"
    header = f"🔎 <b>{escape(title)}</b>\n{sub}"

    if not grouped:
        return [
            f"{header}\n\n"
            "<blockquote>Nenhuma promoção encontrada nos canais monitorados "
            "nesse período.</blockquote>"
        ]

    blocks: list[str] = [header]
    if under:
        blocks.append(f"✅ <b>Dentro do teto ({len(under)})</b>")
        for idx, entry in enumerate(under[:MAX_UNDER_CEILING]):
            blocks.append(_format_card(entry, best=(idx == 0), show_title=multi_product))
        if len(under) > MAX_UNDER_CEILING:
            extra = len(under) - MAX_UNDER_CEILING
            blocks.append(f"<i>… e mais {extra} ofertas dentro do teto.</i>")
    else:
        blocks.append("ℹ️ <b>Nenhuma oferta dentro do seu teto nesse período.</b>")

    if above:
        limit = MAX_ABOVE_CEILING
        shown = above[:limit]
        above_lines = [f"📊 <b>Acima do teto · {len(shown)} menores (de {len(above)})</b>"]
        above_lines.extend(_format_above_line(e, show_title=multi_product) for e in shown)
        blocks.append("<blockquote expandable>" + "\n\n".join(above_lines) + "</blockquote>")

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


def render_channels(channels: Sequence[ChannelEntry]) -> list[str]:
    header = (
        f"📡 <b>Canais monitorados ({len(channels)})</b>\n\n"
        "Estes são os canais e grupos consultados nas buscas:"
    )
    if not channels:
        return [f"{header}\n\n<blockquote>Nenhum canal configurado no momento.</blockquote>"]

    lines: list[str] = []
    for idx, ch in enumerate(channels, start=1):
        label = escape(ch.title[:55])
        if ch.username:
            href = f"https://t.me/{escape(ch.username, quote=True)}"
            item = f'{idx}. <a href="{href}">{label}</a> · @{escape(ch.username)}'
        else:
            item = f"{idx}. <b>{label}</b> <i>(privado)</i>"
        lines.append(item)

    messages: list[str] = []
    chunk: list[str] = []
    prefix = header + "\n\n"
    for line in lines:
        candidate = prefix + "\n".join(chunk + [line])
        if len(candidate) > MAX_HTML_CHARS and chunk:
            messages.append(prefix + "\n".join(chunk))
            prefix = "📡 <b>Canais monitorados (continuação)</b>\n\n"
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
    return sorted(result, key=lambda c: c.title.casefold())


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
