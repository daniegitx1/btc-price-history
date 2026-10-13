"""Exchange data sources. Each `fetch(start, end)` returns daily UTC candles for start..end inclusive."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .http import SourceError

log = logging.getLogger(__name__)

DAY = dt.timedelta(days=1)
DAY_SECONDS = 86_400


@dataclass(frozen=True)
class Candle:
    date: dt.date
    open: str
    high: str
    low: str
    close: str
    volume: str  # base-currency (BTC) volume


class JsonGetter(Protocol):
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any: ...


def to_unix(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp())


def from_unix(ts: int | str) -> dt.date:
    seconds = int(ts)
    if seconds % DAY_SECONDS:
        raise SourceError(f"candle timestamp {ts} is not a UTC midnight")
    return dt.datetime.fromtimestamp(seconds, dt.UTC).date()


def num(value: Any) -> str:
    """Normalise a price/volume from JSON (string or number) to a plain decimal string."""
    try:
        d = Decimal(value.strip()) if isinstance(value, str) else Decimal(repr(value))
    except (InvalidOperation, AttributeError) as exc:
        raise SourceError(f"not a number: {value!r}") from exc
    if not d.is_finite():
        raise SourceError(f"not a finite number: {value!r}")
    return value.strip() if isinstance(value, str) and "e" not in value.lower() else format(d, "f")


def _in_range(candles: dict[dt.date, Candle], start: dt.date, end: dt.date) -> list[Candle]:
    return [candles[d] for d in sorted(candles) if start <= d <= end]


class Bitstamp:
    """Bitstamp v2 OHLC. A request with `start` returns up to `limit` (max 1000) daily candles from that day."""

    name = "bitstamp"
    earliest = dt.date(2011, 8, 18)  # first daily candle Bitstamp returns for btcusd
    page_days = 1000
    url = "https://www.bitstamp.net/api/v2/ohlc/{pair}/"

    def __init__(self, http: JsonGetter, pair: str = "btcusd") -> None:
        self.http = http
        self.pair = pair

    def fetch(self, start: dt.date, end: dt.date) -> list[Candle]:
        candles: dict[dt.date, Candle] = {}
        cursor = max(start, self.earliest)
        while cursor <= end:
            params = {
                "step": DAY_SECONDS,
                "limit": self.page_days,
                "start": to_unix(cursor),
                "exclude_current_candle": "true",
            }
            payload = self.http.get_json(self.url.format(pair=self.pair), params)
            try:
                rows = payload["data"]["ohlc"]
                for r in rows:
                    c = Candle(from_unix(r["timestamp"]), num(r["open"]), num(r["high"]),
                               num(r["low"]), num(r["close"]), num(r["volume"]))
                    candles[c.date] = c
            except (KeyError, TypeError) as exc:
                raise SourceError(f"bitstamp: unexpected response shape: {str(payload)[:300]}") from exc
            log.info("bitstamp %s: page from %s returned %d candles", self.pair, cursor, len(rows))
            cursor += dt.timedelta(days=self.page_days)
        return _in_range(candles, start, end)


class Coinbase:
    """Coinbase Exchange candles. Max 300 per request; rows are [time, low, high, open, close, volume]."""

    name = "coinbase"
    earliest = dt.date(2015, 7, 20)  # first daily BTC-USD candle
    page_days = 300
    url = "https://api.exchange.coinbase.com/products/{product}/candles"

    def __init__(self, http: JsonGetter, product: str = "BTC-USD") -> None:
        self.http = http
        self.product = product

    def fetch(self, start: dt.date, end: dt.date) -> list[Candle]:
        candles: dict[dt.date, Candle] = {}
        cursor = max(start, self.earliest)
        while cursor <= end:
            page_end = min(end, cursor + dt.timedelta(days=self.page_days - 1))
            params = {
                "granularity": DAY_SECONDS,
                "start": f"{cursor.isoformat()}T00:00:00Z",
                "end": f"{page_end.isoformat()}T00:00:00Z",
            }
            payload = self.http.get_json(self.url.format(product=self.product), params)
            if not isinstance(payload, list):
                raise SourceError(f"coinbase: unexpected response: {str(payload)[:300]}")
            try:
                for t, low, high, open_, close, volume, *_ in payload:
                    c = Candle(from_unix(t), num(open_), num(high), num(low), num(close), num(volume))
                    candles[c.date] = c
            except (ValueError, TypeError) as exc:
                raise SourceError(f"coinbase: unexpected row shape: {str(payload)[:300]}") from exc
            log.info("coinbase %s: %s..%s returned %d candles", self.product, cursor, page_end, len(payload))
            cursor = page_end + DAY
        return _in_range(candles, start, end)


class Valr:
    """VALR public buckets. Max 300 per request, newest first. Days with no trades come back as all zeros."""

    name = "valr"
    earliest = dt.date(2019, 1, 1)  # search start; first BTCZAR trade day is 2019-06-11
    page_days = 300
    url = "https://api.valr.com/v1/public/{pair}/buckets"

    def __init__(self, http: JsonGetter, pair: str = "BTCZAR") -> None:
        self.http = http
        self.pair = pair

    def fetch(self, start: dt.date, end: dt.date) -> list[Candle]:
        candles: dict[dt.date, Candle] = {}
        cursor = max(start, self.earliest)
        while cursor <= end:
            page_end = min(end, cursor + dt.timedelta(days=self.page_days - 1))
            params = {
                "periodSeconds": DAY_SECONDS,
                "startTime": to_unix(cursor),
                "endTime": to_unix(page_end),
                "limit": self.page_days,
            }
            payload = self.http.get_json(self.url.format(pair=self.pair), params)
            if not isinstance(payload, list):
                raise SourceError(f"valr: unexpected response: {str(payload)[:300]}")
            empty = 0
            try:
                for b in payload:
                    started = dt.datetime.fromisoformat(b["startTime"].replace("Z", "+00:00"))
                    if started.astimezone(dt.UTC).time() != dt.time(0) or int(b["bucketPeriodInSeconds"]) != DAY_SECONDS:
                        raise SourceError(f"valr: bucket is not a UTC day: {b}")
                    prices = [num(b[k]) for k in ("open", "high", "low", "close")]
                    if all(Decimal(p) == 0 for p in prices):
                        empty += 1  # no trades that day; leave it out so it shows up as a gap
                        continue
                    c = Candle(started.date(), *prices, num(b["volume"]))
                    candles[c.date] = c
            except (KeyError, TypeError, ValueError) as exc:
                raise SourceError(f"valr: unexpected bucket shape: {str(payload)[:300]}") from exc
            log.info("valr %s: %s..%s returned %d buckets (%d without trades)",
                     self.pair, cursor, page_end, len(payload), empty)
            cursor = page_end + DAY
        return _in_range(candles, start, end)
