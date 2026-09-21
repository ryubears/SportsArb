"""
Tests for the scanner's pricing and episode detection over bet groups.
"""

import pytest
import scan
from db.models import FeeRecord, Quote

PM_FEES = {"feesEnabled": True, "feeSchedule": {"rate": 0.05}}
NO_PM_FEES = {"feesEnabled": False}
NO_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 0}
REAL_K_FEES = {"fee_type": "quadratic", "fee_multiplier": 1}
T0 = "2026-09-19T12:00:00+00:00"


def member(venue, contract_id, polarity="yes", start_time=None, close_time="2026-09-29T12:00:00+00:00"):
    return {"venue": venue, "contract_id": contract_id, "polarity": polarity, "start_time": start_time, "close_time": close_time}


def group(members):
    return {"label": "spread 2026-09-20 CAR@ATL ATL 4.5", "kind": "spread", "members": members}


def quote(venue, contract_id, ts, bids, asks):
    return Quote(venue, contract_id, ts, bids, asks)


def histories(fee_by_key, seen_at="2000-01-01T00:00:00+00:00"):
    return {key: [FeeRecord(key[0], key[1], seen_at, info)] for key, info in fee_by_key.items()}


# PRICING

def test_ladder_depends_on_which_side_the_contract_pays():
    q = quote("kalshi", "k", "t", bids=[[0.53, 100]], asks=[[0.54, 50]])
    assert scan.ladder(q, "yes", "yes") == [(0.54, 50)]     # Hold yes through a yes contract, buy it.
    assert scan.ladder(q, "yes", "no") == [(0.47, 100)]     # Hold no through a yes contract, buy the other side.
    assert scan.ladder(q, "no", "no") == [(0.54, 50)]       # Hold no through a no contract, buy it.
    assert scan.ladder(q, "no", "yes") == [(0.47, 100)]


def test_fill_walks_both_ladders_while_the_edge_is_positive():
    leg_a = [(0.40, 10), (0.41, 10)]
    leg_b = [(0.50, 5), (0.58, 100)]
    fee_infos = {"polymarket": NO_PM_FEES}
    top_edge, size, profit = scan.fill(leg_a, leg_b, "polymarket", "polymarket", fee_infos)
    assert top_edge == pytest.approx(0.10)
    assert size == 20
    assert profit == pytest.approx(5 * 0.10 + 5 * 0.02 + 10 * 0.01)


def test_fill_stops_at_zero_edge_and_handles_empty_ladders():
    fee_infos = {"polymarket": NO_PM_FEES}
    assert scan.fill([(0.5, 10)], [(0.5, 10)], "polymarket", "polymarket", fee_infos) == (0.0, 0.0, 0.0)
    assert scan.fill([], [(0.5, 10)], "polymarket", "polymarket", fee_infos) == (-1.0, 0.0, 0.0)


def test_best_trade_picks_the_cheapest_leg_on_each_side_across_venues():
    members = [member("kalshi", "k"), member("polymarket", "us")]
    quotes = {("kalshi", "k"): quote("kalshi", "k", "t", [[0.53, 100]], [[0.54, 100]]),
              ("polymarket", "us"): quote("polymarket", "us", "t", [[0.44, 100]], [[0.45, 100]])}
    fee_infos = {("kalshi", "k"): NO_K_FEES, ("polymarket", "us"): NO_PM_FEES}
    yes, no, edge, size, profit = scan.best_trade(members, quotes, fee_infos)
    # Yes is cheapest at Polymarket's 0.45 ask. No is cheapest at Kalshi, one minus its 0.53 bid.
    assert (yes["venue"], no["venue"]) == ("polymarket", "kalshi")
    assert edge == pytest.approx(1 - 0.45 - 0.47)
    assert size == 100


