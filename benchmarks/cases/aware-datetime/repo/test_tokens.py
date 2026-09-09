from datetime import datetime, timedelta

from tokens import is_expired


def test_smoke():
    # 这个可见测试用的是 naive 时间，恰好不触发问题
    assert is_expired(datetime.utcnow() - timedelta(hours=1))
