from scores import top_scores


def test_simple():
    assert top_scores({"a": 3, "b": 1}, 2) == ["a", "b"]
