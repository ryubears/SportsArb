"""
Test the catalog refresh with the venue calls replaced by canned contracts.
"""

import pipeline
from db import database
from db.models import Contract


def canned(venue, contract_id, **fields):
    c = Contract(venue=venue, contract_id=contract_id, market_id=contract_id, event_id="KXSB-27" if venue == "kalshi" else "pro-football-2027-champion",
                 series_id="KXSB" if venue == "kalshi" else None, sport="nfl", event_title="Pro Football: 2027 Champion",
                 title="Will the Buffalo Bills win the 2027 NFL league championship?", outcome="Yes", market_type=None,
                 line=None, rules=None, start_time=None, close_time="2027-02-01T00:00:00+00:00", fee_info={"rate": 0.05})
    for k, v in fields.items():
        setattr(c, k, v)
    return c


def test_refresh_fetches_classifies_and_pairs(tmp_path, monkeypatch):
    # The refresh must be pointed at a scratch database. It writes every table it touches.
    canned_by_venue = {"polymarket": [canned("polymarket", "pm-token")], "kalshi": [canned("kalshi", "KXSB-27-BUF")], "polymarket_us": []}
    monkeypatch.setattr(pipeline.fetch, "fetch_contracts", lambda venue, sport: canned_by_venue[venue])
    summary = pipeline.refresh("nfl", log=lambda m: None, db_path=tmp_path / "t.sqlite")
    assert summary == ("polymarket 1 contracts, 1 fee records, kalshi 1 contracts, 1 fee records, "
                       "polymarket_us 0 contracts, 0 fee records, 2 bets, 1 groups")
    with database.connect(tmp_path / "t.sqlite") as conn:
        assert list(database.load_groups(conn, "nfl")) == ["champion 2027 BUF"]
