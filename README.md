# telegram-smart-search

A local command-line tool that searches Telegram deal/offer messages and pulls out structured
pricing data: current price, original ("de") price, Pix price, installments, interest-free
flag, payment methods, coupon code and product link. It merges reposted offers conservatively
and writes everything to CSV.

Everything runs on your machine. There are two ways to get messages in:

| Mode | Source | Network | Extra deps |
|------|--------|---------|-----------|
| `export` | Telegram Desktop "Export chat history" JSON (`result.json`) | none | none |
| `live` | Your own account through Telethon (MTProto) | Telegram only | `telethon` |

## Install

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'        # export mode + tests
pip install -e '.[live,dev]'   # also enables live mode
```

You can also run it without installing: `PYTHONPATH=src python -m tgsearch ...`

## Usage

### Offline, from an export

In Telegram Desktop: open the channel → ⋮ → *Export chat history* → format **JSON**.
(Or *Settings → Advanced → Export Telegram data* for every chat at once; both layouts work.)

```bash
tgsearch export -f ~/Downloads/ChatExport/result.json notebook -o notebooks.csv
tgsearch export -f a/result.json -f b/result.json "air fryer" --max-price 400 --sort price
tgsearch export -f result.json ssd hd --any -x usado --since 2026-10-01 --delimiter ';'
```

### Live, from your account

1. Create an API app at <https://my.telegram.org> → *API development tools*.
2. Put the values in your environment (never in this repo):

   ```bash
   export TELEGRAM_API_ID=...
   export TELEGRAM_API_HASH=...
   ```

3. Search one or more chats you already belong to:

   ```bash
   tgsearch live -c @somechannel -c https://t.me/otherchannel notebook --limit 300 -o out.csv
   ```

The first run asks for your phone number and login code (Telethon's interactive login). The
session is stored at `$XDG_DATA_HOME/tgsearch/tgsearch.session` (default
`~/.local/share/tgsearch/`), **outside the project**, in a `0700` directory. Override it with
`--session PATH` or `TGSEARCH_SESSION`. A session file gives full access to your account, so
treat it like a password.

Telegram's server-side search takes a single string. For AND queries (the default), the tool
sends the longest term and checks the rest locally. With `--any`, it searches each term
separately and merges the results by message id. `--limit` applies per chat and per term.

### Options (both modes)

| Option | Meaning |
|--------|---------|
| `TERM ...` | Words or quoted phrases. Matching ignores case and accents and matches from the start of a word (`note` matches *Notebook* but not *Keynote*). No terms = every message. |
| `--any` | Match messages with any term instead of all of them |
| `-x / --exclude TERM` | Drop messages containing TERM (repeatable) |
| `--min-price`, `--max-price` | Filter on `best_price`; accepts `1299`, `1.299,90`, `1,299.90` |
| `--since`, `--until` | Date range (`YYYY-MM-DD`; `--until` includes that whole day) |
| `--include-unpriced` | Keep matching messages where no price was found |
| `--no-dedupe` | Write every matching message |
| `--sort date\|price` | Newest first (default) or cheapest first |
| `-o FILE` | Write to FILE instead of stdout |
| `--delimiter ';'` | Use `;` for spreadsheets set to a pt-BR locale, or `tab` |
| `-q` | Don't print the summary line on stderr |

## CSV columns

| Column | Description |
|--------|-------------|
| `date` | Message time (ISO 8601, UTC) |
| `chat`, `message_id`, `link` | Where the offer came from. Links use `t.me/<user>/<id>`, or `t.me/c/<id>/<msg>` for private channels (members only). |
| `title` | First line of text, with leading emoji and symbols removed |
| `price` | Headline price ("por R$ X", or the first plain price) |
| `original_price` | The "De R$ X" price, kept only when it is higher than `price` |
| `pix_price` | Price marked as Pix / "à vista no Pix" |
| `best_price` | The lower of `price` and `pix_price`, used for filters and sorting |
| `currency` | `BRL`, `USD`, `EUR`, `GBP` |
| `installments`, `installment_value` | e.g. `10` and `279.90` for "10x de R$ 279,90" |
| `interest_free` | `yes` if "sem juros" appears |
| `payment_methods` | `;`-separated: `pix`, `boleto`, `credit_card`, `debit_card`, `paypal`, `mercado_pago`, `picpay` |
| `coupon` | Code after "cupom", "código" or "code" (uppercased) |
| `url` | First product link: hidden text-link URLs first, then plain URLs, skipping `t.me` links |
| `duplicates`, `also_seen` | How many reposts were merged into this row, and where they appeared |
| `text` | Full message text on one line |

Amounts use a dot as the decimal separator (`2799.00`). Cells that begin with `= + - @` get a
leading `'` so spreadsheets won't run them as formulas, since message text comes from strangers.

### How prices are classified

Each amount is labelled from the words just before and after it, on the same line:

- `de` / `antes` / `era` → original price, unless the phrase is "a partir de", which is a
  "from" price.
- `por` / `apenas` / `só` / `sai` / `agora` → current price.
- `pix` nearby ("no Pix", "à vista no Pix", "Pix: R$") → Pix price.
- `desconto`, `off`, `cashback`, `frete`, `cupom`, `acima de`, `em compras` → ignored. These are
  discounts or thresholds, not prices.
- Amounts inside an installment ("10x de R$ 279,90") never become `price`.
- Text like "2x1" or "4x4" is not read as installments unless it has a value, "sem juros", or
  a lead-in like "em até".

## Deduplication (conservative)

Deal channels repost the same offer all the time, so rows are merged, but only when the
evidence is strong:

1. **Same message** (same chat and id, e.g. from overlapping exports): kept once, not counted.
2. **Same deal**: same `best_price`, currency **and** coupon, **plus** either
   - the same canonical URL (lowercase host, no `www.`, no `#fragment`, sorted query, http/https
     treated the same, tracking parameters like `utm_*`, `fbclid`, `gclid` removed), or
   - the same message text after normalisation (case, accents, punctuation and URLs ignored;
     at least 20 characters).

Things that are **not** merged on purpose:

- Same link at a different price: a price drop is new information.
- Different coupon codes.
- Different affiliate parameters (`tag=`, `ref=` and so on), which can point to different
  listings.
- Short links (`amzn.to`, `bit.ly`) are compared as-is, because resolving them would need
  network access.
- Messages that have a URL but no price.
- "Similar" text: there is no fuzzy matching.

The earliest message is kept. Later copies increase `duplicates` and are listed in
`also_seen`. Use `--no-dedupe` to see every row.

## Development

```bash
pytest           # tests use fixtures only and never touch the network or real credentials
ruff check . && ruff format --check .
```

Layout:

```
src/tgsearch/
  cli.py          argparse entry point (tgsearch export|live)
  extract.py      price / installment / payment / coupon extraction
  dedupe.py       conservative merging
  search.py       query matching, filters, pipeline
  csv_out.py      CSV writer (formula-injection safe)
  textutil.py     normalisation, URL canonicalisation
  sources/export.py  Telegram Desktop JSON reader
  sources/live.py    Telethon reader (lazy import, env-only credentials)
tests/            pytest suite and JSON fixtures
```

## Privacy and secrets

- Credentials are read only from environment variables, at runtime. The tool never writes them,
  and their `repr` is masked.
- No `.env` file is loaded. Use your shell, a password manager or `direnv` outside the repo.
- `.gitignore` excludes `*.session`, `.env*`, `exports/`, `result.json` and `*.csv`, because
  exports and results can contain private messages.
- The tool doesn't phone home. The only network traffic is to Telegram, and only in `live`
  mode.
