"""GPU offer analysis: classify posts, split prices by payment method, find
duplicates, and compute per-model, per-method statistics with an audit trail.

Duplicates come in two strengths:

* **Same post** (merged; counted once in the statistics): the same message read
  twice, forwards of the same channel post (or the original and its forward), or
  the same normalised text, or the same canonical product URL, *with the same
  price signature* (every method and amount, plus coupon codes).
* **Probable repost** (flagged, never merged): same model, same store and the
  same price signature, posted within ``PROBABLE_WINDOW`` of the previous post in
  the group (in any chat).
  Channels use their own affiliate links and wording, so this is likely the same
  deal, but it is not proven; the report shows the statistics both ways.

Every post that is left out of the statistics keeps a machine-readable reason.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .dedupe import MIN_TEXT_KEY_LEN, text_key
from .extract import _offer_url, _title
from .gpu import Classification, brand_of
from .models import Message
from .payment import UPFRONT_METHODS, PricePoint, Terms, parse_terms
from .profiles import Profile, gpu_profile
from .textutil import canonical_url, extract_urls, is_telegram_url, normalize

PROBABLE_WINDOW = timedelta(hours=72)

STAT_METHODS = (
    "pix",
    "boleto",
    "card_1x",
    "upfront",
    "unspecified",
    "installments_interest_free",
    "installments_with_interest",
    "installments_interest_unstated",
    "lowest_upfront",
)

STORES: tuple[tuple[str, tuple[str, ...], re.Pattern[str]], ...] = tuple(
    (name, hosts, re.compile(rf"\b{word}\b"))
    for name, hosts, word in (
        ("amazon", ("amazon.com.br", "amzn.to", "amzn.com", "a.co"), "amazon"),
        ("kabum", ("kabum.com.br", "tidd.ly"), "kabum"),
        ("pichau", ("pichau.com.br",), "pichau"),
        ("terabyte", ("terabyteshop.com.br",), "terabyte"),
        (
            "mercadolivre",
            ("mercadolivre.com.br", "mercadolivre.com", "mercadolibre.com"),
            r"mercado\s*livre",
        ),
        ("aliexpress", ("aliexpress.com", "aliexpress.us", "s.click.aliexpress.com"), "aliexpress"),
        ("shopee", ("shopee.com.br", "s.shopee.com.br", "shp.ee"), "shopee"),
        (
            "magalu",
            ("magazineluiza.com.br", "magalu.com", "magazinevoce.com.br"),
            r"magalu|magazine\s*luiza",
        ),
        ("casasbahia", ("casasbahia.com.br",), r"casas\s*bahia"),
        ("girafa", ("girafa.com.br",), "girafa"),
        ("gkinfostore", ("gkinfostore.com.br",), "gkinfostore"),
    )
)


def store_of(urls: Sequence[str], text: str) -> str:
    for url in urls:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        for name, hosts, _ in STORES:
            if any(host == h or host.endswith("." + h) for h in hosts):
                return name
    norm = normalize(text)
    named = [name for name, _, word in STORES if word.search(norm)]
    return named[0] if len(named) == 1 else ""


@dataclass
class Post:
    message: Message
    cls: Classification
    terms: Terms
    store: str
    url: str
    brand: str = ""
    status: str = "included"  # included, excluded, duplicate
    reason: str = ""
    duplicate_of: Post | None = None
    probable_group: int | None = None
    method_prices: dict[str, PricePoint] = field(default_factory=dict)
    method_notes: dict[str, str] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return self.message.ref

    def signature(self) -> tuple:
        points = sorted((p.method, p.amount, p.installments or 0) for p in self.terms.points)
        return (tuple(points), self.terms.coupons)


def _all_urls(message: Message) -> list[str]:
    urls = list(message.urls) + [u for u in extract_urls(message.text) if u not in message.urls]
    return [u for u in urls if not is_telegram_url(u)]


def build_post(message: Message, profile: Profile) -> Post:
    urls = _all_urls(message)
    return Post(
        message=message,
        cls=profile.classify(message.text),
        terms=parse_terms(message.text),
        store=store_of(urls, message.text),
        url=_offer_url(message) or "",
        brand=brand_of(message.text) if profile.name == "gpu" else "",
    )


# --- per-method prices --------------------------------------------------------------------


def _stat_method(p: PricePoint) -> str:
    if p.method != "installments":
        return p.method
    return {
        "free": "installments_interest_free",
        "charged": "installments_with_interest",
    }.get(p.interest, "installments_interest_unstated")


def _assign_method_prices(post: Post, plausible: tuple[Decimal, Decimal] | None) -> None:
    """One amount per method per post; conflicting or implausible amounts are left out."""
    lo, hi = plausible or (Decimal("0.01"), Decimal("1e12"))
    grouped: dict[str, list[PricePoint]] = defaultdict(list)
    for p in post.terms.points:
        if p.currency != "BRL":
            post.method_notes[_stat_method(p)] = f"currency_{p.currency}"
            continue
        if not lo <= p.amount <= hi:
            post.method_notes[_stat_method(p)] = "implausible_amount"
            continue
        grouped[_stat_method(p)].append(p)
    for method, points in grouped.items():
        amounts = {p.amount for p in points}
        if len(amounts) > 1:
            post.method_notes[method] = "conflicting_amounts"
            continue
        post.method_prices[method] = points[0]
        post.method_notes.pop(method, None)
    upfront = [post.method_prices[m] for m in UPFRONT_METHODS if m in post.method_prices]
    if upfront:
        post.method_prices["lowest_upfront"] = min(upfront, key=lambda p: p.amount)


# --- dedupe -------------------------------------------------------------------------------


def _strong_keys(post: Post) -> list[tuple]:
    msg = post.message
    sig = post.signature()
    keys: list[tuple] = []
    own = f"{msg.chat_id}:{msg.message_id}" if msg.chat_id is not None else ""
    if msg.forward_ref:
        keys.append(("post", msg.forward_ref))
    elif own:
        keys.append(("post", own))
    if post.terms.points:
        tkey = text_key(msg.text)
        if len(tkey) >= MIN_TEXT_KEY_LEN:
            keys.append(("text", tkey, sig))
        if post.url:
            keys.append(("url", canonical_url(post.url), sig))
    return keys


def mark_duplicates(posts: list[Post]) -> None:
    index: dict[tuple, Post] = {}
    seen_messages: set[tuple] = set()
    for post in sorted(posts, key=lambda p: (p.message.date is None, p.message.date)):
        msg = post.message
        ident = (msg.chat_id or msg.chat, msg.message_id)
        if ident in seen_messages:
            post.status, post.reason = "duplicate", "same_message"
            continue
        seen_messages.add(ident)
        keys = _strong_keys(post)
        first = next((index[k] for k in keys if k in index), None)
        if first is not None:
            while first.duplicate_of is not None:
                first = first.duplicate_of
            post.status, post.duplicate_of = "duplicate", first
            post.reason = next(k[0] for k in keys if k in index)
            post.reason = {
                "post": "same_forwarded_post",
                "text": "same_text_and_prices",
                "url": "same_url_and_prices",
            }[post.reason]
        for key in keys:
            index.setdefault(key, first or post)


def mark_probable_reposts(posts: list[Post]) -> int:
    """Group included posts that look like the same deal reposted with new wording or links."""
    candidates = [p for p in posts if p.status == "included" and p.store and p.method_prices]
    candidates.sort(key=lambda p: p.message.date)
    groups: list[list[Post]] = []
    by_key: dict[tuple, list[list[Post]]] = defaultdict(list)
    for post in candidates:
        prices = tuple(sorted((m, p.amount) for m, p in post.method_prices.items()))
        key = (post.cls.model, post.store, prices, post.terms.coupons)
        placed = False
        for group in by_key[key]:
            last = group[-1]
            if post.message.date - last.message.date <= PROBABLE_WINDOW:
                group.append(post)
                placed = True
                break
        if not placed:
            group = [post]
            by_key[key].append(group)
            groups.append(group)
    number = 0
    for group in groups:
        if len(group) > 1:
            number += 1
            for post in group:
                post.probable_group = number
    return number


# --- pipeline -----------------------------------------------------------------------------


@dataclass
class Analysis:
    posts: list[Post]
    profile: Profile
    since: datetime | None
    until: datetime | None
    probable_groups: int
    scanned: int


def analyse(
    messages: Iterable[Message],
    *,
    profile: Profile | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Analysis:
    profile = profile or gpu_profile()
    posts: list[Post] = []
    scanned = 0
    for message in messages:
        scanned += 1
        if message.date is None:
            continue
        if since and message.date < since or until and message.date >= until:
            continue
        post = build_post(message, profile)
        if post.cls.reason == "no_target_model":
            continue  # the search term matched something else (a Ryzen 5 7600, say)
        posts.append(post)

    for post in posts:
        _assign_method_prices(post, profile.plausible)
        if post.cls.reason:
            post.status, post.reason = "excluded", post.cls.reason
        elif not post.terms.points:
            post.status, post.reason = "excluded", "no_price_found"
        elif not post.method_prices:
            post.status, post.reason = "excluded", "no_usable_price"

    mark_duplicates([p for p in posts if p.status != "excluded"])
    groups = mark_probable_reposts(posts)
    posts.sort(key=lambda p: p.message.date, reverse=True)
    return Analysis(posts, profile, since, until, groups, scanned)


# --- statistics ---------------------------------------------------------------------------


def _q(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Stat:
    n: int
    mean: Decimal
    median: Decimal
    minimum: Decimal
    maximum: Decimal


def describe(values: Sequence[Decimal]) -> Stat | None:
    if not values:
        return None
    return Stat(
        len(values),
        _q(statistics.mean(values)),
        _q(statistics.median(values)),
        min(values),
        max(values),
    )


def method_values(
    posts: Iterable[Post], model: str, method: str, collapse_probable: bool = False
) -> list[Decimal]:
    values = []
    seen_groups: set[int] = set()
    for post in posts:
        if post.status != "included" or post.cls.model != model:
            continue
        point = post.method_prices.get(method)
        if point is None:
            continue
        if collapse_probable and post.probable_group is not None:
            if post.probable_group in seen_groups:
                continue
            seen_groups.add(post.probable_group)
        values.append(point.amount)
    return values


def store_table(analysis: Analysis) -> dict[str, dict[str, Stat]]:
    """Lowest single-payment price per post, by store (unknown store as "")."""
    table: dict[str, dict[str, Stat]] = {}
    for model in analysis.profile.targets:
        by_store: dict[str, list[Decimal]] = defaultdict(list)
        for post in analysis.posts:
            if post.status == "included" and post.cls.model == model:
                point = post.method_prices.get("lowest_upfront")
                if point is not None:
                    by_store[post.store].append(point.amount)
        stats = {store: describe(values) for store, values in by_store.items()}
        table[model] = dict(sorted(stats.items(), key=lambda kv: -kv[1].n))
    return table


def statistics_table(analysis: Analysis) -> dict[str, dict[str, dict]]:
    table: dict[str, dict[str, dict]] = {}
    for model in analysis.profile.targets:
        rows = {}
        for method in STAT_METHODS:
            posts = describe(method_values(analysis.posts, model, method))
            deals = describe(method_values(analysis.posts, model, method, True))
            if posts:
                rows[method] = {"posts": posts, "deals": deals}
        table[model] = rows
    return table


# --- output -------------------------------------------------------------------------------


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_PREFIXES) else text


def _money(value: Decimal | None) -> str:
    return "" if value is None else f"{value:.2f}"


POST_COLUMNS = (
    "date",
    "chat",
    "link",
    "model",
    "category",
    "status",
    "reason",
    "duplicate_of",
    "hours_after_original",
    "probable_group",
    "store",
    "pix",
    "boleto",
    "card_1x",
    "upfront",
    "unspecified",
    "installments_total",
    "installments_n",
    "installment_value",
    "interest",
    "monthly_rate_pct",
    "installments_source",
    "lowest_upfront",
    "original_price",
    "coupons",
    "coupon_discounts",
    "coupon_applied",
    "price_notes",
    "other_models",
    "url",
    "title",
    "text",
)

POINT_COLUMNS = (
    "date",
    "chat",
    "link",
    "model",
    "post_status",
    "method",
    "stat_method",
    "amount",
    "currency",
    "line",
    "installments_n",
    "installment_value",
    "interest",
    "monthly_rate_pct",
    "coupon_applied",
    "source",
    "used_in_statistics",
    "note",
)


def _installment_point(post: Post) -> PricePoint | None:
    for method in STAT_METHODS:
        if method.startswith("installments") and method in post.method_prices:
            return post.method_prices[method]
    return None


def post_row(post: Post) -> dict[str, str]:
    msg = post.message
    mp = post.method_prices
    inst = _installment_point(post)
    return {
        "date": msg.date.isoformat(),
        "chat": msg.chat,
        "link": msg.link,
        "model": post.cls.model or "",
        "category": post.cls.category,
        "status": post.status,
        "reason": post.reason,
        "duplicate_of": post.duplicate_of.ref if post.duplicate_of else "",
        "hours_after_original": (
            f"{(msg.date - post.duplicate_of.message.date).total_seconds() / 3600:.1f}"
            if post.duplicate_of
            else ""
        ),
        "probable_group": post.probable_group or "",
        "store": post.store,
        **{m: _money(mp[m].amount) if m in mp else "" for m in UPFRONT_METHODS},
        "installments_total": _money(inst.amount) if inst else "",
        "installments_n": inst.installments if inst and inst.installments else "",
        "installment_value": _money(inst.installment_value) if inst else "",
        "interest": inst.interest if inst else "",
        "monthly_rate_pct": _money(inst.monthly_rate) if inst else "",
        "installments_source": inst.source if inst else "",
        "lowest_upfront": _money(mp["lowest_upfront"].amount) if "lowest_upfront" in mp else "",
        "original_price": _money(post.terms.original_price),
        "coupons": " ".join(post.terms.coupons),
        "coupon_discounts": " ".join(post.terms.coupon_discounts),
        "coupon_applied": "yes" if any(p.coupon_applied for p in post.terms.points) else "",
        "price_notes": " ".join(f"{m}={n}" for m, n in sorted(post.method_notes.items())),
        "other_models": " ".join(post.cls.other_models),
        "url": post.url,
        "title": _title(msg.text),
        "text": " ".join(msg.text.split()),
    }


def point_rows(post: Post) -> Iterable[dict[str, str]]:
    msg = post.message
    used = {id(p) for m, p in post.method_prices.items() if m != "lowest_upfront"}
    for p in post.terms.points:
        stat_method = _stat_method(p)
        in_stats = post.status == "included" and id(p) in used
        yield {
            "date": msg.date.isoformat(),
            "chat": msg.chat,
            "link": msg.link,
            "model": post.cls.model or "",
            "post_status": post.status,
            "method": p.method,
            "stat_method": stat_method,
            "amount": _money(p.amount),
            "currency": p.currency,
            "line": p.line,
            "installments_n": p.installments or "",
            "installment_value": _money(p.installment_value),
            "interest": p.interest,
            "monthly_rate_pct": _money(p.monthly_rate),
            "coupon_applied": "yes" if p.coupon_applied else "",
            "source": p.source,
            "used_in_statistics": "yes" if in_stats else "",
            "note": post.method_notes.get(stat_method, "") or (post.reason if not in_stats else ""),
        }


def write_csv(rows: Iterable[dict], columns: Sequence[str], path: Path) -> int:
    import csv

    count = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _safe(row.get(k)) for k in columns})
            count += 1
    return count


def summary_counts(analysis: Analysis) -> dict:
    status = defaultdict(int)
    reasons = defaultdict(int)
    for post in analysis.posts:
        status[post.status] += 1
        if post.status != "included":
            reasons[post.reason] += 1
    per_model = defaultdict(lambda: defaultdict(int))
    for post in analysis.posts:
        if post.status == "included":
            m = per_model[post.cls.model]
            m["posts"] += 1
            m["with_coupon"] += bool(post.terms.coupons)
            m["coupon_applied"] += any(p.coupon_applied for p in post.terms.points)
            m["probable_repost"] += post.probable_group is not None
            inst = _installment_point(post)
            if inst:
                m[f"installments_{inst.interest or 'unstated'}"] += 1
    return {
        "scanned": analysis.scanned,
        "target_posts": len(analysis.posts),
        "status": dict(status),
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "per_model": {k: dict(v) for k, v in per_model.items()},
        "probable_groups": analysis.probable_groups,
        "chats": len({p.message.chat for p in analysis.posts}),
    }


def _stat_json(stat: Stat | None) -> dict | None:
    if stat is None:
        return None
    return {
        "n": stat.n,
        "mean": str(stat.mean),
        "median": str(stat.median),
        "min": str(stat.minimum),
        "max": str(stat.maximum),
    }


METHOD_LABELS = {
    "pix": "Pix",
    "boleto": "Boleto",
    "card_1x": "Card, single payment",
    "upfront": "À vista (method not named)",
    "unspecified": "No payment method stated",
    "installments_interest_free": "Installments, interest-free (total)",
    "installments_with_interest": "Installments, with interest (total)",
    "installments_interest_unstated": "Installments, interest not stated (total)",
    "lowest_upfront": "Lowest single-payment price per post",
}


def _brl(value: Decimal) -> str:
    whole, cents = f"{value:,.2f}".split(".")
    return f"R$ {whole.replace(',', '.')},{cents}"


def render_markdown(analysis: Analysis, table: dict, counts: dict) -> str:
    since = analysis.since.date().isoformat() if analysis.since else "start"
    until = analysis.until.date().isoformat() if analysis.until else "now"
    lines = [
        "# Telegram offers: "
        + ", ".join(analysis.profile.label(t) for t in analysis.profile.targets),
        "",
        f"Window: {since} to {until} (UTC). Messages scanned: {counts['scanned']}; "
        f"posts naming a target model: {counts['target_posts']} from {counts['chats']} chats.",
        "",
        "Posts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts["status"].items())) + ".",
        "",
        "Statistics count each post once after merging proven duplicates. The *deals* "
        "columns also collapse probable reposts (same model, store, prices and coupons, "
        f"each within {int(PROBABLE_WINDOW.total_seconds() // 3600)} h), "
        f"{counts['probable_groups']} groups.",
        "",
    ]
    for model, rows in table.items():
        lines += [f"## {analysis.profile.label(model)}", ""]
        if not rows:
            lines += ["No usable offers.", ""]
            continue
        lines += [
            "| Payment method | Posts | Mean | Median | Min | Max | Deals | Deal median |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for method, cell in rows.items():
            s, d = cell["posts"], cell["deals"]
            lines.append(
                f"| {METHOD_LABELS[method]} | {s.n} | {_brl(s.mean)} | {_brl(s.median)} | "
                f"{_brl(s.minimum)} | {_brl(s.maximum)} | {d.n} | {_brl(d.median)} |"
            )
        stores = store_table(analysis).get(model, {})
        if stores:
            lines += [
                "",
                "Lowest single-payment price per post, by store:",
                "",
                "| Store | Posts | Median | Min | Max |",
                "|---|---:|---:|---:|---:|",
            ]
            lines += [
                f"| {store or 'not identified'} | {st.n} | {_brl(st.median)} | "
                f"{_brl(st.minimum)} | {_brl(st.maximum)} |"
                for store, st in stores.items()
            ]
        info = counts["per_model"].get(model, {})
        lines += [
            "",
            f"Included posts: {info.get('posts', 0)}; with a coupon code: "
            f"{info.get('with_coupon', 0)}; price stated as requiring the coupon: "
            f"{info.get('coupon_applied', 0)}; installments interest-free / with interest / "
            f"not stated: {info.get('installments_free', 0)} / "
            f"{info.get('installments_charged', 0)} / {info.get('installments_unstated', 0)}.",
            "",
        ]
    lines += ["## Left out of the statistics", "", "| Reason | Posts |", "|---|---:|"]
    lines += [f"| {reason} | {n} |" for reason, n in counts["reasons"].items()]
    lines += [
        "",
        "## How to audit",
        "",
        "- `posts.csv`: one row per post with its status, reason, duplicate target, probable "
        "group, store and the price used for each method.",
        "- `price_points.csv`: every amount found, with its message line, how it was read "
        "(stated or computed as count × installment), and whether it entered the statistics.",
        "- `source` in `price_points.csv`: `stated` (amount and payment cue on one line), "
        "`stated_cue_apart` (the post's only amount; its only named method is on another "
        "line), `computed` (installment count × value), `price_with_terms` (a lone "
        '"10x sem juros" line applied to the post\'s only amount).',
        "",
    ]
    return "\n".join(lines)


def write_report(analysis: Analysis, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    table = statistics_table(analysis)
    counts = summary_counts(analysis)
    write_csv((post_row(p) for p in analysis.posts), POST_COLUMNS, out_dir / "posts.csv")
    write_csv(
        (r for p in analysis.posts for r in point_rows(p)),
        POINT_COLUMNS,
        out_dir / "price_points.csv",
    )
    (out_dir / "summary.md").write_text(render_markdown(analysis, table, counts), "utf-8")
    data = {
        "since": analysis.since.isoformat() if analysis.since else None,
        "until": analysis.until.isoformat() if analysis.until else None,
        "counts": counts,
        "statistics": {
            model: {
                method: {"posts": _stat_json(c["posts"]), "deals": _stat_json(c["deals"])}
                for method, c in rows.items()
            }
            for model, rows in table.items()
        },
    }
    data["by_store"] = {
        model: {store or "unknown": _stat_json(st) for store, st in stores.items()}
        for model, stores in store_table(analysis).items()
    }
    (out_dir / "summary.json").write_text(json.dumps(data, indent=2), "utf-8")
    from . import report_html, trends

    model_trends = {}
    offers = {}
    for model in analysis.profile.targets:
        offers[model] = top_offers(analysis, model, 15)
        if trends.priced_posts(analysis, model):
            model_trends[model] = trends.compute(analysis, model)
    data["top_offers"] = {m: [offer_json(o) for o in v] for m, v in offers.items()}
    data["trends"] = {m: trends.to_json(t) for m, t in model_trends.items()}
    (out_dir / "summary.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    html = report_html.render(analysis, table, counts, model_trends, offers)
    (out_dir / "report.html").write_text(html, "utf-8")
    for name in ("posts.csv", "price_points.csv", "summary.md", "summary.json", "report.html"):
        (out_dir / name).chmod(0o600)
    return data


# --- best offers ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopOffer:
    post: Post
    price: PricePoint
    reposts: int  # other posts of the same deal (proven duplicates + probable reposts)


def top_offers(analysis: Analysis, model: str, n: int = 10) -> list[TopOffer]:
    """Cheapest distinct deals: one entry per probable-repost group, cheapest post first."""
    dupes: dict[int, int] = defaultdict(int)
    for post in analysis.posts:
        if post.status == "duplicate" and post.duplicate_of is not None:
            dupes[id(post.duplicate_of)] += 1
    groups: dict[int, list[Post]] = defaultdict(list)
    singles: list[Post] = []
    for post in analysis.posts:
        if post.status != "included" or post.cls.model != model:
            continue
        if "lowest_upfront" not in post.method_prices:
            continue
        if post.probable_group is None:
            singles.append(post)
        else:
            groups[post.probable_group].append(post)
    offers = []
    for post in singles:
        offers.append(TopOffer(post, post.method_prices["lowest_upfront"], dupes[id(post)]))
    for members in groups.values():
        first = min(members, key=lambda p: p.message.date)
        extra = len(members) - 1 + sum(dupes[id(p)] for p in members)
        offers.append(TopOffer(first, first.method_prices["lowest_upfront"], extra))
    offers.sort(key=lambda o: (o.price.amount, o.post.message.date))
    return offers[:n]


def offer_json(offer: TopOffer) -> dict:
    post, msg = offer.post, offer.post.message
    inst = _installment_point(post)
    return {
        "date": msg.date.isoformat(),
        "chat": msg.chat,
        "link": msg.link,
        "title": _title(msg.text),
        "store": post.store,
        "brand": post.brand,
        "price": f"{offer.price.amount:.2f}",
        "method": offer.price.method,
        "coupon_applied": any(p.coupon_applied for p in post.terms.points),
        "coupons": list(post.terms.coupons),
        "installments": None
        if inst is None
        else {
            "n": inst.installments,
            "total": f"{inst.amount:.2f}",
            "interest": inst.interest or "not stated",
        },
        "original_price": _money(post.terms.original_price) or None,
        "reposts": offer.reposts,
    }
