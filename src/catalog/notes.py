"""
Where the venues' rules differ on a pair's bet, as notes the pair carries.

The notes come from reading the venues' rules text, by kind, so nobody has
to reread the rules to know how two members of a pair may settle apart.
match.py adds a pair's notes to its flags, see for_pair(). A note is a
warning to read, not a bar: a pair trades whatever its notes say.
"""

from common.sports import ESPORTS, SOCCER

# GAMES, by sport and kind, from both venues' rules text. Football, baseball, hockey, and basketball as of 2026-09.
FOOTBALL_POSTPONED = ("If the game does not start within 48 hours, Kalshi settles at a fair price. Polymarket US waits up to two weeks "
                      "for a rescheduled game.")
FOOTBALL_NOTES = {"game_winner": "Ties pay half on both venues. If the game does not start within 48 hours, Kalshi settles at a fair "
                                 "price while Polymarket US waits up to two weeks for a rescheduled game.",
                  "spread": FOOTBALL_POSTPONED, "total": FOOTBALL_POSTPONED}
BASEBALL_POSTPONED = ("Extra innings count on both venues. If the game is postponed, Kalshi waits two days for it and then settles at a "
                      "fair price, while Polymarket US waits up to two weeks for it.")
BASEBALL_NOTES = {kind: BASEBALL_POSTPONED for kind in ("game_winner", "spread", "total", "team_total")}
HOCKEY_POSTPONED = "If the game does not start within two days, both venues settle at a fair price, each its own."
HOCKEY_GOALS = f"Overtime counts on both venues, and a shootout as one goal for its winner. {HOCKEY_POSTPONED}"
HOCKEY_NOTES = {"game_winner": f"Overtime and the shootout count on both venues. {HOCKEY_POSTPONED}",
                "spread": HOCKEY_GOALS, "total": HOCKEY_GOALS, "team_total": HOCKEY_GOALS}
BASKETBALL_POSTPONED = ("Overtime counts on both venues. If the game does not start within 48 hours, Kalshi settles at a fair price, "
                        "while Polymarket US waits up to two weeks for it and then settles at its last price.")
BASKETBALL_NOTES = {kind: BASKETBALL_POSTPONED for kind in ("game_winner", "spread", "total", "team_total")}
# Soccer, as of 2026-10: both count 90 minutes and stoppage time, a half its 45 and its stoppage, never extra time or penalties.
SOCCER_POSTPONED = ("Both venues count 90 minutes and stoppage time. If the match moves more than 48 hours, Kalshi settles at a fair "
                    "price, while Polymarket US waits up to two weeks for it and then settles at its last price.")
SOCCER_NOTES = {
    **{f"{period}{kind}": SOCCER_POSTPONED for period in ("", "first_half_", "second_half_")
       for kind in ("result", "spread", "total", "btts", "exact_score")},
    "total_corners": "Kalshi counts the corners of any extra time too. " + SOCCER_POSTPONED,
}
MATCH_NOTES = {
    "tennis": {**{kind: "Both venues settle at a fair price, each its own, if the match never starts, and wait up to two weeks for "
                        "one postponed. A player who retires loses on Polymarket US, which Kalshi's rules leave unsaid."
                  for kind in ("match_winner", "set_1_winner", "set_2_winner", "set_3_winner", "set_4_winner", "set_5_winner")},
               **{kind: "If a player retires, both venues settle what play already decided and a fair price, each its own, "
                        "for the rest." for kind in ("games_spread", "sets_spread", "total_games", "total_sets", "exact_score")}},
    "ufc": {kind: "Both venues pay 50 cents on a draw or a no contest, wait up to two weeks for a postponed fight, and settle a "
                  "cancelled one at a fair price, each its own. A technical decision wins in no round on either."
            for kind in ("match_winner", "go_the_distance", "round_of_victory")},
    "darts": {"match_winner": "A match not played, or a walkover, settles at a fair price on Kalshi and at 50 cents on Polymarket "
                              "US, which also pays 50 cents if the match moves more than two days."},
}
# Esports, as of 2026-10, the same for every title.
ESPORTS_MOVED = ("A match not started within 48 hours, or forfeited before play, settles at a fair price on Kalshi, while Polymarket "
                 "US waits up to two weeks for it and then settles at its last price.")