def test_best_trade_uses_a_no_contract_for_yes_exposure():
    members = [member("kalshi", "k_yes", "yes"), member("kalshi", "k_no", "no")]
    quotes = {("kalshi", "k_yes"): quote("kalshi", "k_yes", "t", [[0.40, 100]], [[0.60, 100]]),
              ("kalshi", "k_no"): quote("kalshi", "k_no", "t", [[0.55, 100]], [[0.70, 100]])}
    fee_infos = {("kalshi", "k_yes"): NO_K_FEES, ("kalshi", "k_no"): NO_K_FEES}
    yes, no, edge, _, _ = scan.best_trade(members, quotes, fee_infos)
    # Yes through the no contract's bid costs 0.45, cheaper than the yes contract's 0.60 ask.
    # No through the yes contract's bid costs 0.60, cheaper than the no contract's 0.70 ask.
    assert (yes["contract_id"], no["contract_id"]) == ("k_no", "k_yes")
    assert edge == pytest.approx(1 - 0.45 - 0.60)


def test_best_trade_never_uses_one_contract_for_both_legs():
    # k_no is cheapest on both sides, so it is paired with the other contract on whichever side works out better.
    members = [member("kalshi", "k_yes", "yes"), member("kalshi", "k_no", "no")]
    quotes = {("kalshi", "k_yes"): quote("kalshi", "k_yes", "t", [[0.40, 100]], [[0.60, 100]]),
              ("kalshi", "k_no"): quote("kalshi", "k_no", "t", [[0.55, 100]], [[0.56, 100]])}
    fee_infos = {("kalshi", "k_yes"): NO_K_FEES, ("kalshi", "k_no"): NO_K_FEES}
    yes, no, edge, _, _ = scan.best_trade(members, quotes, fee_infos)
    assert (yes["contract_id"], no["contract_id"]) == ("k_no", "k_yes")
    assert edge == pytest.approx(1 - 0.45 - 0.60)


def test_best_trade_ignores_a_crossed_book_with_no_partner():
    # A lone book whose bid is above its ask is a feed glitch, not free money.
    members = [member("polymarket", "pm", "yes")]
    quotes = {("polymarket", "pm"): quote("polymarket", "pm", "t", [[0.46, 100]], [[0.36, 100]])}
    assert scan.best_trade(members, quotes, {("polymarket", "pm"): NO_PM_FEES}) is None


def test_best_trade_returns_none_without_two_quoted_sides():
    members = [member("kalshi", "k")]
    quotes = {("kalshi", "k"): quote("kalshi", "k", "t", [[0.53, 100]], [])}
    assert scan.best_trade(members, quotes, {("kalshi", "k"): NO_K_FEES}) is None


def test_fee_at_picks_the_record_in_force():
    history = [FeeRecord("polymarket", "x", "2026-09-18T00:00:00+00:00", {"a": 1}),
               FeeRecord("polymarket", "x", "2026-09-19T16:00:00+00:00", {"a": 2})]
    assert scan.fee_at(history, "2026-09-17T00:00:00+00:00") == {"a": 1}
    assert scan.fee_at(history, "2026-09-19T12:00:00+00:00") == {"a": 1}
    assert scan.fee_at(history, "2026-09-19T16:00:00+00:00") == {"a": 2}
    assert scan.fee_at([], "2026-09-20T00:00:00+00:00") == {}


# EPISODES

