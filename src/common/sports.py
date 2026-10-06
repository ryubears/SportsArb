"""
The sports whose events are alike, in groups, for the tables that treat a
group's sports the same way: how long their games last in config.py, the
notes on their rules in catalog/notes.py, how the Polymarket US classifier
reads their events, and which ones catalog/match.py pairs across dates a
day apart. Every sport is in catalog/fetch.py, and
tests/catalog/test_sports.py checks that each group names only those.
"""

TEAM_SPORTS = ("nfl", "ncaaf", "mlb", "nhl", "nba", "wnba", "ncaab")     # Games between two teams, away and home.
# Matches of two clubs, or of two countries' national teams, men's and women's apart, which can be drawn.
SOCCER = ("epl", "laliga", "seriea", "bundesliga", "ligue1", "ligamx", "mls", "ucl", "uel", "intl", "intlw")
NATIONAL_TEAMS = ("intl", "intlw")              # Of those, the national teams', which never meet on days in a row.
MATCH_SPORTS = ("tennis", "darts", "ufc")       # Matches between two people.
# Matches between two esports teams, one sport a game title: Counter-Strike 2, League of Legends, Valorant, Dota 2,
# Rainbow Six Siege, and Overwatch. A team's name is its key, since the teams change too often for code files.
ESPORTS = ("cs2", "lol", "valorant", "dota2", "r6", "ow")
RACING = ("f1", "nascar")                       # Races, a market a driver or a constructor.
