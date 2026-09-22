"""
Golden cases for the classifier, built from real contract rows seen on both venues.
"""

import classify


def k_row(series, event, ticker, **fields):
    """
    A Kalshi contract row with the fields the classifier reads.
    """
    row = {"venue": "kalshi", "series_id": series, "event_id": event, "contract_id": ticker,
           "title": "", "outcome": "", "market_type": None, "line": None, "start_time": None}
    row.update(fields)
    return row


def bet_fields(bet):
    """
    The fields that matter for matching, as a tuple.
    """
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line, bet.polarity)


# TEAMS

def test_split_codes_handles_two_and_three_letter_codes():
    assert classify.split_codes("CARATL") == ("CAR", "ATL")
    assert classify.split_codes("GBNYJ") == ("GB", "NYJ")
    assert classify.split_codes("LACBUF") == ("LAC", "BUF")
    assert classify.split_codes("LVLAC") == ("LV", "LAC")
    assert classify.split_codes("NEJAC") == ("NE", "JAX")
    assert classify.split_codes("XXYY") == (None, None)


# SEASONS

def test_season_from_text():
    assert classify.season_from_text("Pro Football: 2026-27 AFC #1 Seed") == 2027
    assert classify.season_from_text("Pro Football: 2027 Champion") == 2027
    assert classify.season_from_text("no year here") is None


def test_season_from_date_splits_in_august():
    assert classify.season_from_date("2026-09-20") == 2027
    assert classify.season_from_date("2027-01-10") == 2027
    assert classify.season_from_date("2027-08-01") == 2028


# KALSHI