ESPORTS_NOTES = {
    "match_winner": "Polymarket US pays 50 cents on a drawn match, as a best of two can end, which Kalshi's rules leave unsaid. "
                    + ESPORTS_MOVED,
    **{f"map_{n}_winner": "A map not played settles at a fair price on both venues, each its own. " + ESPORTS_MOVED for n in range(1, 6)},
    "total_maps": ESPORTS_MOVED,
}
RACING_NOTE = ("A driver who retires or is not classified loses on both venues. Kalshi pays on the FIA's final classification and "
               "settles at a fair price if the race does not start within 48 hours; Polymarket US waits up to two weeks for it.")
# Bitcoin, as of 2026-10: both settle on CF Benchmarks' Bitcoin Real-Time Index, but read it apart.
CROSSING_NOTE = ("Kalshi pays on the index itself crossing the price, Polymarket US on a 60 second trimmed mean of it, without the "
                 "top and bottom fifth, crossing it, so a brief spike can settle them apart.")
CRYPTO_NOTES = {
    "updown_15m": "Both venues compare the simple averages of the index's last 60 seconds before the window's end and its start, "
                  "rounded to the cent, Up on equal.",
    "hit_before": CROSSING_NOTE,
    "dip_before": CROSSING_NOTE,
    "year_end_range": "Kalshi reads the simple average of the index's last 60 seconds of 2026, Polymarket US a trimmed mean of "
                      "them, without the top and bottom fifth.",
}
KIND_NOTES = {"nfl": FOOTBALL_NOTES, "ncaaf": FOOTBALL_NOTES, "mlb": BASEBALL_NOTES, "nhl": HOCKEY_NOTES, "nba": BASKETBALL_NOTES,
              "wnba": BASKETBALL_NOTES, "ncaab": BASKETBALL_NOTES,
              **{league: SOCCER_NOTES for league in SOCCER},
              **MATCH_NOTES, "f1": {"race_winner": RACING_NOTE, "race_constructor": RACING_NOTE}, "nascar": {"race_winner": RACING_NOTE},
              "crypto": CRYPTO_NOTES, **{title: ESPORTS_NOTES for title in ESPORTS}}
FOOTBALL_PLAYER_NOTE = ("Both venues settle to the pre-game fair price if the player never takes a snap and count overtime. Polymarket US "
                        "ignores stat corrections made after the game.")
BASKETBALL_PLAYER_NOTE = ("Both venues count overtime, and Polymarket US ignores stat corrections made after the game. A player who is "
                          "active but never takes the court settles at a fair price on both venues, each its own, and Polymarket US "
                          "settles an inactive player the same way, which Kalshi's rules leave unsaid.")
PLAYER_NOTES = {
    "nfl": FOOTBALL_PLAYER_NOTE,
    "ncaaf": FOOTBALL_PLAYER_NOTE,
    "mlb": "Both venues settle to a fair price, each its own, if the player is not in the starting lineup, or for a pitching prop is not "
           "the starting pitcher, and count extra innings. Kalshi also settles at a fair price for a starter who never comes to the plate "
           "or faces a batter, and does not count a pinch hitter's at bats.",
    "nhl": "Overtime counts and shootout goals do not, on Polymarket US by its rules and on Kalshi by the official stats it goes by. "
           "A player who dresses but never plays settles at a fair price on both venues, each its own, and Polymarket US settles a "
           "scratched player the same way, which Kalshi's rules leave unsaid.",
    "nba": BASKETBALL_PLAYER_NOTE,
    "wnba": BASKETBALL_PLAYER_NOTE,
}

