"""Retry timing, including Retry-After support."""

from __future__ import annotations

from mitene_download.retry import MAX_RETRY_AFTER, backoff_delay


class HttpError(Exception):
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_backoff_is_exponential_with_jitter():
    for attempt in range(6):
        delay = backoff_delay(attempt)
        assert 2**attempt <= delay <= 2**attempt + 1


def test_retry_after_is_honoured():
    assert backoff_delay(0, HttpError({"Retry-After": "10"})) >= 10


def test_retry_after_is_capped():
    assert backoff_delay(0, HttpError({"Retry-After": "100000"})) <= MAX_RETRY_AFTER


def test_invalid_retry_after_falls_back_to_backoff():
    delay = backoff_delay(2, HttpError({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    assert 4 <= delay <= 5


def test_missing_headers_and_exceptions_are_safe():
    assert backoff_delay(1) >= 2
    assert backoff_delay(1, HttpError()) >= 2
    assert backoff_delay(0, RuntimeError("boom")) >= 1
