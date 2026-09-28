"""
Tests for the helpers on the models.
"""

from db.models import Leg, Settlement, Trade


def trade():
    return Trade(mode="live", pair_id=1, trade="t", signal_ts="s", edge=0.08, quantity=10,
                 yes_venue="polymarket_us", yes_contract="pm", yes_polarity="yes", yes_limit=0.45,
                 no_venue="kalshi", no_contract="k", no_polarity="yes", no_limit=0.47, pays_at="p", yes_held=10, yes_cost=4.5)


def test_a_trade_gives_each_leg_its_own_fields():
    yes, no = trade().legs()
    assert yes == Leg("yes", "polymarket_us", "pm", "yes", limit=0.45, quantity=10, held=10, cost=4.5) and yes.key == ("polymarket_us", "pm")
    assert no == Leg("no", "kalshi", "k", "yes", limit=0.47, quantity=10) == trade().leg("no")
    assert yes.fee_info is None                         # Not stored with the trade.


def test_a_leg_holds_the_outcome_its_side_and_polarity_give():
    def leg(side, polarity):
        return Leg(side, "kalshi", "k", polarity)
    assert (leg("yes", "yes").outcome, leg("no", "yes").outcome) == ("yes", "no")      # A contract that pays on yes.
    assert (leg("no", "no").outcome, leg("yes", "no").outcome) == ("yes", "no")        # One that pays on no, such as the other team's.


def test_a_settlement_records_one_leg_at_a_time():
    s = Settlement(1, mode="live", settled_at="t")
    s.record("no", "yes", 0.0, "t2")
    assert (s.no_result, s.no_payout, s.no_settled_at, s.yes_result) == ("yes", 0.0, "t2", None)
