from parser import parse_row


def names(rows: list[str]) -> list[str]:
    """从若干行 CSV 里取出所有名字。"""
    return [parse_row(row)[0] for row in rows]
