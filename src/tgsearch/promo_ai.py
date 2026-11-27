"""Automated AI filter suggestions for new PromoBot alerts using the local agy CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .report_html import brl
from .textutil import normalize

DEFAULT_MODEL = "gemini-4-neon-medium"
DEFAULT_EFFORT = "medium"
DEFAULT_TIMEOUT = 40


@dataclass(frozen=True)
class FilterSuggestion:
    exclude: list[str]
    min_price: int  # centavos (0 = disabled)
    tip: str


def _resolve_agy() -> str | None:
    configured = os.getenv("TGSEARCH_AI_CMD", "").strip()
    if configured:
        return configured if Path(configured).is_file() else shutil.which(configured)
    local_bin = Path.home() / ".local/bin/agy"
    if local_bin.is_file():
        return str(local_bin)
    return shutil.which("agy")


def _extract_json(raw: str) -> dict | None:
    text = raw.strip()
    if not text:
        return None
    # Strip optional markdown fences
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    match = re.search(r"\{[^{}]*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _build_prompt(product: str, ceiling_cents: int) -> str:
    ceiling_str = brl(Decimal(ceiling_cents) / 100)
    return (
        "Você configura filtros anti-ruído para um monitor de ofertas de canais brasileiros "
        "do Telegram.\n"
        f'Produto alvo: "{product}"\n'
        f"Preço teto do usuário: {ceiling_str}\n\n"
        "Responda APENAS com um objeto JSON válido (sem markdown, sem texto extra) no formato:\n"
        '{"exclude": ["termo1", "termo2"], "min_price": 500, '
        '"tip": "resumo curto em português"}\n\n'
        "Regras:\n"
        "- exclude: de 4 a 10 palavras/expressões curtas em minúsculas e sem acento para bloquear "
        "falsos positivos comuns desse produto em canais de promoção (ex: acessórios, capas, "
        "coolers, kits/PCs montados, modelos/variantes erradas, usados/seminovos). Nunca inclua "
        "em títulos reais (ex: nunca use 'cooler' sozinho pois CPUs dizem "
        "'sem cooler' — prefira 'water cooler'/'air cooler'; para acessórios "
        "baratos confie no min_price).\n"
        "- min_price: inteiro em reais (sem centavos) abaixo do qual uma oferta desse produto "
        "quase certamente é acessório ou erro (use cerca de 25% a 40% do teto, ou 0 se o teto "
        "for menor que R$ 150).\n"
        "- tip: 1 frase curta em português (até 130 caracteres) explicando o que os bloqueios "
        "evitam (e, se um nome de busca mais curto pegar mais posts, dê a dica brevemente)."
    )


def suggest_filters(product: str, ceiling_cents: int) -> FilterSuggestion | None:
    """Invoke local agy CLI to suggest exclude terms and minimum plausible price."""
    if os.getenv("TGSEARCH_DISABLE_AI", "").strip() == "1":
        return None
    agy = _resolve_agy()
    if not agy:
        return None
    model = os.getenv("TGSEARCH_AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    effort = os.getenv("TGSEARCH_AI_EFFORT", DEFAULT_EFFORT).strip() or DEFAULT_EFFORT
    prompt = _build_prompt(product, ceiling_cents)

    cmd = [
        agy,
        "--model",
        model,
        "--effort",
        effort,
        "--disable-slash-commands",
        "--print-timeout",
        "35s",
        f"--print={prompt}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd="/tmp",
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    data = _extract_json(proc.stdout)
    if not data:
        return None

    product_tokens = set(normalize(product).split())
    raw_exclude = data.get("exclude")
    exclude: list[str] = []
    if isinstance(raw_exclude, list):
        for item in raw_exclude:
            if not isinstance(item, str):
                continue
            cleaned = normalize(item).strip()[:40]
            if not cleaned or cleaned in product_tokens:
                continue
            if cleaned not in exclude:
                exclude.append(cleaned)
            if len(exclude) >= 12:
                break

    min_price_cents = 0
    raw_min = data.get("min_price")
    if isinstance(raw_min, (int, float)) and raw_min > 0:
        candidate = int(round(raw_min)) * 100
        if 0 < candidate <= int(ceiling_cents * 0.75):
            min_price_cents = candidate

    raw_tip = data.get("tip")
    tip = raw_tip.strip()[:160] if isinstance(raw_tip, str) else ""

    if not exclude and not min_price_cents:
        return None
    return FilterSuggestion(exclude=exclude, min_price=min_price_cents, tip=tip)
