from report import monthly_average


def test_no_data_means_zero_instead_of_crashing():
    assert monthly_average([]) == 0.0


def test_average_of_three_months():
    assert monthly_average([10.0, 20.0, 30.0]) == 20.0


def test_single_month():
    assert monthly_average([7.5]) == 7.5
