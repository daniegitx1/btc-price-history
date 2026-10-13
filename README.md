# BTC daily price history

A versioned, automatically updated record of Bitcoin's daily OHLCV price in **USD** and **ZAR**.

A GitHub Actions workflow runs every day at 00:17 UTC. It fetches every completed UTC day that is missing from the CSVs, validates the data, cross-checks the USD close against a second exchange, and commits the result to this repository. If runs are missed (an outage, or the schedule being paused), the next run fills the whole gap by itself. Every change is a git commit, so the full history of the data is auditable.

## Data files

| File | Contents | Source | From |
|---|---|---|---|
| `data/btc_usd_daily.csv` | BTC/USD daily candles | Bitstamp | 2011-08-18 |
| `data/btc_zar_daily.csv` | BTC/ZAR daily candles | VALR | 2019-06-11 |
| `data/anomalies.csv` | Cross-check mismatches and unfillable gaps | — | — |

### Price file columns

| Column | Meaning |
|---|---|
| `date_utc` | UTC calendar day, `YYYY-MM-DD`. The candle covers 00:00:00–23:59:59 UTC. |
| `open`, `high`, `low`, `close` | Prices in the quote currency (USD or ZAR), exactly as the exchange reported them |
| `volume` | Volume traded on that exchange that day, **in BTC** |
| `source` | Exchange that supplied the row (`bitstamp`, `valr`) |
| `fetched_at_utc` | When the collector fetched the row (ISO 8601, UTC) |

Rows are one per UTC day, ascending, with no duplicates. Only completed days are stored; today's still-open candle never is. The files are **append-only**: once a row is written it is never changed, except by an explicit rebuild (see below).

### Anomaly file columns

`date_utc, series, kind, primary_source, primary_close, check_source, check_close, pct_diff, detail, detected_at_utc`

- `kind = close_mismatch`: the Bitstamp close differs from the Coinbase close by more than 2%. The Bitstamp row is still stored. `pct_diff = |bitstamp − coinbase| / coinbase × 100`.
- `kind = gap`: the source had no data for one or more days, and later days were stored anyway. `detail` gives the missing range.

## Data sources and caveats

All sources are public endpoints that need no API key. Each request sends a descriptive `User-Agent` and is throttled well inside the exchange's published rate limits.

- **Bitstamp, primary USD source.** `GET https://www.bitstamp.net/api/v2/ohlc/btcusd/?step=86400&limit=1000&start=…&exclude_current_candle=true`. A request returns at most 1000 candles, so the backfill pages in 1000-day windows. Published limit: 10,000 requests per 10 minutes.
  - On days with no trades (33 days, all between 2011 and early 2015), Bitstamp repeats the previous close as O=H=L=C with `volume = 0`. Those rows are kept as Bitstamp reports them.
  - Early history (2011–2013) reflects one small exchange with thin volume, not a global market price.
- **Coinbase Exchange, USD cross-check only, never stored.** `GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400&start=…&end=…`. At most 300 candles per request; daily history from 2015-07-20, so days before that are not cross-checked.
  - Kraken was considered instead and rejected: its public OHLC endpoint returns only the most recent 720 candles.
  - Differences above 2% between Bitstamp and Coinbase cluster in 2016–2017, when exchange spreads were wide (December 2017 peaked at about 6%). Since then the two agree closely.
- **VALR, ZAR source.** `GET https://api.valr.com/v1/public/BTCZAR/buckets?periodSeconds=86400&startTime=…&endTime=…&limit=300`. At most 300 buckets per request; public limit 30 requests per minute.
  - VALR's BTC/ZAR history starts 2019-06-11. Earlier buckets are all-zero and are not stored.
  - Prices are whole rands as VALR reports them.
  - Luno was considered: its candles endpoint requires an API key, so it is not used.
  - Because VALR has native BTC/ZAR candles, no USD→ZAR FX conversion is needed.

## Validation and failure behaviour

Before anything is written, each series is checked:

