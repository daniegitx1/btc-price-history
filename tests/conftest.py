"""Test helpers: saved API responses and an in-memory fake of the three exchanges (no network)."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DAY = dt.timedelta(days=1)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordingHttp:
    """Returns canned payloads by URL substring and records every call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        for key, handler in self.routes.items():
            if key in url:
                return handler(dict(params or {})) if callable(handler) else handler
        raise AssertionError(f"unexpected URL {url}")


def price(day: dt.date) -> Decimal:
    return Decimal(1000 + day.toordinal() % 97)


def _unix(day: dt.date) -> int:
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC).timestamp())


class FakeExchanges(RecordingHttp):
    """Synthetic Bitstamp / Coinbase / VALR serving a deterministic series up to and including `today`.

    Like the real APIs, it happily returns today's still-open candle (ignoring exclude_current_candle),
    so tests prove the collector filters it out itself.
    """

    def __init__(self, today: dt.date):
        self.today = today
        self.bitstamp_missing: set[dt.date] = set()
        self.bitstamp_until = today
        self.bitstamp_bad: set[dt.date] = set()
        self.coinbase_factor: dict[dt.date, Decimal] = {}
        self.valr_first_trade = dt.date(2019, 6, 11)
        super().__init__({
            "bitstamp": self._bitstamp,
            "coinbase": self._coinbase,
            "valr": self._valr,
        })

    @staticmethod
    def _days(start: dt.date, end: dt.date):
        d = start
        while d <= end:
            yield d
            d += DAY

    def _bitstamp(self, params):
        start = dt.datetime.fromtimestamp(params["start"], dt.UTC).date()
        end = min(start + (params["limit"] - 1) * DAY, self.bitstamp_until)
        rows = []
        for d in self._days(max(start, dt.date(2011, 8, 18)), end):
            if d in self.bitstamp_missing:
                continue
            p = price(d)
            high = p - 5 if d in self.bitstamp_bad else p + 10
            rows.append({"timestamp": str(_unix(d)), "open": str(p - 1), "high": str(high),
                         "low": str(p - 20), "close": str(p), "volume": "12.5"})
        return {"data": {"pair": "BTC/USD", "ohlc": rows}}

    def _coinbase(self, params):
        start = dt.date.fromisoformat(params["start"][:10])
        end = dt.date.fromisoformat(params["end"][:10])
        assert (end - start).days < 300, "coinbase allows at most 300 candles per request"
        rows = []
        for d in self._days(max(start, dt.date(2015, 7, 20)), min(end, self.today)):
            close = price(d) * self.coinbase_factor.get(d, Decimal(1))
            rows.append([_unix(d), float(close - 30), float(close + 30), float(close), float(close), 99.5])
        return rows[::-1]  # newest first, like the real API

    def _valr(self, params):
        start = dt.datetime.fromtimestamp(params["startTime"], dt.UTC).date()
        end = dt.datetime.fromtimestamp(params["endTime"], dt.UTC).date()
        assert (end - start).days < 300
        rows = []
        for d in self._days(start, min(end, self.today)):
            traded = d >= self.valr_first_trade
            p = price(d) * 18 if traded else 0
            rows.append({"currencyPairSymbol": "BTCZAR", "bucketPeriodInSeconds": 86400,
                         "startTime": f"{d.isoformat()}T00:00:00Z",
                         "open": str(p), "high": str(p + 100 if traded else 0), "low": str(p - 100 if traded else 0),
                         "close": str(p), "volume": "3.25" if traded else "0", "quoteVolume": "0"})
        return rows[::-1]


@pytest.fixture
def today():
    return dt.date(2026, 10, 13)


@pytest.fixture
def fake(today):
    return FakeExchanges(today)
