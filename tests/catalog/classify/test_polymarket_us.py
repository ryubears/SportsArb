"""
Golden cases for the Polymarket US classifier, built from real contract rows.
"""

from catalog.classify import polymarket_us


def row(event, slug, market_type=None, line=None, start_time="2026-09-20T17:00:00+00:00"):
    """
    A Polymarket US contract row with the fields the classifier reads.
    """
    return {"venue": "polymarket_us", "contract_id": slug, "event_id": event, "market_type": market_type,
            "line": line, "start_time": start_time, "title": "", "outcome": ""}


def bet_fields(bet):
    """
    The fields that matter for matching, as a tuple.
    """
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line, bet.polarity)


def test_game_kinds():
    winner = polymarket_us.classify(row("nfl-phi-ten-2026-09-20", "aec-nfl-phi-ten-2026-09-20", "football_team_full_game_winner"))
    total = polymarket_us.classify(row("nfl-phi-ten-2026-09-20", "tsc-nfl-phi-ten-2026-09-20-total-45pt5", "football_team_full_game_total", 45.5))
    assert bet_fields(winner) == ("game_winner", 2027, "2026-09-20", "PHI", "TEN", "PHI", None, "yes")
    assert bet_fields(total) == ("total", 2027, "2026-09-20", "PHI", "TEN", None, 45.5, "yes")


def test_spread_line_is_the_away_handicap():
    favored = polymarket_us.classify(row("nfl-phi-ten-2026-09-20", "asc-nfl-phi-ten-2026-09-20-neg-1pt5", "football_team_full_game_spread", -1.5))
    underdog = polymarket_us.classify(row("nfl-phi-ten-2026-09-20", "asc-nfl-phi-ten-2026-09-20-pos-17pt5", "football_team_full_game_spread", 17.5))
    assert bet_fields(favored) == ("spread", 2027, "2026-09-20", "PHI", "TEN", "PHI", 1.5, "yes")
    assert bet_fields(underdog) == ("spread", 2027, "2026-09-20", "PHI", "TEN", "TEN", 17.5, "no")


def test_futures():
    division = polymarket_us.classify(row("nfl-afceast-2027-01-10-w", "tec-nfl-afceast-2027-01-10-w-buf", "futures", start_time=None))
    champion = polymarket_us.classify(row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-buf", "futures", start_time=None))
    seed = polymarket_us.classify(row("nfl-afc1seed-2027-01-10", "tec-nfl-afc1seed-2027-01-10-buf", "futures", start_time=None))
    assert bet_fields(division) == ("division_champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(champion) == ("champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(seed) == ("conf_top_seed", 2027, None, None, None, "BUF", None, "yes")


def test_futures_with_glued_suffixes_and_qualifier():
    champion = polymarket_us.classify(row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-bufbil", "futures", start_time=None))
    packers = polymarket_us.classify(row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-gbpac", "futures", start_time=None))
    qualifier = polymarket_us.classify(row("nfl-afc-2027-01-24-champq", "tec-nfl-afc-2027-01-24-champq-kc", "futures", start_time=None))
    assert (champion.kind, champion.subject) == ("champion", "BUF")
    assert (packers.kind, packers.subject) == ("champion", "GB")
    assert bet_fields(qualifier) == ("reach_conf_final", 2027, None, None, None, "KC", None, "yes")


def test_glued_codes_cover_every_odd_team_name():
    assert polymarket_us.glued_code("San Francisco 49ers") == "saners"
    assert polymarket_us.glued_code("Kansas City Chiefs") == "kanchi"
    assert polymarket_us.glued_code("Los Angeles Chargers") == "loscha"
    assert [polymarket_us.team_suffix(s) for s in ("kanchi", "loscha", "losram", "grepac", "saners", "tambuc", "bufbil", "kc", "gb")] == \
        ["KC", "LAC", "LAR", "GB", "SF", "TB", "BUF", "KC", "GB"]


def test_skips_props_and_awards():
    assert polymarket_us.classify(row("nfl-phi-ten-2026-09-20", "x", "football_player_touchdowns", 0.5)) is None
    assert polymarket_us.classify(row("nfl-mvp-2027-02-11-w", "tec-nfl-mvp-2027-02-11-w-abc", "futures", start_time=None)) is None


def test_player_props_shift_the_at_least_line_by_a_half():
    yards = polymarket_us.classify(row("nfl-atl-gb-2026-09-24", "astatc-nfl-atl-gb-2026-09-24-recyd-bijrob-gte40", "football_player_receiving_yards", 40.0,
                                       start_time="2026-09-25T00:15:00+00:00") | {"title": "Will Bijan Robinson record 40+ receiving yards?"})
    tds = polymarket_us.classify(row("nfl-atl-gb-2026-09-24", "astatc-nfl-atl-gb-2026-09-24-td-bijrob-gte1", "football_player_touchdowns", 1.0,
                                     start_time="2026-09-25T00:15:00+00:00") | {"title": "Will Bijan Robinson record 1+ touchdowns?"})
    first = polymarket_us.classify(row("nfl-atl-gb-2026-09-24", "astatc-nfl-atl-gb-2026-09-24-firsttd-bijrob", "football_player_first_touchdown",
                                       start_time="2026-09-25T00:15:00+00:00") | {"title": "Will Bijan Robinson score the first touchdown?"})
    picks = polymarket_us.classify(row("nfl-atl-gb-2026-09-24", "astatc-nfl-atl-gb-2026-09-24-int-jorlov-gte1", "football_player_interceptions_thrown", 1.0,
                                       start_time="2026-09-25T00:15:00+00:00") | {"title": "Will Jordan Love throw 1+ interceptions?"})
    assert bet_fields(yards) == ("player_receiving_yards", 2027, "2026-09-24", "ATL", "GB", "bijan robinson", 39.5, "yes")
    assert bet_fields(tds) == ("player_touchdowns", 2027, "2026-09-24", "ATL", "GB", "bijan robinson", 0.5, "yes")
    assert bet_fields(first) == ("player_first_touchdown", 2027, "2026-09-24", "ATL", "GB", "bijan robinson", None, "yes")
    assert (picks.kind, picks.subject, picks.line) == ("player_interceptions_thrown", "jordan love", 0.5)


def test_player_props_skip_titles_without_a_player():
    assert polymarket_us.classify(row("nfl-atl-gb-2026-09-24", "astatc-nfl-atl-gb-2026-09-24-firsttd-none", "football_player_first_touchdown",
                                      start_time="2026-09-25T00:15:00+00:00") | {"title": "Will no touchdown be scored?"}) is None
