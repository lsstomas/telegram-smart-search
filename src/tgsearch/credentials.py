"""Telegram API credentials: environment first, then a private file outside the project.

``TELEGRAM_API_ID`` and ``TELEGRAM_API_HASH`` always win. When they are absent,
the credentials saved by ``tgsearch auth`` are read from
``$XDG_CONFIG_HOME/tgsearch/credentials.json`` (default ``~/.config``). That
file is created with ``0600`` permissions inside a ``0700`` directory, and it is
refused if anyone other than the owner can read it.

The values are only ever entered in the user's own terminal (``getpass`` for the
hash), never passed on the command line, where they would land in shell history.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_API_ID = "TELEGRAM_API_ID"
ENV_API_HASH = "TELEGRAM_API_HASH"
ENV_CREDENTIALS = "TGSEARCH_CREDENTIALS"


class LiveConfigError(RuntimeError):
    """Raised when live mode cannot start (missing credentials, dependency or login)."""


@dataclass(frozen=True)
class Credentials:
    api_id: int
    api_hash: str

    def __repr__(self) -> str:  # never leak the values into logs or tracebacks
        return "Credentials(api_id=<hidden>, api_hash=<hidden>)"


def _validate(api_id: str, api_hash: str, source: str) -> Credentials:
    api_id, api_hash = api_id.strip(), api_hash.strip()
    if not api_id.isdigit():
        raise LiveConfigError(f"API ID from {source} must be numeric")
    if len(api_hash) != 32 or any(c not in "0123456789abcdefABCDEF" for c in api_hash):
        raise LiveConfigError(f"API hash from {source} must be 32 hexadecimal characters")
    return Credentials(int(api_id), api_hash.lower())


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get(ENV_CREDENTIALS):
        return Path(env[ENV_CREDENTIALS]).expanduser()
    base = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "tgsearch" / "credentials.json"


def credentials_from_env(env: Mapping[str, str] | None = None) -> Credentials | None:
    env = os.environ if env is None else env
    api_id = env.get(ENV_API_ID, "").strip()
    api_hash = env.get(ENV_API_HASH, "").strip()
    if not api_id and not api_hash:
        return None
    if not api_id or not api_hash:
        raise LiveConfigError(f"set both {ENV_API_ID} and {ENV_API_HASH}, or neither")
    return _validate(api_id, api_hash, "the environment")


def load_saved(path: Path) -> Credentials | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise LiveConfigError(f"{path} is readable by other users; run: chmod 600 {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _validate(str(data["api_id"]), str(data["api_hash"]), str(path))
    except (KeyError, TypeError, ValueError) as exc:
        raise LiveConfigError(f"{path} is not a valid credentials file") from exc


def save(creds: Credentials, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"api_id": creds.api_id, "api_hash": creds.api_hash}, fh)
    os.replace(tmp, path)


def resolve(env: Mapping[str, str] | None = None) -> Credentials:
    """Credentials for a non-interactive run; never prompts."""
    creds = credentials_from_env(env) or load_saved(credentials_path(env))
    if creds is None:
        raise LiveConfigError(
            f"no Telegram API credentials: run `tgsearch auth` in a terminal, or set "
            f"{ENV_API_ID} and {ENV_API_HASH}"
        )
    return creds


def prompt(
    read_line: Callable[[str], str] = input,
    read_secret: Callable[[str], str] | None = None,
) -> Credentials:
    """Ask for the values in the terminal; the hash is read without echo."""
    if read_secret is None:
        from getpass import getpass

        read_secret = getpass
    api_id = read_line("Telegram API ID (from my.telegram.org): ")
    api_hash = read_secret("Telegram API hash (input hidden): ")
    return _validate(api_id, api_hash, "the prompt")
