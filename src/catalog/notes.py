"""
Where the venues' rules differ on a pair's bet, as notes the pair carries.

The notes come from reading the venues' rules text, by sport and kind, so
nobody has to reread the rules to know how two members of a pair may
settle apart. match.py adds a pair's notes to its flags, see for_pair().
"""

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
# From Polymarket US's rules as of June 2026, since it listed no basketball game for the new season yet.
BASKETBALL_POSTPONED = ("Overtime counts on both venues. If the game does not start within 48 hours, Kalshi settles at a fair price, "
                        "while Polymarket US waits up to two weeks for it and then settles at its last price.")
BASKETBALL_NOTES = {kind: BASKETBALL_POSTPONED for kind in ("game_winner", "spread", "total", "team_total")}
KIND_NOTES = {"nfl": FOOTBALL_NOTES, "ncaaf": FOOTBALL_NOTES, "mlb": BASEBALL_NOTES, "nhl": HOCKEY_NOTES, "nba": BASKETBALL_NOTES}

# Awards, in every sport. Both venues follow the official award, and season win totals count the regular season only on both.
AWARD_NOTE = ("Polymarket US pays $1 divided among players who share the award. Kalshi's rules say the same for some awards and "
              "nothing for others.")
FUTURE_NOTES = {kind: AWARD_NOTE for kind in (
    "mvp", "offensive_player", "defensive_player", "offensive_rookie", "defensive_rookie", "comeback_player", "coach",
    "al_mvp", "nl_mvp", "al_cy_young", "nl_cy_young", "al_rookie", "nl_rookie", "world_series_mvp",
    "hart", "norris", "vezina", "jack_adams", "goals_leader", "points_leader", "heisman")}

FOOTBALL_PLAYER_NOTE = ("Both venues settle to the pre-game fair price if the player never takes a snap and count overtime. Polymarket US "
                        "ignores stat corrections made after the game.")
PLAYER_NOTES = {
    "nfl": FOOTBALL_PLAYER_NOTE,
    "ncaaf": FOOTBALL_PLAYER_NOTE,
    "mlb": "Both venues settle to a fair price, each its own, if the player is not in the starting lineup, or for a pitching prop is not "
           "the starting pitcher, and count extra innings. Kalshi also settles at a fair price for a starter who never comes to the plate "
           "or faces a batter, and does not count a pinch hitter's at bats.",
    "nhl": "Overtime counts and shootout goals do not, on Polymarket US by its rules and on Kalshi by the official stats it goes by. "
           "A player who dresses but never plays settles at a fair price on both venues, each its own, and Polymarket US settles a "
           "scratched player the same way, which Kalshi's rules leave unsaid.",
    "nba": "Both venues count overtime, and Polymarket US ignores stat corrections made after the game. A player who is active but "
           "never takes the court settles at a fair price on both venues, each its own, and Polymarket US settles an inactive player "
           "the same way, which Kalshi's rules leave unsaid.",
}


def for_pair(sport, kind):
    """
    The notes a pair of the sport and kind carries: its kind's in the sport,
    its sport's player note for a player prop, and an award's.
    """
    found = []
    if kind in KIND_NOTES.get(sport, {}):
        found.append(KIND_NOTES[sport][kind])
    if kind.startswith("player_") and sport in PLAYER_NOTES:
        found.append(PLAYER_NOTES[sport])
    if kind in FUTURE_NOTES:
        found.append(FUTURE_NOTES[kind])
    return found
