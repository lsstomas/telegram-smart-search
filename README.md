# telegram-smart-search

A local command-line tool that searches Telegram deal/offer messages and pulls out structured
pricing data: current price, original ("de") price, Pix price, installments, interest-free
flag, payment methods, coupon code and product link. It merges reposted offers conservatively
and writes everything to CSV.

On top of that it runs price studies for anything your deal channels post (`scan` and
`report`). A study finds the chats that post the product, splits each offer's price by
payment method, flags duplicates, and looks for when prices drop: weekdays, time of month,
promotion dates, coupons, stores. It reports means, medians and percentiles in a local HTML
page, with an audit trail in CSV. A built-in GPU profile (`gpu-fetch` and `gpu-report`) adds
strict rules for the Radeon RX 7600 and the GeForce RTX 5050.

Everything runs on your machine. Message text is processed only by local rules (regular
expressions and decimal arithmetic). No AI model and no third-party service sees it. There are
two ways to get messages in:

| Mode | Source | Network | Extra deps |
|------|--------|---------|-----------|
| `export` | Telegram Desktop "Export chat history" JSON (`result.json`) | none | none |
| `live`, `chats`, `fetch`, `scan`, `gpu-fetch` | Your own account through Telethon (MTProto) | Telegram only | `telethon` |

## Install

Requires Python 3.10+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'        # export mode + tests
pip install -e '.[live,dev]'   # also enables the live commands
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
2. Run `tgsearch auth` **in an interactive terminal**. It asks for the API ID and the API hash
   (hidden input), connects, and logs in if the session is new: Telethon asks for your phone
   number, the login code and the 2FA password in that terminal. It prints only how many
   dialogs of each kind the account has, never names. When the connection works, the ID and
   hash are saved to `$XDG_CONFIG_HOME/tgsearch/credentials.json` (default `~/.config`) with
   `0600` permissions, so later commands run without asking. `--reset-credentials` asks
   again. The environment variables `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` still take
   precedence, if you prefer not to save anything.
3. Search one or more chats you already belong to:

   ```bash
   tgsearch live -c @somechannel -c https://t.me/otherchannel notebook --limit 300 -o out.csv
   ```

Only `auth` (and `live` in an interactive terminal) can start a phone login. Every other
command fails with a pointer to `tgsearch auth` when the session is not logged in, so scripts
never block on a prompt.

The session is stored at `$XDG_DATA_HOME/tgsearch/tgsearch.session` (default
`~/.local/share/tgsearch/`), **outside the project**, in a `0700` directory. Override it with
`--session PATH` or `TGSEARCH_SESSION`. A session file gives full access to your account, so
treat it like a password. Don't use the same session file on two machines at the same time:
Telegram may revoke it (`AuthKeyDuplicatedError`), and you'd have to log in again.

Telegram's server-side search takes a single string. For AND queries (the default), the tool
sends the longest term and checks the rest locally. With `--any`, it searches each term
separately and merges the results by message id. `--limit` applies per chat and per term.

### Finding and archiving channels

```bash
tgsearch chats                          # every broadcast channel you belong to
tgsearch chats 7600 RTX5050 --days 30   # chats with hits, via Telegram's global search
tgsearch fetch 7600 5050 --discover --days 30 --archive ~/gpu.jsonl
```

`chats QUERY...` uses Telegram's global search over the chats you are in and prints hits per
chat and query. `fetch` searches each chat again on its own, because global search results
can be truncated. It writes the messages to a JSON Lines archive with `0600` permissions,
under `$XDG_DATA_HOME/tgsearch/archives/` by default, so the analysis can be re-run offline.
Private conversations are never read. Groups are left out unless you pass `--include-groups`.

### Price studies for any product

```bash
tgsearch scan "air fryer" -x usada --days 90          # prints the archive path
tgsearch report "air fryer" -x usada -a ARCHIVE.jsonl --days 90 --min-price 150
```

