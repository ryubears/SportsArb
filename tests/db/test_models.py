"""
Tests for the helpers on the models.
"""

from db.models import Settlement, Trade


def trade():
    return Trade(mode="live", pair_id=1, trade="t", signal_ts="s", edge=0.08, quantity=10,
                 yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
                 no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at="p", yes_held=10, yes_cost=4.5)


def test_a_trade_gives_each_leg_its_own_fields():
    yes, no = trade().legs()
    assert yes == ("yes", "polymarket_us", "pm", "yes", 0.45, 10, 4.5) and yes.key == ("polymarket_us", "pm")
    assert no == ("no", "kalshi", "k", "yes", 0.47, 0, 0.0) == trade().leg("no")


def test_a_settlement_records_one_leg_at_a_time():
    s = Settlement(1, mode="live", settled_at="t")
    s.record("no", "yes", 0.0, "t2")
    assert (s.no_result, s.no_payout, s.no_settled_at, s.yes_result) == ("yes", 0.0, "t2", None)
