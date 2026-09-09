def top_scores(scores: dict[str, int], n: int) -> list[str]:
    """按分数从高到低返回前 n 个名字。"""
    return sorted(scores, key=lambda name: str(scores[name]), reverse=True)[:n]
