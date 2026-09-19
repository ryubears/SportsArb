"""
Round trip every table through the database module.
"""

from db import database
from db.models import Bet, Contract, Opportunity, Pair, Quote


def contract(venue, contract_id, **fields):
    c = Contract(venue=venue, contract_id=contract_id, market_id=contract_id, event_id="ev", series_id=None,
                 sport="nfl", event_title=None, title="t", outcome="Yes", market_type=None, line=None, rules=None,
                 start_time=None, close_time="2027-01-01T00:00:00+00:00", fee_info={"rate": 0.05})
    for k, v in fields.items():
        setattr(c, k, v)
    return c


def test_fee_history_records_only_changes(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("polymarket", "A", fee_info={"rate": 0.05})], "2026-01-01T00:00:00+00:00")
    database.upsert_contracts(conn, [contract("polymarket", "A", fee_info={"rate": 0.05})], "2026-01-02T00:00:00+00:00")
    database.upsert_contracts(conn, [contract("polymarket", "A", fee_info={"rate": 0})], "2026-01-03T00:00:00+00:00")
    history = database.load_fee_history(conn, "polymarket", ["A"])["A"]
    assert [(r.seen_at[:10], r.fee_info) for r in history] == [("2026-01-01", {"rate": 0.05}), ("2026-01-03", {"rate": 0})]


def test_contracts_upsert_keeps_first_seen(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("kalshi", "A")], "2026-01-01T00:00:00+00:00")
    database.upsert_contracts(conn, [contract("kalshi", "A", title="renamed")], "2026-01-02T00:00:00+00:00")
    rows = database.load_contracts(conn, sport="nfl")
    assert len(rows) == 1
    assert (rows[0]["title"], rows[0]["first_seen"][:10], rows[0]["last_seen"][:10]) == ("renamed", "2026-01-01", "2026-01-02")


def test_bets_pairs_quotes_and_targets(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    database.upsert_contracts(conn, [contract("polymarket", "pm"), contract("kalshi", "k")], "2026-01-01T00:00:00+00:00")
    bets = [Bet(v, cid, "champion", 2027, None, None, None, "BUF", None, "yes") for v, cid in (("polymarket", "pm"), ("kalshi", "k"))]
    database.replace_bets(conn, "nfl", bets)
    assert {b["venue"] for b in database.load_bets(conn, "nfl")} == {"polymarket", "kalshi"}

    p = Pair("champion", 2027, None, None, None, "BUF", None, "pm", "k", "yes", "yes", 1.0, ["note"])
    database.replace_pairs(conn, "nfl", [p], "2026-01-01T00:00:00+00:00")
    assert database.load_pairs(conn, "nfl")[0]["flags"] == '["note"]'

    targets = database.load_recording_targets(conn, "nfl", "2026-06-01T00:00:00+00:00", "2026-06-08T00:00:00+00:00")
    assert targets == {"polymarket": ["pm"], "kalshi": ["k"]}
    closed = database.load_recording_targets(conn, "nfl", "2028-01-01T00:00:00+00:00", "2028-01-08T00:00:00+00:00")
    assert closed == {"polymarket": [], "kalshi": []}

    database.insert_quotes(conn, [Quote("kalshi", "k", "2026-01-01T00:00:00+00:00", [[0.5, 1]], [[0.6, 2]])])
    q = database.load_quotes(conn, "kalshi", ["k"])["k"][0]
    assert (q.bids, q.asks) == ([[0.5, 1]], [[0.6, 2]])
    assert database.load_quotes(conn, "kalshi", ["k"], since="2026-02-01")["k"] == []


def test_opportunities_are_rebuilt_each_time(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    o = Opportunity("pm", "k", "spread", "label", "buy both yes", "t0", "t1", 60, "t0", 0.02, 100, 2.0, 0, 10.0, 2.04, 74.5)
    database.replace_opportunities(conn, [o, o])
    database.replace_opportunities(conn, [o])
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1


def test_upsert_returns_fee_change_count(tmp_path):
    conn = database.connect(tmp_path / "t.sqlite")
    assert database.upsert_contracts(conn, [contract("kalshi", "A")], "2026-01-01T00:00:00+00:00") == 1
    assert database.upsert_contracts(conn, [contract("kalshi", "A")], "2026-01-02T00:00:00+00:00") == 0
