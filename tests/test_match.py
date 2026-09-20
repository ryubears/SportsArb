"""
Tests for grouping bets across venues.
"""

import match


def bet(venue, contract_id, kind="champion", subject="BUF", polarity="yes", line=None,
        game_date=None, team_a=None, team_b=None, close_time="2027-01-25T00:00:00+00:00"):
    """
    A bet row as load_bets returns it.
    """
    return {"venue": venue, "contract_id": contract_id, "kind": kind, "season": 2027, "game_date": game_date,
            "team_a": team_a, "team_b": team_b, "subject": subject, "line": line, "polarity": polarity,
            "close_time": close_time}


def test_identity_ignores_venue_contract_and_polarity():
    assert match.identity(bet("polymarket", "a", polarity="yes")) == match.identity(bet("kalshi", "b", polarity="no"))


def test_label():
    assert match.label(bet("kalshi", "k")) == "champion 2027 BUF"
    assert match.label(bet("kalshi", "k", kind="spread", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="ATL", line=4.5)) == "spread 2026-09-20 CAR@ATL ATL 4.5"


def test_same_bet_on_two_venues_forms_one_group():
    groups, unmatched = match.match([bet("polymarket", "us"), bet("kalshi", "k")])
    assert len(groups) == 1 and unmatched == []
    g = groups[0]
    assert g.label == "champion 2027 BUF"
    assert g.venues == ["kalshi", "polymarket"]
    assert sorted((m.venue, m.contract_id, m.polarity, m.group_label) for m in g.members) == [
        ("kalshi", "k", "yes", "champion 2027 BUF"), ("polymarket", "us", "yes", "champion 2027 BUF")]


def test_bets_on_a_single_venue_are_left_out():
    groups, unmatched = match.match([bet("polymarket", "us"), bet("kalshi", "k", subject="MIA")])
    assert groups == [] and len(unmatched) == 2


def test_far_apart_close_times_are_flagged():
    groups, _ = match.match([bet("polymarket", "us", close_time="2027-03-31T23:55:00+00:00"),
                             bet("kalshi", "k", close_time="2029-02-13T23:30:00+00:00")])
    assert any(f.startswith("close times") for f in groups[0].flags)
    assert any(f.startswith("close times") for f in groups[0].flags)


def test_polymarket_mirror_token_is_left_out_of_the_group():
    spread = dict(kind="spread", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="ATL", line=4.5)
    groups, _ = match.match([bet("polymarket", "pm_yes", polarity="yes", **spread),
                             bet("polymarket", "pm_no", polarity="no", **spread),
                             bet("kalshi", "k", polarity="yes", **spread)])
    assert sorted(m.contract_id for m in groups[0].members) == ["k", "pm_yes"]


def test_winner_group_holds_both_kalshi_contracts_and_the_away_token():
    game = dict(kind="game_winner", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="CAR")
    groups, _ = match.match([bet("polymarket", "pm_car", polarity="yes", **game),
                             bet("polymarket", "pm_atl", polarity="no", **game),
                             bet("kalshi", "k_car", polarity="yes", **game),
                             bet("kalshi", "k_atl", polarity="no", **game)])
    assert sorted((m.contract_id, m.polarity) for m in groups[0].members) == [("k_atl", "no"), ("k_car", "yes"), ("pm_car", "yes")]


def test_two_kalshi_contracts_alone_do_not_form_a_group():
    game = dict(kind="game_winner", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="CAR")
    groups, unmatched = match.match([bet("kalshi", "k_car", polarity="yes", **game), bet("kalshi", "k_atl", polarity="no", **game)])
    assert groups == [] and len(unmatched) == 2
