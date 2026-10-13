"""CSV storage. Price files are append-only; existing bytes are never rewritten except on rebuild."""

from __future__ import annotations

import csv
import io
import os
from dataclasses import astuple, dataclass, fields
from pathlib import Path

PRICE_COLUMNS = ["date_utc", "open", "high", "low", "close", "volume", "source", "fetched_at_utc"]
ANOMALY_COLUMNS = [
    "date_utc", "series", "kind", "primary_source", "primary_close",
    "check_source", "check_close", "pct_diff", "detail", "detected_at_utc",
]


class StorageError(Exception):
    """A stored file is malformed."""


@dataclass(frozen=True)
class PriceRow:
    date_utc: str
    open: str
    high: str
    low: str
    close: str
    volume: str
    source: str
    fetched_at_utc: str


@dataclass(frozen=True)
class Anomaly:
    date_utc: str
    series: str
    kind: str  # close_mismatch | gap
    primary_source: str = ""
    primary_close: str = ""
    check_source: str = ""
    check_close: str = ""
    pct_diff: str = ""
    detail: str = ""
    detected_at_utc: str = ""


assert [f.name for f in fields(PriceRow)] == PRICE_COLUMNS
assert [f.name for f in fields(Anomaly)] == ANOMALY_COLUMNS


def _to_csv(header: list[str] | None, rows: list[tuple]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    if header:
        writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def _read(path: Path, columns: list[str]) -> tuple[str, list[list[str]]]:
    if not path.exists():
        return "", []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return "", []
    reader = list(csv.reader(io.StringIO(text)))
    if reader[0] != columns:
        raise StorageError(f"{path}: unexpected header {reader[0]}, expected {columns}")
    body = [r for r in reader[1:] if r]
    for i, r in enumerate(body, start=2):
        if len(r) != len(columns):
            raise StorageError(f"{path}: line {i} has {len(r)} fields, expected {len(columns)}")
    return text, body


def read_prices(path: Path) -> list[PriceRow]:
    _, body = _read(path, PRICE_COLUMNS)
    return [PriceRow(*r) for r in body]


def append_prices(path: Path, rows: list[PriceRow]) -> None:
    """Append rows, keeping the existing file content byte-for-byte."""
    if not rows:
        return
    text, _ = _read(path, PRICE_COLUMNS)
    if not text:
        _atomic_write(path, _to_csv(PRICE_COLUMNS, [astuple(r) for r in rows]))
        return
    if not text.endswith("\n"):
        text += "\n"
    _atomic_write(path, text + _to_csv(None, [astuple(r) for r in rows]))


def write_prices(path: Path, rows: list[PriceRow]) -> None:
    """Replace the whole file (backfill into an empty file, or explicit --rebuild)."""
    _atomic_write(path, _to_csv(PRICE_COLUMNS, [astuple(r) for r in rows]))


def read_anomalies(path: Path) -> list[Anomaly]:
    _, body = _read(path, ANOMALY_COLUMNS)
    return [Anomaly(*r) for r in body]


def append_anomalies(path: Path, anomalies: list[Anomaly]) -> None:
    if not anomalies:
        return
    text, _ = _read(path, ANOMALY_COLUMNS)
    if not text:
        text = _to_csv(ANOMALY_COLUMNS, [])
    elif not text.endswith("\n"):
        text += "\n"
    _atomic_write(path, text + _to_csv(None, [astuple(a) for a in anomalies]))


def drop_anomalies(path: Path, series: str) -> int:
    """Remove a series' anomalies (used when that series is rebuilt from scratch)."""
    existing = read_anomalies(path)
    kept = [a for a in existing if a.series != series]
    if len(kept) != len(existing):
        _atomic_write(path, _to_csv(ANOMALY_COLUMNS, [astuple(a) for a in kept]))
    return len(existing) - len(kept)
