# SportsArb

A bot that looks for cross-venue arbitrage between the two US prediction
markets that list NFL, college football, MLB, NHL, and NBA contracts,
Kalshi and Polymarket US, and trades what it finds, on paper, with real
money, or both at once. When the cheapest way to hold *yes* on one venue
and the cheapest way to hold *no* on the other add up to less than a dollar
after fees, buying both locks in the difference whatever the game does.

The whole thing runs on an EC2 instance in us-east-1, as one process with
each venue's feed in a child process of its own: it follows every order
book change for thousands of contracts, prices every cross-venue pair on
every change, sends orders when a pair shows an edge worth the time its
money is tied up, and settles the trades when the contracts resolve. It
trades before games and on season futures, not games under way, where
faster traders take the edges first. By default the orders are paper.
With `--execute live` or `--execute both` it sends real ones.

## How it works

Everything lives in `src/`, in two programs.

**The catalog** (`src/catalog`) works out what can be traded. It fetches
the open NFL, college football, MLB, NHL, and NBA game markets and futures
from both venues, restates each one as a `Bet` in venue neutral terms, and
pairs up the bets both venues list. The pairs go into the database. You can
run it on its own (`python3 -m catalog.pipeline`), and the live process
reruns it every hour.

**The live process** (`src/engine/run.py`) trades the pairs. It follows
every paired contract's order book, prices each pair as its books change,
trades when a pair shows enough edge for the time until it pays, and
settles the trades when the contracts resolve.

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
- **Leg**: one side of a trade: the contract it goes through, the order
  sent, and what it holds after (`db/models.py`).
- **Exposed**: a trade whose legs filled unevenly, so it holds more of
  one side than the other and is not a sure dollar any more.
- **Flatten**: fix an exposed trade by selling the excess back on its own
  venue.
- **Payout**: when a trade's money comes back, the latest of its members':
  half an hour after its game's expected end, or a future's close.
- **Shard**: Kalshi keeps each sport's markets on an exchange shard whose
  cash is its own, football and hockey on shard 0, baseball and basketball
  on 3. An order spends only its market's shard's cash.
- **Mode**: `paper` or `live`. Paper fills are simulated against the real
  books, live ones are real orders. Every trade is stored with its mode.
- **Desk**: everything one mode needs: its money, its executor, and its
  settler.

### The processes

With feed processes on (the default), the live process is three
processes, one per venue feed plus the main one, and a fourth while the
hourly catalog refresh runs:

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
                            (books in   (stores     (executor and
                             memory)     episodes)   settler)
