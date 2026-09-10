from auth import SESSION_TTL_SECONDS, is_session_valid


def test_fresh_session_is_valid():
    assert is_session_valid(1000, 1000) is True


def test_session_at_the_ttl_boundary_is_expired():
    assert is_session_valid(1000, 1000 + SESSION_TTL_SECONDS) is False


def test_future_dated_session_is_rejected():
    assert is_session_valid(2000, 1000) is False


def test_far_future_session_is_rejected():
    assert is_session_valid(10_000_000, 1000) is False
