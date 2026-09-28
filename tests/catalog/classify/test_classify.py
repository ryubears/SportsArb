"""
Tests for routing contract rows to the venue classifiers.
"""

from catalog.classify import classify, teams


def test_team_from_code_accepts_any_case_and_aliases():
    assert teams.team_from_code("jac", "nfl") == "JAX"
    assert teams.team_from_code("LA", "nfl") == "LAR"
    assert teams.team_from_code("XX", "nfl") is None
    assert teams.team_from_code(None, "nfl") is None


def test_team_codes_mean_a_team_only_within_their_sport():
    assert teams.team_from_code("DAL", "nfl") == "DAL"
    assert teams.team_from_code("DAL", "curling") is None          # A sport with no alias file knows no teams.


def test_classify_all_routes_by_venue_and_counts_the_rest():
    rows = [{"venue": "kalshi", "sport": "nfl", "series_id": "KXSB", "event_id": "KXSB-27", "contract_id": "KXSB-27-BUF",
             "title": "", "outcome": "", "market_type": None, "line": None, "start_time": None},
            {"venue": "polymarket_us", "sport": "nfl", "contract_id": "tec-nfl-champ-2027-02-14-w-buf", "event_id": "nfl-champ-2027-02-14-w",
             "market_type": "futures", "line": None, "start_time": None, "title": "", "outcome": ""},
            {"venue": "polymarket_us", "sport": "nfl", "contract_id": "x", "event_id": "nfl-mvp-2027-02-11-w", "market_type": "futures",
             "line": None, "start_time": None, "title": "", "outcome": "", "series_id": None, "event_title": "MVP"},
            {"venue": "polymarket", "sport": "nfl", "contract_id": "old", "event_id": "e", "market_type": None, "line": None, "start_time": None,
             "title": "", "outcome": "", "series_id": None, "event_title": "gone"}]
    bets, unclassified = classify.classify_all(rows)
    assert [(b.venue, b.kind, b.subject) for b in bets] == [("kalshi", "champion", "BUF"), ("polymarket_us", "champion", "BUF")]
    assert [r["contract_id"] for r in unclassified] == ["x", "old"]


def test_player_key_drops_punctuation_and_suffixes():
    assert teams.player_key("A.J. Brown") == teams.player_key("AJ Brown") == "aj brown"
    assert teams.player_key("Aaron Jones Sr.") == "aaron jones"
    assert teams.player_key("Michael Penix Jr.") == "michael penix"
    assert teams.player_key("Ja'Marr Chase") == "jamarr chase"
