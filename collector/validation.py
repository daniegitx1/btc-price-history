"""Checks run before anything is written."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .sources import DAY, Candle


class ValidationError(Exception):
    """Data failed validation; nothing for that series is written."""


def _dec(value: str) -> Decimal:
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(f"not a number: {value!r}") from exc
    if not d.is_finite():
        raise ValidationError(f"not a finite number: {value!r}")
    return d


def check_candles(candles: Iterable[Candle]) -> None:
    """OHLC consistency, positive prices, non-negative volume, ascending unique dates."""
    problems: list[str] = []
    previous: dt.date | None = None
    for c in candles:
        o, h, l, cl, v = (_dec(x) for x in (c.open, c.high, c.low, c.close, c.volume))
        if min(o, h, l, cl) <= 0:
            problems.append(f"{c.date}: non-positive price (o={c.open} h={c.high} l={c.low} c={c.close})")
        if h < max(o, cl):
            problems.append(f"{c.date}: high {c.high} below max(open {c.open}, close {c.close})")
        if l > min(o, cl):
            problems.append(f"{c.date}: low {c.low} above min(open {c.open}, close {c.close})")
        if v < 0:
            problems.append(f"{c.date}: negative volume {c.volume}")
        if previous is not None and c.date <= previous:
            problems.append(f"{c.date}: duplicate or out-of-order date (after {previous})")
        previous = c.date
    if problems:
        shown = "\n  ".join(problems[:20])
        more = f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""
        raise ValidationError(f"{len(problems)} validation problem(s):\n  {shown}{more}")


def check_existing_dates(dates: list[dt.date], label: str) -> None:
    """Stored rows must be strictly ascending with no duplicates."""
    for prev, cur in zip(dates, dates[1:]):
        if cur <= prev:
            raise ValidationError(f"{label}: stored dates not strictly ascending ({prev} then {cur})")


def missing_ranges(dates: list[dt.date], start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    """Inclusive ranges of days in start..end not present in `dates` (which must be sorted)."""
    ranges: list[tuple[dt.date, dt.date]] = []
    expected = start
    for d in dates:
        if d < start or d > end:
            continue
        if d > expected:
            ranges.append((expected, d - DAY))
        expected = d + DAY
    if expected <= end:
        ranges.append((expected, end))
    return ranges
