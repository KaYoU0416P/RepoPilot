from datetime import UTC, datetime


def is_expired(expires_at: datetime) -> bool:
    """expires_at 是带 UTC 时区的时间。已过期返回 True。"""
    return datetime.now(UTC) > expires_at
