"""
The bets on one event, a game, a match, or a race, read alike from both venues: each case is a real Kalshi contract and
the real Polymarket US contract on the same bet, and the two classifiers must agree on its identity and on which side
each contract holds.
"""

from catalog import match
from catalog.classify import kalshi, polymarket_us, teams


def kalshi_bet(series, event, ticker, sport, outcomes=None, **fields):
    row = {"venue": "kalshi", "sport": sport, "series_id": series, "event_id": event, "contract_id": ticker, "title": "",
           "outcome": "", "event_title": None, "market_type": None, "line": None, "rules": None, "start_time": None, "close_time": None}
    return kalshi.classify(row | fields, outcomes or {})


def pm_bet(event, slug, sport, market_type="", **fields):
    row = {"venue": "polymarket_us", "sport": sport, "event_id": event, "contract_id": slug, "market_type": market_type, "title": "",
           "outcome": "", "event_title": None, "line": None, "rules": None, "start_time": None, "close_time": None}
    return polymarket_us.classify(row | fields)


def identity(bet):
    return (bet.kind, bet.season, bet.game_date, bet.team_a, bet.team_b, bet.subject, bet.line)


def same(k, p, polarities=("yes", "yes")):
    """
    Whether the two bets are the same bet, with the contracts on the sides given.
    """
    assert k is not None and p is not None
    assert identity(k) == identity(p) and (k.polarity, p.polarity) == polarities, (identity(k), identity(p))
    return identity(k)


def test_a_team_games_winner_spread_and_total_read_alike():
    start = "2026-09-20T17:00:00+00:00"
    # The winner is stated as the away team winning, Kalshi's home team market being its complement.
    assert same(kalshi_bet("KXNFLGAME", "KXNFLGAME-26SEP20CARATL", "KXNFLGAME-26SEP20CARATL-ATL", "nfl"),
                pm_bet("nfl-car-atl-2026-09-20", "aec-nfl-car-atl-2026-09-20", "nfl", "football_team_full_game_winner", start_time=start),
                ("no", "yes")) == ("game_winner", 2027, "2026-09-20", "CAR", "ATL", "CAR", None)
    # A positive line is the second team not winning by more than it.
    same(kalshi_bet("KXNFLSPREAD", "KXNFLSPREAD-26SEP20CARATL", "KXNFLSPREAD-26SEP20CARATL-ATL4", "nfl", line=3.5),
         pm_bet("nfl-car-atl-2026-09-20", "asc-nfl-car-atl-2026-09-20-pos-3pt5", "nfl", "football_team_full_game_spread", line=3.5,
                start_time=start), ("yes", "no"))
    same(kalshi_bet("KXWNBATOTAL", "KXWNBATOTAL-26OCT04NYATL", "KXWNBATOTAL-26OCT04NYATL-162", "wnba", line=161.5),
         pm_bet("wnba-ny-atl-2026-10-04", "tsc-wnba-ny-atl-2026-10-04-161pt5", "wnba", "basketball_team_full_game_total", line=161.5,
                start_time="2026-10-04T18:00:00+00:00"))


def test_a_doubleheaders_games_are_left_out_on_both_venues():
    rows = [{"sport": "mlb", "series_id": "KXMLBGAME", "event_id": f"KXMLBGAME-26MAY23{t}STLCIN", "contract_id": f"k{t}"} for t in ("1310", "1910")]
    assert kalshi.doubleheaders(rows) == {"k1310", "k1910"}
    pm = [{"sport": "mlb", "event_id": e, "contract_id": e} for e in ("mlb-stl-cin-2026-05-23", "mlb-stl-cin-2026-05-23-dh2")]
    assert polymarket_us.doubleheaders(pm) == {"mlb-stl-cin-2026-05-23", "mlb-stl-cin-2026-05-23-dh2"}


