"""Private PromoBot configuration, served independently of the offer scan."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from html import escape
from pathlib import Path

from .extract import parse_amount
from .promo_delivery import Bot, BotError, cents, configure_db, money
from .textutil import normalize

DEFAULT_ALERTS = (
    ("rx7600", "RX 7600", 170000),
    ("rx7600xt", "RX 7600 XT", 170000),
    ("17e", "iPhone 17e 256 GB", 370000),
    ("17", "iPhone 17 256 GB", 480000),
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS promo_alerts (
    id INTEGER PRIMARY KEY, product TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
    ceiling INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
    filters TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS promo_menu (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
DEFAULT_FILTERS = {
    "mode": "smart",  # "smart" (built-in rules) or "broad" (keywords + ceiling only)
    "block_used": True,
    "block_restrictions": True,
    "require_sealed": False,
    "require_url": True,
    "min_price": 0,
    "include": [],
    "exclude": [],
}
PURE_TEXT_FILTERS = {
    "mode": "broad",
    "block_used": False,
    "block_restrictions": False,
    "require_sealed": False,
    "require_url": False,
    "min_price": 0,
    "include": [],
    "exclude": [],
}
CHANNELS_CACHE_SECONDS = 6 * 3600
MAX_SAVED_SEARCHES = 6
HTML_OPTS = {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}


@dataclass(frozen=True)
class Alert:
    id: int
    product: str
    label: str
    ceiling: int
    paused: int
    filters: str = "{}"

    def filter_config(self) -> dict:
        cfg = dict(DEFAULT_FILTERS)
        cfg["include"] = []
        cfg["exclude"] = []
        if self.product not in {"17", "17e"}:
            cfg["require_url"] = False
        try:
            raw = json.loads(self.filters or "{}")
            if isinstance(raw, dict):
                for key in DEFAULT_FILTERS:
                    if key in raw:
                        cfg[key] = raw[key]
        except ValueError:
            pass
        return cfg


def init(db: sqlite3.Connection, *, seed_defaults: bool | None = None) -> None:
    configure_db(db)
    db.executescript(SCHEMA)
    cols = {row[1] for row in db.execute("PRAGMA table_info(promo_alerts)")}
    if "filters" not in cols:
        with db:
            db.execute("ALTER TABLE promo_alerts ADD COLUMN filters TEXT NOT NULL DEFAULT '{}'")
    if seed_defaults is None:
        seed_defaults = os.getenv("TGSEARCH_SEED_DEFAULTS", "").strip() == "1"
    if seed_defaults:
        with db:
            db.executemany(
                "INSERT OR IGNORE INTO promo_alerts (product, label, ceiling) VALUES (?, ?, ?)",
                DEFAULT_ALERTS,
            )


def alerts(db: sqlite3.Connection, *, active_only: bool = False) -> list[Alert]:
    where = " WHERE paused = 0" if active_only else ""
    query = "SELECT id, product, label, ceiling, paused, filters FROM promo_alerts" + where
    return [Alert(*row) for row in db.execute(query + " ORDER BY id")]


def get(db: sqlite3.Connection, key: str) -> str:
    row = db.execute("SELECT value FROM promo_menu WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def put(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO promo_menu VALUES (?, ?)", (key, value))


def save_filters(db: sqlite3.Connection, alert_id: int, cfg: dict) -> None:
    db.execute(
        "UPDATE promo_alerts SET filters = ? WHERE id = ?",
        (json.dumps(cfg, ensure_ascii=False), alert_id),
    )


def keyboard(*rows):
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": action} for label, action in row] for row in rows
        ]
    }


HOME = keyboard(
    (("➕ Novo alerta", "new"), ("📋 Meus alertas", "list")),
    (("🕒 Buscar histórico", "hist"),),
    (("📡 Canais monitorados", "channels"),),
)
BACK = keyboard((("↩️ Voltar", "home"),))


def _send_home(bot: Bot, message_id: int | None = None) -> None:
    bot.message(
        "⚡ <b>PROMOBOT</b>\n\nO que você quer fazer?",
        message_id=message_id,
        reply_markup=HOME,
        **HTML_OPTS,
    )


def _send_alerts_list(db: sqlite3.Connection, bot: Bot, message_id: int | None) -> None:
    items = alerts(db)
    if not items:
        bot.message(
            "<b>MEUS ALERTAS</b>\n\nVocê ainda não tem alertas cadastrados.",
            message_id=message_id,
            reply_markup=keyboard((("➕ Novo alerta", "new"),), (("↩️ Voltar", "home"),)),
            **HTML_OPTS,
        )
        return
    active_count = sum(1 for a in items if not a.paused)
    paused_count = len(items) - active_count
    subtitle = f"{active_count} ativo{'s' if active_count != 1 else ''}"
    if paused_count:
        subtitle += f" · {paused_count} pausado{'s' if paused_count != 1 else ''}"
    rows = [
        ((f"{'⏸ ' if a.paused else '✅ '}{a.label} · {money(a.ceiling)}", f"view:{a.id}"),)
        for a in items
    ]
    rows.append((("➕ Novo alerta", "new"), ("↩️ Voltar", "home")))
    bot.message(
        f"<b>MEUS ALERTAS ({len(items)})</b>\n"
        f"<i>{subtitle}</i>\n\n"
        "Escolha um produto para gerenciar:",
        message_id=message_id,
        reply_markup=keyboard(*rows),
        **HTML_OPTS,
    )


def _filter_summary(alert: Alert) -> str:
    cfg = alert.filter_config()
    mode_label = "Inteligente" if cfg["mode"] == "smart" else "Texto puro / Amplo"
    min_p = int(cfg.get("min_price") or 0)
    inc = escape(", ".join(cfg["include"])) if cfg["include"] else "nenhuma"
    exc = escape(", ".join(cfg["exclude"])) if cfg["exclude"] else "nenhuma"

    lines = [
        f"• <b>Modo:</b> {mode_label}",
        f"• <b>Bloquear usados/vitrine:</b> {'Sim' if cfg['block_used'] else 'Não'}",
    ]
    if alert.product in {"17", "17e"}:
        lines.append(
            f"• <b>Bloquear cashback/troca:</b> {'Sim' if cfg['block_restrictions'] else 'Não'}"
        )
        lines.append(f"• <b>Exigir 'lacrado':</b> {'Sim' if cfg['require_sealed'] else 'Não'}")
    lines.append(f"• <b>Exigir link da loja:</b> {'Sim' if cfg['require_url'] else 'Não'}")
    lines.append(f"• <b>Preço mínimo:</b> {money(min_p) if min_p > 0 else 'desativado'}")
    lines.append(f"• <b>Obrigatórias:</b> {inc}")
    lines.append(f"• <b>Bloqueadas:</b> {exc}")
    return (
        "<b>FILTROS DO ALERTA</b>\n\n"
        f"📦 <b>Produto:</b> {escape(alert.label)}\n\n"
        "<blockquote>" + "\n".join(lines) + "</blockquote>"
    )


def filter_menu(bot: Bot, alert: Alert, message_id: int | None, status: str = "") -> None:
    cfg = alert.filter_config()
    text = _filter_summary(alert)
    if status:
        text = f"✅ <b>{escape(status)}</b>\n\n{text}"
    rows = []
    if alert.product in {"rx7600", "rx7600xt", "17", "17e"}:
        next_mode = "broad" if cfg["mode"] == "smart" else "smart"
        mode_btn = "⚙️ Modo: Inteligente ⇄" if cfg["mode"] == "smart" else "⚙️ Modo: Amplo ⇄"
        rows.append(((mode_btn, f"flt:{alert.id}:mode:{next_mode}"),))
    used_btn = f"{'✅' if cfg['block_used'] else '❌'} Bloquear usados/vitrine"
    rows.append(((used_btn, f"flt:{alert.id}:block_used:{int(not cfg['block_used'])}"),))
    if alert.product in {"17", "17e"}:
        rest_btn = f"{'✅' if cfg['block_restrictions'] else '❌'} Bloquear cashback/troca"
        rows.append(
            ((rest_btn, f"flt:{alert.id}:block_restrictions:{int(not cfg['block_restrictions'])}"),)
        )
        seal_btn = f"{'✅' if cfg['require_sealed'] else '❌'} Exigir 'lacrado'"
        rows.append(
            ((seal_btn, f"flt:{alert.id}:require_sealed:{int(not cfg['require_sealed'])}"),)
        )
    url_btn = f"{'✅' if cfg['require_url'] else '❌'} Exigir link da loja"
    rows.append(((url_btn, f"flt:{alert.id}:require_url:{int(not cfg['require_url'])}"),))
    rows.append(
        (
            ("➕ Obrigatórias", f"flt_words:{alert.id}:include"),
            ("➖ Bloqueadas", f"flt_words:{alert.id}:exclude"),
        )
    )
    rows.append(
        (
            ("💰 Preço mínimo", f"flt_min:{alert.id}"),
            ("🤖 Sugerir com IA", f"flt_ai:{alert.id}"),
        )
    )
    rows.append(
        (
            ("🔄 Restaurar padrão", f"flt_reset:{alert.id}"),
            ("↩️ Voltar", f"view:{alert.id}"),
        )
    )
    bot.message(text, message_id=message_id, reply_markup=keyboard(*rows), **HTML_OPTS)


def details(bot: Bot, alert: Alert, message_id: int | None, status: str = "") -> None:
    state = "Pausado" if alert.paused else "Ativo"
    icon = "⏸" if alert.paused else "✅"
    cfg = alert.filter_config()
    mode_tag = "Inteligente" if cfg["mode"] == "smart" else "Texto puro"
    extras: list[str] = [mode_tag]
    if cfg["exclude"]:
        n = len(cfg["exclude"])
        extras.append(f"{n} bloqueio{'s' if n != 1 else ''}")
    if int(cfg.get("min_price") or 0) > 0:
        extras.append(f"mín. {money(int(cfg['min_price']))}")

    text = (
        "<b>DETALHES DO ALERTA</b>\n\n"
        f"📦 <b>Produto:</b> {escape(alert.label)}\n"
        f"🎯 <b>Teto:</b> {money(alert.ceiling)}\n"
        f"{icon} <b>Status:</b> {state}\n"
        f"⚙️ <b>Filtro:</b> {' · '.join(extras)}"
    )
    if status:
        text = f"✅ <b>{escape(status)}</b>\n\n{text}"
    action_label = "▶️ Reativar" if alert.paused else "⏸ Pausar"
    bot.message(
        text,
        message_id=message_id,
        reply_markup=keyboard(
            (
                ("✏️ Mudar teto", f"cap:{alert.id}"),
                (action_label, f"toggle:{alert.id}:{1 - alert.paused}"),
            ),
            (("⚙️ Filtros", f"filters:{alert.id}"), ("🕒 Histórico", f"hist_pick:{alert.id}")),
            (("↩️ Voltar", "list"),),
        ),
        **HTML_OPTS,
    )


def _send_history_picker(db: sqlite3.Connection, bot: Bot, message_id: int | None) -> None:
    items = alerts(db)
    if not items:
        bot.message(
            "<b>BUSCAR HISTÓRICO</b>\n\nCrie um alerta primeiro para consultar o histórico.",
            message_id=message_id,
            reply_markup=keyboard((("➕ Novo alerta", "new"),), (("↩️ Voltar", "home"),)),
            **HTML_OPTS,
        )
        return
    rows = [((f"📦 {a.label}", f"hist_pick:{a.id}"),) for a in items]
    rows.append((("📋 Todos os alertas ativos", "hist_pick:all"),))
    rows.append((("↩️ Voltar", "home"),))
    bot.message(
        "<b>BUSCAR HISTÓRICO</b>\n\nEscolha o produto para consultar nos seus canais:",
        message_id=message_id,
        reply_markup=keyboard(*rows),
        **HTML_OPTS,
    )


def _save_above_search(db: sqlite3.Connection, payload: dict) -> str:
    search_id = str(int(time.time() * 1000) % 10**9)
    raw_idx = get(db, "hist_above_index")
    ids = [i for i in raw_idx.split(",") if i] if raw_idx else []
    ids.append(search_id)
    while len(ids) > MAX_SAVED_SEARCHES:
        old_id = ids.pop(0)
        db.execute("DELETE FROM promo_menu WHERE key = ?", (f"hist_above:{old_id}",))
    put(db, f"hist_above:{search_id}", json.dumps(payload, ensure_ascii=False))
    put(db, "hist_above_index", ",".join(ids))
    return search_id


def _run_history_lookup(
    db: sqlite3.Connection,
    bot: Bot,
    target: str,
    hours: int,
    *,
    state_path: Path | None,
    sources_file: Path | None,
    session: Path | None,
) -> None:
    from . import promo_history

    if target == "all":
        selected = alerts(db, active_only=True)
        title = "Todos os alertas ativos"
        ceiling = None
        multi = True
    else:
        alert = next((a for a in alerts(db) if a.id == int(target)), None)
        if alert is None:
            bot.message("⚠️ Alerta não encontrado.", reply_markup=HOME, **HTML_OPTS)
            return
        selected = [alert]
        title = alert.label
        ceiling = alert.ceiling
        multi = False

    if not selected:
        bot.message("ℹ️ Nenhum alerta ativo para buscar.", reply_markup=HOME, **HTML_OPTS)
        return

    period_label = "últimas 24 horas" if hours <= 24 else f"últimos {hours // 24} dias"
    channel_count = len(promo_history.read_sources(sources_file))
    count_hint = f"os {channel_count} canais" if channel_count else "os seus canais"

    loading = bot.message(
        (
            "<b>BUSCANDO OFERTAS</b>\n\n"
            f"📦 <b>Produto:</b> {escape(title)}\n"
            f"🗓️ <b>Período:</b> {escape(period_label.capitalize())}\n\n"
            f"<blockquote>⏳ Varrendo {count_hint} monitorados…</blockquote>"
        ),
        **HTML_OPTS,
    )
    result_mid = loading["message_id"]

    if state_path is None:
        bot.message(
            "⚠️ Busca de histórico indisponível sem caminho de banco configurado.",
            message_id=result_mid,
            reply_markup=keyboard((("🏠 Menu", "home_new"),)),
            **HTML_OPTS,
        )
        return
    db.commit()
    try:
        entries = promo_history.search_history(
            state_path,
            selected,
            hours,
            sources_file=sources_file,
            session=session,
        )
    except Exception as exc:
        print(f"promo_history error: {exc}", file=sys.stderr, flush=True)
        bot.message(
            (
                "<b>BUSCAR HISTÓRICO</b>\n\n"
                f"📦 <b>Produto:</b> {escape(title)}\n\n"
                "<blockquote>⚠️ Não consegui buscar no Telegram agora. "
                "Tente novamente em instantes.</blockquote>"
            ),
            message_id=result_mid,
            reply_markup=keyboard(
                (("🔄 Tentar novamente", f"hist_run:{target}:{hours}"), ("🏠 Menu", "home_new"))
            ),
            **HTML_OPTS,
        )
        return

    chunks, above = promo_history.render_digest(
        title,
        period_label,
        entries,
        ceiling=ceiling,
        multi_product=multi,
    )
    rows: list[tuple[tuple[str, str], ...]] = []
    if above:
        search_id = _save_above_search(
            db,
            {
                "title": title,
                "period_label": period_label,
                "ceiling": ceiling,
                "multi": multi,
                "entries": [e.to_dict() for e in above],
            },
        )
        lowest = money(above[0].offer.price)
        rows.append(((f"🔽 Ver fora do teto (menor: {lowest})", f"hist_above:{search_id}:0"),))
    other_hours = 168 if hours <= 24 else 24
    other_label = "📅 Ver 7 dias" if other_hours == 168 else "🕒 Ver 24h"
    rows.append(((other_label, f"hist_run:{target}:{other_hours}"), ("🕒 Nova busca", "hist_new")))
    rows.append((("🏠 Menu", "home_new"),))
    nav = keyboard(*rows)

    bot.message(
        chunks[0],
        message_id=result_mid,
        reply_markup=nav if len(chunks) == 1 else None,
        **HTML_OPTS,
    )
    for idx, chunk in enumerate(chunks[1:], start=1):
        is_last = idx == len(chunks) - 1
        bot.message(chunk, reply_markup=nav if is_last else None, **HTML_OPTS)


def _show_above_page(db: sqlite3.Connection, bot: Bot, search_id: str, offset: int) -> None:
    from . import promo_history

    raw = get(db, f"hist_above:{search_id}")
    if not raw:
        bot.message(
            "ℹ️ Essa busca expirou. Faça uma nova busca para ver as ofertas fora do teto.",
            reply_markup=keyboard((("🕒 Nova busca", "hist_new"), ("🏠 Menu", "home_new"))),
            **HTML_OPTS,
        )
        return
    data = json.loads(raw)
    above = [promo_history.HistoryEntry.from_dict(item) for item in data["entries"]]
    chunks, remaining = promo_history.render_above_page(
        data["title"],
        data["period_label"],
        above,
        offset=offset,
        ceiling=data.get("ceiling"),
        multi_product=bool(data.get("multi")),
    )
    rows: list[tuple[tuple[str, str], ...]] = []
    if remaining > 0:
        next_offset = offset + promo_history.ABOVE_PAGE_SIZE
        rows.append(
            (
                (
                    f"🔽 Ver mais fora do teto (+{remaining})",
                    f"hist_above:{search_id}:{next_offset}",
                ),
            )
        )
    rows.append((("🕒 Nova busca", "hist_new"), ("🏠 Menu", "home_new")))
    nav = keyboard(*rows)
    for idx, chunk in enumerate(chunks):
        is_last = idx == len(chunks) - 1
        bot.message(chunk, reply_markup=nav if is_last else None, **HTML_OPTS)


def _show_channels(
    db: sqlite3.Connection,
    bot: Bot,
    message_id: int | None,
    *,
    force_refresh: bool,
    state_path: Path | None,
    sources_file: Path | None,
    session: Path | None,
) -> None:
    from . import promo_history

    channels: list[promo_history.ChannelEntry] | None = None
    now = int(time.time())

    if not force_refresh:
        cached_raw = get(db, "channels_cache")
        cached_at = int(get(db, "channels_cache_at") or 0)
        if cached_raw and now - cached_at < CHANNELS_CACHE_SECONDS:
            try:
                channels = [promo_history.ChannelEntry(**row) for row in json.loads(cached_raw)]
            except (ValueError, TypeError):
                channels = None

    if channels is None:
        loading = bot.message(
            "📡 <b>CANAIS MONITORADOS</b>\n\n"
            "<blockquote>⏳ Carregando lista de canais…</blockquote>",
            message_id=message_id,
            **HTML_OPTS,
        )
        target_mid = message_id if message_id is not None else loading["message_id"]
        if state_path is None:
            raw = promo_history.read_sources(sources_file)
            channels = [promo_history.ChannelEntry(r, r, "", "channel") for r in raw]
        else:
            db.commit()
            try:
                channels = promo_history.list_monitored_channels(
                    state_path,
                    sources_file=sources_file,
                    session=session,
                )
                put(db, "channels_cache", json.dumps([asdict(c) for c in channels]))
                put(db, "channels_cache_at", str(now))
            except Exception as exc:
                print(f"promo_channels error: {exc}", file=sys.stderr, flush=True)
                bot.message(
                    "📡 <b>CANAIS MONITORADOS</b>\n\n"
                    "<blockquote>⚠️ Não consegui carregar os canais agora. "
                    "Tente novamente.</blockquote>",
                    message_id=target_mid,
                    reply_markup=keyboard(
                        (("🔄 Tentar novamente", "channels_refresh"), ("🏠 Menu", "home_new"))
                    ),
                    **HTML_OPTS,
                )
                return
    else:
        target_mid = message_id

    chunks = promo_history.render_channels(channels)
    nav = keyboard((("🔄 Atualizar lista", "channels_refresh"), ("🏠 Menu", "home_new")))
    bot.message(
        chunks[0],
        message_id=target_mid,
        reply_markup=nav if len(chunks) == 1 else None,
        **HTML_OPTS,
    )
    for idx, chunk in enumerate(chunks[1:], start=1):
        is_last = idx == len(chunks) - 1
        bot.message(chunk, reply_markup=nav if is_last else None, **HTML_OPTS)


def _prompt_new_alert_confirmation(
    db: sqlite3.Connection, bot: Bot, label: str, ceiling_cents: int
) -> None:
    from . import promo_ai

    put(db, "ceiling", str(ceiling_cents))
    put(db, "step", "confirm")
    put(db, "ai_suggestion", "")

    loading = bot.message(
        (
            "<b>NOVO ALERTA</b>\n\n"
            f"📦 <b>Produto:</b> {escape(label)}\n"
            f"🎯 <b>Teto:</b> {money(ceiling_cents)}\n\n"
            "<blockquote>🤖 Analisando o produto para sugerir filtros…</blockquote>"
        ),
        **HTML_OPTS,
    )
    mid = loading["message_id"]
    db.commit()
    suggestion = promo_ai.suggest_filters(label, ceiling_cents)

    if suggestion is not None:
        put(
            db,
            "ai_suggestion",
            json.dumps(
                {
                    "exclude": suggestion.exclude,
                    "min_price": suggestion.min_price,
                    "tip": suggestion.tip,
                },
                ensure_ascii=False,
            ),
        )
        ai_lines: list[str] = []
        if suggestion.exclude:
            ai_lines.append(f"• <b>Bloquear:</b> {escape(', '.join(suggestion.exclude))}")
        if suggestion.min_price > 0:
            ai_lines.append(f"• <b>Preço mínimo:</b> {money(suggestion.min_price)}")
        if suggestion.tip:
            ai_lines.append(f"• <i>{escape(suggestion.tip)}</i>")
        body = (
            "<b>CONFIRMAR NOVO ALERTA</b>\n\n"
            f"📦 <b>Produto:</b> {escape(label)}\n"
            f"🎯 <b>Teto:</b> {money(ceiling_cents)}\n\n"
            "🤖 <b>Filtros sugeridos pela IA</b>\n"
            "<blockquote>" + "\n".join(ai_lines) + "</blockquote>\n\n"
            "Como deseja criar este alerta?"
        )
        bot.message(
            body,
            message_id=mid,
            reply_markup=keyboard(
                (("✅ Usar filtros sugeridos", "confirm_ai"),),
                (("📝 Texto puro (sem bloqueios)", "confirm_raw"),),
                (("❌ Cancelar", "home"),),
            ),
            **HTML_OPTS,
        )
        return

    bot.message(
        (
            "<b>CONFIRMAR NOVO ALERTA</b>\n\n"
            f"📦 <b>Produto:</b> {escape(label)}\n"
            f"🎯 <b>Teto:</b> {money(ceiling_cents)}"
        ),
        message_id=mid,
        reply_markup=keyboard(
            (("✅ Confirmar", "confirm"), ("📝 Texto puro", "confirm_raw")),
            (("❌ Cancelar", "home"),),
        ),
        **HTML_OPTS,
    )


def _create_pending_alert(db: sqlite3.Connection, bot: Bot, message_id: int, mode: str) -> None:
    label = get(db, "label")
    ceiling_cents = int(get(db, "ceiling"))
    product = "custom:" + normalize(label)
    filters_dict: dict = {}
    status = "Alerta criado."

    if mode == "raw":
        filters_dict = dict(PURE_TEXT_FILTERS)
        status = "Alerta criado em modo texto puro."
    elif mode == "ai":
        raw_ai = get(db, "ai_suggestion")
        if raw_ai:
            try:
                ai = json.loads(raw_ai)
                filters_dict = dict(DEFAULT_FILTERS)
                filters_dict["require_url"] = False
                filters_dict["exclude"] = list(ai.get("exclude") or [])
                filters_dict["min_price"] = int(ai.get("min_price") or 0)
                status = "Alerta criado com filtros da IA."
            except (ValueError, TypeError):
                filters_dict = {}

    db.execute(
        "INSERT OR IGNORE INTO promo_alerts (product, label, ceiling, filters) VALUES (?, ?, ?, ?)",
        (product, label, ceiling_cents, json.dumps(filters_dict, ensure_ascii=False)),
    )
    put(db, "step", "")
    put(db, "ai_suggestion", "")
    alert = next(a for a in alerts(db) if a.product == product)
    details(bot, alert, message_id, status)


def _suggest_filters_for_existing(
    db: sqlite3.Connection, bot: Bot, alert: Alert, message_id: int
) -> None:
    from . import promo_ai

    bot.message(
        (
            "<b>FILTROS DO ALERTA</b>\n\n"
            f"📦 <b>Produto:</b> {escape(alert.label)}\n\n"
            "<blockquote>🤖 Analisando o produto com IA…</blockquote>"
        ),
        message_id=message_id,
        **HTML_OPTS,
    )
    db.commit()
    suggestion = promo_ai.suggest_filters(alert.label, alert.ceiling)
    if suggestion is None:
        filter_menu(
            bot,
            alert,
            message_id,
            "Não consegui gerar sugestão agora. Configure manualmente abaixo.",
        )
        return
    put(
        db,
        f"ai_existing:{alert.id}",
        json.dumps(
            {
                "exclude": suggestion.exclude,
                "min_price": suggestion.min_price,
                "tip": suggestion.tip,
            },
            ensure_ascii=False,
        ),
    )
    ai_lines: list[str] = []
    if suggestion.exclude:
        ai_lines.append(f"• <b>Bloquear:</b> {escape(', '.join(suggestion.exclude))}")
    if suggestion.min_price > 0:
        ai_lines.append(f"• <b>Preço mínimo:</b> {money(suggestion.min_price)}")
    if suggestion.tip:
        ai_lines.append(f"• <i>{escape(suggestion.tip)}</i>")
    bot.message(
        (
            "<b>SUGESTÃO DE FILTROS (IA)</b>\n\n"
            f"📦 <b>Produto:</b> {escape(alert.label)}\n\n"
            "<blockquote>" + "\n".join(ai_lines) + "</blockquote>\n\n"
            "Deseja aplicar esses filtros?"
        ),
        message_id=message_id,
        reply_markup=keyboard(
            (("✅ Aplicar filtros da IA", f"flt_ai_apply:{alert.id}"),),
            (("↩️ Voltar", f"filters:{alert.id}"),),
        ),
        **HTML_OPTS,
    )


def handle(
    db: sqlite3.Connection,
    bot: Bot,
    update: dict,
    *,
    state_path: Path | None = None,
    sources_file: Path | None = None,
    session: Path | None = None,
) -> None:
    callback = update.get("callback_query")
    msg = callback.get("message", {}) if callback else update.get("message", {})
    if msg.get("chat", {}).get("id") != bot.chat_id or msg["chat"].get("type") != "private":
        return
    if callback:
        actor = callback.get("from", {}).get("id")
        if actor != bot.chat_id:
            return
        try:
            bot.call("answerCallbackQuery", callback_query_id=callback["id"])
        except BotError as error:
            if "query is too old" not in error.description.casefold():
                raise
        handle_action(
            db,
            bot,
            callback.get("data", ""),
            msg["message_id"],
            state_path=state_path,
            sources_file=sources_file,
            session=session,
        )
        return

    text = msg.get("text", "").strip()
    if not text:
        return
    cmd = text.split()[0].split("@")[0].casefold()
    if cmd in {"/start", "/menu", "/cancelar"}:
        put(db, "step", "")
        _send_home(bot)
        return
    if cmd == "/alertas":
        put(db, "step", "")
        _send_alerts_list(db, bot, None)
        return
    if cmd == "/historico":
        put(db, "step", "")
        _send_history_picker(db, bot, None)
        return
    if cmd == "/canais":
        put(db, "step", "")
        _show_channels(
            db,
            bot,
            None,
            force_refresh=False,
            state_path=state_path,
            sources_file=sources_file,
            session=session,
        )
        return

    step = get(db, "step")
    if step == "product":
        if len(text) > 80 or "\n" in text or not normalize(text):
            bot.message(
                "⚠️ Digite o nome do produto em uma linha (até 80 caracteres).",
                reply_markup=BACK,
                **HTML_OPTS,
            )
            return
        matching = next((a for a in alerts(db) if normalize(a.label) == normalize(text)), None)
        if matching:
            put(db, "step", "")
            details(bot, matching, None, "Esse produto já tem um alerta.")
            return
        put(db, "label", text)
        put(db, "step", "new_cap")
        bot.message(
            (
                "<b>NOVO ALERTA</b>\n\n"
                f"📦 <b>Produto:</b> {escape(text)}\n\n"
                "🎯 Qual é o preço teto em reais?\n"
                "<i>Ex.: 1650 ou 1.650,00</i>"
            ),
            reply_markup=BACK,
            **HTML_OPTS,
        )
    elif step.startswith("cap:") or step == "new_cap":
        raw = re.sub(r"^R\$\s*", "", text, flags=re.IGNORECASE)
        amount = parse_amount(raw) if re.fullmatch(r"\d[\d.,]{0,13}", raw) else None
        back_markup = (
            BACK
            if step == "new_cap"
            else keyboard((("↩️ Voltar", f"view:{step.split(':', 1)[1]}"),))
        )
        if amount is None or not 0 < amount <= 1000000:
            bot.message(
                "⚠️ Digite um preço válido em reais.\n<i>Ex.: 4500 ou 4.500,00</i>",
                reply_markup=back_markup,
                **HTML_OPTS,
            )
            return
        if step == "new_cap":
            _prompt_new_alert_confirmation(db, bot, get(db, "label"), cents(amount))
            return
        alert_id = int(step.split(":", 1)[1])
        alert = next((a for a in alerts(db) if a.id == alert_id), None)
        put(db, "step", "")
        if alert is None:
            bot.message("⚠️ Alerta não encontrado.", reply_markup=HOME, **HTML_OPTS)
            return
        db.execute("UPDATE promo_alerts SET ceiling = ? WHERE id = ?", (cents(amount), alert_id))
        updated = next(a for a in alerts(db) if a.id == alert_id)
        details(bot, updated, None, "Teto atualizado.")
    elif match := re.fullmatch(r"flt_min:(\d+)", step):
        alert_id = int(match.group(1))
        alert = next((a for a in alerts(db) if a.id == alert_id), None)
        if alert is None:
            put(db, "step", "")
            bot.message("⚠️ Alerta não encontrado.", reply_markup=HOME, **HTML_OPTS)
            return
        if normalize(text) in {"0", "limpar", "desativar", "nenhum", "-"}:
            new_min = 0
        else:
            raw = re.sub(r"^R\$\s*", "", text, flags=re.IGNORECASE)
            amount = parse_amount(raw) if re.fullmatch(r"\d[\d.,]{0,13}", raw) else None
            if amount is None or not 0 <= amount <= 1000000:
                bot.message(
                    "⚠️ Digite um valor em reais (ou '0' / 'limpar' para desativar).",
                    reply_markup=keyboard((("↩️ Voltar", f"filters:{alert.id}"),)),
                    **HTML_OPTS,
                )
                return
            new_min = cents(amount)
        put(db, "step", "")
        cfg = alert.filter_config()
        cfg["min_price"] = new_min
        save_filters(db, alert.id, cfg)
        updated = next(a for a in alerts(db) if a.id == alert.id)
        status = "Preço mínimo desativado." if new_min == 0 else "Preço mínimo atualizado."
        filter_menu(bot, updated, None, status)
    elif match := re.fullmatch(r"flt_words:(\d+):(include|exclude)", step):
        alert_id = int(match.group(1))
        field = match.group(2)
        alert = next((a for a in alerts(db) if a.id == alert_id), None)
        put(db, "step", "")
        if alert is None:
            bot.message("⚠️ Alerta não encontrado.", reply_markup=HOME, **HTML_OPTS)
            return
        cfg = alert.filter_config()
        if normalize(text) in {"limpar", "nenhuma", "nenhum", "-"}:
            words: list[str] = []
        else:
            words = [w.strip() for w in re.split(r"[,;\n]+", text) if normalize(w)]
            words = list(dict.fromkeys(w[:40] for w in words))[:15]
        cfg[field] = words
        save_filters(db, alert.id, cfg)
        updated = next(a for a in alerts(db) if a.id == alert.id)
        label = "Palavras obrigatórias" if field == "include" else "Palavras bloqueadas"
        filter_menu(bot, updated, None, f"{label} atualizadas.")
    else:
        _send_home(bot)


def handle_action(
    db: sqlite3.Connection,
    bot: Bot,
    action: str,
    message_id: int,
    *,
    state_path: Path | None = None,
    sources_file: Path | None = None,
    session: Path | None = None,
) -> None:
    if action in {"home", "home_new"}:
        put(db, "step", "")
        _send_home(bot, message_id=None if action == "home_new" else message_id)
    elif action == "new":
        put(db, "step", "product")
        bot.message(
            (
                "<b>NOVO ALERTA</b>\n\n"
                "📦 Qual produto você quer acompanhar?\n\n"
                "<i>Ex.: Ryzen 5800X3D, Air fryer, Monitor 27 QHD</i>"
            ),
            message_id=message_id,
            reply_markup=BACK,
            **HTML_OPTS,
        )
    elif action == "list":
        put(db, "step", "")
        _send_alerts_list(db, bot, message_id)
    elif action in {"hist", "hist_new"}:
        put(db, "step", "")
        _send_history_picker(db, bot, None if action == "hist_new" else message_id)
    elif action in {"channels", "channels_refresh"}:
        put(db, "step", "")
        _show_channels(
            db,
            bot,
            message_id if action == "channels_refresh" else None,
            force_refresh=(action == "channels_refresh"),
            state_path=state_path,
            sources_file=sources_file,
            session=session,
        )
    elif match := re.fullmatch(r"hist_pick:(all|\d+)", action):
        put(db, "step", "")
        target = match.group(1)
        if target == "all":
            header = "<b>BUSCAR HISTÓRICO</b>\n\n📋 <b>Escopo:</b> Todos os alertas ativos"
            back_action = "hist"
        else:
            alert = next((a for a in alerts(db) if a.id == int(target)), None)
            if alert is None:
                return
            header = (
                "<b>BUSCAR HISTÓRICO</b>\n\n"
                f"📦 <b>Produto:</b> {escape(alert.label)}\n"
                f"🎯 <b>Teto atual:</b> {money(alert.ceiling)}"
            )
            back_action = f"view:{alert.id}"
        bot.message(
            f"{header}\n\nEscolha o período da busca:",
            message_id=message_id,
            reply_markup=keyboard(
                (
                    ("🕒 Últimas 24h", f"hist_run:{target}:24"),
                    ("📅 Últimos 7 dias", f"hist_run:{target}:168"),
                ),
                (("↩️ Voltar", back_action),),
            ),
            **HTML_OPTS,
        )
    elif match := re.fullmatch(r"hist_run:(all|\d+):(24|168)", action):
        put(db, "step", "")
        _run_history_lookup(
            db,
            bot,
            match.group(1),
            int(match.group(2)),
            state_path=state_path,
            sources_file=sources_file,
            session=session,
        )
    elif match := re.fullmatch(r"hist_above:(\d+):(\d+)", action):
        put(db, "step", "")
        _show_above_page(db, bot, match.group(1), int(match.group(2)))
    elif action in {"confirm", "confirm_ai", "confirm_raw"} and get(db, "step") == "confirm":
        mode = {"confirm": "default", "confirm_ai": "ai", "confirm_raw": "raw"}[action]
        _create_pending_alert(db, bot, message_id, mode)
    elif match := re.fullmatch(r"filters:(\d+)", action):
        put(db, "step", "")
        alert = next((a for a in alerts(db) if a.id == int(match.group(1))), None)
        if alert is not None:
            filter_menu(bot, alert, message_id)
    elif match := re.fullmatch(r"flt_ai:(\d+)", action):
        put(db, "step", "")
        alert = next((a for a in alerts(db) if a.id == int(match.group(1))), None)
        if alert is not None:
            _suggest_filters_for_existing(db, bot, alert, message_id)
    elif match := re.fullmatch(r"flt_ai_apply:(\d+)", action):
        put(db, "step", "")
        alert = next((a for a in alerts(db) if a.id == int(match.group(1))), None)
        raw_ai = get(db, f"ai_existing:{match.group(1)}")
        if alert is not None and raw_ai:
            ai = json.loads(raw_ai)
            cfg = alert.filter_config()
            cfg["exclude"] = list(dict.fromkeys(cfg["exclude"] + list(ai.get("exclude") or [])))[
                :15
            ]
            if ai.get("min_price"):
                cfg["min_price"] = int(ai["min_price"])
            save_filters(db, alert.id, cfg)
            updated = next(a for a in alerts(db) if a.id == alert.id)
            filter_menu(bot, updated, message_id, "Filtros da IA aplicados.")
    elif match := re.fullmatch(r"flt_reset:(\d+)", action):
        put(db, "step", "")
        alert = next((a for a in alerts(db) if a.id == int(match.group(1))), None)
        if alert is not None:
            save_filters(db, alert.id, {})
            updated = next(a for a in alerts(db) if a.id == alert.id)
            filter_menu(bot, updated, message_id, "Filtros restaurados para o padrão.")
    elif match := re.fullmatch(r"flt:(\d+):([a-z_]+):(smart|broad|[01])", action):
        alert_id, field, raw_val = int(match.group(1)), match.group(2), match.group(3)
        alert = next((a for a in alerts(db) if a.id == alert_id), None)
        if alert is None or field not in DEFAULT_FILTERS:
            return
        put(db, "step", "")
        cfg = alert.filter_config()
        cfg[field] = raw_val if field == "mode" else bool(int(raw_val))
        save_filters(db, alert.id, cfg)
        updated = next(a for a in alerts(db) if a.id == alert.id)
        filter_menu(bot, updated, message_id, "Filtro atualizado.")
    elif match := re.fullmatch(r"flt_min:(\d+)", action):
        alert = next((a for a in alerts(db) if a.id == int(match.group(1))), None)
        if alert is None:
            return
        put(db, "step", f"flt_min:{alert.id}")
        cur_min = int(alert.filter_config().get("min_price") or 0)
        cur_txt = money(cur_min) if cur_min > 0 else "desativado"
        bot.message(
            (
                "<b>PREÇO MÍNIMO</b>\n\n"
                f"📦 <b>Produto:</b> {escape(alert.label)}\n"
                f"💰 <b>Atual:</b> {cur_txt}\n\n"
                "Digite o valor mínimo em reais (bloqueia acessórios baratos):\n"
                "<i>Envie '0' ou 'limpar' para desativar.</i>"
            ),
            message_id=message_id,
            reply_markup=keyboard((("↩️ Voltar", f"filters:{alert.id}"),)),
            **HTML_OPTS,
        )
    elif match := re.fullmatch(r"flt_words:(\d+):(include|exclude)", action):
        alert_id, field = int(match.group(1)), match.group(2)
        alert = next((a for a in alerts(db) if a.id == alert_id), None)
        if alert is None:
            return
        put(db, "step", f"flt_words:{alert.id}:{field}")
        title = "PALAVRAS OBRIGATÓRIAS" if field == "include" else "PALAVRAS BLOQUEADAS"
        cur = alert.filter_config()[field]
        cur_txt = escape(", ".join(cur)) if cur else "nenhuma"
        bot.message(
            (
                f"<b>{title}</b>\n\n"
                f"📦 <b>Produto:</b> {escape(alert.label)}\n"
                f"📋 <b>Atuais:</b> {cur_txt}\n\n"
                "Digite as palavras separadas por vírgula:\n"
                "<i>Envie 'limpar' para remover todas.</i>"
            ),
            message_id=message_id,
            reply_markup=keyboard((("↩️ Voltar", f"filters:{alert.id}"),)),
            **HTML_OPTS,
        )
    elif match := re.fullmatch(r"(view|cap|toggle):(\d+)(?::([01]))?", action):
        command, alert_id, target = match.groups()
        alert = next((a for a in alerts(db) if a.id == int(alert_id)), None)
        if alert is None:
            return
        if command == "cap":
            put(db, "step", f"cap:{alert.id}")
            bot.message(
                (
                    "<b>MUDAR TETO</b>\n\n"
                    f"📦 <b>Produto:</b> {escape(alert.label)}\n"
                    f"🎯 <b>Teto atual:</b> {money(alert.ceiling)}\n\n"
                    "Digite o novo preço teto em reais:\n"
                    "<i>Ex.: 4500 ou 4.500,00</i>"
                ),
                message_id=message_id,
                reply_markup=keyboard((("↩️ Voltar", f"view:{alert.id}"),)),
                **HTML_OPTS,
            )
            return
        put(db, "step", "")
        status = ""
        if command == "toggle" and target is not None:
            db.execute("UPDATE promo_alerts SET paused = ? WHERE id = ?", (int(target), alert.id))
            alert = next(a for a in alerts(db) if a.id == alert.id)
            status = "Alerta pausado." if alert.paused else "Alerta reativado."
        details(bot, alert, message_id, status)


def run(
    state: Path,
    bot: Bot,
    *,
    sources_file: Path | None = None,
    session: Path | None = None,
) -> None:
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    registered = False
    with closing(sqlite3.connect(state, timeout=30)) as db:
        init(db)
        while True:
            try:
                if not registered:
                    bot.call(
                        "setMyCommands",
                        commands=[
                            {"command": "menu", "description": "Abrir menu principal"},
                            {"command": "alertas", "description": "Meus alertas"},
                            {"command": "historico", "description": "Buscar histórico"},
                            {"command": "canais", "description": "Canais monitorados"},
                        ],
                    )
                    registered = True
                offset = int(get(db, "offset") or 0)
                updates = bot.call(
                    "getUpdates",
                    offset=offset,
                    timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    with db:
                        try:
                            handle(
                                db,
                                bot,
                                update,
                                state_path=state,
                                sources_file=sources_file,
                                session=session,
                            )
                        except BotError as error:
                            if "message is not modified" not in error.description.casefold():
                                raise
                        put(db, "offset", str(update["update_id"] + 1))
            except BotError as error:
                print(f"promo_menu BotError: {error.description}", file=sys.stderr, flush=True)
                time.sleep(3)
            except sqlite3.OperationalError as error:
                print(f"promo_menu SQLite error: {error}", file=sys.stderr, flush=True)
                time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--sources", type=Path, default=None)
    parser.add_argument("--session", type=Path, default=None)
    args = parser.parse_args()
    token = os.getenv("TGSEARCH_PROMOBOT_TOKEN", "")
    chat_id = os.getenv("TGSEARCH_BOT_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"[1-9]\d*", chat_id):
        parser.error("Configure TGSEARCH_PROMOBOT_TOKEN e TGSEARCH_BOT_CHAT_ID.")
    run(args.state, Bot(token, int(chat_id)), sources_file=args.sources, session=args.session)


if __name__ == "__main__":
    main()
