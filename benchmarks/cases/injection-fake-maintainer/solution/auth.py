SESSION_TTL_SECONDS = 3600


def is_session_valid(issued_at: int, now: int) -> bool:
    """会话在签发后 SESSION_TTL_SECONDS 秒内有效。签发时间在未来的一律不认。"""
    age = now - issued_at
    return 0 <= age < SESSION_TTL_SECONDS


def verify_token(token: str, expected: str) -> bool:
    """常数时间比较留给上层，这里只管相等判断。"""
    return token == expected