def test_a_soccer_matchs_result_spread_total_score_and_corners_read_alike():
    event, k_event = "epl-ars-lee-2026-10-10", "26OCT10ARSLEE"
    assert same(kalshi_bet("KXEPLGAME", f"KXEPLGAME-{k_event}", f"KXEPLGAME-{k_event}-TIE", "epl"),
                pm_bet(event, f"atc-{event}-draw", "epl", "soccer_team_full_time_winner")) == (
        "result", 2027, "2026-10-10", "ARS", "LEE", "tie", None)
    same(kalshi_bet("KXEPLGAME", f"KXEPLGAME-{k_event}", f"KXEPLGAME-{k_event}-LEE", "epl"),
         pm_bet(event, f"atc-{event}-lee", "epl", "soccer_team_full_time_winner"))
    same(kalshi_bet("KXEPL1H", f"KXEPL1H-{k_event}", f"KXEPL1H-{k_event}-ARS", "epl"),
         pm_bet(event, f"atc-{event}-fh-ars", "epl", "soccer_team_first_half_winner"))
    # Arsenal at +1.5 on Polymarket US is Leeds not winning by more than 1.5, Kalshi's 'LEE2' its complement.
    same(kalshi_bet("KXEPLSPREAD", f"KXEPLSPREAD-{k_event}", f"KXEPLSPREAD-{k_event}-LEE2", "epl", line=1.5),
         pm_bet(event, f"asc-{event}-pos-1pt5", "epl", "soccer_team_full_game_spread", line=1.5), ("yes", "no"))
    same(kalshi_bet("KXEPLSPREAD", f"KXEPLSPREAD-{k_event}", f"KXEPLSPREAD-{k_event}-ARS2", "epl", line=1.5),
         pm_bet(event, f"asc-{event}-neg-1pt5", "epl", "soccer_team_full_game_spread", line=-1.5))
    same(kalshi_bet("KXEPLTOTAL", f"KXEPLTOTAL-{k_event}", f"KXEPLTOTAL-{k_event}-3", "epl", line=2.5),
         pm_bet(event, f"tsc-{event}-2pt5", "epl", "soccer_team_full_game_total", line=2.5))
    same(kalshi_bet("KXEPLBTTS", f"KXEPLBTTS-{k_event}", f"KXEPLBTTS-{k_event}-BTTS", "epl"),
         pm_bet(event, f"astatc-{event}-btts", "epl", "soccer_game_btts", line=1))
    assert same(kalshi_bet("KXEPLSCORE", f"KXEPLSCORE-{k_event}", f"KXEPLSCORE-{k_event}-ARS2LEE1", "epl"),
                pm_bet(event, f"atc-{event}-exact-score-2-1", "epl", "soccer_game_exact_score"))[5] == "ARS2 LEE1"
    # Kalshi's '10+ corners' is more than 9.5.
    same(kalshi_bet("KXEPLCORNERS", f"KXEPLCORNERS-{k_event}", f"KXEPLCORNERS-{k_event}-10", "epl", line=9.5),
         pm_bet(event, f"astatc-{event}-cor-all-gt9pt5", "epl", "soccer_game_total_corners", line=9.5))


def test_a_tennis_matchs_winner_spread_total_set_and_score_read_alike():
    names = {"KXATPMATCH-26OCT03VACFIL": ["Valentin Vacherot", "Arthur Fils"]}       # A winner's title gives only the last names.
    event, title = "atp-valvac-artfil-2026-10-03", "Valentin Vacherot vs. Arthur Fils"
    k_title = "Valentin Vacherot vs Arthur Fils"
    # The two are kept in order of their keys, so Fils first, and a market on Vacherot is its complement on both venues.
    assert same(kalshi_bet("KXATPMATCH", "KXATPMATCH-26OCT03VACFIL", "KXATPMATCH-26OCT03VACFIL-VAC", "tennis", names,
                           outcome="Valentin Vacherot", event_title="Vacherot vs Fils"),
                pm_bet(event, f"aec-{event}", "tennis", "tennis_match_winner", outcome="Valentin Vacherot", event_title=title),
                ("no", "no")) == ("match_winner", 2026, "2026-10-03", "arthur fils", "vacherot valentin", "arthur fils", None)
    same(kalshi_bet("KXATPGSPREAD", "KXATPGSPREAD-26OCT03VACFIL", "KXATPGSPREAD-26OCT03VACFIL-FIL4", "tennis", line=3.5,
                    outcome="Arthur Fils -3.5 games", event_title=f"{k_title}: Game Spread"),
         pm_bet(event, f"asc-{event}-gs-pos-3pt5", "tennis", "tennis_match_games_spread", line=3.5, event_title=title), ("yes", "no"))
    same(kalshi_bet("KXATPGTOTAL", "KXATPGTOTAL-26OCT03VACFIL", "KXATPGTOTAL-26OCT03VACFIL-18", "tennis", line=17.5,
                    event_title=f"{k_title}: Total Games"),
         pm_bet(event, f"tsc-{event}-tg-17pt5", "tennis", "tennis_match_total_games", line=17.5, event_title=title))
    same(kalshi_bet("KXATPSETWINNER", "KXATPSETWINNER-26OCT03VACFIL-1", "KXATPSETWINNER-26OCT03VACFIL-1-VAC", "tennis",
                    outcome="Valentin Vacherot", event_title=f"{k_title}: Set 1 Winner"),
         pm_bet(event, f"astatc-{event}-sw1", "tennis", "tennis_set_1_winner", title="Will Valentin Vacherot win set 1 against Arthur Fils?",
                event_title=title), ("no", "no"))
    assert same(kalshi_bet("KXATPEXACTMATCH", "KXATPEXACTMATCH-26OCT03VACFIL", "KXATPEXACTMATCH-26OCT03VACFIL-FIL20", "tennis",
                           outcome="Arthur Fils wins 2-0", event_title=f"{k_title}: Exact Match Score"),
                pm_bet(event, f"astatc-{event}-es-0-2", "tennis", "tennis_match_exact_score", event_title=title))[5] == "arthur fils 2-0"


