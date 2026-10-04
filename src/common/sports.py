"""
The sports whose events are alike, in groups, for the tables that treat a
group's sports the same way: how long their games last in config.py, the
notes on their rules in catalog/notes.py, how the Polymarket US classifier
reads their events, and which ones catalog/match.py pairs across dates a
day apart. Every sport is in catalog/fetch.py, and
tests/catalog/test_sports.py checks that each group names only those.
"""

TEAM_SPORTS = ("nfl", "ncaaf", "mlb", "nhl", "nba", "wnba", "ncaab")     # Games between two teams, away and home.
SOCCER = ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ligamx", "mls", "ucl", "uel")    # Matches of two clubs, which can be drawn.
MATCH_SPORTS = ("tennis", "darts", "ufc")       # Matches between two people.
RACING = ("f1", "nascar")                       # Races, a market a driver or a constructor.
