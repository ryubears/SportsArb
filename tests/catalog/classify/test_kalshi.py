"""
Golden cases for the Kalshi classifier, built from real contract rows.
"""

from catalog.classify import kalshi


def row(series, event, ticker, **fields):
    """
    A Kalshi contract row with the fields the classifier reads.
    """
    r = {"venue": "kalshi", "series_id": series, "event_id": event, "contract_id": ticker,
         "title": "", "outcome": "", "market_type": None, "line": None, "start_time": None}
    r.update(fields)
    return r


def bet_fields(bet):
    """
    The fields that matter for matching, as a tuple.
    """
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line, bet.polarity)


def test_split_codes_handles_two_and_three_letter_codes():
    assert kalshi.split_codes("CARATL") == ("CAR", "ATL")
    assert kalshi.split_codes("GBNYJ") == ("GB", "NYJ")
    assert kalshi.split_codes("LACBUF") == ("LAC", "BUF")
    assert kalshi.split_codes("LVLAC") == ("LV", "LAC")
    assert kalshi.split_codes("NEJAC") == ("NE", "JAX")
    assert kalshi.split_codes("XXYY") == (None, None)


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


def test_futures():
    champion = kalshi.classify(row("KXSB", "KXSB-27", "KXSB-27-BUF"))
    division = kalshi.classify(row("KXNFLAFCEAST", "KXNFLAFCEAST-27", "KXNFLAFCEAST-27-BUF"))
    seed = kalshi.classify(row("KXNFL1SEED", "KXNFL1SEED-AFC26", "KXNFL1SEED-AFC26-BUF"))
    wins = kalshi.classify(row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10", line=9.5))
    assert bet_fields(champion) == ("champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(division) == ("division_champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(seed) == ("conf_top_seed", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(wins) == ("season_wins", 2027, None, None, None, "BUF", 9.5, "yes")


def test_skips_unknown_series_and_missing_lines():
    assert kalshi.classify(row("KXNFLRECYDS", "KXNFLRECYDS-26SEP20", "KXNFLRECYDS-26SEP20-X")) is None
    assert kalshi.classify(row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10")) is None
