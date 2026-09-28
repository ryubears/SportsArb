"""
Tests for routing contract rows to the venue classifiers.
"""

from catalog.classify import classify, teams
from common.venues import VENUES


def test_team_from_code_accepts_any_case_and_aliases():
    assert teams.team_from_code("jac", "nfl", "kalshi") == "JAX"
    assert teams.team_from_code("LA", "nfl", "polymarket_us") == "LAR"
    assert teams.team_from_code("XX", "nfl", "kalshi") is None
    assert teams.team_from_code(None, "nfl", "kalshi") is None


def test_team_codes_mean_a_team_only_within_their_sport():
    assert teams.team_from_code("DAL", "nfl", "kalshi") == "DAL"
    assert teams.team_from_code("DAL", "curling", "kalshi") is None          # A sport with no alias file knows no teams.


def test_a_code_the_venues_give_different_teams_is_read_per_venue():
    assert teams.team_from_code("SDST", "ncaaf", "kalshi") == "SDST"            # South Dakota State on Kalshi,
    assert teams.team_from_code("sdst", "ncaaf", "polymarket_us") == "SDSU"     # San Diego State on Polymarket US,
    assert teams.team_from_code("sdkst", "ncaaf", "polymarket_us") == "SDST"    # which calls South Dakota State this.


def test_every_team_has_a_code_on_each_venue_and_no_code_names_two_teams():
    for sport, entries in teams.ALIASES.items():
        for venue in VENUES:
            codes = [c.upper() for entry in entries.values() for c in teams.venue_codes(entry, venue)]
            assert all(teams.venue_codes(entry, venue) for entry in entries.values()), (sport, venue)
            assert len(codes) == len(set(codes)), (sport, venue)


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
