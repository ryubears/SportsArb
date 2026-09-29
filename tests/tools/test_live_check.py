"""
Tests for the live check's reading of the Kalshi key's location attestation.
"""

from tools import live_check

NOW = 1790000000


def test_the_attestation_date_is_shown_with_a_reminder_to_renew_when_it_is_near(capsys):
    assert live_check.check_kalshi_key(lambda: {"api_key_region_expiration_ts": NOW + 30 * 86400}, lambda: NOW)
    assert live_check.check_kalshi_key(lambda: {"api_key_region_expiration_ts": NOW + 6 * 86400}, lambda: NOW)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "kalshi key: location attestation good until 2026-10-21 14:13 UTC, in 30.0 days"
    assert lines[1] == "kalshi key: location attestation good until 2026-09-27 14:13 UTC, in 6.0 days, renew it on Kalshi before then"


def test_a_lapsed_or_unreadable_attestation_fails_the_check(capsys):
    assert not live_check.check_kalshi_key(lambda: {"api_key_region_expiration_ts": NOW - 60}, lambda: NOW)

    def unreadable():
        raise RuntimeError("401")
    assert not live_check.check_kalshi_key(unreadable, lambda: NOW)
    out = capsys.readouterr().out
    assert "lapsed at 2026-09-21 14:12 UTC, so Kalshi refuses it for sports markets until it is renewed on Kalshi" in out
    assert "could not be read (RuntimeError('401'))" in out
