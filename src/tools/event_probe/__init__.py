"""
Phase 0 of trading on plays: a probe that only listens, to learn whether
the leagues' free live feeds see a play before the venues' prices move.

The idea it tests: a play can settle a contract before the game ends, a
player's second hit settling 'over 1.5 hits', and any order still asking
less than the settled price is there to buy. probe.py follows MLB and NHL
games through the leagues' public feeds and, at the same time, the books
of every cataloged contract on those games, and keeps both. report.py
then sets each play that settled a contract against its books: had the
price already moved when the feed showed the play, and what was left at
the old price once an order could arrive.

Nothing outside this folder imports it. It reads the catalog from the bot's
database read only and keeps what it records in a database of its own,
data/event_probe.sqlite. If the idea is dropped, deleting this folder,
tests/tools/event_probe/, that file, and data/event_probe.log removes it.
"""