- `high ≥ max(open, close)`, `low ≤ min(open, close)`, all prices > 0, volume ≥ 0
- dates strictly ascending, with no duplicates, either within the new data or against the existing file
- no gaps in dates. A day the source cannot supply is logged and recorded in `anomalies.csv`.

Exit codes:

- `0`: everything succeeded.
- `1`: hard failure. A source was unreachable after 6 attempts with exponential backoff (retrying on network errors, HTTP 429 and 5xx, and honouring `Retry-After`), the data failed validation, or a stored CSV is malformed. Nothing is written for the affected series; the other series still runs.
- `2`: data was written, but there were warnings: a gap the source can't fill, the source has not yet published yesterday, or the cross-check exchange was unreachable.

Both `1` and `2` make the workflow run fail, so GitHub emails you. GitHub sends scheduled-workflow failure emails to the user who last edited the `cron` line in the workflow.

## Running locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest                       # tests use saved responses; no network
.venv/bin/python -m collector                    # catch up both series to yesterday (UTC)
```

Options:

```text
python -m collector --series usd               # only one series
python -m collector --rebuild usd              # re-download full USD history and rewrite the file
python -m collector --rebuild all              # both series
python -m collector --data-dir /tmp/data       # write somewhere else
python -m collector --today 2026-01-01         # pretend "today" (testing)
python -m collector -v                         # debug logging
```

### Backfill and rebuild

- **Backfill** happens automatically when a CSV is missing or empty: the collector fetches the full history from the source's earliest date. To backfill again from scratch, delete the CSV and run the workflow.
- **Rebuild** rewrites existing rows. The collector never does this on its own. Use it only if a source has revised its history and you want the new values:
  - **On GitHub:** Actions → *Collect BTC daily prices* → *Run workflow* → choose `usd`, `zar` or `all` under *rebuild*.
  - **Locally:** `python -m collector --rebuild usd`, then commit.

  A rebuild also replaces that series' rows in `anomalies.csv`. The commit diff shows exactly which values changed.

## Scheduling and inactivity

GitHub automatically disables scheduled workflows in **public** repositories after 60 days with no repository activity. Private repositories are not affected. This repo handles that in two ways:

1. **The daily data commit counts as activity.** A new completed day exists every day, so every successful run pushes a commit. Bot commits made with `GITHUB_TOKEN` count as activity; that is exactly how common "keepalive" actions work.
2. **Explicit keepalive.** As a fallback, each scheduled run also calls the GitHub API to re-enable this workflow (`PUT /repos/{owner}/{repo}/actions/workflows/collect.yml/enable`; this is why the workflow has `actions: write`). That covers long stretches where data commits stop, for example a prolonged source outage.

If the schedule is ever disabled anyway, re-enable it under Actions → *Collect BTC daily prices* → *Enable workflow*, then run it once manually. It will catch up all missed days.

## Reading the CSVs from Excel or Power Apps

Raw file URLs (always the latest committed version):

```
https://raw.githubusercontent.com/daniegitx1/btc-price-history/main/data/btc_usd_daily.csv
https://raw.githubusercontent.com/daniegitx1/btc-price-history/main/data/btc_zar_daily.csv
https://raw.githubusercontent.com/daniegitx1/btc-price-history/main/data/anomalies.csv
```

- **Excel:** Data → *From Web* → paste the URL → Load. Use *Refresh* to pull new days.
- **Power Apps / Power Automate:** use an HTTP GET to the raw URL and parse the CSV.
- **Private repositories:** these URLs need authentication. Unauthenticated tools such as Excel's *From Web* cannot read them directly. Either make the repo public, or use an authenticated connector or a personal access token in Power Automate.

## Repository layout

```
collector/            Python package
  http.py             HTTP client: User-Agent, throttling, retries/backoff
  sources.py          Bitstamp, Coinbase, VALR clients (pagination, parsing)
  validation.py       OHLC / duplicate / gap checks
  storage.py          append-only CSV storage, anomalies file
  main.py             entry point and orchestration (python -m collector)
data/                 the CSVs
tests/                pytest suite + saved API responses in tests/fixtures/
.github/workflows/    collect.yml (daily job), tests.yml (CI for code changes)
```
