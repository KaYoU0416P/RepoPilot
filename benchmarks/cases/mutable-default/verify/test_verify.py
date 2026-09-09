from tagging import add_tag


def test_calls_do_not_leak_into_each_other():
    assert add_tag("a") == ["a"]
    assert add_tag("b") == ["b"]
    assert add_tag("c") == ["c"]


def test_explicit_list_still_works():
    existing = ["x"]
    assert add_tag("y", existing) == ["x", "y"]
