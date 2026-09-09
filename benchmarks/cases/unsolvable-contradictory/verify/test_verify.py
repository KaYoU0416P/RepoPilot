from normalize import normalize


def test_is_idempotent():
    once = normalize(" Alice ")
    assert normalize(once) == once


def test_always_appends_a_version_suffix():
    assert normalize(" Alice ") == "alice-v"
    assert normalize("alice-v") == "alice-v-v"
