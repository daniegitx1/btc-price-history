import datetime as dt

import pytest

from collector.http import SourceError
from collector.sources import Bitstamp, Candle, Coinbase, Valr, num
from tests.conftest import RecordingHttp, load_fixture


def test_bitstamp_parses_saved_response_and_filters_range():
    http = RecordingHttp({"bitstamp": load_fixture("bitstamp_ohlc.json")})
    candles = Bitstamp(http).fetch(dt.date(2011, 9, 2), dt.date(2011, 9, 4))
    assert [c.date for c in candles] == [dt.date(2011, 9, 2), dt.date(2011, 9, 3), dt.date(2011, 9, 4)]
    assert candles[0] == Candle(dt.date(2011, 9, 2), "8.12", "8.88", "8.00", "8.88", "79.76000000")
    url, params = http.calls[0]
    assert url == "https://www.bitstamp.net/api/v2/ohlc/btcusd/"
    assert params == {"step": 86400, "limit": 1000, "start": 1314921600, "exclude_current_candle": "true"}


def test_bitstamp_paginates_in_1000_day_windows():
    http = RecordingHttp({"bitstamp": {"data": {"ohlc": []}}})
    Bitstamp(http).fetch(dt.date(2011, 8, 18), dt.date(2016, 12, 31))
    starts = [dt.datetime.fromtimestamp(p["start"], dt.UTC).date() for _, p in http.calls]
    assert starts == [dt.date(2011, 8, 18), dt.date(2014, 5, 14)]


def test_bitstamp_bad_shape_is_source_error():
    http = RecordingHttp({"bitstamp": {"code": "oops"}})
    with pytest.raises(SourceError):
        Bitstamp(http).fetch(dt.date(2020, 1, 1), dt.date(2020, 1, 2))


def test_coinbase_field_order_is_time_low_high_open_close_volume():
    http = RecordingHttp({"coinbase": load_fixture("coinbase_candles.json")})
    candles = Coinbase(http).fetch(dt.date(2024, 1, 1), dt.date(2024, 1, 5))
    assert [c.date for c in candles] == [dt.date(2024, 1, d) for d in range(1, 6)]
    first = candles[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        "42288.58", "44240.8", "42175.65", "44220.78", "7977.72851143")
    assert candles[4].low == "42450"


def test_coinbase_requests_at_most_300_candles_and_skips_pre_listing():
    http = RecordingHttp({"coinbase": []})
    Coinbase(http).fetch(dt.date(2015, 1, 1), dt.date(2016, 6, 1))
    spans = [(p["start"], p["end"]) for _, p in http.calls]
    assert spans[0] == ("2015-07-20T00:00:00Z", "2016-05-14T00:00:00Z")
    assert spans[1] == ("2016-05-15T00:00:00Z", "2016-06-01T00:00:00Z")


def test_valr_drops_no_trade_buckets_and_sorts_ascending():
    http = RecordingHttp({"valr": load_fixture("valr_buckets.json")})
    candles = Valr(http).fetch(dt.date(2019, 6, 8), dt.date(2019, 6, 13))
    assert [c.date for c in candles] == [dt.date(2019, 6, 11), dt.date(2019, 6, 12), dt.date(2019, 6, 13)]
    assert candles[0] == Candle(dt.date(2019, 6, 11), "129001", "129001", "121556", "122500", "42.81236723")
    _, params = http.calls[0]
    assert params == {"periodSeconds": 86400, "startTime": 1559952000, "endTime": 1560384000, "limit": 300}


def test_num_normalises_floats_and_rejects_garbage():
    assert num("8.00") == "8.00"
    assert num(1e-05) == "0.00001"
    assert num(42450) == "42450"
    with pytest.raises(SourceError):
        num("abc")
    with pytest.raises(SourceError):
        num("NaN")
