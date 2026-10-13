import datetime as dt

import pytest
import requests

from collector import storage
from collector.http import HttpClient, SourceUnavailable
from collector.sources import Candle
from collector.storage import PriceRow
from collector.validation import ValidationError, check_candles, missing_ranges


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class ScriptedSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append(headers)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(session, sleeps):
    return HttpClient(session, max_attempts=4, base_delay=1, sleep=sleeps.append, min_interval={},
                      user_agent="test-agent/1.0")


def test_retries_429_5xx_and_network_errors_with_backoff():
    sleeps = []
    session = ScriptedSession(FakeResponse(429, headers={"Retry-After": "7"}), requests.Timeout("slow"),
                              FakeResponse(503), FakeResponse(200, {"ok": True}))
    assert client(session, sleeps).get_json("https://x.test/a") == {"ok": True}
    assert sleeps[0] == 7  # Retry-After honoured
    assert 2 <= sleeps[1] <= 2.5 and 4 <= sleeps[2] <= 5  # exponential backoff with jitter
    assert session.requests[0]["User-Agent"] == "test-agent/1.0"


def test_gives_up_after_max_attempts():
    sleeps = []
    session = ScriptedSession(*[FakeResponse(502)] * 4)
    with pytest.raises(SourceUnavailable, match="after 4 attempts"):
        client(session, sleeps).get_json("https://x.test/a")
    assert len(sleeps) == 3


def test_client_errors_are_not_retried():
    sleeps = []
    with pytest.raises(SourceUnavailable, match="HTTP 400"):
        client(ScriptedSession(FakeResponse(400, {"error": "bad"})), sleeps).get_json("https://x.test/a")
    assert sleeps == []


def c(day, o, h, l, cl, v="1"):
    return Candle(dt.date(2024, 1, day), o, h, l, cl, v)


def test_validation_accepts_good_candles():
    check_candles([c(1, "10", "12", "9", "11"), c(2, "11", "11", "11", "11", "0")])


@pytest.mark.parametrize("bad, message", [
    (c(1, "10", "10.5", "9", "11"), "high 10.5 below"),
    (c(1, "10", "12", "10.5", "11"), "low 10.5 above"),
    (c(1, "0", "12", "0", "11"), "non-positive price"),
    (c(1, "10", "12", "9", "11", "-1"), "negative volume"),
])
def test_validation_rejects_bad_candles(bad, message):
    with pytest.raises(ValidationError, match=message):
        check_candles([bad])


def test_validation_rejects_duplicates():
    with pytest.raises(ValidationError, match="duplicate"):
        check_candles([c(1, "10", "12", "9", "11"), c(1, "10", "12", "9", "11")])


def test_missing_ranges():
    d = lambda n: dt.date(2024, 1, n)
    assert missing_ranges([d(1), d(2), d(5), d(6)], d(1), d(8)) == [(d(3), d(4)), (d(7), d(8))]
    assert missing_ranges([], d(1), d(2)) == [(d(1), d(2))]
    assert missing_ranges([d(1), d(2)], d(1), d(2)) == []


def test_append_preserves_existing_bytes_and_uses_lf(tmp_path):
    path = tmp_path / "p.csv"
    row = lambda day: PriceRow(f"2024-01-0{day}", "1", "2", "0.5", "1.5", "3", "bitstamp", "2024-01-09T00:17:00Z")
    storage.append_prices(path, [row(1)])
    first = path.read_bytes()
    storage.append_prices(path, [row(2), row(3)])
    assert path.read_bytes().startswith(first)
    assert b"\r" not in path.read_bytes()
    assert [r.date_utc for r in storage.read_prices(path)] == ["2024-01-01", "2024-01-02", "2024-01-03"]


def test_empty_file_reads_as_no_rows(tmp_path):
    (tmp_path / "p.csv").write_text("")
    assert storage.read_prices(tmp_path / "p.csv") == []
    assert storage.read_prices(tmp_path / "missing.csv") == []
