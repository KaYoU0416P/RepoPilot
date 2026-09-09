from registry import Registry


def test_exact_match():
    r = Registry()
    r.register("alice@x.com")
    assert r.is_registered("alice@x.com")
