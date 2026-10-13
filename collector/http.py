"""HTTP client: descriptive User-Agent, per-host throttling, retries with exponential backoff."""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

# Minimum seconds between requests to the same host, kept well inside each exchange's published limits:
# Bitstamp 10,000 req / 10 min; Coinbase public ~10 req/s; VALR public 30 req/min.
DEFAULT_MIN_INTERVAL = {
    "www.bitstamp.net": 0.25,
    "api.exchange.coinbase.com": 0.4,
    "api.valr.com": 2.1,
}


class SourceError(Exception):
    """A data source returned something unusable."""


class SourceUnavailable(SourceError):
    """A data source could not be reached after all retries."""


def default_user_agent() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    contact = f"+https://github.com/{repo}" if repo else "local run"
    return f"btc-daily-price-collector/1.0 ({contact})"


def _retry_after(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


class HttpClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        max_attempts: int = 6,
        base_delay: float = 2.0,
        max_delay: float = 120.0,
        timeout: float = 30.0,
        min_interval: dict[str, float] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout
        self.min_interval = DEFAULT_MIN_INTERVAL if min_interval is None else min_interval
        self.sleep = sleep
        self.user_agent = user_agent or default_user_agent()
        self._last_request: dict[str, float] = {}

    def _throttle(self, host: str) -> None:
        interval = self.min_interval.get(host, 0.0)
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_request[host] = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        host = urlsplit(url).hostname or ""
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        for attempt in range(1, self.max_attempts + 1):
            self._throttle(host)
            retry_after = None
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                problem = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    problem = f"HTTP {resp.status_code}"
                    retry_after = _retry_after(resp)
                elif resp.status_code >= 400:
                    raise SourceUnavailable(
                        f"{url} {params} returned HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                else:
                    try:
                        return resp.json()
                    except ValueError:
                        problem = "response was not valid JSON"

            if attempt == self.max_attempts:
                raise SourceUnavailable(f"{url} failed after {attempt} attempts: {problem}")
            backoff = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
            delay = retry_after if retry_after is not None else backoff * (1 + random.random() * 0.25)
            log.warning("%s: %s (attempt %d/%d), retrying in %.1fs", host, problem, attempt, self.max_attempts, delay)
            self.sleep(delay)
        raise AssertionError("unreachable")
