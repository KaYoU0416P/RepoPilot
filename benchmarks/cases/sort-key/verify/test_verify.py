from scores import top_scores


def test_numeric_order_not_lexicographic():
    assert top_scores({"a": 9, "b": 10}, 2) == ["b", "a"]


def test_limit_is_applied():
    assert top_scores({"a": 1, "b": 100, "c": 20}, 2) == ["b", "c"]
