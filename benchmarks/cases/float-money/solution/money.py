from decimal import ROUND_HALF_UP, Decimal


def total_cents(amounts: list[float]) -> int:
    """把若干个「元」金额相加，返回总额的「分」，四舍五入到整数分。"""
    total = sum((Decimal(str(a)) for a in amounts), Decimal("0"))
    return int((total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
