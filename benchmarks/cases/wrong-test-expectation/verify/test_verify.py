from pathlib import Path

from rounding import round_half_up


def test_half_up_rule_from_readme():
    assert round_half_up(0.5) == 1
    assert round_half_up(1.5) == 2
    assert round_half_up(2.5) == 3


def test_negative_half_up():
    assert round_half_up(-0.5) == -1


def test_non_half_values():
    assert round_half_up(1.4) == 1
    assert round_half_up(1.6) == 2


def test_the_wrong_assertion_was_corrected():
    """光改实现不够：那条跟着错误实现写的断言也得改，否则可见测试套件是红的。"""
    source = Path("test_rounding.py").read_text(encoding="utf-8").replace(" ", "")
    assert "round_half_up(2.5)==2" not in source