def test_kalshi_game_kinds():
    winner = classify.classify_kalshi(k_row("KXNFLGAME", "KXNFLGAME-26SEP20CARATL", "KXNFLGAME-26SEP20CARATL-ATL"))
    spread = classify.classify_kalshi(k_row("KXNFLSPREAD", "KXNFLSPREAD-26SEP20CARATL", "KXNFLSPREAD-26SEP20CARATL-ATL5", line=4.5))
    total = classify.classify_kalshi(k_row("KXNFLTOTAL", "KXNFLTOTAL-26SEP20CARATL", "KXNFLTOTAL-26SEP20CARATL-27", line=26.5))
    assert bet_fields(winner) == ("game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, "no")
    assert bet_fields(spread) == ("spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, "yes")
    assert bet_fields(total) == ("total", 2027, "2026-09-20", "CAR", "ATL", None, 26.5, "yes")


def test_kalshi_game_with_two_letter_codes():
    bet = classify.classify_kalshi(k_row("KXNFLGAME", "KXNFLGAME-26SEP24ATLGB", "KXNFLGAME-26SEP24ATLGB-GB"))
    assert (bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.polarity) == ("2026-09-24", "ATL", "GB", "ATL", "no")


def test_kalshi_futures():
    champion = classify.classify_kalshi(k_row("KXSB", "KXSB-27", "KXSB-27-BUF"))
    division = classify.classify_kalshi(k_row("KXNFLAFCEAST", "KXNFLAFCEAST-27", "KXNFLAFCEAST-27-BUF"))
    seed = classify.classify_kalshi(k_row("KXNFL1SEED", "KXNFL1SEED-AFC26", "KXNFL1SEED-AFC26-BUF"))
    wins = classify.classify_kalshi(k_row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10", line=9.5))
    assert bet_fields(champion) == ("champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(division) == ("division_champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(seed) == ("conf_top_seed", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(wins) == ("season_wins", 2027, None, None, None, "BUF", 9.5, "yes")


# POLYMARKET US

def us_row(event, slug, market_type=None, line=None, start_time="2026-09-20T17:00:00+00:00"):
    return {"venue": "polymarket_us", "contract_id": slug, "event_id": event, "market_type": market_type,
            "line": line, "start_time": start_time, "title": "", "outcome": ""}


def test_polymarket_us_game_kinds():
    winner = classify.classify_polymarket_us(us_row("nfl-phi-ten-2026-09-20", "aec-nfl-phi-ten-2026-09-20", "football_team_full_game_winner"))
    total = classify.classify_polymarket_us(us_row("nfl-phi-ten-2026-09-20", "tsc-nfl-phi-ten-2026-09-20-total-45pt5", "football_team_full_game_total", 45.5))
    assert bet_fields(winner) == ("game_winner", 2027, "2026-09-20", "PHI", "TEN", "PHI", None, "yes")
    assert bet_fields(total) == ("total", 2027, "2026-09-20", "PHI", "TEN", None, 45.5, "yes")


def test_polymarket_us_spread_line_is_the_away_handicap():
    favored = classify.classify_polymarket_us(us_row("nfl-phi-ten-2026-09-20", "asc-nfl-phi-ten-2026-09-20-neg-1pt5", "football_team_full_game_spread", -1.5))
    underdog = classify.classify_polymarket_us(us_row("nfl-phi-ten-2026-09-20", "asc-nfl-phi-ten-2026-09-20-pos-17pt5", "football_team_full_game_spread", 17.5))
    assert bet_fields(favored) == ("spread", 2027, "2026-09-20", "PHI", "TEN", "PHI", 1.5, "yes")
    assert bet_fields(underdog) == ("spread", 2027, "2026-09-20", "PHI", "TEN", "TEN", 17.5, "no")


def test_polymarket_us_futures():
    division = classify.classify_polymarket_us(us_row("nfl-afceast-2027-01-10-w", "tec-nfl-afceast-2027-01-10-w-buf", "futures", start_time=None))
    champion = classify.classify_polymarket_us(us_row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-buf", "futures", start_time=None))
    seed = classify.classify_polymarket_us(us_row("nfl-afc1seed-2027-01-10", "tec-nfl-afc1seed-2027-01-10-buf", "futures", start_time=None))
    assert bet_fields(division) == ("division_champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(champion) == ("champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(seed) == ("conf_top_seed", 2027, None, None, None, "BUF", None, "yes")


def test_polymarket_us_futures_with_glued_suffixes_and_qualifier():
    champion = classify.classify_polymarket_us(us_row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-bufbil", "futures", start_time=None))
    packers = classify.classify_polymarket_us(us_row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-gbpac", "futures", start_time=None))
    qualifier = classify.classify_polymarket_us(us_row("nfl-afc-2027-01-24-champq", "tec-nfl-afc-2027-01-24-champq-kc", "futures", start_time=None))
    assert (champion.kind, champion.subject) == ("champion", "BUF")
    assert (packers.kind, packers.subject) == ("champion", "GB")
    assert bet_fields(qualifier) == ("reach_conf_final", 2027, None, None, None, "KC", None, "yes")


def test_glued_codes_cover_every_odd_team_name():
    assert classify.glued_code("San Francisco 49ers") == "saners"
    assert classify.glued_code("Kansas City Chiefs") == "kanchi"
    assert classify.glued_code("Los Angeles Chargers") == "loscha"
    assert [classify.us_team_suffix(s) for s in ("kanchi", "loscha", "losram", "grepac", "saners", "tambuc", "bufbil", "kc", "gb")] == \
        ["KC", "LAC", "LAR", "GB", "SF", "TB", "BUF", "KC", "GB"]


def test_polymarket_us_skips_props_and_awards():
    assert classify.classify_polymarket_us(us_row("nfl-phi-ten-2026-09-20", "x", "football_player_touchdowns", 0.5)) is None
    assert classify.classify_polymarket_us(us_row("nfl-mvp-2027-02-11-w", "tec-nfl-mvp-2027-02-11-w-abc", "futures", start_time=None)) is None


def test_kalshi_skips_unknown_series_and_missing_lines():
    assert classify.classify_kalshi(k_row("KXNFLRECYDS", "KXNFLRECYDS-26SEP20", "KXNFLRECYDS-26SEP20-X")) is None
    assert classify.classify_kalshi(k_row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10")) is None
