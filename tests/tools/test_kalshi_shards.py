"""
Tests for splitting the Kalshi cash across the exchange shards live trading uses, with Kalshi's answers scripted.
"""

from engine.helper import config
from tools import kalshi_shards


def test_football_and_hockeys_shard_keeps_most_of_the_cash():
    assert config.LIVE_SHARDS["kalshi"] == {0: 90, 3: 10}


def test_moves_bring_each_shard_to_its_share_to_the_hundredth_of_a_cent():
    # All of it on baseball's shard, as it was left: half moves to football's, which takes the odd hundredth of a cent.
    assert kalshi_shards.moves({0: 0.0, 1: 0.0, 2: 0.0, 3: 91.1819}, {0: 50, 3: 50}) == [(45.591, 3, 0)]
    # A shard outside the split gives up all it has.
    assert kalshi_shards.moves({0: 10.0, 2: 5.0, 3: 30.0}, {0: 50, 3: 50}) == [(5.0, 2, 0), (7.5, 3, 0)]
    # Even already, or off by less than a cent.
    assert kalshi_shards.moves({0: 20.0, 3: 20.0}, {0: 50, 3: 50}) == []
    assert kalshi_shards.moves({0: 20.004, 3: 20.0}, {0: 50, 3: 50}) == []
    # Split 90 to 10 from where it stood on 2026-10-01: all but a tenth of the whole moves to football's.
    assert kalshi_shards.moves({0: 6.94, 3: 44.83}, {0: 90, 3: 10}) == [(39.653, 3, 0)]


class Kalshi:
    """
    Stands in for the Kalshi account: its shards' cash, and every call made to move it or set its target split.
    """

    def __init__(self, shards):
        self.shards = dict(shards)
        self.calls = []

    def read(self):
        return sum(self.shards.values()), dict(self.shards)

    def move(self, dollars, source, destination):
        self.calls.append(("move", dollars, source, destination))
        self.shards[source] -= dollars
        self.shards[destination] += dollars
        return {"transfer_id": "t1"}

    def set_split(self, percents):
        self.calls.append(("split", percents))
        return {}


def split(kalshi, apply):
    lines = []
    kalshi_shards.split_shards(apply, kalshi.read, kalshi.move, kalshi.set_split, wait=lambda seconds: None, out=lines.append)
    return lines


def test_without_apply_it_only_says_what_it_would_do():
    kalshi = Kalshi({0: 0.0, 3: 91.18})
    assert split(kalshi, apply=False) == [
        "kalshi 91.18$ available, by exchange shard: shard 0 0.00$, shard 3 91.18$",
        "target split: shard 0 90%, shard 3 10%",
        "to move: 82.0620$ from shard 3 to shard 0",
        "nothing done: run again with --apply to move the money and set Kalshi's target split"]
    assert kalshi.calls == []


def test_with_apply_it_moves_the_money_before_setting_kalshis_target_split():
    kalshi = Kalshi({0: 0.0, 3: 91.18})
    lines = split(kalshi, apply=True)
    # Moved first, so Kalshi's own rebalancing, once set, finds nothing left to move.
    assert kalshi.calls == [("move", 82.062, 3, 0), ("split", {0: 90, 3: 10})]
    assert lines[-3:] == ["moved 82.0620$ from shard 3 to shard 0: {'transfer_id': 't1'}", "target split set: {}",
                          "now kalshi 91.18$ available, by exchange shard: shard 0 82.06$, shard 3 9.12$"]


def test_a_split_already_made_still_sets_kalshis_target_split():
    kalshi = Kalshi({0: 72.0, 3: 8.0})
    assert "already split, nothing to move" in split(kalshi, apply=True)
    assert kalshi.calls == [("split", {0: 90, 3: 10})]
