"""Recognise graphics-card models in offer text and tell card offers from laptops and PCs.

Everything works on normalised text (casefolded, accents removed). The rules are
deliberately strict, because a near miss pollutes the price statistics:

* ``RX 7600`` and ``RX 7600 XT`` are different products; ``Ryzen 5 7600`` is a CPU.
* ``RTX 5050`` inside a notebook or a complete PC is not a graphics-card offer.
* A post that names another GPU model as well is ambiguous (a list post, or a
  comparison) and is kept out of the statistics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .textutil import normalize


@dataclass(frozen=True)
class Model:
    key: str
    label: str
    pattern: re.Pattern[str]


_SEP = r"[\s\-_]*"

MODELS: tuple[Model, ...] = (
    Model(
        "rx7600xt",
        "Radeon RX 7600 XT",
        re.compile(rf"(?<![a-z0-9])(?:rx|radeon(?:{_SEP}rx)?){_SEP}7600{_SEP}xt(?![a-z0-9])"),
    ),
    Model(
        "rx7600",
        "Radeon RX 7600",
        re.compile(rf"(?<![a-z0-9])(?:rx|radeon(?:{_SEP}rx)?){_SEP}7600(?!{_SEP}xt)(?![a-z0-9])"),
    ),
    Model(
        "rtx5050",
        "GeForce RTX 5050",
        re.compile(rf"(?<![a-z0-9])(?:rtx|geforce(?:{_SEP}rtx)?){_SEP}5050(?![a-z0-9])"),
    ),
)
MODEL_BY_KEY = {m.key: m for m in MODELS}

# Any other discrete GPU model, used to spot multi-product and comparison posts.
_OTHER_GPU = re.compile(
    rf"(?<![a-z0-9])(?:(?:rtx|gtx){_SEP}\d{{4}}(?:{_SEP}(?:ti|super))*"
    rf"|rx{_SEP}\d{{3,4}}(?:{_SEP}(?:xtx|xt|gre))?"
    rf"|arc{_SEP}[ab]\d{{3}})(?![a-z0-9])"
)
_GPU_TOKEN = re.compile(r"[^a-z0-9]")  # "RX 7600-XT" -> "rx7600xt"

_LAPTOP = re.compile(
    r"\b(?:notebook|laptop|ultrabook|chromebook|macbook"
    r"|nitro\s*v|predator\s*helios|ideapad|legion|loq|victus|omen\s*\d|tuf\s*gaming\s*[af]\d"
    r"|rog\s*strix\s*g\d|katana|cyborg|thin\s*gf|aspire|vivobook|dell\s*g1\d|alienware"
    r"|\d{2}(?:[.,]\d)?\s*(?:\"|''|pol\b|polegadas))"
)
_PREBUILT = re.compile(
    r"\b(?:pc\s*gamer|computador|desktop|cpu\s*gamer|pc\s*completo|kit\s*pc|workstation|mini\s*pc)\b"
)
# A CPU or motherboard in the same post means a combo, not a card on its own.
_BUNDLE = re.compile(
    r"\b(?:combo|kit\s*upgrade|placa\s*mae|processador"
    r"|ryzen\s*\d\s*\d{4}|core\s*(?:i\d|ultra)\s*\d*)"
)
_CARD = re.compile(
    r"\b(?:placa\s*de\s*video|placa\s*grafica|placa\s*de\s*v[ií]deo|graphics\s*card|gpu|vga"
    r"|gddr\d|\d{1,2}\s*gb\s*gddr"
    r"|asus\s*(?:dual|prime|tuf)|gigabyte|sapphire|powercolor|xfx|asrock|msi\s*(?:ventus|gaming|shadow)"
    r"|galax|zotac|pny|palit|gainward|inno3d|colorful|biostar|afox|maxsun|mancer|pcyes"
    r"|pulse|hellhound|fighter|challenger|windforce|eagle|ventus|twin\s*edge|white\s*edition"
    r"|twin\s*x\d|triple\s*fan|dual\s*fan|gaming\s*oc|\d\s*gb|nvidia|amd\s*radeon)\b"
)


@dataclass(frozen=True)
class Classification:
    model: str | None  # key of the target model, or None
    category: str  # card, laptop, prebuilt, bundle, unknown
    other_models: tuple[str, ...]  # other GPU models named in the post
    reason: str  # why the post is not usable for card statistics, or ""

    @property
    def usable(self) -> bool:
        return not self.reason


def models_in(text: str) -> list[str]:
    norm = normalize(text)
    return [m.key for m in MODELS if m.pattern.search(norm)]


def mentions_target(text: str, keys: tuple[str, ...] = ("rx7600", "rtx5050")) -> bool:
    """Cheap pre-filter: does the text name one of the target models?"""
    return any(k in keys for k in models_in(text))


def _other_gpus(norm: str, targets: list[str]) -> tuple[str, ...]:
    # Tokens look like "rx7600", "rx7600xt", "rtx4060ti", the same shape as model keys.
    others: list[str] = []
    for m in _OTHER_GPU.finditer(norm):
        token = _GPU_TOKEN.sub("", m.group(0))
        if token not in targets and token not in others:
            others.append(token)
    return tuple(others)


def classify(text: str, targets: tuple[str, ...] = ("rx7600", "rtx5050")) -> Classification:
    norm = normalize(text)
    found = models_in(text)
    target = [k for k in found if k in targets]
    others = _other_gpus(norm, target)
    others += tuple(k for k in found if k not in targets and k not in others)

    if _LAPTOP.search(norm):
        category = "laptop"
    elif _PREBUILT.search(norm):
        category = "prebuilt"
    elif _BUNDLE.search(norm):
        category = "bundle"
    elif _CARD.search(norm):
        category = "card"
    else:
        category = "unknown"

    if not target:
        reason = "no_target_model"
    elif len(target) > 1:
        reason = "several_target_models"
    elif others:
        reason = "other_gpu_named"
    elif category != "card":
        reason = f"category_{category}"
    else:
        reason = ""
    return Classification(target[0] if len(target) == 1 else None, category, others, reason)


BRANDS = (
    ("asrock", r"asrock"),
    ("asus", r"asus"),
    ("biostar", r"biostar"),
    ("gigabyte", r"gigabyte|aorus"),
    ("msi", r"\bmsi\b"),
    ("powercolor", r"power\s*color"),
    ("sapphire", r"sapphire"),
    ("xfx", r"\bxfx\b"),
    ("galax", r"galax"),
    ("zotac", r"zotac"),
    ("pny", r"\bpny\b"),
    ("palit", r"palit"),
    ("gainward", r"gainward"),
    ("inno3d", r"inno\s*3d"),
    ("colorful", r"colorful"),
    ("afox", r"afox"),
    ("maxsun", r"maxsun"),
    ("pcyes", r"pcyes"),
)
_BRANDS = tuple((name, re.compile(rx)) for name, rx in BRANDS)


def brand_of(text: str) -> str:
    """The card maker named in the post, or "" when none or several are named."""
    norm = normalize(text)
    found = [name for name, rx in _BRANDS if rx.search(norm)]
    return found[0] if len(found) == 1 else ""
