def chunk(items: list, size: int) -> list[list]:
    """把列表切成每块 size 个。"""
    out = []
    i = 0
    while i < len(items):
        out.append(items[i : i + size])
        i += size
    return out
