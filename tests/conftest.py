from datetime import datetime, timezone
from pathlib import Path

import pytest

from tgsearch.models import Message

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def make_message():
    counter = iter(range(1, 10_000))

    def _make(text, chat="chan", message_id=None, date=None, urls=(), link=""):
        return Message(
            chat=chat,
            message_id=message_id if message_id is not None else next(counter),
            date=date or datetime(2026, 10, 1, tzinfo=timezone.utc),
            text=text,
            urls=tuple(urls),
            link=link,
        )

    return _make


@pytest.fixture(autouse=True)
def _no_real_telegram_env(monkeypatch, tmp_path):
    """Tests must never see real credentials or touch a real session directory."""
    for var in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TGSEARCH_SESSION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
