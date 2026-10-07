"""
Tests for reading the leagues' free feeds into games and snapshots, and for the reader that keeps its connections open.
"""

import gzip
import http.client
import json
import pytest
from tools.event_probe import leagues

START = 1791410400.0        # 2026-10-07 22:00 UTC.


def test_a_schedule_names_each_game_by_the_catalogs_team_codes():
    schedule = {"dates": [{"date": "2026-10-07", "games": [{
        "gamePk": 849822, "officialDate": "2026-10-07", "gameDate": "2026-10-07T22:00:00Z", "status": {"abstractGameState": "Live"},
        "teams": {"away": {"team": {"abbreviation": "LAD", "name": "Los Angeles Dodgers"}},
                  "home": {"team": {"abbreviation": "ARI", "name": "Arizona Diamondbacks"}}}}]}]}
    # The catalog's Arizona is AZ, found by the team's name when the league's code is not one of its codes.
    assert leagues.mlb_games(schedule) == [leagues.Game("mlb", "849822", "2026-10-07", "LAD", "AZ", START, "live")]
    scores = {"currentDate": "2026-10-07", "games": [{"id": 2026020053, "gameDate": "2026-10-07", "startTimeUTC": "2026-10-07T22:00:00Z",
                                                      "gameState": "OFF", "awayTeam": {"abbrev": "PIT"}, "homeTeam": {"abbrev": "WSH"}}]}
    assert leagues.nhl_games(scores) == [leagues.Game("nhl", "2026020053", "2026-10-07", "PIT", "WSH", START, "final")]
    assert leagues.team_code("nhl", "xyz") == "XYZ"     # One the catalog does not know stays the league's.


def player(pid, name, batting=None, pitching=None):
    return {"person": {"id": pid, "fullName": name}, "stats": {"batting": batting or {}, "pitching": pitching or {}}}


def test_an_mlb_read_gives_the_runs_each_players_counts_the_pitchers_taken_out_and_the_newest_pitch():
    feed = {
        "metaData": {"timeStamp": "20261007_220510"},
        "gameData": {"status": {"abstractGameState": "Live"}},
        "liveData": {
            "linescore": {"teams": {"away": {"runs": 2}, "home": {}}},
            "boxscore": {"teams": {
                "away": {"players": {"ID1": player(1, "José Ramírez", {"hits": 2, "homeRuns": 1, "totalBases": 5, "rbi": 2, "runs": 1})},
                         "pitchers": [3]},
                "home": {"players": {"ID2": player(2, "Chris Sale", pitching={"strikeOuts": 6, "outs": 13}),
                                     "ID4": player(4, "Dylan Lee", pitching={"strikeOuts": 1})},
                         "pitchers": [2, 4]}}},
            "plays": {"allPlays": [
                {"about": {"isComplete": True}, "result": {"description": "José Ramírez homers (1) on a fly ball to left field."},
                 "playEvents": [{"endTime": "2026-10-07T22:04:50.100Z", "details": {"description": "Ball"}},
                                {"endTime": "2026-10-07T22:05:01.500Z", "details": {"description": "In play, run(s)"}}]},
                {"about": {"isComplete": False}, "result": {}, "playEvents": []}]}}}
    snap = leagues.mlb_snapshot(feed)
    assert (snap.state, snap.away, snap.home) == ("live", 2, 0)
    assert snap.players["jose ramirez"] == {**{count: 0 for count in leagues.MLB_COUNTS}, "hits": 2, "home_runs": 1, "total_bases": 5,
                                            "rbis": 2, "runs": 1}
    assert snap.players["chris sale"]["strikeouts"] == 6
    assert snap.pulled == {"chris sale"}            # Dylan Lee followed him. The away side's one pitcher is still in.
    assert snap.made == START + 310
    assert snap.play_ended == START + 301.5
    assert snap.play == "José Ramírez homers (1) on a fly ball to left field."


def test_an_nhl_read_counts_goals_and_assists_from_the_goals_listed_but_not_a_shootouts():
    def goal(scorer, assist=None, period_type="REG"):
        return {"typeDescKey": "goal", "periodDescriptor": {"number": 1, "periodType": period_type}, "timeInPeriod": "07:13",
                "details": {"scoringPlayerId": scorer, **({"assist1PlayerId": assist} if assist else {})}}
    feed = {"gameState": "LIVE", "awayTeam": {"score": 2}, "homeTeam": {"score": 0},
            "rosterSpots": [{"playerId": p, "firstName": {"default": first}, "lastName": {"default": last}}
                            for p, first, last in [(1, "T.J.", "Hughes"), (2, "Steven", "Stamkos"), (3, "Auston", "Matthews")]],
            "plays": [goal(2, 1), goal(2), goal(3, period_type="SO")]}
    snap = leagues.nhl_snapshot(feed)
    assert (snap.state, snap.away, snap.home) == ("live", 2, 0)
    assert snap.players == {"tj hughes": {"goals": 0, "assists": 1}, "steven stamkos": {"goals": 2, "assists": 0},
                            "auston matthews": {"goals": 0, "assists": 0}}
    assert snap.play == "period 1 07:13 goal"
    assert snap.made is None and snap.play_ended is None       # NHL's play-by-play gives no time of day.


class Answer:
    def __init__(self, body, headers):
        self.body, self.headers, self.status = body, headers, 200

    def read(self):
        return self.body

    def getheader(self, name):
        return self.headers.get(name)


class Connection:
    """
    Stands in for an HTTPS connection: the first one made fails its first
    request, as one the host closed would, and each request is counted.
    """
    made = []

    def __init__(self, host, timeout):
        self.requests = 0
        self.closed = False
        Connection.made.append(self)

    def request(self, method, path, headers):
        self.requests += 1
        assert headers["Accept-Encoding"] == "gzip"
        if len(Connection.made) == 1:
            raise http.client.RemoteDisconnected("closed")

    def getresponse(self):
        return Answer(gzip.compress(json.dumps({"ok": True}).encode()), {"Content-Encoding": "gzip", "Age": "7"})

    def close(self):
        self.closed = True


def test_the_reader_keeps_its_connection_and_opens_a_new_one_once_when_the_host_closed_it(monkeypatch):
    monkeypatch.setattr(leagues.http.client, "HTTPSConnection", Connection)
    Connection.made = []
    reader = leagues.Reader()
    data, arrived, age = reader.get("https://statsapi.mlb.com/api/v1.1/game/1/feed/live")
    assert data == {"ok": True} and age == 7.0 and arrived > 0
    reader.get("https://statsapi.mlb.com/api/v1.1/game/1/feed/live")
    assert [(c.requests, c.closed) for c in Connection.made] == [(1, True), (2, False)]


def test_the_reader_gives_up_after_a_second_failure(monkeypatch):
    class Broken(Connection):
        def request(self, method, path, headers):
            raise OSError("unreachable")
    monkeypatch.setattr(leagues.http.client, "HTTPSConnection", Broken)
    with pytest.raises(OSError):
        leagues.Reader().get("https://api-web.nhle.com/v1/score/2026-10-07")
