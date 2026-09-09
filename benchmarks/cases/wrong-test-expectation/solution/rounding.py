from decimal import ROUND_HALF_UP, Decimal


def round_half_up(x: float) -> int:
    """四舍五入（half-up）。规则见 README.md。"""
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
