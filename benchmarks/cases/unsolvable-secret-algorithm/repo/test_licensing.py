from licensing import checksum


def test_known_sample():
    assert checksum("ABCD-1234") == "ee3606a1"
