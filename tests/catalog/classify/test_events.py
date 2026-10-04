"""
The bets on one event, a game, a match, a race, or a Bitcoin window, read alike from both venues: each case is a real
Kalshi contract and the real Polymarket US contract on the same bet, and the two classifiers must agree on its identity
and on which side each contract holds.
"""

from catalog import match
from catalog.classify import kalshi, polymarket_us


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


def test_bitcoins_window_price_targets_and_year_end_bands_read_alike():
    assert same(kalshi_bet("KXBTC15M", "KXBTC15M-26OCT040145", "KXBTC15M-26OCT040145-45", "crypto", close_time="2026-10-04T05:45:00+00:00"),
                pm_bet("btc-updown-15m-2026-10-04-0530z", "cpc-btc-updown-15m-2026-10-04-0530z", "crypto")) == (
        "updown_15m", 2026, "2026-10-04 05:30", None, None, "BTC", None)
    # Kalshi's 'by Dec 31, 2026 at 11:59PM ET' and Polymarket US's 'before 12:00 AM ET on January 1, 2027' both end December 31.
    same(kalshi_bet("KXBTCMAX150", "KXBTCMAX150-25", "KXBTCMAX150-25-26DEC31-149999.99", "crypto", line=149999.99,
                    rules="If the price of Bitcoin is above || price || by Dec 31, 2026 at 11:59PM ET, then the market resolves to Yes."),
         pm_bet("btc-150k", "cpc-btc-150k-12-31-2026", "crypto", rules="This market will settle to Yes if the price of Bitcoin is "
                "above $149,999.99 at any point before 12:00 AM ET on January 1, 2027."))
    assert same(kalshi_bet("KXBTCMINY", "KXBTCMINY-27JAN01", "KXBTCMINY-27JAN01-55000.00", "crypto", line=55000,
                           rules="If the Bitcoin spot price according to the CF Bitcoin Real-Time Index is below $55000.00 starting "
                                 "Feb 5, 2026 and before Jan 1, 2027 at 12:00am ET, then the market resolves to Yes."),
                pm_bet("btc-hitprice-low-yr-12-31-2026", "cpc-btc-hitprice-low-yr-12-31-2026-55k", "crypto", title="Below $55,000.00",
                       rules="This market will settle to Yes if the price of Bitcoin is below $55,000.00 at any point from the creation "
                             "of this market until 12:00 AM ET on January 1, 2027.")) == (
        "dip_before", 2026, None, None, None, "2026-12-31", 55000.0)
    same(kalshi_bet("KXBTCY", "KXBTCY-27JAN0100", "KXBTCY-27JAN0100-B147500", "crypto", outcome="145,000 to 149,999.99",
                    close_time="2027-01-01T05:00:00+00:00"),
         pm_bet("btc-pricerange-yr-12-31-2026", "cpc-btc-pricerange-yr-12-31-2026-147500", "crypto", title="145,000 to 149,999.99",
                rules="This market will settle to Yes if the price of Bitcoin is 145,000 to 149,999.99 at 12:00 AM ET on January 1, 2027."))
