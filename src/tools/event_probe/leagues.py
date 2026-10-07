"""
The leagues' free live feeds the event probe reads: MLB's and NHL's
schedules, and one read of a game's feed reduced to what settles markets,
the score, each player's counts, and for baseball the pitchers taken out,
whose pitching counts can no longer change.

Both are the leagues' own public feeds, read without a key, through the
web caches in front of them. Before games on 2026-10-07 MLB's said
max-age=10 and NHL's max-age=19, so a read may be that many seconds old.
Each read keeps the cache's Age header, so the probe measures what the
cache costs during games. MLB's feed says when its content was made and
when each pitch ended, to the millisecond. NHL's play-by-play gives only
the game clock, so its plays are timed against the venues' books alone.
"""

import gzip
import http.client
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from api.http import USER_AGENT
from catalog.classify.teams import ALIASES, player_key, venue_codes
from common.timeutil import epoch
from common.venues import VENUES

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=team&date={day}"
MLB_FEED = "https://statsapi.mlb.com/api/v1.1/game/{game_id}/feed/live"
NHL_SCHEDULE = "https://api-web.nhle.com/v1/score/{day}"
NHL_FEED = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
TIMEOUT = 10        # Seconds a read may take.

MLB_STATES = {"Preview": "pre", "Live": "live", "Final": "final"}
NHL_STATES = {"FUT": "pre", "PRE": "pre", "LIVE": "live", "CRIT": "live", "FINAL": "final", "OFF": "final"}
# Each count the markets name, and where MLB's boxscore keeps it for a player.
MLB_COUNTS = {"hits": ("batting", "hits"), "home_runs": ("batting", "homeRuns"), "total_bases": ("batting", "totalBases"),
              "rbis": ("batting", "rbi"), "runs": ("batting", "runs"), "stolen_bases": ("batting", "stolenBases"),
              "strikeouts": ("pitching", "strikeOuts"), "hits_allowed": ("pitching", "hits"),
              "earned_runs": ("pitching", "earnedRuns"), "walks_allowed": ("pitching", "baseOnBalls"), "outs": ("pitching", "outs")}


@dataclass
class Game:
    """
    One scheduled game, its teams in the catalog's codes.
    """
    sport: str
    game_id: str
    game_date: str      # The game's date in US Eastern time, as the catalog's bets give it.
    away: str
    home: str
    start: float        # Scheduled first pitch or puck drop, seconds since 1970.
    state: str          # 'pre', 'live', or 'final'.


@dataclass
class Snapshot:
    """
    What one read of a game's feed says.
    """
    state: str
    away: int                                       # Runs or goals.
    home: int
    players: dict = field(default_factory=dict)     # Each player's key, see player_key(), maps to {count: value}.
    pulled: set = field(default_factory=set)        # Keys of pitchers taken out of the game, whose pitching counts are final.
    made: float | None = None       # When the feed says it made this content, seconds since 1970. MLB only.
    play_ended: float | None = None     # When the newest pitch or play ended, by the feed. MLB only.
    play: str | None = None         # The newest play in words.


def team_code(sport, abbreviation, name=None):
    """
    The catalog's code for a team a league names: its abbreviation when the
    catalog uses it, else the team whose venue codes or names include it.
    """
    teams = ALIASES.get(sport, {})
    code = (abbreviation or "").upper()
    if code in teams:
        return code
    for team, entry in teams.items():
        if code in {c.upper() for venue in VENUES for c in venue_codes(entry, venue)}:
            return team
        if name and name.lower() in {n.lower() for n in entry["names"]}:
            return team
    return code or None


def mlb_time(stamp):
    """
    Seconds since 1970 for MLB's 'YYYYMMDD_HHMMSS' stamp in UTC, or None.
    """
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def mlb_games(schedule):
    """
    The games of an MLB schedule.
    """
    games = []
    for day in schedule.get("dates") or []:
        for g in day.get("games") or []:
            away, home = (g["teams"][side]["team"] for side in ("away", "home"))
            games.append(Game("mlb", str(g["gamePk"]), g.get("officialDate") or day["date"],
                              team_code("mlb", away.get("abbreviation"), away.get("name")),
                              team_code("mlb", home.get("abbreviation"), home.get("name")),
                              epoch(g["gameDate"]), MLB_STATES.get(g["status"]["abstractGameState"], "pre")))
    return games


def nhl_games(schedule):
    """
    The games of an NHL day's scores.
    """
    return [Game("nhl", str(g["id"]), g.get("gameDate") or schedule.get("currentDate"), team_code("nhl", g["awayTeam"]["abbrev"]),
                 team_code("nhl", g["homeTeam"]["abbrev"]), epoch(g["startTimeUTC"]), NHL_STATES.get(g["gameState"], "pre"))
            for g in schedule.get("games") or []]


