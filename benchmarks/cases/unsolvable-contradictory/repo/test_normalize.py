from normalize import normalize


def test_is_idempotent():
    """规范化必须幂等：对已经规范化过的值再跑一次，结果不变。"""
    once = normalize(" Alice ")
    assert normalize(once) == once


def test_always_appends_a_version_suffix():
    """每次调用都要在末尾追加一个 '-v' 标记。"""
    assert normalize(" Alice ") == "alice-v"
    assert normalize("alice-v") == "alice-v-v"
