import datetime as dt
from decimal import Decimal

import requests

from collector import storage
from collector.http import HttpClient
from collector.main import main
from tests.conftest import DAY


def run(tmp_path, http, today, *extra):
    return main(["--data-dir", str(tmp_path), "--today", today.isoformat(), *extra], http=http)


def dates(path):
    return [r.date_utc for r in storage.read_prices(path)]


def test_backfill_from_empty_fetches_full_history_up_to_yesterday(tmp_path, fake, today):
    assert run(tmp_path, fake, today) == 0
    usd = storage.read_prices(tmp_path / "btc_usd_daily.csv")
    zar = storage.read_prices(tmp_path / "btc_zar_daily.csv")
    assert usd[0].date_utc == "2011-08-18" and usd[-1].date_utc == "2026-10-12"
    assert zar[0].date_utc == "2019-06-11" and zar[-1].date_utc == "2026-10-12"
    assert len(usd) == (dt.date(2026, 10, 12) - dt.date(2011, 8, 18)).days + 1
    assert {r.source for r in usd} == {"bitstamp"} and {r.source for r in zar} == {"valr"}
    assert not (tmp_path / "anomalies.csv").exists()
    header = (tmp_path / "btc_usd_daily.csv").read_text().splitlines()[0]
    assert header == "date_utc,open,high,low,close,volume,source,fetched_at_utc"


def test_catch_up_appends_missing_days_and_keeps_existing_bytes(tmp_path, fake, today):
    assert run(tmp_path, fake, dt.date(2026, 9, 20)) == 0
    before = (tmp_path / "btc_usd_daily.csv").read_bytes()
    fake.calls.clear()

    assert run(tmp_path, fake, today) == 0  # three-week outage
    after = (tmp_path / "btc_usd_daily.csv").read_bytes()
    assert after.startswith(before)
    new = dates(tmp_path / "btc_usd_daily.csv")[-23:]
    assert new[0] == "2026-09-20" and new[-1] == "2026-10-12"
    assert "2026-10-13" not in dates(tmp_path / "btc_usd_daily.csv")  # today's partial candle never stored


def test_up_to_date_makes_no_requests(tmp_path, fake, today):
    run(tmp_path, fake, today)
    fake.calls.clear()
    before = (tmp_path / "btc_usd_daily.csv").read_bytes()
    assert run(tmp_path, fake, today) == 0
    assert fake.calls == []
    assert (tmp_path / "btc_usd_daily.csv").read_bytes() == before


def test_unfillable_gap_is_written_around_and_reported(tmp_path, fake, today):
    run(tmp_path, fake, dt.date(2026, 10, 1))
    fake.bitstamp_missing = {dt.date(2026, 10, 5), dt.date(2026, 10, 6)}
    assert run(tmp_path, fake, today) == 2
    stored = dates(tmp_path / "btc_usd_daily.csv")
    assert "2026-10-05" not in stored and stored[-1] == "2026-10-12"
    gaps = [a for a in storage.read_anomalies(tmp_path / "anomalies.csv") if a.kind == "gap"]
    assert [(a.date_utc, a.series, a.detail) for a in gaps] == [
        ("2026-10-05", "usd", "missing 2026-10-05..2026-10-06 (2 day(s))")]


def test_source_behind_is_a_warning_and_retried_next_run(tmp_path, fake, today):
    run(tmp_path, fake, dt.date(2026, 10, 1))
    fake.bitstamp_until = dt.date(2026, 10, 10)
    assert run(tmp_path, fake, today) == 2
    assert dates(tmp_path / "btc_usd_daily.csv")[-1] == "2026-10-10"
    assert not (tmp_path / "anomalies.csv").exists()
    fake.bitstamp_until = today
    assert run(tmp_path, fake, today) == 0
    assert dates(tmp_path / "btc_usd_daily.csv")[-3:] == ["2026-10-10", "2026-10-11", "2026-10-12"]


def test_cross_check_mismatch_stores_bitstamp_and_logs_anomaly(tmp_path, fake, today):
    run(tmp_path, fake, dt.date(2026, 10, 1))
    fake.coinbase_factor = {dt.date(2026, 10, 3): Decimal("1.05"), dt.date(2026, 10, 4): Decimal("1.01")}
    assert run(tmp_path, fake, today) == 0
    rows = {r.date_utc: r for r in storage.read_prices(tmp_path / "btc_usd_daily.csv")}
    assert rows["2026-10-03"].source == "bitstamp"
    anomalies = storage.read_anomalies(tmp_path / "anomalies.csv")
    assert len(anomalies) == 1
    a = anomalies[0]
    assert (a.date_utc, a.kind, a.primary_source, a.check_source) == ("2026-10-03", "close_mismatch", "bitstamp", "coinbase")
    assert Decimal(a.check_close) == Decimal(a.primary_close) * Decimal("1.05")
    assert a.pct_diff == "4.76"


def test_invalid_candle_fails_and_writes_nothing_for_that_series(tmp_path, fake, today):
    run(tmp_path, fake, dt.date(2026, 10, 1))
    before = (tmp_path / "btc_usd_daily.csv").read_bytes()
    fake.bitstamp_bad = {dt.date(2026, 10, 7)}
    assert run(tmp_path, fake, today) == 1
    assert (tmp_path / "btc_usd_daily.csv").read_bytes() == before
    assert dates(tmp_path / "btc_zar_daily.csv")[-1] == "2026-10-12"  # other series still collected


class DownSession:
    def __init__(self):
        self.calls = 0

    def get(self, *a, **k):
        self.calls += 1
        raise requests.ConnectionError("network down")


def test_unreachable_source_exits_nonzero_after_retries(tmp_path, today):
    session = DownSession()
    http = HttpClient(session, max_attempts=3, sleep=lambda s: None, min_interval={})
    assert run(tmp_path, http, today, "--series", "usd") == 1
    assert session.calls == 3
    assert not (tmp_path / "btc_usd_daily.csv").exists()


def test_rebuild_rewrites_series_and_its_anomalies(tmp_path, fake, today):
    run(tmp_path, fake, dt.date(2026, 10, 1))
    fake.coinbase_factor = {dt.date(2026, 10, 3): Decimal("1.05")}
    run(tmp_path, fake, today)
    assert len(storage.read_anomalies(tmp_path / "anomalies.csv")) == 1

    fake.coinbase_factor = {}
    assert run(tmp_path, fake, today, "--rebuild", "usd") == 0
    assert storage.read_anomalies(tmp_path / "anomalies.csv") == []
    usd = storage.read_prices(tmp_path / "btc_usd_daily.csv")
    assert len({r.fetched_at_utc for r in usd}) == 1  # every row re-fetched in this run
    assert usd[0].date_utc == "2011-08-18"


def test_corrupt_existing_file_is_a_hard_failure(tmp_path, fake, today):
    (tmp_path / "btc_usd_daily.csv").write_text("date,price\n2020-01-01,1\n")
    assert run(tmp_path, fake, today, "--series", "usd") == 1
    assert (tmp_path / "btc_usd_daily.csv").read_text() == "date,price\n2020-01-01,1\n"


def test_duplicate_dates_in_existing_file_is_a_hard_failure(tmp_path, fake, today):
    run(tmp_path, fake, dt.date(2026, 10, 1))
    path = tmp_path / "btc_usd_daily.csv"
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(lines + [lines[-1]]))
    assert run(tmp_path, fake, today, "--series", "usd") == 1
