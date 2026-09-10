SESSION_TTL_SECONDS = 3600


def is_session_valid(issued_at: int, now: int) -> bool:
    """会话在签发后 SESSION_TTL_SECONDS 秒内有效。"""
    return now - issued_at < SESSION_TTL_SECONDS


def verify_token(token: str, expected: str) -> bool:
    """常数时间比较留给上层，这里只管相等判断。"""
    return token == expected
