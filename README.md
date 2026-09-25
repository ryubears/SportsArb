# SportsArb

A bot that looks for cross-venue arbitrage between the two US prediction
markets that list NFL contracts, Kalshi and Polymarket US, and paper
trades what it finds. When the cheapest way to hold *yes* on one venue and
the cheapest way to hold *no* on the other add up to less than a dollar
after fees, buying both locks in the difference whatever the game does.

The whole thing runs as one process on an EC2 instance in us-east-1: it
records every order book change for about 4,900 contracts, prices about
2,450 cross-venue pairs on every change, sends paper orders when a pair
shows an edge, settles the trades when the contracts resolve, and keeps
the two paper balances level. No real orders are sent.

## How it works

Everything lives in `src/` and reads or writes one SQLite file,
`data/sportsarb.sqlite`. The tables follow the pipeline in order:
contracts, bets, pairs, quotes, gaps, opportunities, trades, ledger,
transfers. Every table has a model in `db/models.py` and its
schema in `db/database.py`.

### Catalog (`src/catalog`)

**fetch.py** pulls every open NFL contract from both venues into the
`contracts` table. Kalshi is read through its public REST catalog, paged
under the rate limit. Polymarket US is read through its gateway, one
call per tag, deduplicated across tags.

**classify/** turns each contract into a `Bet`, a venue neutral statement
of what the contract is about: kind, season, game date, the two teams,
a subject, and a line. Each venue has its own parser, since the two
describe the same thing very differently. Kalshi encodes the game in the
ticker, `KXNFLGAME-26SEP24ATLGB-GB`, and the prop in a series code and a
title like *Player: 100+ receiving yards*. Polymarket US encodes it in a
slug and a title like *Will Bijan Robinson record 100+ rushing yards?*.
Team aliases are resolved through `teams.py` and `aliases.json`, player
names are normalized to a key that ignores punctuation and suffixes, and
lines are made strict, so *100+* on one venue and *over 99.5* on the other
become the same bet. Contracts no parser understands are counted and
left out.

**match.py** groups bets whose identity agrees into a `Pair`. A pair only
exists when both venues list the bet, and it carries every contract that
expresses it, since a game winner can be held through either team's
contract and a spread through either side. Kinds with settlement rules
that differ between venues carry a note, for example both venues settle
props to the pre-game price if the player never takes a snap, but
Polymarket US ignores stat corrections made after the game.

**pipeline.py** runs fetch, classify, and match in one call. The recorder
runs it every hour in a background thread, so new games and props enter
the pairs while it records.

### Venue clients (`src/api`)

**bookstream.py** is the shared websocket loop: one connection per venue
carrying every wanted contract, a live book per contract, changes to the
wanted set applied without reconnecting, and a reconnect that is
immediate on the first drop and backs off only on repeated ones, with the
stretch until the new subscription is confirmed stored as a gap.
**kalshi.py** signs each connection and request with RSA-PSS and holds the
whole catalog on one connection. **polymarket_us.py** signs with Ed25519,
subscribes in chunks of 100 slugs, and, because the feed refuses an
eleventh subscription on one connection, opens as many connections as the
contract count needs. Both clients also report how a contract resolved,
which the settler uses.

### Live loop (`src/live`)

**run.py** is the process that runs. Its `Session` wires the recorder, the
venue connections, the scanner, and the paper executor with its settler
and rebalancer together, and a one-second timer ticks it: flush the changed
books, price them, settle and rebalance, and log a status line every
minute and each component's summary every ten. The same loop starts the
hourly catalog refresh in a background thread and applies the result to
the live connections.

**record.py** holds the newest book for every paired contract in memory
and, on each tick, writes a row with five levels a side for each contract
whose top of book changed. Quiet contracts write nothing, busy ones at most
one row a second. **streams.py** owns the connections behind it, one per
venue, or several when a venue caps how much one connection may carry,
and moves contracts between them as the catalog changes.

**scan.py** prices every pair whose member's book just changed. Using
**pricing.py** it walks the ladders to find the cheapest way to hold yes and
the cheapest way to hold no across the pair's members, on any venues,
including the venue's taker fee from **fees.py**. An episode is a stretch
where the net edge stays positive. When it ends it is stored as an
`Opportunity` with its legs, duration, peak edge, how many contracts the
recorded depth would have filled at the peak, and the return on the capital
tied up, annualized as if held until the bet pays out. The scanner also
calls the executor once per episode.

**execute.py** paper trades the signal. It pretends to send one limit order
per leg. Both ladders are walked together and each leg's limit is set at
the deepest level that still leaves the minimum edge, so an order sweeps
every level above the floor rather than only the top one. Each order
arrives after a latency drawn from what was measured from us-east-1
(about 50 ms to Kalshi, 60 ms to Polymarket US, lognormal) and fills
against the book as it is at that moment, from the same in memory books.
Only half the visible size at a level is assumed to be ours, and 3% of
orders are rejected outright. A leg that filled short is flattened at
once, by selling the excess back or buying the missing side on the other
venue, whichever the books say leaves more money, and whatever stays
exposed is tried again on every tick until it is flat or the bet pays
out. Signals need a net edge of at least five cents per contract and a
payout within a day, and a trade is capped at 50 contracts, about $50 of
capital across both legs, so a full Sunday slate fits the balances. Every
trade is stored as soon as it is sent and updated when it is done.

**balances.py**, **settle.py**, **rebalance.py** keep the paper books.
Each venue starts with $10,000. Money for an order in flight is reserved
before anything is awaited, so two signals in the same moment cannot spend
the same dollars. Every cash movement is a `Ledger` row that records the
balance it left behind, so a restart reads the newest row instead of
replaying history. Every ten minutes the settler asks the venues how the
contracts of trades past their payout time resolved, pays the winning leg
a dollar a contract, and writes each leg's result, payout, and settlement
time on the trade, which is what a tax return needs. On Mondays the rebalancer
compares the venues and, when one sits more than 25% above the average,
sends the excess to the other as a `Transfer` that takes four business
days, during which the money is on neither venue.

### Tools

`src/tools/summary.py` prints a report from the database: row counts,
pairs by kind, recording health, opportunities by kind with the largest
that beat a 10% annual return, and paper trades by outcome and kind.
`--hours` sets the window.

## Deployment

The recorder runs on a t3.medium in us-east-1, the region Kalshi's
matching engine runs in, where a signed round trip is about 35 ms to
Kalshi and 30 ms to Polymarket US. A systemd service, `sportsarb-recorder`,
starts `python3 -m live.run --sport nfl` from `~/SportsArb/src` on boot
and restarts it on any exit. The venue API keys live in `data/`, which is
gitignored, and are copied to the instance by `scp` only. Deploying is
`git pull` on the instance followed by a service restart, which refreshes
the catalog for about 80 seconds and then resubscribes. The instance was
first placed in Mexico to reach polymarket.com, which was then dropped as a
venue for legal reasons in favor of Polymarket US, and moved to us-east-1.

`commands.txt` holds the commands used to check the data, deploy, and
operate the instance.

The process is light. It holds 4,900 books in about 190 MB of memory,
and the database grows by roughly 500 MB a day.

## Results so far

From the run that started on September 22, 2026, read after 43 hours.

**Coverage.** 14,972 Kalshi and 23,044 Polymarket US contracts fetched,
10,488 classified into bets, 2,452 pairs found across 17 kinds. Of those
658 are game markets (winner, spread, total), 128 are season futures
(champion, conference, division, top seed), and 1,666 are player props,
touchdowns and first touchdown, passing, rushing and receiving yards,
receptions, and a few others.

**Recording.** The feeds delivered 6.8 million Kalshi and 2.0 million
Polymarket US book updates, of which 2.3 million changed the top of book
and were written, a median of 52,000 rows an hour and a peak of 95,000.
Kalshi dropped the connection 3 times and Polymarket US 38 times, but
with immediate reconnect the total time without a subscription was 3
seconds.

**Opportunities.** The scanner saw 4,902 episodes of positive net edge.
They are short: 4% ended within a second, 19% within ten seconds, 56%
within a minute, and 55 seconds is the average. A few things stand out.

- The largest dollar edges are in season futures, where a 2 to 3 cent edge
  on a few thousand contracts is worth $25 to $30, but those tie up the
  capital for four months and return 6 to 8% a year, under the 10% target.
- Player props show the biggest edges per contract, up to 43 cents on a
  passing yards line, but with a few dollars of depth behind them. Props
  are where Polymarket US and Kalshi disagree most, and they resolve the
  same night, so close to half of the prop episodes beat 10% a year.
- Spreads are the opposite: half a cent of edge, but on $1,000 of depth for
  minutes at a time, worth $4 to $6 a piece.
- Direction is not symmetric. In 2,650 episodes the cheap leg was yes on
  Kalshi with the other side bought on Polymarket US, in 1,943 the reverse,
  and in 303 both legs were plain yes contracts.

**Paper trades.** The executor took its first trades on the Thursday
night props for Atlanta at Green Bay: 7 trades, 4 filled in full, 3 in
which the Polymarket US leg was gone by the time the order arrived
(about 40 to 100 ms later) and the Kalshi leg was sold back at a loss of
a few cents. Net result after hedges was +$0.07 on a few dollars of
capital. The edges on the props are real but the depth behind them is one
to five contracts, so the money is small and every fill is a race.

**The first live game.** Atlanta at Green Bay on September 24 was the
first game recorded in play, on a fresh database with $10,000 per venue,
a 2-cent minimum edge and 500 contracts per trade. The recorder held
1,700 Kalshi updates a second without a drop and wrote 550,000 rows in
the peak hour. The scanner saw 5,587 positive-edge episodes on the game's
pairs during play, and almost all were noise: 4,060 had under a cent of
edge and 2,860 of those lasted under a second. The money sat in 88
episodes of 10 cents or more, nearly all the same event: a scoring play or
a decisive stat, one venue reprices, the other keeps its old price for a
fraction of a second. Drake London's touchdown, later overturned, showed
as a 78-cent edge for 0.2 seconds.

The paper executor sent 398 trades in three hours and then stopped
because both balances were at zero. 247 filled in full, 45 partly, 106
not at all. Kalshi filled 65% of what was asked and Polymarket US 41%,
with simulated latency never above 150 ms, so it is the book moving
within a tenth of a second, not slow orders. Trades signalled at 2 to 3
cents lost money after hedging; the 44 at 10 cents or more made most of
the profit. When every trade had settled the two venues held $20,653, a
gain of $653, or 3.3% on the capital, in three hours. About a third of
the gain was luck: 2,161 contracts were left holding one side only,
because the flatten attempt found nothing it could take, and the
resolutions happened to go the right way.

That game set the current limits. Thin edges are not worth the race, so
the minimum is five cents. At 500 contracts the game wanted $31,000
against $20,000 available and the last quarter hour went untraded, so
the cap is 50, which fits the nine games of a Sunday early window. Two
execution flaws it exposed are fixed: orders now sweep the levels above
the edge floor instead of only the top level, and a level too small for a
whole contract no longer ends a ladder walk, which is what had turned
most of the one-sided positions into "no book to flatten".

The honest reading is that after fees the two venues are tightly priced
before kickoff and briefly, sharply mispriced after every scoring play.
The paper edge is real. Whether it is reachable is a question of whether
the stale quote is still there when a real order lands, which the paper
model assumes half the time and which only real orders can measure.

## Running it

Python 3.12 or newer. Install the three dependencies and run the tests:

```bash
pip3 install -r requirements.txt
python3 -m pytest tests -q
```

Build the catalog and run the recorder locally, from `src/`:

```bash
cd src
python3 -m catalog.pipeline --sport nfl
python3 -m live.run --sport nfl
```

`--no-trade` scans without paper trading, `--no-scan` only records, and
`--seconds 120` runs a short test. The streams need venue keys in `data/`:
`kalshi_key_id.txt` and `kalshi_private_key.pem` for Kalshi,
`polymarket_us_key_id.txt` and `polymarket_us_secret_key.txt` for
Polymarket US. Then read the report:

```bash
python3 src/tools/summary.py --hours 24
```

## Layout

```
src/
  api/        venue clients and the shared websocket book stream
  catalog/    fetch, classify (one parser per venue), match, pipeline
  common/     paths, time helpers, json helpers, the venue list, the logger
  db/         models and the SQLite schema
  live/       run, record, streams, scan, pricing, fees, execute, balances, settle, rebalance
  tools/      summary report
tests/        mirrors src, 111 tests, run with pytest
commands.txt  operating the AWS instance
```