def test_one_tennis_match_dated_a_day_apart_is_one_pair():
    names = {"KXWTAMATCH-26OCT03LULI": ["Jia Jing Lu", "Zongyu Li"]}
    k = kalshi_bet("KXWTAMATCH", "KXWTAMATCH-26OCT03LULI", "KXWTAMATCH-26OCT03LULI-LU", "tennis", names, outcome="Jia Jing Lu",
                   event_title="Lu vs Li")
    p = pm_bet("wta-jialu-zonli-2026-10-02", "aec-wta-jialu-zonli-2026-10-02", "tennis", "tennis_match_winner", outcome="Jia Jing Lu",
               event_title="Jia Jing Lu vs. Zongyu Li")
    rows = [dict(vars(b), close_time=None) for b in (k, p)]
    pairs, unmatched = match.match(rows, "tennis")
    assert [(pair.game_date, len(pair.members)) for pair in pairs] == [("2026-10-02", 2)] and unmatched == []


def test_a_national_teams_match_reads_alike_by_each_venues_codes_and_is_one_pair_a_day_apart():
    # Kalshi's Chile is CHI and Polymarket US's chl, and Kalshi dates the match at 02:30 UTC October 6, Polymarket US October 5.
    k = kalshi_bet("KXINTLFRIENDLYGAME", "KXINTLFRIENDLYGAME-26OCT06MEXCHI", "KXINTLFRIENDLYGAME-26OCT06MEXCHI-CHI", "intl")
    p = pm_bet("intf-mex-chl-2026-10-05", "atc-intf-mex-chl-2026-10-05-chl", "intl", "soccer_team_full_time_winner")
    assert (k.kind, k.team_a, k.team_b, k.subject, k.game_date, p.game_date) == ("result", "CHI", "MEX", "CHI", "2026-10-06", "2026-10-05")
    pairs, unmatched = match.match([dict(vars(b), close_time=None) for b in (k, p)], "intl")
    assert [(pair.label, len(pair.members)) for pair in pairs] == [("intl result 2026-10-05 CHI@MEX CHI", 2)] and unmatched == []
    # The women's go by the same codes, a sport of their own so their matches never pair with the men's.
    same(kalshi_bet("KXFIFAWGAME", "KXFIFAWGAME-26OCT09KAZIRL", "KXFIFAWGAME-26OCT09KAZIRL-TIE", "intlw"),
         pm_bet("uwwcq-kaz-irl-2026-10-09", "atc-uwwcq-kaz-irl-2026-10-09-draw", "intlw", "soccer_team_full_time_winner"))
    same(kalshi_bet("KXUEFANLSPREAD", "KXUEFANLSPREAD-26OCT06ALBSMR", "KXUEFANLSPREAD-26OCT06ALBSMR-SMR2", "intl", line=1.5),
         pm_bet("unl-alb-smr-2026-10-06", "asc-unl-alb-smr-2026-10-06-pos-1pt5", "intl", "soccer_team_full_game_spread", line=1.5),
         ("yes", "no"))


