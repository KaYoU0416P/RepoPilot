def add_tag(tag: str, tags: list[str] | None = None) -> list[str]:
    """把 tag 加进 tags 并返回。不传 tags 时从空列表开始。"""
    if tags is None:
        tags = []
    tags.append(tag)
    return tags
