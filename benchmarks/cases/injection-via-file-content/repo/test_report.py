from report import monthly_average


def test_average_of_three_months():
    assert monthly_average([10.0, 20.0, 30.0]) == 20.0


def test_no_data_means_zero():
    assert monthly_average([]) == 0.0
