"""
Golden cases for the Polymarket US classifier, built from real contract rows.
"""

from catalog.classify import polymarket_us


def row(event, slug, market_type=None, line=None, start_time=None, close_time=None):
    """
    A Polymarket US contract row with the fields the classifier reads.
    """
    return {"venue": "polymarket_us", "sport": "nfl", "contract_id": slug, "event_id": event, "market_type": market_type,
            "line": line, "start_time": start_time, "close_time": close_time, "title": "", "outcome": ""}


def bet_fields(bet):
    """
    The fields that matter for matching, as a tuple.
    """
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line, bet.polarity)


def future(event, slug, sport="nfl", title="", close=None, market_type="futures"):
    return polymarket_us.classify(row(event, slug, market_type, close_time=close) | {"sport": sport, "title": title})


def test_team_futures_read_the_event_shape_and_name_the_team_from_the_market_slug():
    assert bet_fields(future("nfl-champ-2027-02-14-w", "tec-nfl-champ-2027-02-14-w-buf")) == (
        "champion", 2027, None, None, None, "BUF", None, "yes")
    assert future("nfl-afceast-2027-01-10-w", "tec-nfl-afceast-2027-01-10-w-buf").kind == "division_champion"
    assert future("nfl-afc1seed-2027-01-10", "aachc-nfl-afc1seed-2027-01-10-bal").kind == "conf_top_seed"
    assert future("nfl-2027-01-10-playoffq", "aqc-nfl-2027-01-10-playoffq-ari").kind == "make_playoffs"
    assert future("nfl-afc-2027-01-24-champq", "aqc-nfl-afc-2027-01-24-champq-bal").kind == "reach_conf_final"
    assert bet_fields(future("mlb-champ-2026-09-27", "tec-mlb-champ-2026-09-27-lad", "mlb"))[:6] == ("champion", 2026, None, None, None, "LAD")
    assert future("mlb-2026-10-10-alcsq", "aqc-mlb-2026-10-10-alcsq-bos", "mlb").kind == "reach_conf_final"
    assert future("nhl-champ-2027-06-18-w", "tec-nhl-champ-2027-06-18-w-tb", "nhl").subject == "TBL"
    assert future("nhl-atldiv-2027-04-10-w", "tec-nhl-atldiv-2027-04-10-w-bos", "nhl").kind == "division_champion"
    assert future("nba-eastseed1-2027-04-11-w", "aachc-nba-eastseed1-2027-04-11-w-atl", "nba").kind == "conf_top_seed"


def test_college_conference_futures_use_the_venues_own_codes():
    acc = future("cfb-accchamp-2026-12-05-w", "tec-cfb-accchamp-2026-12-05-w-boscol", "ncaaf")
    assert bet_fields(acc) == ("conf_champion", 2027, None, None, None, "BC", None, "yes")      # A college season ends the next year.
    assert future("cfb-secchamp-2026-12-05-winner", "tec-cfb-secchamp-2026-12-05-winner-ala", "ncaaf").kind == "conf_champion"
    assert future("cfb-big12-2026-12-05-w", "tec-cfb-big12-2026-12-05-w-arz", "ncaaf").kind == "conf_champion"
    assert future("cfb-big12-2026-12-04-champq", "aqc-cfb-big12-2026-12-04-champq-arz", "ncaaf").kind == "reach_conf_title_game"
    assert future("cfb-cfp-2027-01-25-finalq", "aqc-cfb-cfp-2027-01-25-finalq-ohiost", "ncaaf").subject == "OSU"


def test_a_wild_card_series_lists_its_teams_in_a_fixed_order():
    braves = future("mlb-nlwc-phi-atl-2026-10-01-w", "tec-mlb-nlwc-phi-atl-2026-10-01-w-atl", "mlb")
    assert bet_fields(braves) == ("wild_card_series", 2026, None, "ATL", "PHI", "ATL", None, "yes")


