"""A self-contained HTML report: inline CSS and SVG, no scripts, no network requests.

The page is written next to the CSVs and opened locally in a browser. Text that
comes from Telegram is HTML-escaped; links are only emitted for ``https://t.me``
post links and ``http(s)`` store links.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape

from .analysis import METHOD_LABELS, Analysis, TopOffer
from .trends import Trends


def brl(value: Decimal | str | None) -> str:
    if value is None:
        return "–"
    whole, cents = f"{Decimal(value):,.2f}".split(".")
    return f"R$ {whole.replace(',', '.')},{cents}"


def _link(url: str, label: str) -> str:
    if not url.startswith(("https://", "http://")):
        return escape(label)
    return f'<a href="{escape(url, quote=True)}" rel="noreferrer noopener">{escape(label)}</a>'


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1b1b1f;--muted:#6b6b76;--line:#e4e4e0;--accent:#2f6fed;
--good:#1f9d55;--warn:#c27c0e;--bar:#9db7f5}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--card:#1d1d21;--ink:#ececf1;--muted:#9a9aa6;
--line:#2c2c33;--accent:#7aa2ff;--good:#4cc38a;--warn:#e0a84a;--bar:#3c5aa6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:36px 0 12px}h3{font-size:16px;margin:20px 0 8px}
.muted{color:var(--muted)}.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(180px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi{font-size:24px;font-weight:650}.kpi small{font-size:13px;font-weight:400;color:var(--muted)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
border-radius:12px;overflow:hidden;font-size:14px}th,td{padding:8px 10px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}td.n,th.n{text-align:right;
font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}svg{width:100%;height:auto;display:block}
a{color:var(--accent)}.tag{display:inline-block;padding:1px 8px;border-radius:999px;border:1px solid var(--line);
font-size:12px;color:var(--muted);margin-right:4px}
"""


def _line_chart(daily: dict[date, Decimal], good: Decimal | None, start: date, end: date) -> str:
    if not daily:
        return '<p class="muted">No priced posts.</p>'
    w, h, pad = 960, 260, 44
    lo = min(daily.values())
    hi = max(daily.values())
    span = (hi - lo) or Decimal(1)
    total = max((end - start).days, 1)

    def x(d: date) -> float:
        return pad + (w - 2 * pad) * (d - start).days / total

    def y(v: Decimal) -> float:
        return h - pad - float((v - lo) / span) * (h - 2 * pad)

    pts = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in daily.items())
    dots = "".join(
        f'<circle cx="{x(d):.1f}" cy="{y(v):.1f}" r="3" fill="var(--accent)">'
        f"<title>{d.isoformat()}: {brl(v)}</title></circle>"
        for d, v in daily.items()
    )
    guide = ""
    if good is not None and lo <= good <= hi:
        gy = y(good)
        guide = (
            f'<line x1="{pad}" x2="{w - pad}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="var(--good)" '
            f'stroke-dasharray="5 4"/><text x="{w - pad}" y="{gy - 6:.1f}" text-anchor="end" '
            f'fill="var(--good)" font-size="12">good price ≤ {brl(good)}</text>'
        )
    labels = (
        f'<text x="{pad - 6}" y="{y(hi) + 4:.1f}" text-anchor="end" font-size="12" '
        f'fill="var(--muted)">{brl(hi)}</text>'
        f'<text x="{pad - 6}" y="{y(lo) + 4:.1f}" text-anchor="end" font-size="12" '
        f'fill="var(--muted)">{brl(lo)}</text>'
        f'<text x="{pad}" y="{h - 12}" font-size="12" fill="var(--muted)">{start}</text>'
        f'<text x="{w - pad}" y="{h - 12}" text-anchor="end" font-size="12" '
        f'fill="var(--muted)">{end}</text>'
    )
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Lowest price per day">'
        f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="1.5" '
        f'opacity=".5"/>{guide}{dots}{labels}</svg>'
    )


def _groups_table(groups, title: str) -> str:
    rows = "".join(
        f"<tr><td>{escape(g.label)}</td><td class='n'>{g.days_with_posts}/{g.days}</td>"
        f"<td class='n'>{g.posts}</td><td class='n'>{brl(g.median_daily_low)}</td>"
        f"<td class='n'>{brl(g.best)}</td><td class='n'>{g.good_days}</td></tr>"
        for g in groups
    )
    return (
        f"<h3>{escape(title)}</h3><div class='scroll'><table><tr><th>Group</th>"
        "<th class='n'>Days with posts</th><th class='n'>Posts</th>"
        "<th class='n'>Median daily low</th><th class='n'>Best</th>"
        f"<th class='n'>Good-price days</th></tr>{rows}</table></div>"
    )


def _split_table(stats: dict[str, dict], title: str) -> str:
    rows = "".join(
        f"<tr><td>{escape(k)}</td><td class='n'>{v['n']}</td><td class='n'>{brl(v.get('median'))}</td>"
        f"<td class='n'>{brl(v.get('p25'))}</td><td class='n'>{brl(v.get('min'))}</td></tr>"
        for k, v in stats.items()
        if v.get("n")
    )
    return (
        f"<h3>{escape(title)}</h3><div class='scroll'><table><tr><th></th><th class='n'>Posts</th>"
        "<th class='n'>Median</th><th class='n'>25th pct</th><th class='n'>Min</th></tr>"
        f"{rows}</table></div>"
    )


