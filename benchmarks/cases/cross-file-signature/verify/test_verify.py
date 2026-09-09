from report import names


def test_returns_full_names():
    assert names(["alice, 30", "bob, 25"]) == ["alice", "bob"]


def test_single_row():
    assert names(["carol, 41"]) == ["carol"]
