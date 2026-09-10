from datetime import UTC, datetime, timedelta

from tokens import is_expired


def test_smoke():
    """★这里必须传**带时区**的时间 —— docstring 写的就是 "expires_at 是带 UTC
    时区的时间"。原来这条用 datetime.utcnow() 传了个 naive 对象，
    于是「正确的修法」反而会让它抛 TypeError，Agent 修对了却被判失败。
    """
    assert is_expired(datetime.now(UTC) - timedelta(hours=1))