```

A child keeps its venue's connections and full books, and sends the main
process only the best five levels of each changed book, plus any gaps.
When the main process falls behind, a child sends only the newest book of
each contract, not a backlog. `--set feed_processes=false` runs the feeds
inside the main process instead. The catalog refresh runs in a child
process of its own too. It briefly holds some 300 MB, and a child gives
all of it back when it ends, where a thread of the main process kept
about 50 MB of each refresh for good.

### The life of a book update

Follow one Kalshi order book change from the wire to a trade.

1. **Parse** (`api/kalshi.py`, `KalshiBookStream.handle` and `apply`).
   The feed child reads the websocket message, applies the change to its
   copy of the book, and passes the best levels on, with Kalshi's own
   time for the change. The base class,
   `api/bookstream.py`, handles connecting, subscribing, and reconnecting.
   It also records each stretch without a connection as a gap.
2. **Hand over** (`engine/components/market/feeds.py`). `VenueFeed`
   stamps the time the book arrived, and the `Outbox` holds the newest
   book of each contract until its thread sends it down the pipe.
3. **Receive** (`FeedProcess.receive`, then `Streams.on_book` in
   `market/streams.py`). The main process drops books of contracts the
   catalog has since removed and passes the rest on.
4. **Remember** (`Recorder.on_book` in `market/record.py`). The recorder
   keeps the book as the contract's newest `Book`, with our time for it
   and the venue's. If the best bid or ask changed, it tells the scanner.
5. **Price** (`Scanner.on_book` and `update` in `market/scan.py`, with
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

Polymarket US differs in one way: each book message carries the whole
book, so the child replaces its copy rather than patching it. Both venues
say when they made each change, so the status line every minute says how
far behind the venue each feed's books reached us, which the executor
checks before it trades, below.

### The life of a trade

1. **Decide** (`Executor.signal` in `trading/executor.py`). The executor
   takes the signal when the edge is at least `MIN_EDGE`, the pair is a
   future or its game has not kicked off (`before_kickoff`), the bet pays
   out `MIN_PAYOUT_HOURS` or more away and the edge returns
   `MIN_ANNUAL_PCT` a year or more until then (`pays_enough`), and a
   Polymarket US leg's book is current (`confirmed`): newer than the Kalshi
   leg's last change, by the venues' own clocks, or else that change is
   `CONFIRM_SECONDS` old. Otherwise the edge waits, and the scanner offers
   it again at the next change or tick. `quantity_for` walks both ladders
   together through the levels that keep that edge and sets each leg's
   limit at the deepest one. It then asks for `FILL_SHARE` of what those
   levels show, but no more than the cash each venue can spend, on Kalshi
   the cash on the market's shard, live as on paper. The cash is reserved
   and the trade is stored before any order goes out.
2. **Fill** (`run_trade`). Both legs go out at once.
   - Paper (`trading/paper.py`): each order waits a latency drawn from
     what was measured, then fills against the book as it is then.
   - Live (`trading/live.py`): each order is a real immediate or cancel
     limit order through the venue client's `place_order`. It is stored in
     the orders table before it is sent and again with the answer.
3. **Flatten** (`flatten`). If the legs filled unevenly, `sell_excess`
   sells the excess back on its own venue. Buying the missing side on the
   other venue might cost less, but would tie the money up until the bet
   pays, where a sale frees it at once.
4. **Keep trying** (`retry`, every tick). Whatever stays exposed is tried
   again against newer books until it is flat, its payout time passes, or
   it settles. A restart reads exposed trades back from the database
   (`reload_exposed`).
5. **Settle** (`Settler` in `money/settle.py`). From kickoff, every 30
   seconds, the settler asks the venues how the held contracts resolved.
   It pays each winning leg a dollar a contract into the desk's money,
   stores the `Settlement`, and tells the executor to stop flattening that
   trade.

### What happens when

`Session.tick` runs once a second:

- the scanner prices every open episode again, so episodes whose books go
  stale end;
- every hour the attestation watch reads when the Kalshi key's location
  attestation lapses;
- each desk reads the live balances every 15 seconds, retries exposed
  trades, and starts a settlement pass every 30 seconds, and the live desk
  emails once when the cash on Polymarket US, or on either Kalshi shard it
  trades on, falls under `LIVE_LOW_CASH`;
- a status line is logged every minute, and each component's summary
  every ten.

Every hour each sport's catalog is refreshed in a child process, and the
new pairs' contracts are added to the running feeds without reconnecting.

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
| ledger | paper money, every dollar in and out |
| alerts | every email the live process sends: low cash, halts, and the Kalshi key's attestation |

Trades and settlements carry a `mode`, `paper` or `live`, so the two modes
never mix. Every table has a model in `db/models.py` and its schema in
`db/schema.sql`. Changes to a table for databases that already exist are
numbered steps in `db/migrations.py`, and `db/database.py` holds the reads
and writes.

## Part by part

### Catalog (`src/catalog`)

**fetch.py** pulls a sport's open contracts from both venues into the
`contracts` table. Of the hundreds of series Kalshi lists for a sport it
takes only those classified: the game winner, spread, and total,
baseball's, hockey's, and basketball's team totals, and player props: the
NFL's and MLB's, the NHL's goals and points, and the NBA's points,
rebounds, assists, threes, and blocks, and each sport's futures. Kalshi is
read through its public REST catalog, paged under the rate limit, and each
series brings the exchange shard its markets trade on. Polymarket US is
read through its gateway, one call per tag, deduplicated across tags,
baseball through the `mlb` tag, since `baseball` brings Korean and Japanese
league games too. An event there is a game, whose start time is its
kickoff, when it has a game id, the venue's own or Sportradar's, which NHL
preseason games and many small college games carry alone. A future's start
time is left out.

**classify/** turns each contract into a `Bet`, a venue neutral statement
of what the contract is about: kind, season, game date, the two teams, a
subject, and a line. Each venue has its own parser, since the two describe
the same thing very differently. Kalshi encodes the game in the ticker,
`KXNFLGAME-26SEP24ATLGB-GB`, and the prop in a series code and a title like
*Player: 100+ receiving yards*. Polymarket US encodes it in a slug and a
title like *Will Bijan Robinson record 100+ rushing yards?*. Team aliases
are resolved through `teams.py` and `aliases/`, which has a file for each
sport, since leagues reuse codes (DAL is the Cowboys and the Mavericks).
College football's file lists each venue's codes apart, since the venues
give some codes to different schools: SDST is South Dakota State on Kalshi
and San Diego State on Polymarket US. Its Kalshi codes run from two letters
to five, so a ticker's two glued codes are split every way, and one that
splits into two teams more than one way is left out. Hockey's file lists
four teams' codes per venue too, Montreal being MTL on Kalshi and `mon` on
Polymarket US, and basketball's five, New York being NYK and `ny`.
Polymarket US typed every sport's game markets `moneyline`, `spreads`, and
`totals` until June 2026 and names them by sport since, so basketball's,
which it has not listed yet for the new season, are read by the names the
other sports' now have, `basketball_team_full_game_winner` and the like.
Baseball's Kalshi event tickers carry the start time too,
`KXMLBGAME-26SEP291400PHIATL`, and a team total names its team in the
market's ticker or slug. A bet knows a game by its date and teams, which a
doubleheader's two games share, so both venues' contracts on doubleheaders
are left out for now. A football, hockey, or basketball season is named for
the year it ends, a baseball season for the year it is played. Player names
are normalized to a key that ignores accents, punctuation, and suffixes, so
Kalshi's *Ronald Acuña Jr.* is Polymarket US's *Ronald Acuna*, and lines
are made strict, so *100+* on one venue and *over 99.5* on the other become
the same bet. Futures, bets on a season rather than a game, are read too:
champions, conference and division winners, top seeds, playoff places,
awards, and season win and point totals. Kalshi gives each kind a series of
its own, and Polymarket US tells them apart by the shape of the event slug,
`nfl-afceast-D-w` being a division winner. The venues number seasons
differently, so a future's season is the one it settles in. Polymarket US's
futures do not always use its games' team codes, gluing city and nickname,
`bufbil`, or borrowing Kalshi's, `gsw`, so a future's team is the code
whose team its market title could name. Contracts no parser understands are
counted and left out.

**match.py** groups a sport's bets whose identity agrees into a `Pair`,
whose label starts with the sport, for example `nfl spread 2026-09-20
CAR@ATL ATL 4.5`. A pair only exists when both venues list the bet, and it
carries every contract that expresses it, since a game winner can be held
through either team's contract and a spread through either side. Kinds with
settlement rules that differ between venues carry a note for their sport,
from **notes.py**. For example both venues settle football props to the
pre-game price if the player never takes a snap, but Polymarket US ignores
stat corrections made after the game, and a postponed baseball game settles
at a fair price on Kalshi after two days while Polymarket US waits up to
two weeks for it. Hockey's rules agree: both venues count overtime, and a
shootout as one goal for its winner in spreads and totals, but not in a
player's goals. Basketball's postponed games part as baseball's do: Kalshi
settles at a fair price after 48 hours, while Polymarket US waits up to two
weeks. A future's label names its season in place of the game, `nfl
champion 2027 KC`, and an award's pair notes that Polymarket US divides the
dollar among players who share the award, where Kalshi's rules do not
always say.

**pipeline.py** runs fetch, classify, and match in one call. The live
process runs it every hour in a child process, so new games and props
enter the pairs while it runs.

### Venue clients (`src/api`)

**bookstream.py** is the shared websocket loop: one connection per venue
carrying every wanted contract, a live book per contract, changes to the
wanted set applied without reconnecting, and a reconnect that is immediate
on the first drop and backs off only on repeated ones, with the stretch
until the new subscription is confirmed stored as a gap, and the books of
that connection's contracts dropped until it sends them again. Each change
is passed on with the venue's own time for it, when the message gives one.
**kalshi.py** signs each connection and request with RSA-PSS and holds the
whole catalog on one connection, on the hosts Kalshi dedicates to API
traders, `external-api`, where a signed call took 17 ms against 27 on the
old host. It also reads the cash on each exchange shard, moves cash between
shards and sets the split Kalshi keeps them to, for
`tools/kalshi_shards.py`, and reads when the key's location attestation
lapses, past which Kalshi refuses the key for sports markets.
**polymarket_us.py** signs with Ed25519 and subscribes to books in requests
of up to 100 slugs. The feed takes ten requests on a connection, so a
connection carries 1,000 markets, and has no unsubscribe, and every catalog
refresh that adds contracts spends more, however few it adds. So new
contracts go to a connection with requests left, a new connection opens
when none has any, and contracts a connection refuses anyway, as one
request too many, move to another. Updates are not batched, since batching
held our view of the books behind the venue's. Its trade feed was dropped
on September 30, since trades came no sooner than the book that showed
them. Both clients also report how a contract resolved, which the settler
uses, and carry the live trading calls: the account's balance, and an
immediate or cancel limit order whose answer they turn into an `Answer`,
the same for both venues. Polymarket US prices every order on the long
side, so a short side order at p is sent at 1 - p, and when its answer does
not say how an order ended, the order itself is looked up, since a returned
order id does not mean the order is done. Kalshi turns away an order whose
market's shard lacks the cash, which counts as unfilled rather than
refused, since nothing traded. Trading calls go over kept HTTPS connections
from **http.py**, since a new TLS connection costs round trips a race
cannot spare, and are never retried, since an order sent twice trades
twice. The field names come from the venues' published Python SDKs.

### Engine (`src/engine`)

**run.py** is the process that runs. Its `Session` wires the recorder, the
venue connections, the scanner, and a `Desk` for each mode it trades in
together, and a one-second timer ticks it, as [What happens
when](#what-happens-when) describes. A desk is one mode's executor, its
money, and its settler. `--execute` picks the desks: `paper`, the default,
`live`, or `both`, which trades the same signals on paper and for real and
so measures how far the paper fills are from real ones. The same loop
starts the hourly catalog refresh in a child process and applies the result
to the live connections. One run trades every sport given to `--sport`,
comma separated as in `--sport nfl,ncaaf,mlb,nhl,nba`, since the money is
one pool and a second process would spend the same dollars. How long a game
is expected to last is set for each sport in `GAME_HOURS`, which sets when
the bets on it pay out. The pieces it wires together are in
`engine/components/`, in three folders by what they do: `market/` follows
the venues, `trading/` makes the trades, and `money/` keeps the cash. What
they share is in `engine/helper/`: the settings, game timing, pricing, and
fees.

**market/** follows the venues: their books and the edges between them.
**record.py** holds the newest book for every paired contract in memory,
five levels a side, which the scanner prices and the executors trade
against. Books are not stored, only the gaps when a venue's feed was down.
A game's contracts on both venues are followed until five hours after
kickoff, whatever their close times say, since Kalshi's close is its guess
at the final whistle, three hours in, which most games outlast. Every
minute its status line says, for each venue, how many changes came, how far
behind the venue they reached us, median and 90th percentile, and how much
of that was ours. **feeds.py** holds a venue's connections, one, or several
when the venue caps how much one connection may carry, and applies catalog
changes to them in place, each new contract going to a connection with
room. Each venue's feed runs in a child process of its own, so receiving,
parsing, and keeping the books use another core. The child passes each
changed book's best levels over a pipe, only the newest of a contract when
the main process falls behind, and a child that dies is started again, with
the stretch stored as a gap. **streams.py** gives each venue its feed and
passes the books to the recorder. With `--set feed_processes=false` every
feed runs in the main process instead.

**scan.py** prices every pair whose member's book just changed. Using
**pricing.py** it walks the ladders to find the cheapest way to hold yes
and the cheapest way to hold no across the pair's members, on any venues,
including the venue's taker fee from **fees.py**. An episode is a stretch
where the net edge stays positive. When it ends it is stored as an
`Opportunity` with its legs, duration, peak edge, how many contracts the
recorded depth would have filled at the peak, and the return on the capital
tied up, annualized as if held until the bet pays out. While an episode is
open the scanner offers it to each executor on every update until that
executor takes a trade, and then not again, so one mispricing makes one
trade. The live executor is offered it first. The edge coming back after it
has gone is a new episode. An episode also keeps its longest stretch at
`MIN_EDGE` or more, and the contracts that stayed fillable through all of
it, which is what an order sent any time in the stretch could have had. A
book goes stale after a minute only once its game may have started: a
future's markets, and a game's before kickoff, can rest unchanged for hours
while they are open, so their books are priced however old they are.

**trading/** trades the signal. **executor.py** holds what paper and live
share, which is everything but how an order is filled. Only bets that pay
out a day or more away are traded, before their game or on a season's
future, and never a game once it has kicked off: near a game and during
it, faster traders take an edge before our Polymarket US order lands, and
the leg is missed. A signal needs a net edge of at least five cents per
contract (`MIN_EDGE`), a payout at least 24 hours away
(`MIN_PAYOUT_HOURS`), and a return of at least 30% a year on the money it
ties up until then (`MIN_ANNUAL_PCT`). Five cents clears that for a bet
paying within 64 days, ten cents within 135, twenty within 304. One limit
order is sent per leg, both at once. Both ladders are walked together and
each leg's limit is set at the deepest level that still leaves the minimum
edge, so an order sweeps every level above the floor rather than only the
top one. A trade asks for half of what those levels show (`FILL_SHARE`),
as far as the cash free on each venue pays for, both legs from one venue's
cash when they share it, live as on paper, with no cap on contracts. No
cash is held back: trades may spend all that is free.

In **paper.py** each order arrives after a latency drawn from what was
measured from us-east-1 (about 50 ms to Kalshi, 60 ms to Polymarket US,
lognormal) and fills against the book as it is at that moment, from the
same in memory books. Only half the visible size at a level is assumed to
be ours, and 3% of orders are rejected outright. A paper order leaves the
venue's book as it was, so paper remembers what it took from each level
and takes it off the books it later sizes, fills, and sells into, until
the level shrinks below that or goes. In **live.py** each order is a real
immediate or cancel limit order, sent on a thread pool of its own, and
stored in the orders table before it is sent and again with the venue's
answer. Either way a leg that filled short is flattened at once by selling
the excess back on its own venue, even when buying the missing side on the
other venue would cost less, since a sale frees the money now and a
purchase would hold it until the bet pays. Whatever stays exposed is tried
again on every tick until it is flat, the bet pays out, or the settler
settles it. A restart takes back from the trades table whatever is still
exposed, so a crash or a deploy does not leave it unhedged. A live sale is
limited to the deepest price the books said it would reach, so a book that
moved leaves the rest for the next tick rather than filling far from its
price. Once a game has started, orders and flattening only use a book that
has changed within the last minute (`pricing.fresh`), as the scanner does,
since a market that has closed may stop changing rather than empty its
book, and its last book cannot be traded. Every trade is stored as soon as
it is sent and updated when it is done.

A leg on Polymarket US trades only on a current book. That venue's books
reached us about 85 ms after it changed them at the median, 160 at the
90th percentile, and Kalshi's in about 12, so after a score Kalshi's new
price could sit next to Polymarket US's old one for a moment, an edge
already gone there. Before a trade the executor compares the venues' own
times for the two books (`confirmed`). A Polymarket US book newer than
the Kalshi book's last change already shows any reaction to it. An older
one is trusted only once that change is `CONFIRM_SECONDS`, 0.3 seconds,
old, long enough for a reaction on Polymarket US to have reached us, and
until then the edge waits for the scanner to offer it again at the next
change or tick. A Kalshi leg has no such wait, since its feed is fast.
Live trading takes every sport the run does. A Kalshi leg spends only the
cash on its market's shard, football's and hockey's on shard 0, baseball's
and basketball's on 3, so `tools/kalshi_shards.py` splits the Kalshi cash
evenly between the two (`LIVE_SHARDS`), and the live executor emails once
when either shard, or Polymarket US, falls under $5 (`LIVE_LOW_CASH`), and
again only after it has been back over.

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
human has checked the venues and removed that file.

**notify.py** tells a human by email when live trading needs one: when it
halts, when the cash on a live venue or Kalshi shard runs low, and when
the Kalshi key's location attestation is two days from lapsing, and again
once it has, since past it Kalshi refuses the key for sports markets until
a person renews it on Kalshi. Its `AttestationWatch` reads the date every
hour, whether or not live trading is on, since the feed uses the same key,
and sends each attestation email once, even across restarts. Every alert
is stored in the alerts table and emailed in a background thread through
the SMTP server in `data/email.json`.

**money/** keeps the cash: paper money in **paper.py** and live money in
**live.py**, with what they share in **balances.py**, and the settler that
pays out trades in **settle.py**. Money has the same shape in both modes:
free cash per venue, `reserve` and `release` for an order in flight, and
`apply` for a cash movement. Each paper venue starts with $10,000. Money
for an order in flight is reserved before anything is awaited, so two
signals in the same moment cannot spend the same dollars. Every cash
movement is a `Ledger` row that records the balance it left behind,
starting with a `transfer_in` of each venue's opening balance, so the
ledger accounts for every dollar and a restart reads the newest row instead
of replaying history. From kickoff, every 30 seconds, the settler asks the
venues how the contracts of open trades resolved, pays the winning leg a
dollar a contract, and stores each leg's result, payout, and settlement
time as the trade's `Settlement`, which is what a tax return needs. A trade
still exposed on one side is settled as it stands, each leg paid for what
it holds. The settler skips a trade while an order to flatten it is in
flight, and once a trade settles the executor stops flattening it. The
venue holding a winning leg receives the whole dollar, so the balances
drift apart over time. Nothing moves money between venues: paper trades
until a venue's cash runs out, and live emails when one runs low, for a
human to move money by hand.

Live money has no ledger of ours. `LiveBalances` reads each venue's balance
every 15 seconds, which also keeps the venue's kept HTTPS connection warm
for the next order, and at once after a payout, and applies what our own
fills move in between, so a burst of trades does not spend the same dollars
twice. Nothing is traded before the first reading. Kalshi's cash on each
exchange shard comes in the same reading and is kept the same way, and an
order on a shard spends no more than that shard has free, whatever the
venue as a whole has. The settler settles live trades as it does paper
ones, storing each as a `live` `Settlement`, while the venue pays out on
its own.

### Tools

`src/tools/summary.py` prints a report from the database: row counts, pairs
by sport and kind, feed drops, and opportunities during games apart from
those before games and on futures. Each group shows all its episodes by
sport and kind, with how many beat `MIN_ANNUAL_PCT` a year, and the
largest, then those whose edge reached `MIN_EDGE`: how long it held there
unbroken, and the money it could have taken and locked in at full size,
with the return and annual return. Before games and futures are shown once
more, only those within the trading rules: paying `MIN_PAYOUT_HOURS` or
more out and `MIN_ANNUAL_PCT` a year or more. For paper and live apart it
shows the trades by outcome and by sport and kind, with the days their
money is held, the settled legs by venue, and what open trades hold on each
venue and when it comes back, then the paper balances, the real orders
sent by venue and what came back, and the live balances read from the
venues now, with Kalshi's shards. `--hours` sets the window, and
`--no-live` leaves out the live balances.

`src/tools/live_check.py` reads both venues' balances with the keys in
`data/` and says when the Kalshi key's location attestation lapses, and
with `--email` sends a test email, without trading. Run it before a live
run.

`src/tools/kalshi_shards.py` splits the live Kalshi cash evenly between the
exchange shards live trading uses, 0 and 3. It reads each shard's cash and
says what it would move, and with `--apply` moves it, then sets Kalshi's
own target split to the same shares, which Kalshi keeps every 10 seconds,
payouts included. The money stays in the account, and nothing is traded.

`src/tools/latency_report.py` reports, over a stretch such as a game, how
far behind the venues the books ran, minute by minute from the status
lines in `data/record.log`, how long the real orders took there and back
and how many filled, how long the edges lasted, and how many edges waited
for a Polymarket US book to catch up. `src/tools/feed_check.py` compares
ways of following the books side by side on the same markets, without
trading: Polymarket US's full book with and without compression, its
best prices only feed, and its trade feed, and Kalshi's order book on its
old host and on `external-api`, each with and without compression.

## Deployment

The live process runs on a c7a.large in us-east-1, the region Polymarket
US's exchange runs in, about 10 ms from Kalshi's API hosts in us-east-2. A
signed round trip is about 35 ms to Kalshi and 30 ms to Polymarket US. A
systemd service, `sportsarb-recorder`, starts `python3 -m engine.run
--sport nfl,ncaaf,mlb,nhl,nba --execute paper` from `~/SportsArb/src` on
boot and restarts it on any exit, and the process starts a child for each
venue's feed and each catalog refresh, which stop with it. That trades all
five sports on paper only, and going live means `--execute both` in the
service's command. The venue API keys live in `data/`, which is
gitignored, and are copied to the instance by `scp` only. Deploying is
`git pull` on the instance, the tests, and a service restart only if they
pass, which refreshes the catalog for about 40 seconds and then
resubscribes. Each run logs the commit it runs and every setting when it
starts, so the log says what produced its results. The instance was first
placed in Mexico to reach polymarket.com, which was then dropped as a venue
for legal reasons in favor of Polymarket US, and moved to us-east-1.

`commands.txt` holds the commands used to check the data, deploy, and
operate the instance, with the instance's address, key, and ids written
into them. It is gitignored, so it lives only on the machine that operates
the instance.

The process is light. The main process holds some 14,000 books, futures
included, in about 260 MB of memory, and each feed process its own
venue's books.
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
window; trades are now sized by the books and the cash alone. Two
execution flaws it exposed are fixed: orders now sweep the levels above
the edge floor instead of only the top level, and a level too small for a
whole contract no longer ends a ladder walk, which is what had turned
most of the one-sided positions into "no book to flatten".

**The first real orders.** On September 28, the first game traded with real
money, only 4 of the 72 orders sent to Polymarket US filled, while Kalshi's
filled 50 times. Polymarket US's books reached us about 85 ms after the
venue changed them at the median, 160 at the 90th percentile, and Kalshi's
in about 12, so after a score an edge often paired Kalshi's new price with
Polymarket US's old one, already gone. Since then the Polymarket US feed is
no longer batched, a Polymarket US leg waits for a current book, and Kalshi
is reached on the hosts it dedicates to API traders. A trade feed, tried
for a day, was dropped, since trades came no sooner than the book that
showed them.

**Lasting edges.** The race after a score could not be won from retail
access: of 89 live trades on September 29, 79 were on edges gone within
0.2 seconds of the signal, and one filled both legs. So from September 30
the bot looks for edges that last and pay enough for the time their money
is tied up, which faster traders tend to leave alone. Futures came back
into the catalog, every episode records how long its edge stayed at five
cents or more, and the executors stopped trading games in play. They take
only bets paying a day or more away that return enough a year, at first
100% and from October 1 30%, sell back a missed leg rather than buy the
other side, and size by the books and the cash alone. At 100% a year one
edge in the first 24 hours of live trading qualified, and it lasted an
instant.

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

`--sport ncaaf`, `--sport mlb`, `--sport nhl`, or `--sport nba` builds or
runs college football, baseball, hockey, or basketball, and `--sport
nfl,ncaaf,mlb,nhl,nba` runs them all from one pool of money. `--no-trade`
scans without trading, `--no-scan` only records, and `--seconds 120` runs a
short test. `--execute live` trades with real money and `--execute both`
trades the same signals on paper and for real. The settings a run is tuned
by, such as the minimum edge, the annual return, and the starting balance,
are in `src/engine/helper/config.py`. Those only paper trading reads start
with `PAPER_`, those only live trading reads with `LIVE_`, and the rest
hold for both. `--set NAME=VALUE` overrides one for a run, for example
`python3 -m engine.run --sport nfl --set min_edge=0.03`. The run logs every
setting when it starts. The streams need venue keys in `data/`:
`kalshi_key_id.txt` and `kalshi_private_key.pem` for Kalshi,
`polymarket_us_key_id.txt` and `polymarket_us_secret_key.txt` for
Polymarket US. Live trading uses the same keys, which need trading
permission, and emails its alerts through `data/email.json`:

```json
{"host": "smtp.gmail.com", "port": 587, "user": "me@gmail.com",
 "password": "an app password", "from": "me@gmail.com", "to": ["me@gmail.com"]}
