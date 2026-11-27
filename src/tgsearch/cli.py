"""Command-line interface: ``tgsearch export ...`` and ``tgsearch live ...``."""

from __future__ import annotations

import argparse
import os
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


def _add_session(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--session",
        type=Path,
        help="Telethon session path (default: $XDG_DATA_HOME/tgsearch/tgsearch)",
    )


def _add_days(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=30, help="look back this many days (default 30)")
    p.add_argument(
        "--include-groups",
        action="store_true",
        help="also read supergroups and groups (default: broadcast channels only)",
    )


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
    _add_session(live)
    _add_common(live)

    auth = sub.add_parser(
        "auth", help="enter API credentials in this terminal and verify the Telegram session"
    )
    auth.add_argument(
        "--reset-credentials",
        action="store_true",
        help="ask for the API ID and hash again even if they are already saved",
    )
    _add_session(auth)

    chats = sub.add_parser(
        "chats", help="list the channels this account belongs to, or the ones with search hits"
    )
    chats.add_argument(
        "queries",
        nargs="*",
        metavar="QUERY",
        help="Telegram search strings; with none, every channel is listed",
    )
    chats.add_argument("--limit", type=int, default=500, help="max global search results per query")
    _add_days(chats)
    _add_session(chats)

    fetch = sub.add_parser(
        "fetch", help="search channels and save matching messages to a local archive"
    )
    fetch.add_argument("queries", nargs="+", metavar="QUERY", help="Telegram search strings")
    where = fetch.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--chat", "-c", dest="chats", action="append", help="@username, t.me link or id"
    )
    where.add_argument(
        "--discover",
        action="store_true",
        help="use every channel that global search finds for the queries",
    )
    fetch.add_argument(
        "--limit", type=int, default=300, help="max messages per chat and query (default 300)"
    )
    fetch.add_argument(
        "--archive", type=Path, help="JSONL file to write (default: under $XDG_DATA_HOME)"
    )
    _add_days(fetch)
    _add_session(fetch)

    models = ("rx7600", "rtx5050", "rx7600xt")

    def study_target(p: argparse.ArgumentParser, gpu: bool) -> None:
        if gpu:
            p.set_defaults(profile="gpu", terms=[], exclude=[], any_term=False)
            p.set_defaults(min_price=None, max_price=None)
        else:
            p.add_argument("terms", nargs="*", metavar="TERM", help="what to look for")
            p.add_argument("--profile", choices=("gpu",), help="built-in profile instead of terms")
            p.add_argument("--any", dest="any_term", action="store_true", help="any term matches")
            p.add_argument(
                "-x", "--exclude", action="append", default=[], metavar="TERM", help="drop TERM"
            )
            p.add_argument("--min-price", type=_price, help="lowest plausible price")
            p.add_argument("--max-price", type=_price, help="highest plausible price")
        p.add_argument(
            "--model", action="append", choices=models, help="GPU model (with the gpu profile)"
        )

    for name, gpu, help_text in (
        ("scan", False, "find offers for any terms in your chats and save them to an archive"),
        ("gpu-fetch", True, "scan with the GPU profile (RX 7600 / RTX 5050)"),
    ):
        p = sub.add_parser(name, help=help_text)
        study_target(p, gpu)
        p.add_argument(
            "--query",
            dest="queries",
            action="append",
            metavar="QUERY",
            help="Telegram search string to send (default: from the terms or profile)",
        )
        p.add_argument("--days", type=int, default=30, help="look back this many days")
        p.add_argument("--channels-only", action="store_true", help="skip supergroups and groups")
        p.add_argument("--limit", type=int, default=1000, help="max messages per chat and query")
        p.add_argument(
            "--discover-limit", type=int, default=1000, help="max global search results per query"
        )
        p.add_argument("--archive", type=Path, help="JSONL file to write")
        _add_session(p)

    for name, gpu, help_text in (
        ("report", False, "analyse archived offers: prices per payment method, trends, HTML"),
        ("gpu-report", True, "report with the GPU profile"),
    ):
        p = sub.add_parser(name, help=help_text)
        if gpu:
            p.add_argument("archives", nargs="+", type=Path, metavar="ARCHIVE", help="JSONL file")
        else:  # positional terms, so the archives go in an option
            p.add_argument(
                "-a",
                "--archive",
                dest="archives",
                type=Path,
                action="append",
                required=True,
                help="JSONL archive written by scan (repeatable)",
            )
        study_target(p, gpu)
        p.add_argument("--days", type=int, default=30, help="window ending now (default 30)")
        p.add_argument("--since", type=_date, help="window start (overrides --days)")
        p.add_argument("--until", type=_date, help="window end, inclusive (default now)")
        p.add_argument("--out", type=Path, help="report directory")
    watch = sub.add_parser("watch", help="check selected chats once and send a private GPU digest")
    watch.add_argument(
        "--chat", action="append", required=True, help="group or channel, repeatable"
    )
    watch.add_argument("--state", type=Path, required=True, help="persistent SQLite database")
    _add_session(watch)
    wlive = sub.add_parser("watch-live", help="stream selected chats in real time (1-3s delay)")
    wlive.add_argument(
        "--chat", action="append", required=True, help="group or channel, repeatable"
    )
    wlive.add_argument("--state", type=Path, required=True, help="persistent SQLite database")
    _add_session(wlive)
    menu = sub.add_parser("promo-menu", help="serve the private PromoBot configuration menu")
    menu.add_argument("--state", type=Path, required=True, help="persistent SQLite database")
    menu.add_argument("--sources", type=Path, help="file with monitored channel IDs")
    _add_session(menu)
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
        interactive=sys.stdin.isatty(),
    )


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _cmd_auth(args: argparse.Namespace) -> int:
    from . import credentials
    from .sources.live import check_session

    if not sys.stdin.isatty():
        print("tgsearch: auth must run in an interactive terminal", file=sys.stderr)
        return 2
    path = credentials.credentials_path()
    creds = (
        None
        if args.reset_credentials
        else (credentials.credentials_from_env() or credentials.load_saved(path))
    )
    entered = creds is None
    if entered:
        creds = credentials.prompt()
    counts = check_session(args.session, interactive=True, creds=creds)
    if entered:
        credentials.save(creds, path)
        print(f"Credentials saved to {path} (mode 0600).")
    print(
        "Session is authorised. Dialogs: "
        f"{counts['channel']} channels, {counts['supergroup']} supergroups, "
        f"{counts['group']} groups, {counts['user']} private chats (not read)."
    )
    return 0


