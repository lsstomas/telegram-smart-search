"""When are prices lowest? Daily lows, weekdays, time of month, promotion dates, coupons.

Every figure is built from *included* posts (proven duplicates merged), using
each post's lowest single-payment price. Days are Brazilian calendar days
(America/Sao_Paulo), because that is when shoppers see the posts.

Deal channels only post when they think a price is worth posting, so these are
prices *offered in posts*, not a market average: a quiet day means no post,
not a high price.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .analysis import Analysis, Post, _q

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover - zoneinfo data missing
    LOCAL_TZ = timezone(timedelta(hours=-3))

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_PARTS = (("1-7", 1, 7), ("8-14", 8, 14), ("15-21", 15, 21), ("22-end", 22, 31))


def local_day(post: Post) -> date:
    return post.message.date.astimezone(LOCAL_TZ).date()


def black_friday(year: int) -> date:
    nov1 = date(year, 11, 1)
    first_friday = nov1 + timedelta(days=(4 - nov1.weekday()) % 7)
    return first_friday + timedelta(weeks=3)


def promo_events(year: int) -> dict[date, str]:
    """Brazilian retail dates; double dates (9.9, 10.10, 11.11) are marketplace campaigns."""
    bf = black_friday(year)
    events = {date(year, m, m): f"{m}.{m}" for m in range(1, 13)}
    events.update(
        {
            date(year, 3, 15): "Consumer Day",
            date(year, 9, 15): "Customer Day",
            date(year, 10, 12): "Children's Day",
            bf: "Black Friday",
            bf + timedelta(days=3): "Cyber Monday",
        }
    )
    return events


def event_for(day: date, window: int = 1) -> str:
    """The promotion within ``window`` days of ``day`` (campaigns start early, end late)."""
    for delta in range(-window, window + 1):
        name = promo_events(day.year).get(day + timedelta(days=delta))
        if name:
            return name
    return ""


@dataclass(frozen=True)
class Group:
    label: str
    days: int  # calendar days in the window that fall in this group
    days_with_posts: int
    posts: int
    median_daily_low: Decimal | None
    best: Decimal | None
    good_days: int  # days whose low was at or under the "good price" line


def _price(post: Post) -> Decimal | None:
    point = post.method_prices.get("lowest_upfront")
    return point.amount if point else None


def priced_posts(analysis: Analysis, model: str) -> list[Post]:
    return [
        p
        for p in analysis.posts
        if p.status == "included" and p.cls.model == model and _price(p) is not None
    ]


def daily_lows(posts: Iterable[Post]) -> dict[date, Decimal]:
    lows: dict[date, Decimal] = {}
    for post in posts:
        day, price = local_day(post), _price(post)
        if day not in lows or price < lows[day]:
            lows[day] = price
    return dict(sorted(lows.items()))


def percentile(values: Sequence[Decimal], pct: float) -> Decimal:
    """Linear-interpolation percentile (same as numpy's default)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return _q(ordered[low] + (ordered[high] - ordered[low]) * Decimal(str(rank - low)))


def _calendar(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _group(label, days, lows, counts, good_line) -> Group:
    seen = [d for d in days if d in lows]
    values = [lows[d] for d in seen]
    return Group(
        label=label,
        days=len(days),
        days_with_posts=len(seen),
        posts=sum(counts.get(d, 0) for d in days),
        median_daily_low=_q(statistics.median(values)) if values else None,
        best=min(values) if values else None,
        good_days=sum(1 for v in values if good_line is not None and v <= good_line),
    )


@dataclass
class Trends:
    model: str
    start: date
    end: date
    daily: dict[date, Decimal]
    posts_per_day: dict[date, int]
    deal_prices: list[Decimal]  # one price per deal: probable reposts collapsed
    good_line: Decimal | None  # 25th percentile of deal prices
    weekdays: list[Group]
    month_parts: list[Group]
    events: list[Group]
    weekly: list[tuple[date, Decimal, int]]  # week start, median price, posts
    thresholds: list[tuple[Decimal, int, int]]  # price, days at or under it, longest wait
    coupon: dict[str, dict]
    stores: dict[str, dict]
    brands: dict[str, dict]
    methods: dict[str, dict]


def _deal_prices(posts: Sequence[Post]) -> list[Decimal]:
    seen: set[int] = set()
    out = []
    for post in sorted(posts, key=lambda p: p.message.date):
        if post.probable_group is not None:
            if post.probable_group in seen:
                continue
            seen.add(post.probable_group)
        out.append(_price(post))
    return out


def _longest_gap(days: Sequence[date], start: date, end: date) -> int:
    """Longest run of consecutive days with no qualifying post, in days."""
    marks = [start - timedelta(days=1), *sorted(days), end + timedelta(days=1)]
    return max((b - a).days - 1 for a, b in zip(marks, marks[1:], strict=False))


def _summary(values: Sequence[Decimal]) -> dict:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "median": _q(statistics.median(values)),
        "p25": percentile(values, 0.25),
        "min": min(values),
        "max": max(values),
    }


def _split(posts: Sequence[Post], key) -> dict[str, dict]:
    groups: dict[str, list[Decimal]] = defaultdict(list)
    for post in posts:
        groups[key(post)].append(_price(post))
    stats = {k: _summary(v) for k, v in groups.items()}
    return dict(sorted(stats.items(), key=lambda kv: -kv[1]["n"]))


def _coupon_kind(post: Post) -> str:
    if any(p.coupon_applied for p in post.terms.points):
        return "price needs coupon"
    if post.terms.coupons:
        return "coupon code mentioned"
    return "no coupon"


def compute(analysis: Analysis, model: str, step: Decimal = Decimal("50")) -> Trends:
    posts = priced_posts(analysis, model)
    start = (analysis.since or min(p.message.date for p in posts)).astimezone(LOCAL_TZ).date()
    end = (analysis.until or max(p.message.date for p in posts)).astimezone(LOCAL_TZ).date()
    if analysis.until and analysis.until.astimezone(LOCAL_TZ).time() == datetime.min.time():
        end -= timedelta(days=1)  # an exclusive midnight end
    days = _calendar(start, end)
    lows = daily_lows(posts)
    counts: dict[date, int] = defaultdict(int)
    for post in posts:
        counts[local_day(post)] += 1

    deals = _deal_prices(posts)
    good = percentile(deals, 0.25) if deals else None

    weekdays = [
        _group(name, [d for d in days if d.weekday() == i], lows, counts, good)
        for i, name in enumerate(WEEKDAYS)
    ]
    parts = [
        _group(label, [d for d in days if lo <= d.day <= hi], lows, counts, good)
        for label, lo, hi in MONTH_PARTS
    ]
    by_event: dict[str, list[date]] = defaultdict(list)
    for d in days:
        by_event[event_for(d) or "no promotion date"].append(d)
    events = [_group(label, ds, lows, counts, good) for label, ds in by_event.items()]
    events.sort(key=lambda g: (g.label == "no promotion date", min(by_event[g.label])))

    weekly: list[tuple[date, Decimal, int]] = []
    by_week: dict[date, list[Decimal]] = defaultdict(list)
    for post in posts:
        day = local_day(post)
        by_week[day - timedelta(days=day.weekday())].append(_price(post))
    for week, values in sorted(by_week.items()):
        weekly.append((week, _q(statistics.median(values)), len(values)))

    thresholds = []
    if lows:
        top = max(lows.values())
        # Start near the 5th percentile, so one freak low does not fill the table.
        price = (percentile(list(lows.values()), 0.05) // step) * step
        while price <= top + step:
            hit = [d for d, v in lows.items() if v <= price]
            thresholds.append((price, len(hit), _longest_gap(hit, start, end)))
            price += step

    methods: dict[str, list[Decimal]] = defaultdict(list)
    for post in posts:
        for method, point in post.method_prices.items():
            if method != "lowest_upfront":
                methods[method].append(point.amount)

    return Trends(
        model=model,
        start=start,
        end=end,
        daily=lows,
        posts_per_day=dict(sorted(counts.items())),
        deal_prices=deals,
        good_line=good,
        weekdays=weekdays,
        month_parts=parts,
        events=events,
        weekly=weekly,
        thresholds=thresholds,
        coupon=_split(posts, _coupon_kind),
        stores=_split(posts, lambda p: p.store or "not identified"),
        brands=_split(posts, lambda p: p.brand or "not identified"),
        methods={m: _summary(v) for m, v in methods.items()},
    )


def to_json(t: Trends) -> dict:
    def g(group: Group) -> dict:
        return {
            "label": group.label,
            "days": group.days,
            "days_with_posts": group.days_with_posts,
            "posts": group.posts,
            "median_daily_low": _s(group.median_daily_low),
            "best": _s(group.best),
            "good_days": group.good_days,
        }

    return {
        "model": t.model,
        "start": t.start.isoformat(),
        "end": t.end.isoformat(),
        "good_line_p25": _s(t.good_line),
        "deal_prices": _summary_json(_summary(t.deal_prices)),
        "deal_percentiles": {
            f"p{int(p * 100)}": _s(percentile(t.deal_prices, p))
            for p in (0.1, 0.25, 0.5, 0.75, 0.9)
        }
        if t.deal_prices
        else {},
        "daily": [
            {"day": d.isoformat(), "low": _s(v), "posts": t.posts_per_day.get(d, 0)}
            for d, v in t.daily.items()
        ],
        "weekdays": [g(x) for x in t.weekdays],
        "month_parts": [g(x) for x in t.month_parts],
        "events": [g(x) for x in t.events],
        "weekly": [{"week": w.isoformat(), "median": _s(m), "posts": n} for w, m, n in t.weekly],
        "thresholds": [
            {"price": _s(p), "days": n, "longest_wait_days": gap} for p, n, gap in t.thresholds
        ],
        "coupon": {k: _summary_json(v) for k, v in t.coupon.items()},
        "stores": {k: _summary_json(v) for k, v in t.stores.items()},
        "brands": {k: _summary_json(v) for k, v in t.brands.items()},
        "methods": {k: _summary_json(v) for k, v in t.methods.items()},
    }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _summary_json(summary: dict) -> dict:
    return {k: (_s(v) if isinstance(v, Decimal) else v) for k, v in summary.items()}
