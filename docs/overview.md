# How the code fits together

A guided tour of the code, in the order data moves through it. The
README says what the project does and what it has found. This file says
where each piece of that lives, so you can find your way around the code.

## The two programs

**The catalog** (`src/catalog`) works out what can be traded. It fetches
every open NFL market from both venues, restates each one as a `Bet` in
venue neutral terms, and pairs up the bets both venues list. The pairs go
into the database. You can run it on its own (`python3 -m
catalog.pipeline`), and the live process reruns it every hour.

**The live process** (`src/engine/run.py`) trades the pairs. It follows
every paired contract's order book, prices each pair as its books change,
trades when a pair shows enough edge, and settles the trades when the
games end.

Both share the venue clients in `src/api`, the database layer in
`src/db`, and small helpers in `src/common`.

## The words the code uses

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

## The processes

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

## The life of a book update

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

## The life of a trade

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

## Money

- **Balances** (`balance/`). Each desk has its own money, with the same
  shape for both modes: free cash per venue, `reserve` and `release` for
  orders in flight, and `apply` for a cash movement.
  - Paper (`paper.py`): a ledger in the database, starting at 10,000
    dollars a venue.
  - Live (`live.py`): the venues' own balance calls, read every 30
    seconds, plus what our orders moved since the last reading.
- **Sizing** (`allocate.py`). The games in play share each venue's money
  equally, and a game's share, at 20 dollars a contract, becomes the cap
  on how many contracts one of its trades may hold.
- **Floors**. New trades leave 500 paper dollars or 5 live dollars on each
  venue, so flattening always has something to use.
- **Keeping the venues funded** (`balance/rebalance.py`). The venue
  holding a winning leg gets the whole dollar, so the two drift apart.
  Paper money is moved on Tuesdays when the venues have drifted more than
  25% apart. For live money a human gets an email saying how much to move
  where.

## Live safety

- **One order of unknown outcome** (no answer, a venue error, an answer
  that cannot be read) sets its own trade aside. No more orders go out for
  that trade, and a human gets an email saying which order to look up
  (`LiveExecutor.set_trade_aside`).
- **Brakes** (`execute/brakes.py`). Too many unknown outcomes, or a venue
  refusing three orders in a row, halts all live orders. Losing too much,
  or too many losing trades, over six hours halts new trades, but
  flattening goes on. A halt is written to `data/live_halt.txt` and
  lasts across restarts until a human removes that file.
- **Emails** (`notify.py`). Alerts are stored in the alerts table and
  sent through the SMTP settings in `data/email.json`.

## What happens when

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

## Where things are stored

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

`db/schema.sql` has the tables. `db/migrations.py` brings older databases
up to date, one numbered step at a time.

## Where to look

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

The tests mirror `src/` under `tests/`. Helpers they share, and a stream
that a feed process can run without a venue, are in `tests/support/`.
Run them with `python3 -m pytest` from the repository root.
