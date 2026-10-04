"""
Where the venues' rules differ on a pair's bet, as notes the pair carries.

The notes come from reading the venues' rules text, by kind, so nobody has
to reread the rules to know how two members of a pair may settle apart.
match.py adds a pair's notes to its flags, see for_pair(). A note is a
warning to read, not a bar: a pair trades whatever its notes say.
"""

# Awards, in every sport. Both venues follow the official award, and season win totals count the regular season only on both.
AWARD_NOTE = ("Polymarket US pays $1 divided among players who share the award. Kalshi's rules say the same for some awards and "
              "nothing for others.")
LEADER_NOTE = ("Polymarket US pays $1 divided among those who tie for the lead. Kalshi's rules leave a tie unsaid.")
TEAM_TIE_NOTE = ("Polymarket US pays $1 divided among teams that tie for it. Kalshi's rules leave a tie unsaid.")
UFC_NOTE = ("Kalshi reads the title holder at noon Eastern on December 31, Polymarket US at 11:59 PM, so a title fight that night "
            "settles them apart. Polymarket US counts only an undisputed champion, not an interim one.")
RANKING_NOTE = "Kalshi reads the ranking at noon Eastern on December 31, Polymarket US at 11:59 PM."
RACE_NOTE = ("Kalshi pays on the party of whoever is sworn in or inaugurated, in January, Polymarket US on the party's nominee "
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
    "senate_race": RACE_NOTE, "governor_race": RACE_NOTE, "house_race": RACE_NOTE,
    "house_control": CONTROL_NOTE, "senate_control": CONTROL_NOTE,
}


def for_pair(sport, kind):
    """
    The notes a pair of the sport and kind carries, its kind's, or none for a kind the venues settle alike.
    """
    return [FUTURE_NOTES[kind]] if kind in FUTURE_NOTES else []
