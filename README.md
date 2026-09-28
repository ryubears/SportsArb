# SportsArb

A bot that looks for cross-venue arbitrage between the two US prediction
markets that list NFL contracts, Kalshi and Polymarket US, and trades what
it finds, on paper, with real money, or both at once. When the cheapest
way to hold *yes* on one venue and the cheapest way to hold *no* on the
other add up to less than a dollar after fees, buying both locks in the
difference whatever the game does.

The whole thing runs on an EC2 instance in us-east-1, as one process with
each venue's feed in a child process of its own: it follows every order
book change for about 4,900 contracts, prices about
2,450 cross-venue pairs on every change, sends orders when a pair shows an
edge, settles the trades when the contracts resolve, and keeps the two
venues funded. By default the orders are paper. With `--execute live` or
`--execute both` it sends real ones.

## How it works

Everything lives in `src/`, in two programs.

**The catalog** (`src/catalog`) works out what can be traded. It fetches
every open NFL market from both venues, restates each one as a `Bet` in
venue neutral terms, and pairs up the bets both venues list. The pairs go
into the database. You can run it on its own (`python3 -m
catalog.pipeline`), and the live process reruns it every hour.

**The live process** (`src/engine/run.py`) trades the pairs. It follows
every paired contract's order book, prices each pair as its books change,
trades when a pair shows enough edge, and settles the trades when the
games end.

