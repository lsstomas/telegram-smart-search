"""Text normalisation and URL helpers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?)]}>*_~"

# Query parameters that only track the click and never change the product.
# Affiliate parameters (tag, ref, aff_id, ...) are kept on purpose: removing them
# could merge links that point to different listings.
TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "_ga", "si"}
)


def normalize(text: str) -> str:
    """Casefold and strip accents so 'Cartão' and 'cartao' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def extract_urls(text: str) -> list[str]:
    urls = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING_PUNCT)
        if url:
            urls.append(url)
    return urls


def canonical_url(url: str) -> str:
    """Canonical form for comparison: lowercase host, no www, no fragment or tracking params."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit(("https", host, path, urlencode(query), ""))


def is_telegram_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in {"t.me", "telegram.me", "telegram.dog"} or host.endswith(".t.me")


def collapse_ws(text: str) -> str:
    return " ".join(text.split())
