"""
Tests for the live check's reading of the balances and of the Kalshi key's location attestation.
"""

from tools import live_check

NOW = 1790000000


def test_each_venues_balance_is_shown_with_its_shards_and_one_that_cannot_be_read_fails_the_check(capsys):
    def unreadable():
        raise RuntimeError("401")
    assert live_check.check_balances({"kalshi": lambda: (100.0, {3: 20.0, 0: 80.0}), "polymarket_us": lambda: (50.0, {})})
    assert not live_check.check_balances({"kalshi": unreadable, "polymarket_us": lambda: (50.0, {})})
    assert capsys.readouterr().out.splitlines() == [
        "kalshi: 100.00$ available to trade",
        "kalshi by exchange shard, each trading only its own markets: shard 0 80.00$, shard 3 20.00$",
        "polymarket_us: 50.00$ available to trade",
        "kalshi: the balance could not be read (RuntimeError('401'))",
        "polymarket_us: 50.00$ available to trade",
    ]


def test_the_attestation_date_is_shown_with_a_reminder_to_renew_when_it_is_near(capsys):
    assert live_check.check_kalshi_key(lambda: NOW + 30 * 86400, lambda: NOW)
    assert live_check.check_kalshi_key(lambda: NOW + 6 * 86400, lambda: NOW)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "kalshi key: location attestation good until 2026-10-21 14:13 UTC, in 30.0 days"
    assert lines[1] == "kalshi key: location attestation good until 2026-09-27 14:13 UTC, in 6.0 days, renew it on Kalshi before then"


def test_a_lapsed_or_unreadable_attestation_fails_the_check(capsys):
    assert not live_check.check_kalshi_key(lambda: NOW - 60, lambda: NOW)

    def unreadable():
        raise RuntimeError("401")
    assert not live_check.check_kalshi_key(unreadable, lambda: NOW)
    out = capsys.readouterr().out
    assert "lapsed at 2026-09-21 14:12 UTC, so Kalshi refuses it for sports markets until it is renewed on Kalshi" in out
    assert "could not be read (RuntimeError('401'))" in out
