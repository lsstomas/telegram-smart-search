"""On-demand Telegram history search (24h / 7d) for PromoBot."""

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
from .sources.live import chat_kind, default_session_path, open_client, to_message

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
    if sources_file is None or not sources_file.is_file():
        return set()
    ids: set[int] = set()
    for line in sources_file.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.lstrip("-").isdigit():
            if raw.startswith("-100"):
                ids.add(int(raw[4:]))
            else:
                ids.add(abs(int(raw)))
    return ids


def group_history_entries(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """Merge duplicate reposts of the same (offer_key, price) keeping all source links."""
    grouped: dict[tuple[str, int], HistoryEntry] = {}
    for entry in entries:
        gkey = (entry.offer.key, entry.offer.price)
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
        if existing.offer.shipping is None and entry.offer.shipping is not None:
            existing.offer.shipping = entry.offer.shipping
        if entry.seen_at > existing.seen_at:
            existing.seen_at = entry.seen_at
        existing.within_ceiling = existing.within_ceiling or entry.within_ceiling
    return sorted(
        grouped.values(),
        key=lambda e: (not e.within_ceiling, e.offer.price, -e.seen_at.timestamp()),
    )


def _format_entry(entry: HistoryEntry) -> str:
    offer = entry.offer
    dt_str = entry.seen_at.astimezone(BRT).strftime("%d/%m %H:%M")
    store = promo_delivery.store_display(offer.store) or "Loja não identificada"
    lines = [
        f"• <b>{escape(offer.title)}</b> — <b>{promo_delivery.money(offer.price)}</b>",
        f"  🏪 {escape(store)} · 🕒 {dt_str}",
    ]
    if offer.payment_method and offer.payment_method != "Não informada":
        lines.append(f"  💳 {escape(offer.payment_method)}")
    if offer.installments:
        lines.append(f"  🧾 {escape(offer.installments)}")
    if offer.coupons:
        codes = ", ".join(f"<code>{escape(c)}</code>" for c in offer.coupons)
        lines.append(f"  🎟️ Cupom: {codes}")
    if offer.shipping is not None:
        freight = (
            f"Frete {promo_delivery.money(offer.shipping)}" if offer.shipping else "Frete grátis"
        )
        lines.append(f"  🚚 {freight}")
    if offer.seller:
        lines.append(f"  🏷️ {escape(offer.seller[:60])}")
    links_parts: list[str] = []
    buy = promo_delivery.link(offer.buy_url, "Abrir oferta") if offer.buy_url else ""
    if buy:
        links_parts.append(f"🛒 {buy}")
    refs = [promo_delivery.link(url, title) for url, title in offer.posts.items()]
    refs = [r for r in refs if r]
    if refs:
        visible = refs[:4]
        extra = len(refs) - len(visible)
        suffix = f" (+{extra})" if extra else ""
        links_parts.append("🔗 " + " · ".join(visible) + suffix)
    if links_parts:
        lines.append("  " + " | ".join(links_parts))
    return "\n".join(lines)


def render_digest(
    title: str,
    period_label: str,
    entries: Sequence[HistoryEntry],
) -> list[str]:
    grouped = group_history_entries(entries)
    under = [e for e in grouped if e.within_ceiling]
    above = [e for e in grouped if not e.within_ceiling]

    header = f"🔎 <b>Histórico · {escape(title)}</b>\n🗓️ Período: {escape(period_label)}"
    if not grouped:
        return [f"{header}\n\nNenhuma promoção encontrada nos canais monitorados nesse período."]

    blocks: list[str] = [header]
    if under:
        blocks.append(f"✅ <b>Dentro do teto ({len(under)})</b>")
        for entry in under[:MAX_UNDER_CEILING]:
            blocks.append(_format_entry(entry))
        if len(under) > MAX_UNDER_CEILING:
            blocks.append(
                f"<i>… e mais {len(under) - MAX_UNDER_CEILING} ofertas dentro do teto.</i>"
            )
    else:
        blocks.append("ℹ️ <b>Nenhuma oferta dentro do seu teto nesse período.</b>")

    if above:
        limit = MAX_ABOVE_CEILING
        blocks.append(
            f"📊 <b>Menores preços acima do teto ({min(len(above), limit)} de {len(above)})</b>"
        )
        for entry in above[:limit]:
            blocks.append(_format_entry(entry))

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


async def _fetch_history(
    alerts: Sequence[promo_menu.Alert],
    hours: int,
    sources_file: Path | None,
    session: Path | None,
) -> list[HistoryEntry]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    source_ids = load_source_ids(sources_file)
    limit = 120 if hours <= 24 else 250

    # Prepare high-ceiling targets so we can also surface cheapest above-ceiling deals.
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


def search_history(
    state: Path,
    alerts: Sequence[promo_menu.Alert],
    hours: int,
    *,
    sources_file: Path | None = None,
    session: Path | None = None,
) -> list[HistoryEntry]:
    """Run an on-demand Telegram history search safely serialized with watch.py."""
    if sources_file is None:
        env_sources = os.getenv("TGSEARCH_SOURCES", "").strip()
        if env_sources:
            sources_file = Path(env_sources).expanduser()
        else:
            default_sources = Path.home() / ".config/tgsearch-watch/sources"
            if default_sources.is_file():
                sources_file = default_sources
    with watch._scan_lock(state):
        return asyncio.run(_fetch_history(alerts, hours, sources_file, session))