def test_scan_group_finds_one_episode_with_duration_and_return():
    g = group([member("kalshi", "k"), member("polymarket", "pm")])
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]])],
              ("kalshi", "k"): [quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]]),
                                quote("kalshi", "k", "2026-09-19T12:01:01+00:00", [[0.49, 100]], [[0.50, 100]])]}
    fees_ = histories({("polymarket", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES})
    episodes = scan.scan_group(g, quotes, fees_)
    assert len(episodes) == 1
    o = episodes[0]
    assert (o.start_ts, o.end_ts, o.seconds) == ("2026-09-19T12:00:01+00:00", "2026-09-19T12:01:01+00:00", 60)
    assert (o.yes_venue, o.no_venue) == ("polymarket", "kalshi")
    assert o.trade == "yes: PM buy, no: K buy other side"
    assert o.peak_edge == pytest.approx(0.04)
    assert o.peak_size == 100
    assert o.live == 0
    assert o.days_held == pytest.approx(10, rel=1e-4)
    assert o.return_pct == pytest.approx(100 * 0.04 / 0.96)
    assert o.annual_pct == pytest.approx(o.return_pct * 365 / o.days_held)


def test_scan_group_marks_live_and_uses_kickoff_for_payout():
    kickoff = "2026-09-19T11:00:00+00:00"
    g = group([member("kalshi", "k", start_time=kickoff), member("polymarket", "pm", start_time=kickoff)])
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]])],
              ("kalshi", "k"): [quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])]}
    o = scan.scan_group(g, quotes, histories({("polymarket", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES}))[0]
    assert o.live == 1
    assert o.end_ts == "2026-09-19T12:00:01+00:00"
    assert o.days_held == pytest.approx((scan.GAME_HOURS - 1) / 24, rel=1e-3)


def test_scan_group_applies_the_fee_in_force_at_each_quote():
    g = group([member("kalshi", "k"), member("polymarket", "pm")])
    # Polymarket fees switch off at 12:00:30, between the two Kalshi quotes, which both fall within the stale limit.
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]])],
              ("kalshi", "k"): [quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]]),
                                quote("kalshi", "k", "2026-09-19T12:00:59+00:00", [[0.53, 100]], [[0.54, 100]])]}
    fees_ = histories({("kalshi", "k"): NO_K_FEES})
    fees_[("polymarket", "pm")] = [FeeRecord("polymarket", "pm", T0, PM_FEES),
                                   FeeRecord("polymarket", "pm", "2026-09-19T12:00:30+00:00", NO_PM_FEES)]
    o = scan.scan_group(g, quotes, fees_)[0]
    assert o.peak_ts == "2026-09-19T12:00:59+00:00"
    assert o.peak_edge == pytest.approx(0.04)


def test_scan_group_holds_until_the_slower_leg_pays():
    g = group([member("kalshi", "k", close_time="2026-10-19T12:00:00+00:00"), member("polymarket", "pm", close_time="2026-09-29T12:00:00+00:00")])
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]])],
              ("kalshi", "k"): [quote("kalshi", "k", "2026-09-19T12:00:01+00:00", [[0.53, 100]], [[0.54, 100]])]}
    o = scan.scan_group(g, quotes, histories({("polymarket", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES}))[0]
    assert o.days_held == pytest.approx(30, rel=1e-4)


def test_scan_group_ignores_a_member_whose_quote_went_stale():
    g = group([member("kalshi", "k"), member("polymarket", "pm")])
    # Polymarket quoted once, then went quiet. Two minutes later Kalshi reprices and would appear to cross it.
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]])],
              ("kalshi", "k"): [quote("kalshi", "k", "2026-09-19T12:02:01+00:00", [[0.53, 100]], [[0.54, 100]])]}
    assert scan.scan_group(g, quotes, histories({("polymarket", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES})) == []


def test_scan_group_ignores_a_quote_from_before_its_venue_dropped():
    g = group([member("kalshi", "k"), member("polymarket", "pm")])
    # Polymarket quoted at 12:00:00, dropped at 12:00:30, and requoted the same book at 12:01:30. Kalshi crosses it at 12:01:00.
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]]),
                                     quote("polymarket", "pm", "2026-09-19T12:01:30+00:00", [[0.48, 100]], [[0.49, 100]])],
              ("kalshi", "k"): [quote("kalshi", "k", "2026-09-19T12:01:00+00:00", [[0.53, 100]], [[0.54, 100]])]}
    fees_ = histories({("polymarket", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES})
    assert len(scan.scan_group(g, quotes, fees_)) == 1
    episodes = scan.scan_group(g, quotes, fees_, {"polymarket": ["2026-09-19T12:00:30+00:00"]})
    assert [o.start_ts for o in episodes] == ["2026-09-19T12:01:30+00:00"]     # Only once Polymarket is seen again.


def test_scan_group_ignores_time_before_two_members_have_quotes():
    g = group([member("kalshi", "k"), member("polymarket", "pm")])
    quotes = {("polymarket", "pm"): [quote("polymarket", "pm", T0, [[0.48, 100]], [[0.49, 100]])], ("kalshi", "k"): []}
    assert scan.scan_group(g, quotes, histories({("polymarket", "pm"): NO_PM_FEES, ("kalshi", "k"): NO_K_FEES})) == []
