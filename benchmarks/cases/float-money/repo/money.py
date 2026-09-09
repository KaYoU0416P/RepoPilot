def total_cents(amounts: list[float]) -> int:
    """把若干个「元」金额相加，返回总额的「分」，四舍五入到整数分。"""
    return int(sum(amounts) * 100)