```

Before a live run, check the balances, the Kalshi key's attestation, and
the email, and split the Kalshi cash evenly across its shards, from `src/`:

```bash
python3 -m tools.live_check --email
python3 -m tools.kalshi_shards --apply
```

Then read the reports:

```bash
python3 src/tools/summary.py --hours 24
python3 src/tools/latency_report.py --hours 4
```

To compare ways of following the books during a game, from `src/`:

```bash
python3 -m tools.feed_check --seconds 300 --markets 100
```

## Layout

```
src/
  api/        venue clients and the shared websocket book stream
  catalog/    fetch, classify (one parser per venue), match, notes, pipeline
  common/     paths, time and json helpers, the venue list, the logger, the timer background work runs on,
              how a child process starts, quantiles
  db/         models, the SQLite schema and its migrations, reads and writes
  engine/     run, the process that wires the components together
    components/
      market/    record, feeds, streams, scan: the venues' books and the edges between them
      trading/   executor (what paper and live share), paper, live, brakes, notify
      money/     balances (what paper and live share), paper, live, settle
    helper/      config (the settings a run is tuned by), game (when a game is played and when its bets pay out), pricing, fees
  tools/      summary report, live_check, kalshi_shards, latency_report, feed_check
tests/        mirrors src, run with pytest, configured in pyproject.toml
  support/    helpers the tests share, and the streams and refreshes a child process can run
commands.txt  operating the AWS instance, gitignored, kept locally
```

Where to look to change something:

| to change | look in |
|---|---|
| what a venue's API returns | `api/kalshi.py`, `api/polymarket_us.py` |
| how a websocket connects and reconnects | `api/bookstream.py` |
| which markets are understood | `catalog/classify/` |
| how bets are paired | `catalog/match.py` |
| where the venues' rules differ | `catalog/notes.py` |
| fees | `engine/helper/fees.py` |
| how an edge is priced | `engine/helper/pricing.py` |
| game timing | `engine/helper/game.py` |
| every tunable number | `engine/helper/config.py` |
| when a trade is taken and sized | `engine/components/trading/executor.py` |
| the Kalshi cash on each shard | `tools/kalshi_shards.py` |
| how an order fills | `trading/paper.py`, `trading/live.py` |
| when live trading halts | `engine/components/trading/brakes.py` |
| the email alerts | `engine/components/trading/notify.py` |
| how far behind the feeds run | `tools/latency_report.py`, `tools/feed_check.py` |
| how the processes are wired | `engine/run.py` |
| the report on the database | `tools/summary.py` |