Both share the venue clients in `src/api`, the database layer in `src/db`,
and small helpers in `src/common`. This section walks the code in the order
data moves through it. [Part by part](#part-by-part) says what each file
does in more detail, and [Layout](#layout), at the end, where to look for
what.

### The words the code uses

- **Contract**: one tradable market on one venue, seen from its Yes side.
- **Bet**: what a contract is about, in venue neutral terms: kind, game,
  teams, subject, line. Two contracts with the same bet fields are the
  same bet.
- **Pair**: the contracts on both venues that describe one bet. Its
  contracts are its **members**.
- **Side**: `yes` or `no`, the two outcomes of a bet. Holding both sides
  pays exactly a dollar a pair, whichever way the game goes.
- **Polarity**: which side of the bet a contract pays on. You hold the
  side a contract pays on by buying the contract, and the other side by
  buying its opposite, at one minus its bid.
- **Book**: one contract's order book at one moment, a few price levels
  of bids and asks, from the Yes side (`db/models.py`).
- **Ladder**: what holding one side through one contract costs, level by
  level, fees left out, cheapest first (`pricing.ladder`).
- **Edge**: a dollar less what holding both sides costs, fees included,
  per contract. Positive edge is the arbitrage.
- **Episode**: a stretch while a pair's edge stays positive. Each ends up
  as an **Opportunity** in the database.
- **Leg**: one side of a trade: the member it goes through, the order
  sent, and what it holds after.
- **Exposed**: a trade whose legs filled unevenly, so it holds more of
  one side than the other and is not a sure dollar any more.
- **Flatten**: fix an exposed trade by selling the excess back or buying
  the missing side.
- **Mode**: `paper` or `live`. Paper fills are simulated against the real
  books, live ones are real orders. Every trade is stored with its mode.
- **Desk**: everything one mode needs: its money, its sizing, its
  executor, its settler, and its rebalancer.

### The processes

With feed processes on (the default), the live process is three
processes, one per venue feed plus the main one:

```
  Kalshi websocket              Polymarket US websockets
          |                                |
          v                                v
  kalshi feed process           polymarket_us feed process
          |                                |
          +---------------+----------------+
                          |   changed books and gaps, over pipes
                          v
  main process:  Streams -> Recorder -> Scanner -> a Desk per mode
                            (books in   (stores     (executor, allocator,
                             memory)     episodes)   settler, rebalancer)
```

A child keeps its venue's connections and full books, and sends the main
process only the best five levels of each changed book, plus any gaps.
When the main process falls behind, a child sends only the newest book of
each contract, not a backlog. `--set feed_processes=false` runs the feeds
inside the main process instead.

### The life of a book update

Follow one Kalshi order book change from the wire to a trade.

1. **Parse** (`api/kalshi.py`, `KalshiBookStream.handle` and `apply`).
   The feed child reads the websocket message, applies the change to its
   copy of the book, and passes the best levels on. The base class,
   `api/bookstream.py`, handles connecting, subscribing, and reconnecting.
   It also records each stretch without a connection as a gap.
2. **Hand over** (`engine/components/feeds.py`). `VenueFeed` stamps the
   time the book arrived, and the `Outbox` holds the newest book of each
   contract until its thread sends it down the pipe.
3. **Receive** (`FeedProcess.receive`, then `Streams.on_book` in
   `streams.py`). The main process drops books of contracts the catalog
   has since removed and passes the rest on.
4. **Remember** (`Recorder.on_book` in `record.py`). The recorder keeps
   the book as the contract's newest `Book`. If the best bid or ask
   changed, it tells the scanner.
5. **Price** (`Scanner.on_book` and `update` in `scan.py`, with
   `pricing.best_trade`). For every pair the contract belongs to, the
   scanner finds the cheapest way to hold yes and the cheapest way to
   hold no across the pair's members. Then it works out the edge of
   buying both.
6. **Follow the episode**. A positive edge opens an `Episode` or extends
   it and keeps its peak. When the edge ends, the episode is stored as an
   Opportunity. While it lasts, each desk's executor is offered it, until
   that executor takes a trade.

One live game brought up to 1,700 Kalshi changes a second. That is why
steps 1 and 2 run in the feed processes, and only a change to a best
level reaches the scanner.

### The life of a trade

1. **Decide** (`Executor.signal` in `execute/executor.py`). The executor
   takes the signal when the edge is at least `MIN_EDGE` and the game is
   in play. `quantity_for` walks both ladders together through the levels
   that keep that edge and sets each leg's limit at the deepest one. It
   then asks for `FILL_SHARE` of what those levels show, capped by the
   allocator and by the cash each venue can spend. The cash is reserved
   and the trade is stored before any order goes out.
2. **Fill** (`run_trade`). Both legs go out at once.
   - Paper (`execute/paper.py`): each order waits a latency drawn from
     what was measured, then fills against the book as it is then.
   - Live (`execute/live.py`): each order is a real immediate or cancel
     limit order through the venue client's `place_order`. It is stored in
     the orders table before it is sent and again with the answer.
3. **Flatten** (`flatten`). If the legs filled unevenly, `sale_value` and
   `purchase_value` price selling the excess back against buying the
   missing side on the other venue. `sell_excess` or `buy_missing` then
   does whichever leaves more money.
4. **Keep trying** (`retry`, every tick). Whatever stays exposed is tried
   again against newer books until it is flat, its payout time passes, or
   it settles. A restart reads exposed trades back from the database
   (`reload_exposed`).
5. **Settle** (`Settler` in `settle.py`). From kickoff, every 30 seconds,
   the settler asks the venues how the held contracts resolved. It pays
   each winning leg a dollar a contract into the desk's money, stores the
   `Settlement`, and tells the executor to stop flattening that trade.

### What happens when

`Session.tick` runs once a second:

- the scanner prices every open episode again, so episodes whose books go
  stale end;
- each desk reads the live balances when due, retries exposed trades,
  starts a settlement pass every 30 seconds, and lets its rebalancer
  check the balances;
- a status line is logged every minute, and each component's summary
  every ten.

Every hour the catalog is refreshed in a background thread, and the new
pairs' contracts are added to the running feeds without reconnecting.

### Where things are stored

One SQLite file, `data/sportsarb.sqlite`. Each table is written by one
place (see the top of `db/database.py`):

| table | written by |
|---|---|
| contracts, bets, pairs | the catalog |
| gaps | the recorder, when a feed connection drops |
| opportunities | the scanner, one row per episode |
| trades | the executors, paper and live |
| orders | the live executor, one row per real order |
| settlements | the settler |
| ledger, transfers | paper money and its weekly transfers |
| alerts | every email the live process sends |

Trades and settlements carry a `mode`, `paper` or `live`, so the two modes
never mix. Every table has a model in `db/models.py` and its schema in
`db/schema.sql`. Changes to a table for databases that already exist are
numbered steps in `db/migrations.py`, and `db/database.py` holds the reads
and writes.

## Part by part

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

**pipeline.py** runs fetch, classify, and match in one call. The live
process runs it every hour in a background thread, so new games and props
enter the pairs while it runs.

### Venue clients (`src/api`)

**bookstream.py** is the shared websocket loop: one connection per venue
carrying every wanted contract, a live book per contract, changes to the
wanted set applied without reconnecting, and a reconnect that is
immediate on the first drop and backs off only on repeated ones, with the
stretch until the new subscription is confirmed stored as a gap, and the
books of that connection's contracts dropped until it sends them again.
**kalshi.py** signs each connection and request with RSA-PSS and holds the
whole catalog on one connection. **polymarket_us.py** signs with Ed25519
and subscribes in requests of up to 100 slugs. The feed takes ten requests
on a connection and has no unsubscribe, and every catalog refresh that
adds contracts spends one more, however few it adds. So new contracts go
to a connection with requests left, a new connection opens when none has
any, and contracts a connection refuses anyway, as one request too many,
move to another. Both clients also report how a contract resolved,
which the settler uses, and carry the live trading calls: the account's
balance, and an immediate or cancel limit order whose answer they turn
into an `Answer`, the same for both venues. Polymarket US prices
every order on the long side, so a short side order at p is sent at 1 - p,
and when its answer does not say how an order ended, the order itself is
looked up, since a returned order id does not mean the order is done.
Trading calls go over kept HTTPS connections from **http.py**, since a new
TLS connection costs round trips a race cannot spare, and are never
retried, since an order sent twice trades twice. The field names come from
the venues' published Python SDKs.

### Engine (`src/engine`)

**run.py** is the process that runs. Its `Session` wires the recorder, the
venue connections, the scanner, and a `Desk` for each mode it trades in
together, and a one-second timer ticks it, as
[What happens when](#what-happens-when) describes. A desk is one mode's
executor, its money, its allocator and settler, and its rebalancer, which
keeps its venues funded. `--execute` picks the desks: `paper`, the default,
`live`, or `both`, which trades the same signals on paper and for real and
so measures how far the paper fills are from real ones. The same loop
starts the hourly catalog refresh in a background thread and applies the
result to the live connections. The pieces it wires together are in
`engine/components/`, and what they share is in `engine/helper/`: the
settings, game timing, pricing, and fees.

**record.py** holds the newest book for every paired contract in memory,
five levels a side, which the scanner prices and the executors trade
against. Books are not stored, only the gaps when a venue's feed was down.
**feeds.py** holds a venue's connections, one, or several when the venue
caps how much one connection may carry, and applies catalog changes to them
in place, each new contract going to a connection with room. Each venue's
feed runs in a child process of its own, so receiving, parsing, and keeping
the books use another core. The child passes each changed book's best
levels over a pipe, only the newest of a contract when the main process
falls behind, and a child that dies is started again, with the stretch
stored as a gap. **streams.py** gives each venue its feed and passes the
books to the recorder. With `--set feed_processes=false` every feed runs in
the main process instead.

**scan.py** prices every pair whose member's book just changed. Using
**pricing.py** it walks the ladders to find the cheapest way to hold yes and
the cheapest way to hold no across the pair's members, on any venues,
including the venue's taker fee from **fees.py**. An episode is a stretch
where the net edge stays positive. When it ends it is stored as an
`Opportunity` with its legs, duration, peak edge, how many contracts the
recorded depth would have filled at the peak, and the return on the capital
tied up, annualized as if held until the bet pays out. While an episode
is open the scanner offers it to each executor on every update until that
executor takes a trade, and then not again: paper orders take nothing out
of the books they fill against, so a second trade on the same books would
count the same contracts twice. The live executor is offered it first. The
edge coming back after it has gone is a new episode.

**execute/** trades the signal. **executor.py** holds what paper and live
share, which is everything but how an order is filled. One limit order is
sent per leg. Both ladders are walked together and each leg's limit is set
at the deepest level that still leaves the minimum edge, so an order
sweeps every level above the floor rather than only the top one. In
**paper.py** each order arrives after a latency drawn from what was
measured from us-east-1 (about 50 ms to Kalshi, 60 ms to Polymarket US,
lognormal) and fills against the book as it is at that moment, from the
same in memory books. Only half the visible size at a level is assumed to
be ours, and 3% of orders are rejected outright. In **live.py** each order
is a real immediate or cancel limit order, sent on a thread pool of its
own, and stored in the orders table before it is sent and again with the
venue's answer. Either way a leg that filled short is flattened at once,
by selling the excess back or buying the missing side on the other venue,
whichever the books say leaves more money, and whatever stays exposed is
tried again on every tick until it is flat, the bet pays out, or the
settler settles it. A restart takes back from the trades table whatever
is still exposed, so a crash or a deploy does not leave it unhedged. A
live order that flattens is limited to the deepest price the books said
it would reach, so a book that moved leaves the rest for the next tick
rather than filling far from its price. Orders and
flattening only trade against a book that has changed within the last
minute, since a market that has closed may stop changing rather than empty
its book, and its last book cannot be traded. Signals need a net edge of
at least five cents per contract, and only games being played are traded,
so the money comes back the same day. New trades leave a floor of cash
untouched on each venue, $500 on paper and $5 live, so the money is never
run down to nothing and flattening, which may use it, still can; a venue
under its floor makes new trades wait until more arrives. Every trade is
stored as soon as it is sent and updated when it is done.

Live trading has brakes, in **brakes.py**, sized for a test with about $100
on each venue. An order whose outcome cannot be known (a timeout, a dropped
connection, a venue failing on its side, or an answer that cannot be read)
sets its trade aside (`LiveExecutor.set_trade_aside`): no more orders are
sent for it, since what it holds is unknown, and the log says which order
to look up. No email goes out for it. Trading goes on. It halts, sending no
more orders of any kind, when the orders themselves fail:

- 3 of the last 20 orders had an unknown outcome, three in a row or a
  steady error rate. The halt names each of those orders, to look up;
- one venue refused its last 3 orders. A refusal is the venue answering
  that it will not take an order, so nothing traded: not authorized, not
  enough money, a bad price, too many requests, or a market that has
  closed. An order that found nothing at its price is unfilled, not
  refused, as is one Polymarket US turned away for no liquidity or for
  being slow.

It halts new trades, but goes on flattening what is exposed, when the
live trades decided in the last 6 hours lost more than 10% of the live
money, net. If the orders then fail as above, flattening stops as well.

A trade is decided once its legs hold the same number of contracts, which
pay a dollar each whichever way the game goes, or once it settles. Each
rule reads the orders and trades tables, so a restart does not reset it,
and starts over after a halt. A halt is logged and emailed, and what is
held is still settled. It is also written to `data/live_halt.txt`, and
live trading stays halted across restarts, a crash or a deploy, until a
human has checked the venues and removed that file. Live trades hold 1 to
5 contracts until the live results earn more.

**allocate.py** sets how many contracts one trade may hold, so the money
covers every game in play. The games from kickoff until they settle share
each venue's pool, its free cash plus what they already hold, equally. A
game's share becomes a cap at $10 of spending per contract of cap, a little
above the median of the 14 games on Sunday, September 27, between 10 and
1,000 contracts on paper and 1 and 5 live. A game that has spent its share
gets nothing more until others settle and fewer games share the pool. Paper
and live each size from their own money and trades. A live game whose share
is too small for one contract of cap still gets one contract until it has
spent its share. With $100 a venue that happens once more than ten games
share the pool, as while Sunday's late games have kicked off and the early
ones have not yet settled, about $7 a game, and live trading goes on there
rather than stopping. The cap only bounds how many contracts a trade may
hold: whether one is sent at all still depends on the edge, the depth of
the books, and the cash.

**balance/** and **settle.py** track the money: paper money in
**balance/paper.py** and live money in **balance/live.py**, with what they
share in **balances.py**, the rebalancers that keep the venues funded in
**balance/rebalance.py**, and **notify.py** for the live alerts. Money has
the same shape in both modes: free cash per venue, `reserve` and `release`
for an order in flight, and `apply` for a cash movement. Each paper venue
starts with $10,000. Money for an order in flight is reserved before
anything is awaited, so two signals in the same moment cannot spend the
same dollars. Every cash movement is a `Ledger` row that records the
balance it left behind, starting with a `transfer_in` of each venue's
opening balance, so the ledger accounts for every dollar and a restart
reads the newest row instead of replaying history. From kickoff, every 30
seconds, the settler asks the venues how the contracts of open trades
resolved, pays the winning leg a dollar a contract, and stores each leg's
result, payout, and settlement time as the trade's `Settlement`, which is
what a tax return needs. A trade still exposed on one side is settled as it
stands, each leg paid for what it holds. The settler skips a trade while an
order to flatten it is in flight, and once a trade settles the executor
stops flattening it. The venue holding a winning leg receives the whole
dollar, so the balances drift apart. Trades are open most of the time, so
the venues are compared as they stand: each counts its free cash plus what
open trades hold on it at cost, which is about what those trades will pay
back there. On Tuesdays the `PaperRebalancer` compares them and, when one
sits more than 20% above the average, sends the excess to the other, as
much of it as is free above the floor, as a `Transfer` that takes four
business days, during which the money is on neither venue.

Live money has no ledger of ours. `LiveBalances` reads each venue's balance
every 30 seconds, and at once after a payout, and applies what our own
fills move in between, so a burst of trades does not spend the same dollars
twice. Nothing is traded before the first reading. The settler settles live
trades as it does paper ones, storing each as a `live` `Settlement`, while
the venue pays out on its own. Live money is moved between venues by hand,
so instead of transferring, the `LiveRebalancer` emails when one venue,
counted the same way, sits more than 20% above the average, saying how much
of its free cash to move where, and again each day while they stay apart.
`notify.py` stores every alert in the alerts table and emails it in a
background thread through the SMTP server in `data/email.json`.

### Tools

`src/tools/summary.py` prints a report from the database: row counts,
pairs by kind, feed drops, opportunities by kind with the largest
that beat a 10% annual return, and for paper and live apart the trades by
outcome and kind and the settled legs by venue, then the paper balances
with any transfer in transit, and the real orders sent by venue and what
came back. `--hours` sets the window.

`src/tools/live_check.py` reads both venues' balances with the keys in
`data/`, and with `--email` sends a test email, without trading. Run it
before the first live run.

## Deployment

The live process runs on a t3.medium in us-east-1, the region Kalshi's
matching engine runs in, where a signed round trip is about 35 ms to
Kalshi and 30 ms to Polymarket US. A systemd service, `sportsarb-recorder`,
starts `python3 -m engine.run --sport nfl` from `~/SportsArb/src` on boot
and restarts it on any exit, and the process starts a child for each
venue's feed, which stops with it. That trades on paper only. Going live
means adding `--execute both` to the service's command. The venue API keys
live in `data/`, which is gitignored, and are copied to the instance by
`scp` only. Deploying is `git pull` on the instance, the tests, and a
service restart only if they pass, which refreshes the catalog for about
80 seconds and then resubscribes. Each run logs the commit it runs and
every setting when it starts, so the log says what produced its results.
The instance was first placed in Mexico to reach polymarket.com, which was
then dropped as a venue for legal reasons in favor of Polymarket US, and
moved to us-east-1.

`commands.txt` holds the commands used to check the data, deploy, and
operate the instance, with the instance's address, key, and ids written
into them. It is gitignored, so it lives only on the machine that operates
the instance.

The process is light. The main process holds 4,900 books in about
190 MB of memory, and each feed process its own venue's books.
Books are not stored, so the database grows only with the episodes,
trades, and orders.

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
the cap was set to 50, which fits the nine games of a Sunday early
window; the allocator has since replaced that fixed cap. Two
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

Build the catalog and run the live process locally, from `src/`:

```bash
cd src
python3 -m catalog.pipeline --sport nfl
python3 -m engine.run --sport nfl
```

`--no-trade` scans without trading, `--no-scan` only records, and
`--seconds 120` runs a short test. `--execute live` trades with real money
and `--execute both` trades the same signals on paper and for real. The
settings a run is tuned by, such as the minimum edge, the trade caps, and
the starting balance, are in `src/engine/helper/config.py`, and `--set
NAME=VALUE` overrides one for a run, for example `python3 -m engine.run
--sport nfl --set min_edge=0.03`. The run logs every setting when it
starts. The streams need venue keys in `data/`: `kalshi_key_id.txt` and
`kalshi_private_key.pem` for Kalshi, `polymarket_us_key_id.txt` and
`polymarket_us_secret_key.txt` for Polymarket US. Live trading uses the
same keys, which need trading permission, and emails its alerts through
`data/email.json`:

```json
{"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
 "password": "an app password", "from": "me@gmail.com", "to": ["me@gmail.com"]}
```

Before the first live run, check both from `src/`:

```bash
python3 -m tools.live_check --email
```

Then read the report:

```bash
python3 src/tools/summary.py --hours 24
```

## Layout

```
src/
  api/        venue clients and the shared websocket book stream
  catalog/    fetch, classify (one parser per venue), match, pipeline
  common/     paths, time and json helpers, the venue list, the logger
  db/         models, the SQLite schema and its migrations, reads and writes
  engine/     run, the process that wires the components together
    components/  record, feeds, streams, scan, allocate, settle, notify
      balance/   balances (what paper and live share), paper, live, rebalance
      execute/   executor (what paper and live share), paper, live
    helper/      config (the settings a run is tuned by), game (which game a bet is on and when it is played), pricing, fees
  tools/      summary report, live_check
tests/        mirrors src, run with pytest, configured in pyproject.toml
  support/    helpers the tests share, and a stream a feed process can run
commands.txt  operating the AWS instance, gitignored, kept locally
```

Where to look to change something:

| to change | look in |
|---|---|
| what a venue's API returns | `api/kalshi.py`, `api/polymarket_us.py` |
| how a websocket connects and reconnects | `api/bookstream.py` |
| which markets are understood | `catalog/classify/` |
| how bets are paired | `catalog/match.py` |
| fees | `engine/helper/fees.py` |
| how an edge is priced | `engine/helper/pricing.py` |
| game timing | `engine/helper/game.py` |
| every tunable number | `engine/helper/config.py` |
| when a trade is taken and sized | `engine/components/execute/executor.py` |
| how an order fills | `execute/paper.py`, `execute/live.py` |
| when live trading halts | `engine/components/execute/brakes.py` |
| how the processes are wired | `engine/run.py` |
| the report on the database | `tools/summary.py` |
