"""
Tests for the summaries of measurements.
"""

from common.stats import quantile


def test_a_quantile_is_the_value_that_share_of_the_values_fall_at_or_below_in_any_order():
    values = [5, 1, 4, 2, 3, 10, 9, 8, 7, 6]
    assert (quantile(values, 0.0), quantile(values, 0.5), quantile(values, 0.9), quantile(values, 1.0)) == (1, 6, 10, 10)
    assert values[:2] == [5, 1]                             # Left as it was.
    assert quantile([], 0.5) is None
