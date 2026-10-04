"""
Tests for pairing up bets across venues.
"""

from catalog import match, notes


def bet(venue, contract_id, kind="game_winner", subject="CAR", polarity="yes", line=None,
        game_date="2026-09-20", team_a="CAR", team_b="ATL", close_time="2026-09-20T20:00:00+00:00"):
    """
    A bet row as load_bets returns it.
    """
    return {"venue": venue, "contract_id": contract_id, "kind": kind, "season": 2027, "game_date": game_date,
            "team_a": team_a, "team_b": team_b, "subject": subject, "line": line, "polarity": polarity,
            "close_time": close_time}


def test_identity_ignores_venue_contract_and_polarity():
    assert match.identity(bet("polymarket_us", "a", polarity="yes")) == match.identity(bet("kalshi", "b", polarity="no"))


def test_label():
    assert match.label(bet("kalshi", "k"), "nfl") == "nfl game_winner 2026-09-20 CAR@ATL CAR"
    assert match.label(bet("kalshi", "k", kind="spread", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="ATL", line=4.5), "nfl") == "nfl spread 2026-09-20 CAR@ATL ATL 4.5"


def test_a_futures_label_names_the_season_in_place_of_the_game():
    assert match.label(bet("kalshi", "k", kind="champion", game_date=None, team_a=None, team_b=None, subject="KC"), "nfl") == \
        "nfl champion 2027 KC"
    assert match.label(bet("kalshi", "k", kind="wild_card_series", game_date=None, team_a="ATL", team_b="PHI", subject="ATL"), "mlb") == \
        "mlb wild_card_series 2027 ATL vs PHI ATL"
    assert match.label(bet("kalshi", "k", kind="season_wins", game_date=None, team_a=None, team_b=None, subject="ARI", line=2.5), "nfl") == \
        "nfl season_wins 2027 ARI 2.5"


def test_an_award_pair_notes_how_each_venue_settles_a_shared_award():
    pair = match.make_pair([bet("kalshi", "k", kind="mvp", game_date=None, team_a=None, team_b=None, subject="josh allen"),
                            bet("polymarket_us", "p", kind="mvp", game_date=None, team_a=None, team_b=None, subject="josh allen")], "nfl")
    assert pair.flags == [notes.AWARD_NOTE]


def test_the_same_bet_in_two_sports_has_two_labels():
    game = dict(game_date="2026-11-01", team_a="DAL", team_b="DEN", subject="DAL")     # Dallas at Denver, the same day in both leagues.
    cowboys, mavericks = bet("kalshi", "k", **game), bet("kalshi", "k2", **game)
    assert (match.label(cowboys, "nfl"), match.label(mavericks, "nba")) == ("nfl game_winner 2026-11-01 DAL@DEN DAL",
                                                                            "nba game_winner 2026-11-01 DAL@DEN DAL")


def test_same_bet_on_two_venues_forms_one_pair():
    pairs, unmatched = match.match([bet("polymarket_us", "us"), bet("kalshi", "k")], "nfl")
    assert len(pairs) == 1 and unmatched == []
    g = pairs[0]
    assert g.label == "nfl game_winner 2026-09-20 CAR@ATL CAR" and g.sport == "nfl"
    assert g.venues == ["kalshi", "polymarket_us"]
    assert sorted((m.venue, m.contract_id, m.polarity) for m in g.members) == [("kalshi", "k", "yes"), ("polymarket_us", "us", "yes")]


def test_bets_on_a_single_venue_are_left_out():
    pairs, unmatched = match.match([bet("polymarket_us", "us"), bet("kalshi", "k", subject="MIA")], "nfl")
    assert pairs == [] and len(unmatched) == 2


def test_far_apart_close_times_are_flagged():
    pairs, _ = match.match([bet("polymarket_us", "us", close_time="2027-03-31T23:55:00+00:00"),
                             bet("kalshi", "k", close_time="2029-02-13T23:30:00+00:00")], "nfl")
    assert any(f.startswith("close times") for f in pairs[0].flags)
    assert any(f.startswith("close times") for f in pairs[0].flags)


def test_winner_pair_holds_both_kalshi_contracts_and_the_away_market():
    game = dict(kind="game_winner", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="CAR")
    pairs, _ = match.match([bet("polymarket_us", "us_car", polarity="yes", **game),
                             bet("kalshi", "k_car", polarity="yes", **game),
                             bet("kalshi", "k_atl", polarity="no", **game)], "nfl")
    assert sorted((m.contract_id, m.polarity) for m in pairs[0].members) == [("k_atl", "no"), ("k_car", "yes"), ("us_car", "yes")]


def test_two_kalshi_contracts_alone_do_not_form_a_pair():
    game = dict(kind="game_winner", game_date="2026-09-20", team_a="CAR", team_b="ATL", subject="CAR")
    pairs, unmatched = match.match([bet("kalshi", "k_car", polarity="yes", **game), bet("kalshi", "k_atl", polarity="no", **game)], "nfl")
    assert pairs == [] and len(unmatched) == 2


def test_leader_and_race_pairs_carry_their_notes_and_a_kind_settled_alike_none():
    future = dict(game_date=None, team_a=None, team_b=None)
    leader = dict(future, kind="passing_yards_leader", subject="joe burrow")
    race = dict(future, kind="senate_race", subject="GA D")
    total = dict(future, kind="season_receiving_yards", subject="jamarr chase", line=999.5)
    pairs, _ = match.match([bet("kalshi", "k", **leader), bet("polymarket_us", "us", **leader),
                            bet("kalshi", "kr", **race), bet("polymarket_us", "usr", **race),
                            bet("kalshi", "kt", **total), bet("polymarket_us", "ust", **total)], "nfl")
    by_kind = {p.kind: p for p in pairs}
    assert by_kind["passing_yards_leader"].flags == [notes.LEADER_NOTE]
    assert by_kind["senate_race"].flags == [notes.ELECTION_NOTE]
    assert by_kind["season_receiving_yards"].flags == []
    assert by_kind["senate_race"].label == "nfl senate_race 2027 GA D"
