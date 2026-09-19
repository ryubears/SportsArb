"""
Tests for pairing bets across venues.
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
    a = bet("polymarket", "a", polarity="yes")
    b = bet("kalshi", "b", polarity="no")
    assert match.identity(a) == match.identity(b)


def test_same_bet_on_both_venues_pairs_once():
    pairs, unmatched = match.match([bet("polymarket", "pm"), bet("kalshi", "k")])
    assert len(pairs) == 1 and unmatched == []
    assert (pairs[0].polymarket_id, pairs[0].kalshi_id) == ("pm", "k")
    assert pairs[0].kind == "champion"


def test_bets_without_a_partner_are_unmatched():
    pairs, unmatched = match.match([bet("polymarket", "pm"), bet("kalshi", "k", subject="MIA")])
    assert pairs == [] and len(unmatched) == 2


def test_far_apart_close_times_are_flagged():
    pairs, _ = match.match([bet("polymarket", "pm", close_time="2027-03-31T23:55:00+00:00"),
                            bet("kalshi", "k", close_time="2029-02-13T23:30:00+00:00")])
    assert pairs[0].close_gap_days > match.CLOSE_GAP_LIMIT_DAYS
    assert any(f.startswith("close times") for f in pairs[0].flags)
    assert match.KIND_NOTES["champion"] in pairs[0].flags


def test_polymarket_no_twin_is_dropped_when_yes_side_exists():
    spread = dict(kind="spread", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="ATL", line=4.5)
    pairs, _ = match.match([bet("polymarket", "pm_yes", polarity="yes", **spread),
                            bet("polymarket", "pm_no", polarity="no", **spread),
                            bet("kalshi", "k", polarity="yes", **spread)])
    assert [p.polymarket_id for p in pairs] == ["pm_yes"]


def test_opposite_polarity_pair_is_kept_when_it_is_the_only_one():
    total = dict(kind="total", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject=None, line=45.5)
    pairs, _ = match.match([bet("polymarket", "pm_under", polarity="no", **total),
                            bet("kalshi", "k_over", polarity="yes", **total)])
    assert len(pairs) == 1
    assert (pairs[0].polymarket_polarity, pairs[0].kalshi_polarity) == ("no", "yes")