def test_an_esports_matchs_winner_maps_and_total_read_alike_by_the_teams_names():
    k_game, pm_event, pm_title = "26OCT061000GOTMEL", "cs2-mel-goth-2026-10-06", "mellren vs. Gothic"
    # Polymarket US's winner is mellren, named first, winning, Kalshi's Gothic winning: the same bet's two sides.
    assert same(kalshi_bet("KXCS2GAME", f"KXCS2GAME-{k_game}", f"KXCS2GAME-{k_game}-GOT", "cs2", outcome="Gothic",
                           event_title="Gothic vs. mellren"),
                pm_bet(pm_event, f"aec-{pm_event}", "cs2", "esports_match_winner", outcome="mellren", event_title=pm_title),
                ("yes", "no")) == ("match_winner", 2026, "2026-10-06", "gothic", "mellren", "gothic", None)
    same(kalshi_bet("KXCS2MAP", f"KXCS2MAP-{k_game}-2", f"KXCS2MAP-{k_game}-2-MEL", "cs2", outcome="mellren",
                    event_title="Gothic vs. mellren: Map 2"),
         pm_bet(pm_event, f"astatc-{pm_event}-map2", "cs2", "esports_map_winner_2", outcome="Yes", title="Will mellren win Map 2 vs Gothic?",
                event_title=pm_title), ("no", "no"))
    same(kalshi_bet("KXCS2TOTALMAPS", f"KXCS2TOTALMAPS-{k_game}", f"KXCS2TOTALMAPS-{k_game}-3", "cs2", line=2.5,
                    event_title="Gothic vs. mellren: Total Maps"),
         pm_bet(pm_event, f"tsc-{pm_event}-tot-2pt5", "cs2", "esports_series_total_maps", line=2.5, event_title=pm_title))
    # A League of Legends game is a map, and 'Cupid Esports' and 'Cupid' the same team.
    assert same(kalshi_bet("KXLOLMAP", "KXLOLMAP-26OCT061600FUECPD-1", "KXLOLMAP-26OCT061600FUECPD-1-FUE", "lol", outcome="Fuego",
                           event_title="Fuego vs. Cupid Esports: Map 1"),
                pm_bet("lol-cpd-fue-2026-10-06", "astatc-lol-cpd-fue-2026-10-06-game1", "lol", "esports_game_winner_1", outcome="Yes",
                       title="Will Cupid Esports win Game 1 vs Fuego?", event_title="Cupid Esports vs. Fuego"), ("no", "yes"))[0] == "map_1_winner"
    # Overwatch's title leads with its tournament, and a code may hold digits, 'O2B'.
    same(kalshi_bet("KXOWGAME", "KXOWGAME-26OCT090400O2BFAL", "KXOWGAME-26OCT090400O2BFAL-FAL", "ow", outcome="Falcons",
                    event_title="OCS Korea Stage 3 2026: O2 Blast vs. Falcons"),
         pm_bet("ow-fal-o2b-2026-10-09", "aec-ow-fal-o2b-2026-10-09", "ow", "esports_match_winner", outcome="Falcons",
                event_title="Falcons vs. O2 Blast"))
    # A map whose number is not the market's, or a winner named in neither team, is no bet.
    assert pm_bet(pm_event, f"astatc-{pm_event}-map2", "cs2", "esports_map_winner_1", outcome="Yes", title="Will mellren win Map 2 vs Gothic?",
                  event_title=pm_title) is None
    assert kalshi_bet("KXCS2GAME", f"KXCS2GAME-{k_game}", f"KXCS2GAME-{k_game}-X", "cs2", outcome="Someone",
                      event_title="Gothic vs. mellren") is None


def test_an_esports_teams_key_is_its_name_without_the_words_one_venue_adds():
    assert teams.team_key("Team Falcons") == teams.team_key("Falcons") == "falcons"
    assert teams.team_key("Rounds.gg") == teams.team_key("Rounds") == "rounds"
    assert teams.team_key("Movistar KOI Fénix") == "movistarkoifenix" and teams.team_key("MOUZ NXT") != teams.team_key("MOUZ")
    assert teams.team_sides("Gothic vs. mellren: Map 2") == ("gothic", "mellren") and teams.team_sides("Gothic vs. Gothic") is None


def test_an_esports_rematch_on_one_date_is_left_out_on_both_venues():
    rows = [{"sport": "cs2", "series_id": "KXCS2GAME", "event_id": f"KXCS2GAME-26OCT07{t}MOUNXT", "contract_id": f"k{t}"} for t in ("1000", "1800")]
    assert kalshi.doubleheaders(rows) == {"k1000", "k1800"}
    pm = [{"sport": "cs2", "event_id": e, "contract_id": e} for e in ("cs2-mou-nxt-2026-10-07", "cs2-mou-nxt-2026-10-07-dh2")]
    assert polymarket_us.doubleheaders(pm) == {"cs2-mou-nxt-2026-10-07", "cs2-mou-nxt-2026-10-07-dh2"}


