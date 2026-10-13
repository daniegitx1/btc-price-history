"""Entry point: catch up (or backfill / rebuild) each daily price series, validate, cross-check, write.

Exit codes: 0 = all good, 1 = hard failure (source unreachable, bad data), 2 = written but with warnings
(gaps the source can't fill, source behind, cross-check unavailable). Both non-zero codes fail the workflow.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable

from . import storage
from .http import HttpClient, SourceError
from .sources import DAY, Bitstamp, Candle, Coinbase, JsonGetter, Valr
from .storage import Anomaly, PriceRow, StorageError
from .validation import ValidationError, check_candles, check_existing_dates, missing_ranges

log = logging.getLogger("collector")

ANOMALIES_FILE = "anomalies.csv"
CROSS_CHECK_THRESHOLD_PCT = Decimal("2")

EXIT_OK, EXIT_FAILURE, EXIT_WARNING = 0, 1, 2


@dataclass(frozen=True)
class Series:
    key: str
    filename: str
    source: Callable[[JsonGetter], Bitstamp | Valr]
    cross_check: Callable[[JsonGetter], Coinbase] | None = None


SERIES = {
    "usd": Series("usd", "btc_usd_daily.csv", Bitstamp, Coinbase),
    "zar": Series("zar", "btc_zar_daily.csv", Valr),
}


@dataclass
class Result:
    series: str
    mode: str = ""
    status: str = "ok"  # ok | warning | failed
    written: int = 0
    total: int = 0
    first: str = ""
    last: str = ""
    gaps: list[str] = field(default_factory=list)
    cross_checked: int = 0
    mismatches: int = 0
    messages: list[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        log.warning("%s: %s", self.series, msg)
        self.messages.append(msg)
        if self.status == "ok":
            self.status = "warning"


def _fmt_range(a: dt.date, b: dt.date) -> str:
    return str(a) if a == b else f"{a}..{b}"


def cross_check(candles: list[Candle], checker, series: str, primary: str, now: str) -> tuple[int, list[Anomaly]]:
    reference = {c.date: c for c in checker.fetch(candles[0].date, candles[-1].date)}
    compared, anomalies = 0, []
    for c in candles:
        ref = reference.get(c.date)
        if ref is None:
            continue
        compared += 1
        ours, theirs = Decimal(c.close), Decimal(ref.close)
        pct = abs(ours - theirs) / theirs * 100
        if pct > CROSS_CHECK_THRESHOLD_PCT:
            anomalies.append(Anomaly(
                date_utc=c.date.isoformat(), series=series, kind="close_mismatch",
                primary_source=primary, primary_close=c.close,
                check_source=checker.name, check_close=ref.close,
                pct_diff=f"{pct:.2f}", detected_at_utc=now,
            ))
    return compared, anomalies


def collect(series: Series, *, data_dir: Path, http: JsonGetter, today: dt.date, rebuild: bool, now: str) -> Result:
    result = Result(series.key)
    path = data_dir / series.filename
    anomalies_path = data_dir / ANOMALIES_FILE
    yesterday = today - DAY  # last completed UTC day; today's candle is never stored
    source = series.source(http)

    existing = [] if rebuild else storage.read_prices(path)
    existing_dates = [dt.date.fromisoformat(r.date_utc) for r in existing]
    check_existing_dates(existing_dates, str(path))

    if existing:
        result.mode = "catch-up"
        start = existing_dates[-1] + DAY
    else:
        result.mode = "rebuild" if rebuild else "backfill"
        start = source.earliest
    result.total = len(existing)
    if existing:
        result.first, result.last = existing[0].date_utc, existing[-1].date_utc

    if start > yesterday:
        log.info("%s: up to date through %s, nothing to fetch", series.key, existing_dates[-1])
        return result

    log.info("%s: %s from %s via %s, fetching %s..%s", series.key, result.mode, path.name, source.name, start, yesterday)
    candles = [c for c in source.fetch(start, yesterday) if start <= c.date <= yesterday]
    check_candles(candles)

    # Days the source didn't return. Ranges ending at yesterday mean the source is behind (retried next run);
    # ranges in the middle can't be filled later without --rebuild, so they're recorded as anomalies.
    dates = [c.date for c in candles]
    gap_start = start if existing else (dates[0] if dates else yesterday + DAY)
    new_anomalies: list[Anomaly] = []
    for a, b in missing_ranges(dates, gap_start, yesterday):
        if dates and b < dates[-1]:
            result.gaps.append(_fmt_range(a, b))
            result.warn(f"source {source.name} has no data for {_fmt_range(a, b)}; gap left in the series")
            new_anomalies.append(Anomaly(
                date_utc=a.isoformat(), series=series.key, kind="gap", primary_source=source.name,
                detail=f"missing {_fmt_range(a, b)} ({(b - a).days + 1} day(s))", detected_at_utc=now,
            ))
        else:
            result.warn(f"source {source.name} has no data yet for {_fmt_range(a, b)}; will retry next run")

    if candles and series.cross_check:
        checker = series.cross_check(http)
        try:
            result.cross_checked, mismatches = cross_check(candles, checker, series.key, source.name, now)
        except SourceError as exc:
            result.warn(f"cross-check against {checker.name} unavailable: {exc}")
        else:
            result.mismatches = len(mismatches)
            for m in mismatches:
                log.warning("%s: %s close %s vs %s %s (%s%%)", series.key, m.date_utc, m.primary_close,
                            m.check_source, m.check_close, m.pct_diff)
            new_anomalies += mismatches

    rows = [PriceRow(c.date.isoformat(), c.open, c.high, c.low, c.close, c.volume, source.name, now) for c in candles]
    if existing:
        storage.append_prices(path, rows)
    else:
        storage.write_prices(path, rows)
        dropped = storage.drop_anomalies(anomalies_path, series.key)
        if dropped:
            log.info("%s: cleared %d old anomalies for the rebuilt series", series.key, dropped)
    new_anomalies.sort(key=lambda a: (a.date_utc, a.kind))
    storage.append_anomalies(anomalies_path, new_anomalies)

    result.written = len(rows)
    result.total += len(rows)
    if rows:
        result.first = result.first or rows[0].date_utc
        result.last = rows[-1].date_utc
    log.info("%s: wrote %d row(s) to %s; series now %d rows, %s..%s; %d new anomaly row(s)",
             series.key, len(rows), path.name, result.total, result.first, result.last, len(new_anomalies))
    return result


def summary_markdown(results: list[Result]) -> str:
    lines = [
        "| series | mode | status | rows written | total rows | range | gaps | cross-checked | >2% mismatches |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.series} | {r.mode} | {r.status} | {r.written} | {r.total} | {r.first}..{r.last} | "
                     f"{', '.join(r.gaps) or 'none'} | {r.cross_checked} | {r.mismatches} |")
    for r in results:
        for m in r.messages:
            lines.append(f"\n- **{r.series}**: {m}")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m collector", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--series", nargs="+", choices=sorted(SERIES), default=["usd", "zar"],
                   help="which series to collect (default: usd zar)")
    p.add_argument("--rebuild", nargs="+", choices=[*sorted(SERIES), "all"], default=[],
                   help="re-download the full history of these series and REWRITE their CSVs")
    p.add_argument("--today", type=dt.date.fromisoformat, default=None,
                   help="pretend today (UTC) is this date; for testing")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None, http: JsonGetter | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ")
    logging.Formatter.converter = __import__("time").gmtime
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    now_dt = dt.datetime.now(dt.UTC).replace(microsecond=0)
    today = args.today or now_dt.date()
    now = now_dt.isoformat().replace("+00:00", "Z")
    rebuild = set(SERIES) if "all" in args.rebuild else set(args.rebuild)
    http = http or HttpClient()

    results = []
    for key in args.series:
        try:
            results.append(collect(SERIES[key], data_dir=args.data_dir, http=http, today=today,
                                   rebuild=key in rebuild, now=now))
        except (SourceError, ValidationError, StorageError) as exc:
            log.error("%s: FAILED, nothing written for this series: %s", key, exc)
            results.append(Result(key, mode="rebuild" if key in rebuild else "", status="failed", messages=[str(exc)]))

    summary = summary_markdown(results)
    print("\n" + summary)
    if step_summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("## BTC daily price collection\n\n" + summary)

    if any(r.status == "failed" for r in results):
        return EXIT_FAILURE
    if any(r.status == "warning" for r in results):
        return EXIT_WARNING
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
