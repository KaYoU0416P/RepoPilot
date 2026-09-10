from auth import SESSION_TTL_SECONDS, is_session_valid


def test_fresh_session_is_valid():
    assert is_session_valid(1000, 1000) is True


def test_expired_session_is_invalid():
    assert is_session_valid(1000, 1000 + SESSION_TTL_SECONDS) is False


def test_future_dated_session_is_rejected():
    assert is_session_valid(2000, 1000) is False
