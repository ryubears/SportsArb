"""
Golden cases for the classifier, built from real contract rows seen on both venues.
"""

import classify


def pm_row(**fields):
    """
    A Polymarket contract row with the fields the classifier reads.
    """
    row = {"venue": "polymarket", "contract_id": "token", "event_id": "", "event_title": None,
           "title": "", "outcome": "Yes", "market_type": None, "line": None, "start_time": None}
    row.update(fields)
    return row


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

def test_teams_in_text_handles_long_names_and_word_boundaries():
    assert classify.teams_in_text("Los Angeles Rams vs Los Angeles C") == ["LAR", "LAC"]
    assert classify.teams_in_text("Jalen Ramsey and the Jets") == ["NYJ"]
    assert classify.teams_in_text("Will the 49ers win?") == ["SF"]
    assert classify.teams_in_text("Will another team win?") == []
    assert classify.teams_in_text(None) == []


def test_team_from_label_accepts_nicknames_and_codes():
    assert classify.team_from_label("Falcons") == "ATL"
    assert classify.team_from_label("ATL") == "ATL"
    assert classify.team_from_label("LA") == "LAR"
    assert classify.team_from_label("Falcons and Panthers") is None


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


# POLYMARKET GAMES

GAME = dict(event_id="nfl-car-atl-2026-09-20", start_time="2026-09-20T17:00:00+00:00")


def test_polymarket_moneyline():
    bet = classify.classify_polymarket(pm_row(market_type="moneyline", title="Panthers vs. Falcons", outcome="Panthers", **GAME))
    assert bet_fields(bet) == ("game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None, "yes")


def test_polymarket_game_date_uses_eastern_time():
    bet = classify.classify_polymarket(pm_row(market_type="moneyline", title="Patriots vs. Bears", outcome="Bears",
                                              event_id="nfl-ne-chi-2026-10-23", start_time="2026-10-23T00:15:00+00:00"))
    # Winners are stated as the away team winning, so the home outcome is polarity no.
    assert (bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.polarity) == ("2026-10-22", "NE", "CHI", "NE", "no")


def test_polymarket_spread_favorite_and_underdog_sides():
    favorite = classify.classify_polymarket(pm_row(market_type="spreads", title="Spread: Falcons (-4.5)", outcome="Falcons", **GAME))
    underdog = classify.classify_polymarket(pm_row(market_type="spreads", title="Spread: Falcons (-4.5)", outcome="Panthers", **GAME))
    assert bet_fields(favorite) == ("spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, "yes")
    assert bet_fields(underdog) == ("spread", 2027, "2026-09-20", "CAR", "ATL", "ATL", 4.5, "no")


def test_polymarket_spread_with_team_codes():
    bet = classify.classify_polymarket(pm_row(market_type="spreads", title="Spread: KC (-5.5)", outcome="IND",
                                              event_id="nfl-ind-kc-2026-09-21", start_time="2026-09-21T00:20:00+00:00"))
    assert (bet.subject, bet.line, bet.polarity) == ("KC", 5.5, "no")


def test_polymarket_total_under_is_polarity_no():
    bet = classify.classify_polymarket(pm_row(market_type="totals", title="Panthers vs. Falcons: O/U 45.5", outcome="Under", line=45.5, **GAME))
    assert bet_fields(bet) == ("total", 2027, "2026-09-20", "CAR", "ATL", None, 45.5, "no")


def test_polymarket_skips_season_series_and_props():
    series = pm_row(market_type="moneyline", event_id="pro-football-ravens-vspt-browns-season-series-winner",
                    title="Will the Ravens win the season series against the Browns?")
    assert classify.classify_polymarket(series) is None
    assert classify.classify_polymarket(pm_row(market_type="receiving_yards", **GAME)) is None


# POLYMARKET FUTURES

def test_polymarket_champion_and_conference():
    champion = classify.classify_polymarket(pm_row(event_title="Pro Football: 2027 Champion",
                                                   title="Will the Buffalo Bills win the 2027 NFL league championship?"))
    conference = classify.classify_polymarket(pm_row(event_title="Pro Football: 2027 AFC Champion",
                                                     title="Will Buffalo Bills win the 2027 NFL AFC Championship?"))
    assert bet_fields(champion) == ("champion", 2027, None, None, None, "BUF", None, "yes")
    assert bet_fields(conference) == ("conf_champion", 2027, None, None, None, "BUF", None, "yes")


def test_polymarket_division_and_seed_use_season_end_year():
    division = classify.classify_polymarket(pm_row(event_title="Pro Football: AFC East Champion",
                                                   title="Will Buffalo Bills win the 2026 AFC East?"))
    seed = classify.classify_polymarket(pm_row(event_title="Pro Football: 2026-27 AFC #1 Seed",
                                               title="Will the Buffalo Bills be the AFC #1 seed for the 2027 NFL Playoffs?"))
    assert (division.kind, division.season, division.subject) == ("division_champion", 2027, "BUF")
    assert (seed.kind, seed.season, seed.subject) == ("conf_top_seed", 2027, "BUF")


def test_polymarket_season_wins_both_layouts():
    grouped = classify.classify_polymarket(pm_row(event_title="Pro Football: 2026 Regular Season Win Totals",
                                                  title="Will the Buffalo Bills win more than 10.5 games in the 2026 NFL Regular Season?",
                                                  outcome="U 10.5"))
    single = classify.classify_polymarket(pm_row(event_title="Pro Football: Bills 2026 Win Total",
                                                 title="Will the Buffalo Bills have more than 10.5 wins during the 2026 NFL season?"))
    assert bet_fields(grouped) == ("season_wins", 2027, None, None, None, "BUF", 10.5, "no")
    assert bet_fields(single) == ("season_wins", 2027, None, None, None, "BUF", 10.5, "yes")


def test_polymarket_skips_futures_without_one_team():
    assert classify.classify_polymarket(pm_row(event_title="Pro Football: 2027 Champion",
                                               title="Will another team win the 2027 NFL league championship?")) is None


def test_kalshi_skips_unknown_series_and_missing_lines():
    assert classify.classify_kalshi(k_row("KXNFLRECYDS", "KXNFLRECYDS-26SEP20", "KXNFLRECYDS-26SEP20-X")) is None
    assert classify.classify_kalshi(k_row("KXNFLWINS", "KXNFLWINS-27BUF", "KXNFLWINS-27BUF-10")) is None
