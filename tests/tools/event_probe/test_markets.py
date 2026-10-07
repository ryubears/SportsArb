"""
Tests for which contracts the event probe watches on a game and what a read of the game's feed says of each one's bet.
"""

from db import database
from db.models import Bet, Contract
from tools.event_probe import markets
from tools.event_probe.leagues import Game, Snapshot

GAME = Game("mlb", "849822", "2026-10-07", "LAD", "ATL", 1791410400.0, "live")


def bet(kind, subject=None, line=None, polarity="yes"):
    return markets.Watched("kalshi", f"{kind}-{subject}-{line}", kind, subject, line, polarity)


def read(state="live", away=0, home=0, players=None, pulled=()):
    return Snapshot(state, away, home, players or {}, set(pulled))


def test_a_count_past_its_line_settles_the_bet_while_the_game_goes_on():
    hits = bet("player_hits", "freddie freeman", 1.5)
    assert markets.statement(hits, read(players={"freddie freeman": {"hits": 1}}), GAME) is None
    assert markets.statement(hits, read(players={"freddie freeman": {"hits": 2}}), GAME) is True
    # Hits, runs, and RBIs add up.
    hrr = bet("player_hits_runs_rbis", "freddie freeman", 2.5)
    assert markets.statement(hrr, read(players={"freddie freeman": {"hits": 1, "runs": 1, "rbis": 1}}), GAME) is True
    assert markets.count(hrr, read(players={"freddie freeman": {"hits": 1, "runs": 1, "rbis": 1}}), GAME) == 3
    # Runs of both teams, or one.
    assert markets.statement(bet("total", line=4.5), read(away=3, home=2), GAME) is True
    assert markets.statement(bet("team_total", "ATL", 2.5), read(away=3, home=2), GAME) is None
    assert markets.statement(bet("team_total", "LAD", 2.5), read(away=3, home=2), GAME) is True


def test_the_end_settles_the_rest_and_a_pulled_pitcher_settles_his_pitching_bets_at_once():
    strikeouts = bet("player_strikeouts", "chris sale", 6.5)
    hits = bet("player_hits", "chris sale", 0.5)
    players = {"chris sale": {"strikeouts": 6, "hits": 0}}
    assert markets.statement(strikeouts, read(players=players), GAME) is None
    assert markets.statement(strikeouts, read(players=players, pulled={"chris sale"}), GAME) is False
    assert markets.statement(hits, read(players=players, pulled={"chris sale"}), GAME) is None     # Not a pitching count.
    assert markets.statement(hits, read("final", players=players), GAME) is False
    assert markets.statement(bet("total", line=4.5), read("final", 2, 2), GAME) is False


def test_a_winner_and_a_spread_settle_only_at_the_end():
    assert markets.statement(bet("game_winner", "LAD"), read(away=5, home=1), GAME) is None
    assert markets.statement(bet("game_winner", "LAD"), read("final", 5, 1), GAME) is True
    assert markets.statement(bet("game_winner", "ATL"), read("final", 5, 1), GAME) is False
    assert markets.statement(bet("spread", "LAD", 3.5), read("final", 5, 1), GAME) is True
    assert markets.statement(bet("spread", "LAD", 4.5), read("final", 5, 1), GAME) is False
    assert markets.statement(bet("spread", "ATL", 1.5), read("final", 1, 5), GAME) is True


def test_nothing_settles_a_bet_on_a_player_or_team_the_feed_does_not_have():
    # A player whose name the feed spells differently, or one not dressed, may be voided by the venue, so it is not a loss.
    assert markets.statement(bet("player_hits", "nobody", 0.5), read("final"), GAME) is None
    assert markets.statement(bet("game_winner", "NYY"), read("final", 5, 1), GAME) is None


def test_the_paying_outcome_follows_the_contracts_polarity():
    assert markets.winner(bet("total", line=4.5), True) == "yes"
    assert markets.winner(bet("total", line=4.5, polarity="no"), True) == "no"
    assert markets.winner(bet("total", line=4.5, polarity="no"), False) == "yes"
    assert markets.winner(bet("total", line=4.5), None) is None


def contract(venue, contract_id):
    return Contract(venue=venue, contract_id=contract_id, market_id=contract_id, event_id="e", series_id="s", sport="mlb",
                    event_title=None, title=contract_id, outcome="Yes", market_type=None, line=None, rules=None, start_time=None,
                    close_time=None, fee_info={"fee_multiplier": 1})


def test_a_games_contracts_come_from_the_catalog_with_the_teams_in_either_order(tmp_path):
    conn = database.connect(tmp_path / "catalog.sqlite")
    rows = [("kalshi", "K-HITS", "player_hits", "LAD", "ATL", "freddie freeman", 0.5),
            ("polymarket_us", "pm-total", "total", "ATL", "LAD", None, 7.5),            # Teams the other way round.
            ("kalshi", "K-OTHER", "total", "TB", "NYY", None, 7.5),                    # Another game.
            ("kalshi", "K-FIRST", "player_first_home_run", "LAD", "ATL", "shohei ohtani", None)]     # A kind no count settles.
    database.upsert_contracts(conn, [contract(venue, cid) for venue, cid, *_ in rows], "2026-10-07T00:00:00+00:00")
    database.replace_bets(conn, "mlb", [Bet(venue, cid, kind, 2026, "2026-10-07", a, b, subject, line, "yes")
                                        for venue, cid, kind, a, b, subject, line in rows])
    watched = markets.load_watched(database.read_only(tmp_path / "catalog.sqlite"), GAME)
    assert [(w.venue, w.contract_id, w.kind, w.fee_info) for w in watched] == [
        ("kalshi", "K-HITS", "player_hits", {"fee_multiplier": 1}), ("polymarket_us", "pm-total", "total", {"fee_multiplier": 1})]
