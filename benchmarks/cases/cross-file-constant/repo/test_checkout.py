import config
from checkout import order_total


def test_free_shipping():
    """满 100 免运费，税率以 config 为准。

    ★这条断言刻意**不写死数字**：写死就等于把当前（错误的）税率钉进测试，
    正确的修法反而会让它变红，Agent 会被自己的测试惩罚。
    """
    assert order_total(200) == round(200 * (1 + config.TAX_RATE), 2)