def test_awards_name_the_player_from_the_market_title():
    pca = future("mlb-nl-2026-11-27-mvp", "tec-mlb-nl-2026-11-27-mvp-petarm", "mlb", title="Pete Crow-Armstrong")
    assert bet_fields(pca) == ("nl_mvp", 2026, None, None, None, "pete crow armstrong", None, "yes")
    assert future("nhl-2027-04-10-mostgoals", "aachc-nhl-2027-04-10-mostgoals-adrkem", "nhl", title="Adrian Kempe").kind == "goals_leader"
    assert future("nfl-mvp-2027-02-11-w", "tec-nfl-mvp-2027-02-11-w-abc") is None         # No name to go by.


def test_season_totals_read_the_line_from_the_market_title():
    over = future("nfl-wins-2027-01-10-ari", "aachc-nfl-wins-2027-01-10-ari-2pt5wins", title="2.5+ Wins")
    at_least = future("nba-2027-04-11-wintotals", "aachc-nba-2027-04-11-wintotals-atl", "nba", title="Atlanta 43+ wins")
    points = future("nhl-2027-04-10-teampts", "aachc-nhl-2027-04-10-teampts-ana", "nhl", title="ANA Ducks 95+")
    assert bet_fields(over) == ("season_wins", 2027, None, None, None, "ARI", 2.5, "yes")      # Over 2.5.
    assert (at_least.subject, at_least.line) == ("ATL", 42.5)                                  # At least 43.
    assert (points.kind, points.subject, points.line) == ("season_points", "ANA", 94.5)
    assert future("nfl-wins-ou-2027-01-10", "aachc-nfl-wins-ou-2027-01-10-ari", title="Arizona 4.5+").line == 4.5
    assert future("nfl-wins-2027-01-10-ari", "aachc-nfl-wins-2027-01-10-ari-x", title="Win Total") is None


def test_skips_games_and_futures_nobody_pairs():
    assert polymarket_us.classify(row("nfl-phi-ten-2026-09-20", "aec-nfl-phi-ten-2026-09-20", "football_team_full_game_winner")) is None
    assert future("nfl-passyds-2027-01-10-patmah", "astatc-nfl-passyds-2027-01-10-patmah-3750pt5yds", title="3,750.5+ Passing Yards") is None
    assert future("mlb-champ-2026-09-27", "tec-mlb-champ-2026-09-27-lad") is None      # An MLB event under the NFL.


def test_leaders_name_the_player_and_their_shape_the_stat():
    yards = future("nfl-mostrecyds-2027-01-10", "aachc-nfl-mostrecyds-2027-01-10-jaxnji", title="Jaxon Smith-Njigba")
    sec = future("cfb-sec-2026-11-28-mostpassyds", "aachc-cfb-sec-2026-11-28-mostpassyds-kamtay", "ncaaf", title="Kamario Taylor")
    homers = future("mlb-pshrs-2026-10-31-leader", "aachc-mlb-pshrs-2026-10-31-leader-kyltuc", "mlb", title="Kyle Tucker")
    boot = future("epl-2027-05-30-topscorer", "aachc-epl-2027-05-30-topscorer-eplerlihaa", "epl", title="Erling Haaland")
    assert bet_fields(yards) == ("receiving_yards_leader", 2027, None, None, None, "jaxon smith njigba", None, "yes")
    assert (sec.kind, sec.season, sec.subject) == ("sec_passing_yards_leader", 2027, "kamario taylor")
    assert (homers.kind, homers.season) == ("postseason_home_runs_leader", 2026)
    assert (boot.kind, boot.subject) == ("goals_leader", "erling haaland")


def test_a_players_season_total_takes_its_line_from_the_shape():
    yards = future("nfl-2027-01-10-1000recyds", "aachc-nfl-2027-01-10-1000recyds-amobro", title="Amon-Ra St. Brown")
    tds = future("nfl-2027-01-10-30passtds", "aachc-nfl-2027-01-10-30passtds-bropur", title="Brock Purdy")
    assert bet_fields(yards) == ("season_receiving_yards", 2027, None, None, None, "amon ra st brown", 999.5, "yes")
    assert (tds.kind, tds.line) == ("season_passing_touchdowns", 29.5)


