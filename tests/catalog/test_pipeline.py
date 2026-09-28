"""
Test the catalog refresh with the venue calls replaced by canned contracts.
"""

from catalog import pipeline
from db import database
from db.models import Contract


def canned(venue, contract_id, **fields):
    c = Contract(venue=venue, contract_id=contract_id, market_id=contract_id,
                 event_id="KXNFLGAME-26SEP20CARATL" if venue == "kalshi" else "nfl-car-atl-2026-09-20",
                 series_id="KXNFLGAME" if venue == "kalshi" else "nfl-2026", sport="nfl", event_title="Carolina at Atlanta",
                 title="Carolina at Atlanta Winner?", outcome="Carolina",
                 market_type=None if venue == "kalshi" else "football_team_full_game_winner",
                 line=None, rules=None, start_time=None if venue == "kalshi" else "2026-09-20T17:00:00+00:00",
                 close_time="2026-09-20T20:00:00+00:00", fee_info={"rate": 0.05})
    for k, v in fields.items():
        setattr(c, k, v)
    return c


def test_refresh_fetches_classifies_and_pairs(tmp_path, monkeypatch):
    # The refresh must be pointed at a scratch database. It writes every table it touches.
    canned_by_venue = {"kalshi": [canned("kalshi", "KXNFLGAME-26SEP20CARATL-CAR")], "polymarket_us": [canned("polymarket_us", "aec-nfl-car-atl-2026-09-20")]}
    monkeypatch.setattr(pipeline.fetch, "fetch_contracts", lambda venue, sport: canned_by_venue[venue])
    logs = []
    summary = pipeline.refresh("nfl", log=logs.append, db_path=tmp_path / "t.sqlite")
    assert summary == "kalshi 1 contracts, polymarket_us 1 contracts, 2 bets, 1 pairs"
    assert [line.split(" in ")[0] for line in logs] == ["fetched 1 nfl contracts from kalshi", "fetched 1 nfl contracts from polymarket_us"]
    with database.connect(tmp_path / "t.sqlite") as conn:
        assert [p["label"] for p in database.load_pairs(conn, "nfl").values()] == ["nfl game_winner 2026-09-20 CAR@ATL CAR"]
