from datetime import UTC, datetime, timedelta, timezone

from tokens import is_expired

PLUS_8 = timezone(timedelta(hours=8))


def test_aware_future_is_not_expired():
    assert not is_expired(datetime.now(UTC) + timedelta(hours=1))


def test_aware_past_is_expired():
    assert is_expired(datetime.now(UTC) - timedelta(hours=1))


def test_non_utc_timezone_is_handled():
    future = (datetime.now(UTC) + timedelta(hours=2)).astimezone(PLUS_8)
    assert not is_expired(future)