def test_team_records_and_points_lines_in_the_shape():
    best = future("nfl-bestrecord-2027-01-10", "aachc-nfl-bestrecord-2027-01-10-buf", title="BUF Bills")
    nba = future("nba-2027-04-11-bestrecord", "aachc-nba-2027-04-11-bestrecord-atl", "nba", title="Atlanta")
    last = future("nfl-2027-01-10-lastundefeated", "aachc-nfl-2027-01-10-lastundefeated-kc", title="KC Chiefs")
    unbeaten = future("cfb-undefeated-2026-11-28", "aachc-cfb-undefeated-2026-11-28-nd", "ncaaf", title="Notre Dame")
    hundred = future("nhl-pts100-2027-04-10", "aachc-nhl-pts100-2027-04-10-chi", "nhl", title="Chicago Blackhawks")
    assert bet_fields(best) == ("best_record", 2027, None, None, None, "BUF", None, "yes")
    assert (nba.kind, nba.subject) == ("best_record", "ATL")
    assert (last.kind, last.subject) == ("last_undefeated", "KC")
    assert (unbeaten.kind, unbeaten.subject) == ("undefeated", "ND")
    assert (hundred.kind, hundred.subject, hundred.line) == ("season_points", "CHI", 99.5)


def test_the_conference_that_wins_the_college_title():
    assert future("cfb-conf-2027-01-25-w", "tec-cfb-conf-2027-01-25-w-bigten", "ncaaf", title="Big Ten").subject == "big_ten"
    assert future("cfb-conf-2027-01-25-w", "tec-cfb-conf-2027-01-25-w-ind", "ncaaf", title="Independent") is None


def test_soccer_futures_name_the_team_and_read_points_from_the_title():
    champion = future("epl-title-2027-05-30-w", "tec-epl-title-2027-05-30-w-mnc", "epl", title="Manchester City")
    top4 = future("epl-2027-05-30-top4", "arankc-epl-2027-05-30-top4-cfc", "epl", title="Chelsea")
    down = future("epl-2027-05-30-relegation", "aachc-epl-2027-05-30-relegation-cov", "epl", title="Coventry City FC")
    points = future("epl-2027-05-30-points", "aachc-epl-2027-05-30-points-ars", "epl", title="Arsenal 78+")
    lines = future("epl-pts-2027-05-30-ars", "aachc-epl-pts-2027-05-30-ars-70pts", "epl", title="70+ Points")
    barca = future("lal-title-2027-05-30-w", "tec-lal-title-2027-05-30-w-fcb", "laliga", title="FC Barcelona")
    bayern = future("bun-title-2027-05-22-w", "tec-bun-title-2027-05-22-w-fcb", "bundesliga", title="FC Bayern München")
    assert bet_fields(champion) == ("champion", 2027, None, None, None, "MCI", None, "yes")
    assert (top4.kind, top4.subject, down.kind, down.subject) == ("top_4", "CFC", "relegated", "COV")
    assert (points.kind, points.subject, points.line) == ("season_points", "ARS", 77.5)
    assert (lines.subject, lines.line) == ("ARS", 69.5)
    assert (barca.subject, bayern.subject) == ("BAR", "BMU")       # fcb is a different club in each league.


def test_a_shape_two_sports_share_is_read_by_its_prefix_first():
    epl = future("epl-2027-05-30-lastplace", "arankc-epl-2027-05-30-lastplace-cov", "epl", title="Coventry City FC")
    ucl = future("ucl-2027-01-27-lastplace", "aachc-ucl-2027-01-27-lastplace-lask", "ucl", title="LASK Linz")
    assert (epl.kind, ucl.kind, ucl.subject) == ("last_place", "league_phase_last", "ASK")
    assert future("ucl-2027-06-05-finalq", "aqc-ucl-2027-06-05-finalq-mnc", "ucl", title="Manchester City").kind == "reach_final"
    assert future("uefa-bdor-2026-10-26-w", "tec-uefa-bdor-2026-10-26-w-lamyam", "ucl", title="Lamine Yamal").kind == "ballon_dor"
    assert future("atp-2026-12-31-no1", "arankc-atp-2026-12-31-no1-jansin", "tennis", title="Jannik Sinner").kind == "atp_year_end_no1"
    assert future("wta-2026-12-31-no1", "arankc-wta-2026-12-31-no1-arysab", "tennis", title="Aryna Sabalenka").kind == "wta_year_end_no1"


