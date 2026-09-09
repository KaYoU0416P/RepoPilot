from tagging import add_tag


def test_add_one():
    assert add_tag("a") == ["a"]
