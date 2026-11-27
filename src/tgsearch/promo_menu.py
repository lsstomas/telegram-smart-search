"""Private PromoBot configuration, served independently of the offer scan."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import dataclass
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
    ceiling INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS promo_menu (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Alert:
    id: int
    product: str
    label: str
    ceiling: int
    paused: int


def init(db: sqlite3.Connection, *, seed_defaults: bool | None = None) -> None:
    configure_db(db)
    db.executescript(SCHEMA)
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
    query = "SELECT * FROM promo_alerts" + where + " ORDER BY id"
    return [Alert(*row) for row in db.execute(query)]


def get(db: sqlite3.Connection, key: str) -> str:
    row = db.execute("SELECT value FROM promo_menu WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def put(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO promo_menu VALUES (?, ?)", (key, value))


def keyboard(*rows):
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": action} for label, action in row] for row in rows
        ]
    }


HOME = keyboard((("➕ Novo alerta", "new"),), (("📋 Meus alertas", "list"),))
BACK = keyboard((("Voltar", "home"),))


def details(bot: Bot, alert: Alert, message_id: int | None, status: str = "") -> None:
    state = "Pausado" if alert.paused else "Ativo"
    icon = "⏸" if alert.paused else "✅"
    text = f"📦 {alert.label}\n💰 Teto: {money(alert.ceiling)}\n{icon} Status: {state}"
    if status:
        text = f"{status}\n\n{text}"
    action = "Reativar" if alert.paused else "Pausar"
    bot.message(
        text,
        message_id=message_id,
        reply_markup=keyboard(
            (("Mudar teto", f"cap:{alert.id}"), (action, f"toggle:{alert.id}:{1 - alert.paused}")),
            (("Voltar", "list"),),
        ),
    )


def handle(db: sqlite3.Connection, bot: Bot, update: dict) -> None:
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
        handle_action(db, bot, callback.get("data", ""), msg["message_id"])
        return

    text = msg.get("text", "").strip()
    if not text:
        return
    cmd = text.split()[0].split("@")[0].casefold()
    if cmd in {"/start", "/menu", "/cancelar"}:
        put(db, "step", "")
        bot.message("⚡ PromoBot\n\nO que você quer fazer?", reply_markup=HOME)
        return
    step = get(db, "step")
    if step == "product":
        if len(text) > 80 or "\n" in text or not normalize(text):
            bot.message(
                "Digite o nome do produto em uma linha, com até 80 caracteres.",
                reply_markup=BACK,
            )
            return
        matching = next((a for a in alerts(db) if normalize(a.label) == normalize(text)), None)
        if matching:
            put(db, "step", "")
            details(bot, matching, None, "Esse produto já tem um alerta.")
            return
        put(db, "label", text)
        put(db, "step", "new_cap")
        bot.message(f"Qual é o preço teto para {text}?\nEx.: 4500 ou 4.500,00", reply_markup=BACK)
    elif step.startswith("cap:") or step == "new_cap":
        raw = re.sub(r"^R\$\s*", "", text, flags=re.IGNORECASE)
        amount = parse_amount(raw) if re.fullmatch(r"\d[\d.,]{0,13}", raw) else None
        if amount is None or not 0 < amount <= 1000000:
            bot.message("Digite um preço em reais.\nEx.: 4500 ou 4.500,00", reply_markup=BACK)
            return
        if step == "new_cap":
            put(db, "ceiling", str(cents(amount)))
            put(db, "step", "confirm")
            bot.message(
                (
                    f"Confirma o novo alerta?\n\n📦 {get(db, 'label')}"
                    f"\n💰 Teto: {money(cents(amount))}"
                ),
                reply_markup=keyboard((("Confirmar", "confirm"), ("Cancelar", "home"))),
            )
            return
        alert_id = int(step.split(":", 1)[1])
        alert = next((a for a in alerts(db) if a.id == alert_id), None)
        put(db, "step", "")
        if alert is None:
            bot.message("Alerta não encontrado.", reply_markup=HOME)
            return
        db.execute("UPDATE promo_alerts SET ceiling = ? WHERE id = ?", (cents(amount), alert_id))
        updated = next(a for a in alerts(db) if a.id == alert_id)
        details(bot, updated, None, "Teto atualizado.")
    else:
        bot.message("⚡ PromoBot\n\nO que você quer fazer?", reply_markup=HOME)


def handle_action(db: sqlite3.Connection, bot: Bot, action: str, message_id: int) -> None:
    if action == "home":
        put(db, "step", "")
        bot.message(
            "⚡ PromoBot\n\nO que você quer fazer?",
            message_id=message_id,
            reply_markup=HOME,
        )
    elif action == "new":
        put(db, "step", "product")
        bot.message("Qual produto você quer acompanhar?", message_id=message_id, reply_markup=BACK)
    elif action == "list":
        put(db, "step", "")
        items = alerts(db)
        if not items:
            bot.message(
                "📋 Meus alertas\n\nVocê ainda não tem alertas cadastrados.",
                message_id=message_id,
                reply_markup=keyboard((("➕ Novo alerta", "new"),), (("Voltar", "home"),)),
            )
            return
        rows = [
            ((f"{'⏸ ' if a.paused else '✅ '}{a.label} · {money(a.ceiling)}", f"view:{a.id}"),)
            for a in items
        ]
        rows.append((("Voltar", "home"),))
        bot.message(
            "📋 Meus alertas\n\nEscolha um produto para mudar o teto, pausar ou reativar:",
            message_id=message_id,
            reply_markup=keyboard(*rows),
        )
    elif action == "confirm" and get(db, "step") == "confirm":
        label = get(db, "label")
        product = "custom:" + normalize(label)
        db.execute(
            "INSERT OR IGNORE INTO promo_alerts (product, label, ceiling) VALUES (?, ?, ?)",
            (product, label, int(get(db, "ceiling"))),
        )
        put(db, "step", "")
        alert = next(a for a in alerts(db) if a.product == product)
        details(bot, alert, message_id, "Alerta criado.")
    elif match := re.fullmatch(r"(view|cap|toggle):(\d+)(?::([01]))?", action):
        command, alert_id, target = match.groups()
        alert = next((a for a in alerts(db) if a.id == int(alert_id)), None)
        if alert is None:
            return
        if command == "cap":
            put(db, "step", f"cap:{alert.id}")
            bot.message(
                f"Qual é o novo preço teto para {alert.label}?\nEx.: 4500 ou 4.500,00",
                message_id=message_id,
                reply_markup=BACK,
            )
            return
        put(db, "step", "")
        status = ""
        if command == "toggle" and target is not None:
            db.execute("UPDATE promo_alerts SET paused = ? WHERE id = ?", (int(target), alert.id))
            alert = next(a for a in alerts(db) if a.id == alert.id)
            status = "Alerta pausado." if alert.paused else "Alerta reativado."
        details(bot, alert, message_id, status)


def run(state: Path, bot: Bot) -> None:
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
                            {"command": "menu", "description": "Abrir menu"},
                            {"command": "start", "description": "Iniciar PromoBot"},
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
                            handle(db, bot, update)
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
    args = parser.parse_args()
    token = os.getenv("TGSEARCH_PROMOBOT_TOKEN", "")
    chat_id = os.getenv("TGSEARCH_BOT_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"[1-9]\d*", chat_id):
        parser.error("Configure TGSEARCH_PROMOBOT_TOKEN e TGSEARCH_BOT_CHAT_ID.")
    run(args.state, Bot(token, int(chat_id)))


if __name__ == "__main__":
    main()
