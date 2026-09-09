def chunk(items: list, size: int) -> list[list]:
    """把列表切成每块 size 个。size 必须为正。"""
    if size <= 0:
        raise ValueError("size 必须大于 0")
    out = []
    i = 0
    while i < len(items):
        out.append(items[i : i + size])
        i += size
    return out