def _offers_table(offers: list[TopOffer]) -> str:
    rows = []
    for o in offers:
        post, msg = o.post, o.post.message
        tags = []
        if any(p.coupon_applied for p in post.terms.points):
            tags.append("needs coupon")
        tags += [f"coupon {c}" for c in post.terms.coupons[:2]]
        if o.reposts:
            tags.append(f"+{o.reposts} reposts")
        title = post.message.text.strip().splitlines()[0][:110] if msg.text.strip() else ""
        rows.append(
            f"<tr><td class='n'><b>{brl(o.price.amount)}</b><br><span class='muted'>"
            f"{escape(METHOD_LABELS.get(o.price.method, o.price.method))}</span></td>"
            f"<td>{_link(msg.link, title)}<br>"
            + "".join(f"<span class='tag'>{escape(t)}</span>" for t in tags)
            + f"</td><td>{escape(post.store or '–')}</td><td>{escape(post.brand or '–')}</td>"
            f"<td>{msg.date.date().isoformat()}<br><span class='muted'>{escape(msg.chat)}</span></td></tr>"
        )
    return (
        "<div class='scroll'><table><tr><th class='n'>Price</th><th>Post</th><th>Store</th>"
        f"<th>Brand</th><th>Date / chat</th></tr>{''.join(rows)}</table></div>"
    )


def render(
    analysis: Analysis,
    table: dict,
    counts: dict,
    trends: dict[str, Trends],
    offers: dict[str, list[TopOffer]],
) -> str:
    profile = analysis.profile
    since = analysis.since.date().isoformat() if analysis.since else "start"
    until = analysis.until.date().isoformat() if analysis.until else "now"
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Telegram offers report</title>"
        f"<style>{CSS}</style></head><body><main>",
        f"<h1>Offers: {escape(', '.join(profile.label(t) for t in profile.targets))}</h1>",
        f"<p class='muted'>{since} to {until} · {counts['target_posts']} posts from "
        f"{counts['chats']} chats · {counts['status'].get('included', 0)} counted, "
        f"{counts['status'].get('duplicate', 0)} duplicates merged, "
        f"{counts['status'].get('excluded', 0)} excluded.</p>",
    ]
    for model in profile.targets:
        rows = table.get(model, {})
        t = trends.get(model)
        parts.append(f"<h2>{escape(profile.label(model))}</h2>")
        if not rows or t is None:
            parts.append("<p class='muted'>No usable offers.</p>")
            continue
        low = rows.get("lowest_upfront", {}).get("deals")
        pix = rows.get("pix", {}).get("posts")
        inst = rows.get("installments_interest_free", {}).get("posts")
        cards = [
            (
                "Typical deal (median)",
                brl(low.median) if low else "–",
                f"{low.n if low else 0} deals",
            ),
            ("Good price (25th pct)", brl(t.good_line), "a quarter of deals at or below"),
            ("Best seen", brl(low.minimum) if low else "–", "lowest single payment"),
            ("Pix median", brl(pix.median) if pix else "–", f"{pix.n if pix else 0} posts"),
            (
                "Interest-free installments",
                brl(inst.median) if inst else "–",
                f"median total, {inst.n if inst else 0} posts",
            ),
        ]
        parts.append(
            "<div class='grid'>"
            + "".join(
                f"<div class='card'><div class='muted'>{escape(a)}</div>"
                f"<div class='kpi'>{escape(b)}</div><small class='muted'>{escape(c)}</small></div>"
                for a, b, c in cards
            )
            + "</div>"
        )
        parts.append("<h3>Lowest price posted each day</h3>")
        parts.append(f"<div class='card'>{_line_chart(t.daily, t.good_line, t.start, t.end)}</div>")
        parts.append("<h3>Cheapest distinct deals</h3>")
        parts.append(_offers_table(offers.get(model, [])))
        parts.append(_groups_table(t.weekdays, "By weekday"))
        parts.append(_groups_table(t.month_parts, "By time of month"))
        parts.append(_groups_table(t.events, "Promotion dates (±1 day)"))
        parts.append(_split_table(t.coupon, "Coupons"))
        parts.append(_split_table(t.stores, "Stores"))
        if any(k != "not identified" for k in t.brands):
            parts.append(_split_table(t.brands, "Brands"))
    reasons = "".join(
        f"<tr><td>{escape(r)}</td><td class='n'>{n}</td></tr>" for r, n in counts["reasons"].items()
    )
    parts.append(
        "<h2>Left out of the statistics</h2><div class='scroll'><table><tr><th>Reason</th>"
        f"<th class='n'>Posts</th></tr>{reasons}</table></div>"
        "<p class='muted'>Prices are what the posts say; deal channels post when a price looks "
        "good, so quiet days mean no post, not a high price. Audit every number in posts.csv "
        "and price_points.csv.</p></main></body></html>"
    )
    return "".join(parts)
