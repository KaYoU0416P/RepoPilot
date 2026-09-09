def add_tag(tag: str, tags: list[str] = []) -> list[str]:
    """把 tag 加进 tags 并返回。不传 tags 时应该从空列表开始。"""
    tags.append(tag)
    return tags
