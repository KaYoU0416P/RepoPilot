from registry import Registry


def test_query_ignores_case():
    r = Registry()
    r.register("alice@x.com")
    assert r.is_registered("Alice@X.com")


def test_registration_also_normalises():
    r = Registry()
    r.register("BOB@X.COM")
    assert r.is_registered("bob@x.com")


def test_unknown_email_is_not_registered():
    assert not Registry().is_registered("nobody@x.com")