`scan` turns the terms into Telegram searches (with AND, the longest term is sent and the
rest is checked locally; with `--any`, every term is sent), discovers the chats with hits,
and reads each one. `--query` overrides the strings sent to Telegram, for spellings the terms
don't cover. Every message goes through a **local filter before anything is stored**. To be
kept, a message must match the terms and carry an amount. Outside broadcast channels, where
ordinary members write, it must also link to a store. Everything else is dropped in memory and
only counted. Supergroups are included because many deal feeds are supergroups;
`--channels-only` skips them.

`report` reads one or more archives (`-a`, repeatable; overlapping archives are merged by
chat and message id). `--min-price` / `--max-price` set the plausible band, so accessories
and typos stay out of the statistics. It writes to
`$XDG_DATA_HOME/tgsearch/reports/<profile>-<time>/` (or `--out`), all files `0600`:

| File | Contents |
|------|----------|
| `report.html` | Self-contained page (no scripts, no network): key prices, daily-low chart, cheapest distinct deals, weekday / time-of-month / promotion-date / coupon / store / brand tables |
| `summary.md` | Mean, median, min and max per target and payment method; per-store table; exclusion counts |
| `summary.json` | All of the above plus the trend data, machine-readable |
| `posts.csv` | One row per post: status (`included`, `duplicate`, `excluded`), reason, the original it duplicates and how many hours later, probable-repost group, store, the price used for each method, coupons |
| `price_points.csv` | Every amount found, with its message line, how it was read and whether it entered the statistics |

**When prices drop.** Trends use each counted post's lowest single-payment price, on Brazilian
calendar days (America/Sao_Paulo):

- **Daily low:** the cheapest post of each day.
- **Weekday and time of month** (1–7, 8–14, 15–21, 22–end): median daily low, best price,
  and how many days reached the *good price* line, the 25th percentile of distinct deals.
- **Promotion dates, ±1 day:** double dates (9.9, 10.10, 11.11), Consumer Day, Customer Day,
  Children's Day, Black Friday and Cyber Monday.
- **Price thresholds:** for each R$ 50 step, on how many days a post reached it and the
  longest stretch without one, which is how long you might wait.
- **Coupons:** price stated as needing the coupon, coupon code mentioned, or no coupon.
- **Stores and brands.**

Deal channels post when a price looks good, so a quiet day means no post, not a high price.

### Scheduled Telegram alerts (PromoBot)

The `watch` and `promo-menu` commands turn any VPS or always-on machine into a personal
Telegram deal monitor. Anyone can deploy their own instance using their own Telegram
account (read-only via Telethon) and their own bot created with `@BotFather`:

- **Private configuration menu (`tgsearch promo-menu`)**: manage alerts directly inside
  your private chat with the bot (**Novo alerta** and **Meus alertas** — set product
  name and price ceiling, edit ceilings, pause or reactivate). Changes are stored in the
  shared SQLite database and persist across restarts.
- **Periodic offer polling (`tgsearch watch`)**: polls only the groups and channels
  passed via `--chat`. On the first run it records the latest message ID per chat and
  tracks new posts from that point forward.
- **Smart deduplication & editable notifications**:
  - One formatted message per offer (product, store, optional seller, price, payment
    method, installments/interest when stated, coupon, confirmed shipping, **Abrir
    oferta** button, and source post links).
  - Same product, store, price, and coupon within **1 hour** edits the original message
    to append the new source post link (restarting the 1-hour sliding window).
  - Reprocessing an already-seen post never triggers a duplicate alert.
  - Different store, different coupon, confirmed shipping change, confirmed restock, or a
    price drop of at least **R$ 10** sends a new alert.

#### Quick setup

1. Install with `pip install -e '.[live]'`. Run `tgsearch auth` interactively to save your
   Telegram API credentials and session, then list your channels/groups with
   `tgsearch chats --include-groups`. Use `@username` when available, or
   `-(1000000000000 + ID)` for a channel/supergroup (`-ID` for a basic group).