def _cmd_chats(args: argparse.Namespace) -> int:
    from .sources.live import discover_chats, list_chats

    out = sys.stdout
    if not args.queries:
        for chat in list_chats(args.session, include_groups=args.include_groups):
            name = f"@{chat.username}" if chat.username else ""
            print(f"{chat.id}\t{chat.kind}\t{name}\t{chat.title}", file=out)
        return 0
    hits = discover_chats(
        args.queries,
        since=_since(args.days),
        limit=args.limit,
        include_groups=args.include_groups,
        session=args.session,
    )
    print("hits\tlatest\tid\tkind\tusername\ttitle\tper_query", file=out)
    for hit in hits:
        chat = hit.chat
        latest = hit.latest.date().isoformat() if hit.latest else ""
        per = ";".join(f"{q}={n}" for q, n in hit.hits.items())
        name = f"@{chat.username}" if chat.username else ""
        print(f"{hit.total}\t{latest}\t{chat.id}\t{chat.kind}\t{name}\t{chat.title}\t{per}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    from .archive import default_archive_dir, write_archive
    from .sources.live import discover_and_fetch, fetch_messages

    since = _since(args.days)
    if args.discover:
        hits, messages, _ = discover_and_fetch(
            args.queries,
            since=since,
            limit=args.limit,
            include_groups=args.include_groups,
            session=args.session,
        )
        print(f"global search found {len(hits)} chats with hits", file=sys.stderr)
    else:
        messages = fetch_messages(
            args.chats,
            args.queries,
            any_term=True,
            limit=args.limit,
            since=since,
            session=args.session,
        )
    path = args.archive or (
        default_archive_dir() / f"fetch-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    )
    count = write_archive(messages, path)
    chats = len({m.chat_id or m.chat for m in messages})
    print(f"wrote {count} messages from {chats} chats to {path}", file=sys.stderr)
    return 0


def _profile(args: argparse.Namespace):
    from .profiles import gpu_profile, terms_profile

    if args.profile == "gpu":
        return gpu_profile(args.model or ("rx7600", "rtx5050"))
    if args.model:
        raise ValueError("--model needs --profile gpu")
    plausible = None
    if args.min_price is not None or args.max_price is not None:
        plausible = (args.min_price or Decimal("0.01"), args.max_price or Decimal("1e12"))
    return terms_profile(
        args.terms, exclude=args.exclude, any_term=args.any_term, plausible=plausible
    )


def _cmd_scan(args: argparse.Namespace) -> int:
    from .archive import default_archive_dir, write_archive
    from .profiles import mentions, offer_filter
    from .sources.live import discover_and_fetch

    profile = _profile(args)
    hits, messages, stats = discover_and_fetch(
        args.queries or profile.queries,
        since=_since(args.days),
        limit=args.limit,
        discover_limit=args.discover_limit,
        include_groups=not args.channels_only,
        discover_accept=mentions(profile),
        accept=offer_filter(profile),
        session=args.session,
    )
    path = args.archive or (
        default_archive_dir() / f"{profile.name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    )
    write_archive(messages, path)
    print(
        f"{len(hits)} chats with hits; read {stats.read} search results, kept "
        f"{stats.kept} offer-like posts, dropped {stats.dropped} without storing them",
        file=sys.stderr,
    )
    print(path)
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from .analysis import analyse, write_report
    from .archive import read_archive

    profile = _profile(args)
    now = datetime.now(timezone.utc)
    since = args.since or now - timedelta(days=args.days)
    until = args.until + timedelta(days=1) if args.until else now
    # Overlapping archives hold the same posts; keep one copy, the latest fetched.
    unique = {}
    for message in chain.from_iterable(read_archive(p) for p in args.archives):
        unique[(message.chat_id or message.chat, message.message_id)] = message
    analysis = analyse(unique.values(), profile=profile, since=since, until=until)
    out = (
        args.out
        or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        / "tgsearch"
        / "reports"
        / f"{profile.name}-{now:%Y%m%dT%H%M%SZ}"
    )
    data = write_report(analysis, out)
    counts = data["counts"]
    print(
        f"{counts['target_posts']} target posts: "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts["status"].items())),
        file=sys.stderr,
    )
    print(out / "report.html")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from asyncio import run

    from .watch import run as watch

    run(watch(args.chat, args.state, args.session))
    return 0


def _cmd_watch_live(args: argparse.Namespace) -> int:
    from asyncio import run

    from .watch import run_live

    run(run_live(args.chat, args.state, args.session))
    return 0


def _cmd_promo_menu(args: argparse.Namespace) -> int:
    import os
    import re

    from .credentials import LiveConfigError
    from .promo_delivery import Bot
    from .promo_menu import run as run_menu

    token = os.getenv("TGSEARCH_PROMOBOT_TOKEN", "")
    chat_id = os.getenv("TGSEARCH_BOT_CHAT_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(
        r"[1-9][0-9]*", chat_id
    ):
        raise LiveConfigError("Configure TGSEARCH_PROMOBOT_TOKEN e TGSEARCH_BOT_CHAT_ID.")
    run_menu(args.state, Bot(token, int(chat_id)), sources_file=args.sources, session=args.session)
    return 0


_LIVE_COMMANDS = {
    "auth": _cmd_auth,
    "chats": _cmd_chats,
    "fetch": _cmd_fetch,
    "scan": _cmd_scan,
    "gpu-fetch": _cmd_scan,
    "watch": _cmd_watch,
    "watch-live": _cmd_watch_live,
    "promo-menu": _cmd_promo_menu,
}


def _run_live_command(args: argparse.Namespace) -> int:
    from .credentials import LiveConfigError

    try:
        return _LIVE_COMMANDS[args.command](args)
    except LiveConfigError as exc:
        print(f"tgsearch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("tgsearch: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # Telethon RPC errors; never print request payloads
        if type(exc).__module__.startswith("telethon"):
            print(f"tgsearch: Telegram error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "limit", 1) <= 0:
        parser.error("--limit must be positive")
    if getattr(args, "days", 1) <= 0:
        parser.error("--days must be positive")
    if args.command in _LIVE_COMMANDS:
        try:
            return _run_live_command(args)
        except ValueError as exc:
            print(f"tgsearch: {exc}", file=sys.stderr)
            return 2
    if args.command in ("report", "gpu-report"):
        try:
            return _cmd_report(args)
        except (OSError, ValueError) as exc:
            print(f"tgsearch: {exc}", file=sys.stderr)
            return 1

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

    from .credentials import LiveConfigError

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
