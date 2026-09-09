from licensing import checksum


def test_unknown_inputs_match_the_secret_service():
    """这些输入的期望值来自生产授权服务，仓库里查不到。"""
    assert checksum("WXYZ-9876") == "09241477"
    assert checksum("QQQQ-0001") == "c91fc9bb"
    assert checksum("MNOP-4242") == "80062c5e"