# Awards, in every sport. Both venues follow the official award, and season win totals count the regular season only on both.
AWARD_NOTE = ("Polymarket US pays $1 divided among players who share the award. Kalshi's rules say the same for some awards and "
              "nothing for others.")
LEADER_NOTE = "Polymarket US pays $1 divided among those who tie for the lead. Kalshi's rules leave a tie unsaid."
TEAM_TIE_NOTE = "Polymarket US pays $1 divided among teams that tie for it. Kalshi's rules leave a tie unsaid."
UFC_NOTE = ("Kalshi reads the title holder at noon Eastern on December 31, Polymarket US at 11:59 PM, so a title fight that night "
            "settles them apart. Polymarket US counts only an undisputed champion, not an interim one.")
RANKING_NOTE = "Kalshi reads the ranking at noon Eastern on December 31, Polymarket US at 11:59 PM."
ELECTION_NOTE = ("Kalshi pays on the party of whoever is sworn in or inaugurated, in January, Polymarket US on the party's nominee "
                 "winning the election, runoffs included. They part if the winner leaves or changes party before taking office.")
CONTROL_NOTE = ("Both venues pay on the party that wins the chamber, by its seats and the tie breaks each lists, but Kalshi's "
                "market runs to February 1.")
DIVISIONS = ("flyweight", "bantamweight", "featherweight", "lightweight", "welterweight", "middleweight", "light_heavyweight", "heavyweight")
FUTURE_NOTES = {
    **{kind: AWARD_NOTE for kind in (
        "mvp", "offensive_player", "defensive_player", "offensive_rookie", "defensive_rookie", "comeback_player", "coach",
        "al_mvp", "nl_mvp", "al_cy_young", "nl_cy_young", "al_rookie", "nl_rookie", "world_series_mvp",
        "hart", "norris", "vezina", "jack_adams", "calder", "goals_leader", "points_leader", "heisman", "ballon_dor")},
    **{kind: LEADER_NOTE for kind in (
        "passing_yards_leader", "receiving_yards_leader", "rushing_yards_leader", "passing_touchdowns_leader",
        "receiving_touchdowns_leader", "rushing_touchdowns_leader", "sacks_leader", "interceptions_leader",
        "interceptions_thrown_leader", "assists_leader",
        "postseason_home_runs_leader", "postseason_rbi_leader", "postseason_strikeouts_leader", "postseason_runs_leader",
        "postseason_stolen_bases_leader",
        *(f"{conference}_{stat}" for conference in ("sec", "big_ten", "big_12", "acc") for stat in (
            "passing_yards_leader", "passing_touchdowns_leader", "receiving_yards_leader", "rushing_yards_leader", "sacks_leader")))},
    **{kind: TEAM_TIE_NOTE for kind in ("best_record", "worst_record", "last_undefeated", "last_winless")},
    **{f"{division}_champion": UFC_NOTE for division in DIVISIONS},
    "atp_year_end_no1": RANKING_NOTE, "wta_year_end_no1": RANKING_NOTE,
    "senate_race": ELECTION_NOTE, "governor_race": ELECTION_NOTE, "house_race": ELECTION_NOTE,
    "house_control": CONTROL_NOTE, "senate_control": CONTROL_NOTE,
}


def for_pair(sport, kind):
    """
    The notes a pair of the sport and kind carries: its kind's in the sport, its sport's player note for a player prop,
    and a future's, or none for a kind the venues settle alike.
    """
    found = []
    if kind in KIND_NOTES.get(sport, {}):
        found.append(KIND_NOTES[sport][kind])
    if kind.startswith("player_") and sport in PLAYER_NOTES:
        found.append(PLAYER_NOTES[sport])
    if kind in FUTURE_NOTES:
        found.append(FUTURE_NOTES[kind])
    return found
