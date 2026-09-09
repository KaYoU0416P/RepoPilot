from report import names


def test_smoke():
    assert isinstance(names(["alice, 30"]), list)