2. Create a bot with `@BotFather`, open its private chat, and tap **Start** (`/start`).
   Find your numeric private `chat_id` via `getUpdates` (or your own Telethon user ID)
   and save the credentials in a private `0600` environment file outside the repository:

   ```sh
   TGSEARCH_PROMOBOT_TOKEN=123456:your-bot-token
   TGSEARCH_BOT_CHAT_ID=123456789
   ```

   *(If you only set `TGSEARCH_BOT_TOKEN` instead of `TGSEARCH_PROMOBOT_TOKEN`, `watch`
   runs in legacy digest mode.)*

3. Start the interactive menu daemon (e.g. as a `systemd --user` service):

   ```sh
   tgsearch promo-menu --state ~/.local/share/tgsearch-watch/watch.sqlite3
   ```

4. Schedule `tgsearch watch` every 2 minutes (via `systemd --user` timer or cron),
   passing the same `--state` SQLite file and your `--chat` sources:

   ```sh
   tgsearch watch \
       --chat @example_deal_channel --chat=-1001234567890 \
       --state ~/.local/share/tgsearch-watch/watch.sqlite3
   ```

   Set `TGSEARCH_SESSION` if you want a dedicated Telethon session file. In the bot's
   private chat, send `/menu` to add your first alert.

### GPU profile (RX 7600 and RTX 5050)

```bash
tgsearch gpu-fetch --days 30                         # or --model rx7600 --days 90
tgsearch gpu-report ~/.local/share/tgsearch/archives/gpu-*.jsonl --days 30
```

`gpu-fetch` and `gpu-report` are `scan` and `report` with the GPU profile
(`--profile gpu`). The profile searches for `7600`, `RX7600`, `5050` and `RTX5050`, keeps
amounts between R$ 700 and R$ 6,000, names the card maker, and applies strict product rules.
`RX 7600 XT` is a different card and is not counted as an RX 7600 (target it with
`--model rx7600xt`). `Ryzen 5 7600` is a CPU and is ignored. Posts for notebooks, complete PCs
and CPU/motherboard combos are excluded (`category_laptop`, `category_prebuilt`,
`category_bundle`), as are posts that name another GPU (`other_gpu_named`: list posts and
comparisons) and posts with no recognisable card wording (`category_unknown`).

**Payment methods.** Each amount is labelled from its own line:

| Method | Example |
|--------|---------|
| `pix` | `R$ 1.599 no Pix`, `Pix: R$ 1.599` |
| `boleto` | `R$ 1.599 no boleto` |
| `card_1x` | `Cartão: R$ 1.699`, `💳 R$ 1.699` |
| `installments` | `R$ 1.799 em até 10x sem juros` (stated total), `10x de R$ 179,90` (computed total 1799.00) |
| `upfront` | `R$ 1.699 à vista` with no method named |
| `unspecified` | `💵 R$ 1.799`, with no payment cue at all |

Installments are reported as totals and split into interest-free, with interest (`com juros`,
`1,99% a.m.`, the rate is kept) and interest not stated. Two cross-line rules apply, and only
when the post has a single amount. If the only payment method the post names sits on a line
without an amount (`🎟 com cupom X e pagando no pix`), the amount gets that method
(`stated_cue_apart`). A lone `10x sem juros` line adds an interest-free installment option
at that amount (`price_with_terms`).

Amounts after `cupom de`, `off`, `desconto`, `cashback` or `frete` are discounts, not prices.
`com cupom: R$ X` is a price that needs the coupon and is flagged `coupon_applied`. A post with
two different amounts for the same method is left out for that method (`conflicting_amounts`),
and amounts outside R$ 700–6,000 are left out (`implausible_amount`). Nothing is guessed
silently: every rule leaves its mark in the CSVs. These rules apply to every study, not
only GPUs.

**Duplicates.** Two strengths, so that distinct offers are never merged:

- *Proven duplicates* are merged and counted once. They are the same message read twice,
  forwards of the same channel post, or the same normalised text or the same canonical URL
  **with the same price signature** (every method and amount, plus coupon codes). A price
  change, another coupon or another payment method keeps both posts.
- *Probable reposts* are grouped, never merged. They share the model, the store, every price
  and the coupons, and each was posted within 72 hours of the previous one in the group, with
  different wording or affiliate links. The report shows statistics per post and per deal
  (probable groups collapsed), so you can see how much reposting weighs on the numbers.

### Options (export and live)

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

## CSV columns (export and live)

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
The GPU report CSVs follow the same rules.

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

## Deduplication in export and live (conservative)

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
pytest           # fixtures and a fake Telegram client only: no network, no real credentials
ruff check . && ruff format --check .
```

Layout:

```
src/tgsearch/
  cli.py          entry point: export, live, auth, chats, fetch, scan, report, watch, promo-menu
  extract.py      price / installment / payment / coupon extraction
  dedupe.py       conservative merging
  search.py       query matching, filters, pipeline
  csv_out.py      CSV writer (formula-injection safe)
  textutil.py     normalisation, URL canonicalisation
  credentials.py  API ID / hash: environment, then the 0600 file written by `auth`
  archive.py      local JSON Lines archive of fetched messages (0600)
  profiles.py     study profiles (any terms, or gpu) and the pre-storage offer filter
  gpu.py          GPU model matching, card vs laptop / PC, card makers
  payment.py      prices per payment method, installments, interest, coupons
  analysis.py     studies: duplicates, probable reposts, statistics, best deals, report files
  trends.py       daily lows, weekdays, time of month, promotion dates, thresholds
  report_html.py  self-contained HTML report (inline SVG, no scripts)
  watch.py        periodic channel monitor and offer matcher
  promo_delivery.py  editable PromoBot alerts, 1h sliding dedup and outbox
  promo_menu.py   private Telegram inline menu for managing alerts
  sources/export.py  Telegram Desktop JSON reader
  sources/live.py    Telethon reader: dialogs, global search, per-chat search
tests/            pytest suite and JSON fixtures
```

## Privacy and secrets

- Credentials are typed into your own terminal (the hash without echo), never passed as
  arguments, which would end up in shell history. They are saved only by `tgsearch auth`, to
  a `0600` file outside the project, and refused if anyone else can read that file. Their
  `repr` is masked.
- No `.env` file is loaded.
- Sessions, archives and reports live under `~/.local/share/tgsearch/` and
  `~/.config/tgsearch/`, never in the project. `.gitignore` also excludes `*.session`,
  `.env*`, `exports/`, `result.json`, `*.csv`, `*.jsonl` and `out/` in case something is
  written there by hand.
- Access is read-only and limited to chats the account already belongs to: no joins, posts or
  forwards. Private chats are never read. Searches go through Telethon, which waits out
  Telegram's flood limits instead of retrying aggressively.
- The tool doesn't phone home. The only network traffic is to Telegram, and only in the live
  commands.

## Limitations

- Prices are read from what the post says. Coupon-dependent prices are flagged but not
  verified, and short links (`amzn.to`, `s.shopee.com.br`) are never resolved, so store
  detection falls back to store names in the text.
- Most deal posts quote one price with no payment method (`💵 R$ 1.799`). Those stay
  `unspecified` rather than being assumed to be Pix.
- Telegram's search is word-based: `RX7600` and `RX 7600` are different queries, so both are
  sent. A spelling no query covers (say `R X 7600`) is missed.
- Probable-repost grouping needs a store, so posts with unidentified stores are never grouped.
- How far back you can go depends on the chats: broadcast channels expose their full history,
  but some groups hide messages from before you joined. Posting volume also changes over time,
  so check how many days have posts before comparing periods.
- Weekday and promotion-date tables cover a few occurrences each (13 Mondays in 90 days), so
  read them as hints, not rules.
- Edits made after the fetch are not seen until you fetch again. Appending a newer fetch to an
  archive keeps the latest version of each post.