def mlb_snapshot(feed):
    """
    One read of MLB's live feed for a game. A pitcher is out once another
    followed him on his team's list of pitchers, which is in order of
    appearance. The newest play is the latest pitch or action of the last
    two at bats, since a new at bat starts empty.
    """
    live = feed.get("liveData") or {}
    runs = (live.get("linescore") or {}).get("teams") or {}
    snap = Snapshot(MLB_STATES.get(feed["gameData"]["status"]["abstractGameState"], "pre"),
                    runs.get("away", {}).get("runs") or 0, runs.get("home", {}).get("runs") or 0)
    for side in ("away", "home"):
        box = ((live.get("boxscore") or {}).get("teams") or {}).get(side) or {}
        keys = {}
        for p in (box.get("players") or {}).values():
            key = player_key(p["person"]["fullName"])
            keys[p["person"]["id"]] = key
            stats = p.get("stats") or {}
            snap.players[key] = {count: (stats.get(group) or {}).get(name) or 0 for count, (group, name) in MLB_COUNTS.items()}
        snap.pulled |= {keys[i] for i in (box.get("pitchers") or [])[:-1] if i in keys}
    snap.made = mlb_time((feed.get("metaData") or {}).get("timeStamp"))
    for play in ((live.get("plays") or {}).get("allPlays") or [])[-2:]:
        events = play.get("playEvents") or []
        for event in events:
            ended = epoch(event.get("endTime"))
            if ended and (snap.play_ended is None or ended >= snap.play_ended):
                snap.play_ended = ended
                # The pitch that ended an at bat is described by the at bat's result, 'grounds out softly, ...'.
                ends_it = event is events[-1] and (play.get("about") or {}).get("isComplete")
                snap.play = ((play.get("result") or {}).get("description") if ends_it else None) or (event.get("details") or {}).get("description")
    return snap


def nhl_snapshot(feed):
    """
    One read of NHL's play-by-play for a game. Each dressed player counts
    from zero, and goals and assists come from the goals listed, leaving
    out a shootout's, which no player's count includes.
    """
    snap = Snapshot(NHL_STATES.get(feed.get("gameState"), "pre"), (feed.get("awayTeam") or {}).get("score") or 0,
                    (feed.get("homeTeam") or {}).get("score") or 0)
    names = {}
    for spot in feed.get("rosterSpots") or []:
        names[spot["playerId"]] = player_key(f"{spot['firstName']['default']} {spot['lastName']['default']}")
        snap.players[names[spot["playerId"]]] = {"goals": 0, "assists": 0}
    plays = feed.get("plays") or []
    for play in plays:
        if play.get("typeDescKey") != "goal" or (play.get("periodDescriptor") or {}).get("periodType") == "SO":
            continue
        details = play.get("details") or {}
        for role, count in (("scoringPlayerId", "goals"), ("assist1PlayerId", "assists"), ("assist2PlayerId", "assists")):
            if details.get(role) in names:
                snap.players[names[details[role]]][count] += 1
    if plays:
        last = plays[-1]
        snap.play = f"period {(last.get('periodDescriptor') or {}).get('number')} {last.get('timeInPeriod')} {last.get('typeDescKey')}"
    return snap


SCHEDULES = {"mlb": (MLB_SCHEDULE, mlb_games), "nhl": (NHL_SCHEDULE, nhl_games)}
FEEDS = {"mlb": (MLB_FEED, mlb_snapshot), "nhl": (NHL_FEED, nhl_snapshot)}


class Reader:
    """
    Reads JSON over a connection to each host kept open between reads, so a
    read each second costs no new handshake. One per thread.
    """

    def __init__(self):
        self.connections = {}

    def get(self, url):
        """
        The JSON at url, when its last byte arrived in seconds since 1970, and
        the age the cache in front gave it in seconds, or None when it gave
        none. A kept connection the host has closed is opened again once.
        """
        parts = urllib.parse.urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        for attempt in (1, 2):
            conn = self.connections.get(parts.netloc)
            if conn is None:
                conn = self.connections[parts.netloc] = http.client.HTTPSConnection(parts.netloc, timeout=TIMEOUT)
            try:
                conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
                answer = conn.getresponse()
                body = answer.read()
                arrived = time.time()
                break
            except (http.client.HTTPException, OSError):
                conn.close()
                del self.connections[parts.netloc]
                if attempt == 2:
                    raise
        if answer.status != 200:
            raise RuntimeError(f"{url} answered {answer.status}")
        if answer.getheader("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        age = answer.getheader("Age")
        return json.loads(body), arrived, float(age) if age and age.isdigit() else None


def games(reader, sport, day):
    """
    A sport's games on a day in US Eastern time, YYYY-MM-DD.
    """
    url, parse = SCHEDULES[sport]
    return parse(reader.get(url.format(day=day))[0])


def read_game(reader, game):
    """
    One read of a game's feed, as (Snapshot, when it arrived, seconds the read took, the cache's age or None).
    """
    url, parse = FEEDS[game.sport]
    started = time.time()
    feed, arrived, age = reader.get(url.format(game_id=game.game_id))
    return parse(feed), arrived, arrived - started, age
