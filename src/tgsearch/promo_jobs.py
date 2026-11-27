"""Lightweight SQLite job queue so promo_menu can reuse watch-live's Telethon session."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from . import promo_delivery, promo_menu

HEARTBEAT_KEY = "live_watcher_heartbeat"
HEARTBEAT_TTL_SECONDS = 45
JOB_TTL_SECONDS = 600
SCHEMA = """
CREATE TABLE IF NOT EXISTS promo_jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
"""


def init(db: sqlite3.Connection) -> None:
    promo_delivery.configure_db(db)
    promo_menu.init(db)
    db.executescript(SCHEMA)


def touch_heartbeat(db: sqlite3.Connection, now: int | None = None) -> None:
    ts = int(time.time()) if now is None else now
    with db:
        promo_menu.put(db, HEARTBEAT_KEY, str(ts))
        db.execute("DELETE FROM promo_jobs WHERE created_at < ?", (ts - JOB_TTL_SECONDS,))


def clear_heartbeat(db: sqlite3.Connection) -> None:
    with db:
        promo_menu.put(db, HEARTBEAT_KEY, "0")


def is_live_watcher_active(db: sqlite3.Connection, now: int | None = None) -> bool:
    ts = int(time.time()) if now is None else now
    raw = promo_menu.get(db, HEARTBEAT_KEY)
    try:
        beat = int(raw)
    except ValueError:
        return False
    return beat > 0 and (ts - beat) <= HEARTBEAT_TTL_SECONDS


def claim_next(db: sqlite3.Connection) -> tuple[int, str, dict] | None:
    peek = db.execute("SELECT 1 FROM promo_jobs WHERE status = 'pending' LIMIT 1").fetchone()
    if peek is None:
        return None
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT id, kind, payload FROM promo_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        job_id, kind, raw_payload = row
        db.execute("UPDATE promo_jobs SET status = 'running' WHERE id = ?", (job_id,))
    return job_id, kind, json.loads(raw_payload)


def complete(db: sqlite3.Connection, job_id: int, result: dict) -> None:
    with db:
        db.execute(
            "UPDATE promo_jobs SET status = 'done', result = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), job_id),
        )


def fail(db: sqlite3.Connection, job_id: int, error: str) -> None:
    with db:
        db.execute(
            "UPDATE promo_jobs SET status = 'error', result = ? WHERE id = ?",
            (error[:400], job_id),
        )


def submit_and_wait(
    state: Path,
    kind: str,
    payload: dict,
    *,
    timeout: float = 45.0,
    poll_interval: float = 0.15,
) -> dict | None:
    """Submit a job to watch-live and wait for its result.

    Returns None if watch-live is not running so the caller can fall back to opening Telethon.
    """
    with closing(sqlite3.connect(state, timeout=30)) as db:
        init(db)
        if not is_live_watcher_active(db):
            return None
        now = int(time.time())
        with db:
            cur = db.execute(
                "INSERT INTO promo_jobs (kind, payload, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (kind, json.dumps(payload, ensure_ascii=False), now),
            )
            job_id = cur.lastrowid

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = db.execute(
                "SELECT status, result FROM promo_jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Job de busca foi removido antes de concluir.")
            status, raw_result = row
            if status == "done":
                with db:
                    db.execute("DELETE FROM promo_jobs WHERE id = ?", (job_id,))
                return json.loads(raw_result)
            if status == "error":
                with db:
                    db.execute("DELETE FROM promo_jobs WHERE id = ?", (job_id,))
                raise RuntimeError(raw_result or "Falha ao executar busca no Telegram.")
            time.sleep(poll_interval)

        with db:
            db.execute("DELETE FROM promo_jobs WHERE id = ?", (job_id,))
        raise TimeoutError("Tempo esgotado aguardando o monitor ao vivo responder à busca.")