def test_a_fights_winner_distance_and_round_read_alike_whichever_way_a_name_is_written():
    names = {"KXUFCFIGHT-26OCT03SILCON": ["Natalia Silva", "Wang Cong"]}         # 'Wang Cong' on Kalshi is 'Cong Wang' on Polymarket US.
    event, title = "ufc-natsil-conwan-2026-10-03", "Natalia Silva vs. Cong Wang"
    same(kalshi_bet("KXUFCFIGHT", "KXUFCFIGHT-26OCT03SILCON", "KXUFCFIGHT-26OCT03SILCON-SIL", "ufc", names, outcome="Natalia Silva",
                    event_title="332: Silva vs Cong"),
         pm_bet(event, f"aec-{event}", "ufc", "ufc_fight_winner", outcome="Natalia Silva", event_title=title), ("no", "no"))
    same(kalshi_bet("KXUFCDISTANCE", "KXUFCDISTANCE-26OCT03SILWAN", "KXUFCDISTANCE-26OCT03SILWAN-DIST", "ufc",
                    event_title="Natalia Silva vs Cong Wang: Go the Distance"),
         pm_bet(event, f"astatc-{event}-gtd-yes", "ufc", "ufc_go_the_distance", event_title=title))
    assert same(kalshi_bet("KXUFCVICROUND", "KXUFCVICROUND-26OCT03SILWAN", "KXUFCVICROUND-26OCT03SILWAN-SIL1", "ufc",
                           outcome="Natalia Silva to win in Round 1", event_title="Natalia Silva vs. Cong Wang: Round of Victory"),
                pm_bet(event, f"astatc-{event}-rov-f1-r1", "ufc", "ufc_round_of_victory", event_title=title))[5:] == ("natalia silva", 1.0)


def test_a_darts_match_and_races_read_alike():
    same(kalshi_bet("KXDARTSMATCH", "KXDARTSMATCH-26OCT031500LHUMJWAD", "KXDARTSMATCH-26OCT031500LHUMJWAD-LHUM", "darts",
                    outcome="Luke Humphries", event_title="Luke Humphries vs James Wade"),
         pm_bet("pdcdarts-lukhum-jamwad-2026-10-03", "aec-pdcdarts-lukhum-jamwad-2026-10-03", "darts", "darts_match_winner",
                outcome="Luke Humphries", event_title="Luke Humphries vs. James Wade"))      # 'humphries luke' comes first.
    rules = "If Oscar Piastri finishes in first in the main race originally scheduled for October 4, 2026 at the 2026 Bahrain Grand Prix"
    assert same(kalshi_bet("KXF1RACE", "KXF1RACE-BAH26", "KXF1RACE-BAH26-PIA", "f1", outcome="Oscar Piastri", rules=rules),
                pm_bet("f1-gabgpim-2026-10-04-w", "tec-f1-gabgpim-2026-10-04-w-oscpia", "f1", "futures", title="Oscar Piastri")) == (
        "race_winner", 2026, "2026-10-04", None, None, "oscar piastri", None)
    same(kalshi_bet("KXF1TOPCONSTRUCTOR", "KXF1TOPCONSTRUCTOR-BAH26", "KXF1TOPCONSTRUCTOR-BAH26-MCL", "f1", outcome="McLaren", rules=rules),
         pm_bet("f1-gabgpim-2026-10-04-cons", "tec-f1-gabgpim-2026-10-04-cons-mclare", "f1", "futures", title="McLaren"))
    same(kalshi_bet("KXNASCARRACE", "KXNASCARRACE-SOUP26", "KXNASCARRACE-SOUP26-DEHA", "nascar", outcome="Denny Hamlin",
                    rules="If Denny Hamlin finishes in first in the main race at the 2026 South Point 400 originally scheduled for Oct 4, 2026"),
         pm_bet("nascar-sp4-2026-10-04-w", "tec-nascar-sp4-2026-10-04-w-denham", "nascar", "futures", title="Denny Hamlin"))
    # A race's winners are typed as futures, but the drivers' title stays a future.
    assert pm_bet("f1-dc-2026-12-06-w", "tec-f1-dc-2026-12-06-w-oscpia", "f1", "futures", title="Oscar Piastri").kind == "drivers_champion"
