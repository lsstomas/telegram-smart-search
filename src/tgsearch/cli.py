"""Command-line interface: ``tgsearch export ...`` and ``tgsearch live ...``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import chain
from pathlib import Path

from . import __version__
from .csv_out import write_csv
from .extract import parse_amount
from .models import Message
from .search import Filters, Query, run_search


def _date(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}, use YYYY-MM-DD") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _price(value: str) -> Decimal:
    amount = parse_amount(value.replace("R$", "").replace("$", "").strip())
    if amount is None or amount < 0:
        raise argparse.ArgumentTypeError(f"invalid price {value!r}")
    return amount


def _delimiter(value: str) -> str:
    mapping = {"tab": "\t", "\\t": "\t"}
    value = mapping.get(value, value)
    if len(value) != 1:
        raise argparse.ArgumentTypeError("delimiter must be a single character (or 'tab')")
    return value


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "terms",
        nargs="*",
        metavar="TERM",
        help="search terms; all must match unless --any (quote phrases)",
    )
    p.add_argument(
        "--any",
        dest="any_term",
        action="store_true",
        help="match messages containing any term instead of all",
    )
    p.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="TERM",
        help="drop messages containing TERM (repeatable)",
    )
    p.add_argument("--min-price", type=_price, help="minimum best price (e.g. 100 or 1.299,90)")
    p.add_argument("--max-price", type=_price, help="maximum best price")
    p.add_argument("--since", type=_date, help="only messages on/after this date (YYYY-MM-DD)")
    p.add_argument(
        "--until", type=_date, help="only messages before the end of this date (YYYY-MM-DD)"
    )
    p.add_argument(
        "--include-unpriced",
        action="store_true",
        help="keep matching messages where no price was detected",
    )
    p.add_argument("--no-dedupe", action="store_true", help="disable deduplication")
    p.add_argument(
        "--sort",
        choices=("date", "price"),
        default="date",
        help="date = newest first (default), price = cheapest first",
    )
    p.add_argument("-o", "--output", type=Path, help="CSV file to write (default: stdout)")
    p.add_argument(
        "--delimiter",
        type=_delimiter,
        default=",",
        help="CSV delimiter, e.g. ';' for spreadsheets in pt-BR locale",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tgsearch",
        description="Search Telegram offers locally and export price/payment data to CSV.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="search Telegram Desktop JSON exports (offline)")
    exp.add_argument(
        "--file",
        "-f",
        dest="files",
        type=Path,
        action="append",
        required=True,
        help="path to result.json (repeatable)",
    )
    _add_common(exp)

    live = sub.add_parser("live", help="search chats through your Telegram account (Telethon)")
    live.add_argument(
        "--chat",
        "-c",
        dest="chats",
        action="append",
        required=True,
        help="@username, t.me link or numeric id (repeatable)",
    )
    live.add_argument(
        "--limit",
        type=int,
        default=200,
        help="max messages fetched per chat and server-side term (default 200)",
    )
    live.add_argument(
        "--session",
        type=Path,
        help="Telethon session path (default: $XDG_DATA_HOME/tgsearch/tgsearch)",
    )
    _add_common(live)
    return parser


def _messages(args: argparse.Namespace) -> Iterable[Message]:
    if args.command == "export":
        from .sources.export import read_export

        return chain.from_iterable(read_export(f) for f in args.files)

    from .sources.live import fetch_messages

    return fetch_messages(
        args.chats,
        args.terms,
        any_term=args.any_term,
        limit=args.limit,
        since=args.since,
        session=args.session,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "live" and args.limit <= 0:
        parser.error("--limit must be positive")

    until = args.until + timedelta(days=1) if args.until else None
    if args.until and (args.until.hour or args.until.minute or args.until.second):
        until = args.until  # an explicit time is used as-is

    query = Query(terms=args.terms, exclude=args.exclude, any_term=args.any_term)
    filters = Filters(
        min_price=args.min_price,
        max_price=args.max_price,
        since=args.since,
        until=until,
        include_unpriced=args.include_unpriced,
    )

    from .sources.live import LiveConfigError

    try:
        offers, stats = run_search(
            _messages(args), query, filters, deduplicate=not args.no_dedupe, sort=args.sort
        )
    except LiveConfigError as exc:
        print(f"tgsearch: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"tgsearch: {exc}", file=sys.stderr)
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="") as fh:
            write_csv(offers, fh, delimiter=args.delimiter)
    else:
        write_csv(offers, sys.stdout, delimiter=args.delimiter)

    if not args.quiet:
        print(
            f"scanned {stats.scanned} messages, {stats.matched} matched, "
            f"{stats.offers} offers written ({stats.merged} duplicates merged)",
            file=sys.stderr,
        )
    return 0
