"""
Golden cases for the Kalshi classifier, built from real contract rows.
"""

from catalog.classify import kalshi


def row(series, event, ticker, **fields):
    """
    A Kalshi contract row with the fields the classifier reads.
    """
    r = {"venue": "kalshi", "sport": "nfl", "series_id": series, "event_id": event, "contract_id": ticker,
         "title": "", "outcome": "", "market_type": None, "line": None, "start_time": None}
    r.update(fields)
    return r


def bet_fields(bet):
    """
    The fields that matter for matching, as a tuple.
    """
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line, bet.polarity)


def test_split_codes_handles_two_and_three_letter_codes():
    assert kalshi.split_codes("CARATL", "nfl") == ("CAR", "ATL")
    assert kalshi.split_codes("GBNYJ", "nfl") == ("GB", "NYJ")
    assert kalshi.split_codes("LACBUF", "nfl") == ("LAC", "BUF")
    assert kalshi.split_codes("LVLAC", "nfl") == ("LV", "LAC")
    assert kalshi.split_codes("NEJAC", "nfl") == ("NE", "JAX")
    assert kalshi.split_codes("XXYY", "nfl") == (None, None)


def test_split_codes_takes_college_codes_of_any_length_and_refuses_to_guess():
    assert kalshi.split_codes("WKUNMSU", "ncaaf") == ("WKU", "NMSU")
    assert kalshi.split_codes("BCSMU", "ncaaf") == ("BC", "SMU")
    assert kalshi.split_codes("UTRGVETAM", "ncaaf") == ("UTRGV", "ETAM")
    assert kalshi.split_codes("TCUND", "ncaaf") == (None, None)     # TCU and Notre Dame, or Tusculum and North Dakota.
    assert kalshi.split_codes("CARATL", "ncaaf") == (None, None)    # NFL codes mean nothing in college football.


def test_game_kinds():
    winner = kalshi.classify(row("KXNFLGAME", "KXNFLGAME-26SEP20CARATL", "KXNFLGAME-26SEP20CARATL-ATL"))
    spread = kalshi.classify(row("KXNFLSPREAD", "KXNFLSPREAD-26SEP20CARATL", "KXNFLSPREAD-26SEP20CARATL-ATL5", line=4.5))
    total = kalshi.classify(row("KXNFLTOTAL", "KXNFLTOTAL-26SEP20CARATL", "KXNFLTOTAL-26SEP20CARATL-27", line=26.5))
    assert bet_fields(winner) == ("game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, "no")
    assert bet_fields(spread) == ("spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, "yes")
    assert bet_fields(total) == ("total", 2027, "2026-09-20", "CAR", "ATL", None, 26.5, "yes")


def test_game_with_two_letter_codes():
    bet = kalshi.classify(row("KXNFLGAME", "KXNFLGAME-26SEP24ATLGB", "KXNFLGAME-26SEP24ATLGB-GB"))
    assert (bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.polarity) == ("2026-09-24", "ATL", "GB", "ATL", "no")


def test_college_games_read_like_the_nfls():
    winner = kalshi.classify(row("KXNCAAFGAME", "KXNCAAFGAME-26OCT01WKUNMSU", "KXNCAAFGAME-26OCT01WKUNMSU-NMSU", sport="ncaaf"))
    spread = kalshi.classify(row("KXNCAAFSPREAD", "KXNCAAFSPREAD-26OCT03BCSMU", "KXNCAAFSPREAD-26OCT03BCSMU-SMU10", sport="ncaaf", line=9.5))
    total = kalshi.classify(row("KXNCAAFTOTAL", "KXNCAAFTOTAL-26OCT03BCSMU", "KXNCAAFTOTAL-26OCT03BCSMU-44", sport="ncaaf", line=43.5))
    assert bet_fields(winner) == ("game_winner", 2027, "2026-10-01", "WKU", "NMSU", "WKU", None, "no")
    assert bet_fields(spread) == ("spread", 2027, "2026-10-03", "BC", "SMU", "SMU", 9.5, "yes")
    assert bet_fields(total) == ("total", 2027, "2026-10-03", "BC", "SMU", None, 43.5, "yes")


def test_futures_are_left_out():
    assert kalshi.classify(row("KXSB", "KXSB-27", "KXSB-27-BUF")) is None
    assert kalshi.classify(row("KXNFLAFCEAST", "KXNFLAFCEAST-27", "KXNFLAFCEAST-27-BUF")) is None
    assert kalshi.classify(row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10", line=9.5)) is None


def test_skips_unknown_series_and_missing_lines():
    assert kalshi.classify(row("KXNFLRECYDS", "KXNFLRECYDS-26SEP20", "KXNFLRECYDS-26SEP20-X")) is None
    assert kalshi.classify(row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10")) is None


def test_player_props_name_the_player_and_keep_the_strict_line():
    yards = kalshi.classify(row("KXNFLRECYDS", "KXNFLRECYDS-26SEP24ATLGB", "KXNFLRECYDS-26SEP24ATLGB-ATLBROBINSON7-100",
                                title="Bijan Robinson: 100+ receiving yards", line=99.5))
    first = kalshi.classify(row("KXNFLFIRSTTD", "KXNFLFIRSTTD-26SEP24ATLGB", "KXNFLFIRSTTD-26SEP24ATLGB-ATLBROBINSON7",
                                title="Bijan Robinson: 1st Touchdown"))
    senior = kalshi.classify(row("KXNFLRSHYDS", "KXNFLRSHYDS-26SEP24ATLGB", "KXNFLRSHYDS-26SEP24ATLGB-GBAJONES33-40",
                                 title="Aaron Jones Sr.: 40+ rushing yards", line=39.5))
    assert bet_fields(yards) == ("player_receiving_yards", 2027, "2026-09-24", "ATL", "GB", "bijan robinson", 99.5, "yes")
    assert bet_fields(first) == ("player_first_touchdown", 2027, "2026-09-24", "ATL", "GB", "bijan robinson", None, "yes")
    assert (senior.subject, senior.line) == ("aaron jones", 39.5)


def test_player_props_skip_team_units_and_missing_lines():
    assert kalshi.classify(row("KXNFLTD", "KXNFLTD-26SEP24ATLGB", "KXNFLTD-26SEP24ATLGB-ATLATLDST-1", title="ATL Falcons D/ST: 1+ touchdowns", line=0.5)) is None
    assert kalshi.classify(row("KXNFLRECYDS", "KXNFLRECYDS-26SEP24ATLGB", "KXNFLRECYDS-26SEP24ATLGB-ATLBROBINSON7-100",
                               title="Bijan Robinson: 100+ receiving yards")) is None