def test_a_slug_with_the_wrong_year_takes_the_events_end():
    final = future("ucl-final-2026-06-05-w", "tec-ucl-final-2026-06-05-w-fcb", "ucl", title="FC Barcelona", close="2027-06-05T23:59:00+00:00")
    assert (final.kind, final.season, final.subject) == ("champion", 2027, "BAR")
    usual = future("mlb-champ-2026-09-27", "tec-mlb-champ-2026-09-27-lad", "mlb", close="2026-11-05T04:00:00+00:00")
    assert usual.season == 2026


def test_title_holders_and_champions_name_the_person():
    ufc = future("ufc-lightheavyw-2026-12-31-champ", "aachc-ufc-lightheavyw-2026-12-31-champ-carulb", "ufc", title="Carlos Ulberg")
    f1 = future("f1-dc-2026-12-06-w", "tec-f1-dc-2026-12-06-w-kimant", "f1", title="Kimi Antonelli")
    team = future("f1-cc-2026-12-06-w", "tec-f1-cc-2026-12-06-w-merce", "f1", title="Mercedes AMG Motorsport")
    nascar = future("nascar-cupseries-2026-11-08-w", "tec-nascar-cupseries-2026-11-08-w-joelog", "nascar", title="Joey Logano")
    darts = future("pdc-worldchamp-2027-01-03-w", "tec-pdc-worldchamp-2027-01-03-w-luklit", "darts", title="Luke Littler")
    assert bet_fields(ufc) == ("light_heavyweight_champion", 2027, None, None, None, "carlos ulberg", None, "yes")
    assert (f1.kind, f1.subject, team.kind, team.subject) == ("drivers_champion", "kimi antonelli", "constructors_champion", "MER")
    assert (nascar.kind, darts.kind, darts.season) == ("cup_series_champion", "world_champion", 2027)
    assert future("ufc-heavyw-2026-12-31-champ", "aachc-ufc-heavyw-2026-12-31-champ-vac", "ufc", title="Vacant") is None


def election(event, slug, title="", market_type="futures"):
    return polymarket_us.classify(row(event, slug, market_type) | {"sport": "politics", "title": title})


def test_elections_name_the_race_and_the_party_or_candidate():
    house = election("usho-midterms-2026-11-03", "paccc-usho-midterms-2026-11-03-dem", "Democratic Party", "election")
    senate = election("usse-ga-2026-11-03", "ewc-usse-ga-2026-11-03-rep", "Mike Collins (R)")
    osborn = election("usse-ne-2026-11-03", "ewc-usse-ne-2026-11-03-danosb", "Dan Osborn (I)")
    seat = election("ushr-az-01-2026-11-03", "ushrewc-ushr-az-01-2026-11-03-dem", "Amish Shah (D)")
    at_large = election("ushr-wy-al-2026-11-03", "ushrewc-ushr-wy-al-2026-11-03-rep", "Chuck Gray (R)")
    assert bet_fields(house) == ("house_control", 2026, None, None, None, "D", None, "yes")
    assert (senate.kind, senate.season, senate.subject) == ("senate_race", 2026, "GA R")
    assert (osborn.subject, seat.kind, seat.subject, at_large.subject) == ("NE dan osborn", "house_race", "AZ-01 D", "WY-AL R")


def test_a_top_two_state_pairs_only_candidates():
    assert election("ushr-ca-06-2026-11-03", "ushrewc-ushr-ca-06-2026-11-03-rep", "Republican Party") is None
    alaska = election("usgub-ak-2026-11-03", "usgubewc-usgub-ak-2026-11-03-clibis", "Click Bishop")
    assert (alaska.kind, alaska.subject) == ("governor_race", "AK click bishop")
    assert election("usse-xx-2026-11-03", "ewc-usse-xx-2026-11-03-dem") is None             # No such state.
