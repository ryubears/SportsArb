"""
Golden cases for the Polymarket US classifier, built from real contract rows.
"""

from catalog.classify import polymarket_us


def row(event, slug, market_type=None, line=None, start_time="2026-09-20T17:00:00+00:00"):
    """
    A Polymarket US contract row with the fields the classifier reads.
    """
    return {"venue": "polymarket_us", "sport": "nfl", "contract_id": slug, "event_id": event, "market_type": market_type,
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


def test_college_games_use_cfb_slugs_and_the_venues_own_codes():
    def college(event, slug, market_type, line=None):
        return polymarket_us.classify(row(event, slug, market_type, line, start_time="2026-10-03T19:30:00+00:00") | {"sport": "ncaaf"})

    winner = college("cfb-boscol-smu-2026-10-03", "aec-cfb-boscol-smu-2026-10-03", "football_team_full_game_winner")
    spread = college("cfb-boscol-smu-2026-10-03", "asc-cfb-boscol-smu-2026-10-03-pos-9pt5", "football_team_full_game_spread", 9.5)
    total = college("cfb-boscol-smu-2026-10-03", "tsc-cfb-boscol-smu-2026-10-03-total-43pt5", "football_team_full_game_total", 43.5)
    aztecs = college("cfb-txst-sdst-2026-10-03", "aec-cfb-txst-sdst-2026-10-03", "football_team_full_game_winner")
    assert bet_fields(winner) == ("game_winner", 2027, "2026-10-03", "BC", "SMU", "BC", None, "yes")
    assert bet_fields(spread) == ("spread", 2027, "2026-10-03", "BC", "SMU", "SMU", 9.5, "no")      # Kalshi's SMU by over 9.5, as its No.
    assert bet_fields(total) == ("total", 2027, "2026-10-03", "BC", "SMU", None, 43.5, "yes")
    assert (aztecs.team_a, aztecs.team_b) == ("TXST", "SDSU")        # sdst is San Diego State here, SDST South Dakota State on Kalshi.
    assert college("nfl-phi-ten-2026-09-20", "aec-nfl-phi-ten-2026-09-20", "football_team_full_game_winner") is None


def test_futures_are_left_out():
    assert polymarket_us.classify(row("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-buf", "futures", start_time=None)) is None
    assert polymarket_us.classify(row("nfl-afceast-2027-01-10-w", "tec-nfl-afceast-2027-01-10-w-buf", "futures", start_time=None)) is None
    assert polymarket_us.classify(row("cfb-champ-2027-01-25-w", "tec-cfb-champ-2027-01-25-w-nd", "futures", start_time=None) | {"sport": "ncaaf"}) is None


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


def mlb(event, slug, market_type, line=None, title="", start_time="2026-09-30T00:00:00+00:00"):
    return polymarket_us.classify(row(event, slug, market_type, line, start_time) | {"sport": "mlb", "title": title})


def test_baseball_game_markets_read_like_footballs_and_team_totals_name_the_team_in_the_slug():
    event = "mlb-bos-nyy-2026-09-29"
    winner = mlb(event, "aec-mlb-bos-nyy-2026-09-29", "baseball_team_full_game_winner")
    favored = mlb(event, "asc-mlb-bos-nyy-2026-09-29-neg-1pt5", "baseball_team_full_game_spread", -1.5)
    underdog = mlb(event, "asc-mlb-bos-nyy-2026-09-29-pos-2pt5", "baseball_team_full_game_spread", 2.5)
    total = mlb(event, "tsc-mlb-bos-nyy-2026-09-29-5pt5", "baseball_team_full_game_total", 5.5)
    team_total = mlb(event, "tsc-mlb-bos-nyy-2026-09-29-tt-nyy-1pt5", "baseball_team_total_runs", 1.5)
    # The 8:00 PM Eastern start is the next day in UTC, and the game date is Eastern.
    assert bet_fields(winner) == ("game_winner", 2026, "2026-09-29", "BOS", "NYY", "BOS", None, "yes")
    assert bet_fields(favored) == ("spread", 2026, "2026-09-29", "BOS", "NYY", "BOS", 1.5, "yes")
    assert bet_fields(underdog) == ("spread", 2026, "2026-09-29", "BOS", "NYY", "NYY", 2.5, "no")
    assert bet_fields(total) == ("total", 2026, "2026-09-29", "BOS", "NYY", None, 5.5, "yes")
    assert bet_fields(team_total) == ("team_total", 2026, "2026-09-29", "BOS", "NYY", "NYY", 1.5, "yes")


def test_baseball_player_props_shift_the_at_least_line_by_a_half():
    prop = mlb("mlb-bos-nyy-2026-09-29", "astatc-mlb-bos-nyy-2026-09-29-k-paytol-gte5", "baseball_player_strikeouts", 5.0,
               title="Will Payton Tolle record at least 5 pitching strikeouts in Game 1: BOS Red Sox vs. NY Yankees?")
    assert bet_fields(prop) == ("player_strikeouts", 2026, "2026-09-29", "BOS", "NYY", "payton tolle", 4.5, "yes")


def nhl(event, slug, market_type, line=None, title="", start_time="2026-09-29T21:00:00+00:00"):
    return polymarket_us.classify(row(event, slug, market_type, line, start_time) | {"sport": "nhl", "title": title})


def test_hockey_markets_read_like_baseballs_and_the_venues_own_codes_name_the_nhls_teams():
    event = "nhl-fla-car-2026-09-29"
    winner = nhl(event, "aec-nhl-fla-car-2026-09-29", "hockey_team_full_game_winner")
    favored = nhl(event, "asc-nhl-fla-car-2026-09-29-neg-1pt5", "hockey_team_full_game_spread", -1.5)
    underdog = nhl(event, "asc-nhl-fla-car-2026-09-29-pos-1pt5", "hockey_team_full_game_spread", 1.5)
    total = nhl(event, "tsc-nhl-fla-car-2026-09-29-5pt5", "hockey_team_full_game_total", 5.5)
    team_total = nhl(event, "tsc-nhl-fla-car-2026-09-29-tt-car-2pt5", "hockey_team_total_goals", 2.5)
    saves = nhl(event, "astatc-nhl-fla-car-2026-09-29-saves-car-gte17", "hockey_team_saves", 17)
    montreal = nhl("nhl-mon-tor-2026-09-29", "aec-nhl-mon-tor-2026-09-29", "hockey_team_full_game_winner", start_time="2026-09-29T23:00:00+00:00")
    assert bet_fields(winner) == ("game_winner", 2027, "2026-09-29", "FLA", "CAR", "FLA", None, "yes")
    assert bet_fields(favored) == ("spread", 2027, "2026-09-29", "FLA", "CAR", "FLA", 1.5, "yes")
    assert bet_fields(underdog) == ("spread", 2027, "2026-09-29", "FLA", "CAR", "CAR", 1.5, "no")
    assert bet_fields(total) == ("total", 2027, "2026-09-29", "FLA", "CAR", None, 5.5, "yes")
    assert bet_fields(team_total) == ("team_total", 2027, "2026-09-29", "FLA", "CAR", "CAR", 2.5, "yes")
    assert saves is None                    # A team's saves, where Kalshi's are a goalie's.
    assert (montreal.team_a, montreal.team_b) == ("MTL", "TOR")


def test_hockey_player_props_shift_the_at_least_line_by_a_half():
    event = "nhl-fla-car-2026-09-29"
    goals = nhl(event, "astatc-nhl-fla-car-2026-09-29-goals-alebar-gte2", "hockey_player_goals", 2.0,
                title="Will Aleksander Barkov record at least 2 goals in FLA vs CAR?")
    points = nhl(event, "astatc-nhl-fla-car-2026-09-29-pts-sebaho-gte1", "hockey_player_points", 1.0,
                 title="Will Sebastian Aho record at least 1 points in FLA vs CAR?")
    assert bet_fields(goals) == ("player_goals", 2027, "2026-09-29", "FLA", "CAR", "aleksander barkov", 1.5, "yes")
    assert bet_fields(points) == ("player_points", 2027, "2026-09-29", "FLA", "CAR", "sebastian aho", 0.5, "yes")


def nba(event, slug, market_type, line=None, title="", start_time="2026-06-14T00:30:00+00:00"):
    return polymarket_us.classify(row(event, slug, market_type, line, start_time) | {"sport": "nba", "title": title})


def test_basketball_markets_read_like_the_others_and_the_venues_own_codes_name_the_nbas_teams():
    event = "nba-ny-sa-2026-06-13"                  # 8:30 PM Eastern, the next day in UTC.
    winner = nba(event, "aec-nba-ny-sa-2026-06-13", "basketball_team_full_game_winner")
    favored = nba(event, "asc-nba-ny-sa-2026-06-13-neg-10pt5", "basketball_team_full_game_spread", -10.5)
    underdog = nba(event, "asc-nba-ny-sa-2026-06-13-pos-4pt5", "basketball_team_full_game_spread", 4.5)
    total = nba(event, "tsc-nba-ny-sa-2026-06-13-197pt5", "basketball_team_full_game_total", 197.5)
    assert bet_fields(winner) == ("game_winner", 2026, "2026-06-13", "NYK", "SAS", "NYK", None, "yes")
    assert bet_fields(favored) == ("spread", 2026, "2026-06-13", "NYK", "SAS", "NYK", 10.5, "yes")
    assert bet_fields(underdog) == ("spread", 2026, "2026-06-13", "NYK", "SAS", "SAS", 4.5, "no")
    assert bet_fields(total) == ("total", 2026, "2026-06-13", "NYK", "SAS", None, 197.5, "yes")
    assert nba(event, "aec-nba-ny-sa-2026-06-13", "moneyline") is None             # Last season's name, left out.
    warriors = nba("nba-gs-pho-2026-10-21", "aec-nba-gs-pho-2026-10-21", "basketball_team_full_game_winner",
                   start_time="2026-10-22T02:00:00+00:00")
    assert (warriors.team_a, warriors.team_b) == ("GSW", "PHX")


def test_basketball_player_props_shift_the_at_least_line_by_a_half():
    event = "nba-ny-sa-2026-06-13"
    points = nba(event, "astatc-nba-ny-sa-2026-06-13-pts-defox-gte10", "basketball_player_points", 10.0,
                 title="Will De'Aaron Fox record at least 10 points in NY vs SA?")
    threes = nba(event, "astatc-nba-ny-sa-2026-06-13-threes-defox-gte2", "basketball_player_threes", 2.0,
                 title="Will De'Aaron Fox record at least 2 three pointers made in NY vs SA?")
    assert bet_fields(points) == ("player_points", 2026, "2026-06-13", "NYK", "SAS", "deaaron fox", 9.5, "yes")
    assert bet_fields(threes) == ("player_threes", 2026, "2026-06-13", "NYK", "SAS", "deaaron fox", 1.5, "yes")


def test_a_doubleheader_is_a_dh_slug_or_two_events_for_one_date_and_teams():
    def contract(event, slug):
        return row(event, slug, "baseball_team_full_game_winner") | {"sport": "mlb"}
    rows = [contract("mlb-stl-cin-2026-05-23", "aec-mlb-stl-cin-2026-05-23"),               # A first game without the suffix.
            contract("mlb-stl-cin-2026-05-23-dh2", "aec-mlb-stl-cin-2026-05-23-dh2"),
            contract("mlb-mil-pit-2026-07-11-dh1", "aec-mlb-mil-pit-2026-07-11-dh1"),       # Its other game not listed.
            contract("mlb-phi-atl-2026-09-29", "aec-mlb-phi-atl-2026-09-29"),
            contract("mlb-phi-atl-2026-09-30", "aec-mlb-phi-atl-2026-09-30")]              # The next day's game is no doubleheader.
    assert polymarket_us.doubleheaders(rows) == {"aec-mlb-stl-cin-2026-05-23", "aec-mlb-stl-cin-2026-05-23-dh2", "aec-mlb-mil-pit-2026-07-11-dh1"}
