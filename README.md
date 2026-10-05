# SportsArb

A bot that looks for cross-venue arbitrage between the two US prediction
markets, Kalshi and Polymarket US, and trades what it finds, on paper, with
real money, or both at once. It covers the NFL, college football, MLB, NHL,
NBA, WNBA, and men's college basketball, the Premier League, La Liga, Serie
A, the Bundesliga, Ligue 1, Liga MX, MLS, the Champions League and Europa
League, Formula 1, NASCAR, UFC, tennis, darts, the 2026 US elections, and
Bitcoin. It follows two kinds of bet: futures, titles, awards, a season's
leaders, season totals, election races, and Bitcoin's price by a date,
which live trades, and the bets on one event, games, matches, fights,
races, and Bitcoin's 15 minute windows, which live trades once they are
under way and paper before and while they are played. When the cheapest way to hold *yes* on one venue and
the cheapest way to hold *no* on the other add up to less than a dollar
after fees, buying both locks in the difference whatever happens.

The whole thing runs on an EC2 instance in us-east-1, as one process with
each venue's feed in a child process of its own: it follows every order
book change for thousands of contracts, prices every cross-venue pair on
every change, sends orders when a pair shows an edge worth the time its
money is tied up, and settles the trades when the contracts resolve. Live
trades futures, where an edge lasts long enough for our orders to reach
it, and paper the bets on one event paying within a day, where faster
traders may take the edges first, to see how they would do, its orders
timed as live ones are. By default the orders are paper.
With `--execute live` or `--execute both` it sends real ones, and with
`--live-in-play` live trades the games under way too, Polymarket US's
order first. From October 4 to 5 the service on the instance traded every
sport's futures and the elections live, with paper on every event beside
it, and the in-play test below. From October 5 it is set to trade live
alone: every sport's futures, Bitcoin's and the elections' included, and
every game under way (`--execute live --live-in-play --not-live none`).

## How it works

Everything lives in `src/`, in two programs.

**The catalog** (`src/catalog`) works out what can be traded. It fetches
each sport's open futures, and the elections', from both venues, restates
each one as a `Bet` in venue neutral terms, and pairs up the bets both
venues list. The pairs go into the database. You can
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
   While a paper order is in flight on the contract, the book also goes on
   the contract's `Tape` (`market/tape.py`), every one in turn, so the
   order can find the book the venue had when it would have arrived.
5. **Price** (`Scanner.on_book` and `update` in `market/scan.py`, with
   `pricing.best_trade`). For every pair the contract belongs to, the
   scanner finds the cheapest way to hold yes and the cheapest way to
   hold no across the pair's members, on two different venues. Then it
   works out the edge of buying both.
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
   takes the signal when the edge is at least `MIN_EDGE`, for live the pair
   is a future, or with `--live-in-play` a game under way (`plays`), the
   bet pays when the desk trades it (`pays_in_time`), a future
   `MIN_PAYOUT_HOURS` or more away and a game within `MAX_PAYOUT_HOURS`,
   and the edge returns `MIN_ANNUAL_PCT` a year or more until then
   (`pays_enough`), for live the edge has stayed at `MIN_EDGE` or more for
   `LIVE_MIN_EDGE_SECONDS`, half a second, unbroken, as the scanner's
   episode times it (`hold`, `Scanner.edge_since`), and a Polymarket US
   leg's book is current (`confirm_wait`): newer than the Kalshi leg's last
   change, by the venues' own clocks, or else that change is
   `CONFIRM_SECONDS` old. Otherwise the edge waits, and the scanner offers
   it again at the next change, or the moment the wait ends (`recheck`),
   not at the next tick up to a second later. `quantity_for` walks both
   ladders together through the levels that keep that edge and sets each
   leg's limit at the deepest one. It then asks for `FILL_SHARE` of what
   those levels show, but no more than the cash each venue can spend, on
   Kalshi the cash on the market's shard, live as on paper. The cash is
   reserved and the trade is stored before any order goes out.
2. **Fill** (`run_trade`). Both legs go out at once, but on a game under
   way Polymarket US's goes first and Kalshi's only once that has
   answered, for what it filled, and not at all when it filled nothing
   (`fill_legs`).
   - Paper (`trading/paper.py`): each order takes a trip there and back
     drawn from what the live orders took, and fills against the book the
     venue had when it would have arrived, by the venue's own clock, from
     the contract's tape.
   - Live (`trading/live.py`): each order is a real immediate or cancel
     limit order through the venue client's `place_order`. It is stored in
     the orders table before it is sent and again with the answer.
3. **Flatten** (`flatten`). If the legs filled unevenly, `sell_excess`
   sells the excess back on its own venue. Buying the missing side on the
   other venue might cost less, but would tie the money up until the bet
   pays, where a sale frees it at once.
4. **Keep trying** (`retry`, every tick). Whatever stays exposed is tried
   again against newer books until it is flat, its payout time passes, or
   it settles, except that a sale that filled nothing waits a minute
   (`SALE_RETRY_SECONDS`), and one the venue turned away for lack of cash
   waits until the cash there has grown. A restart reads exposed trades
   back from the database (`reload_exposed`).
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
- every 5 minutes the live desk reads each venue's positions and logs any
  contract the venue holds more or less of than the live trades say
  (`LIVE_POSITION_SECONDS`);
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
| twins | the in-play test of 2026-10-04 to 10-05, each of its 200 live trades in play, its paper twin, and the order its orders went in; nothing writes to it now |

Trades and settlements carry a `mode`, `paper` or `live`, so the two modes
never mix. Every table has a model in `db/models.py` and its schema in
`db/schema.sql`. Changes to a table for databases that already exist are
numbered steps in `db/migrations.py`, and `db/database.py` holds the reads
and writes.

## Part by part

### Catalog (`src/catalog`)

**fetch.py** pulls a sport's open markets from both venues into the
`contracts` table, its futures and its games, matches, races, or windows.
Our sport keys are `nfl`, `ncaaf`, `mlb`, `nhl`, `nba`, `wnba`, `ncaab`,
`epl`, `laliga`, `seriea`, `bundesliga`, `ligue1`, `ligamx`, `mls`, `ucl`,
`uel`, `f1`, `nascar`, `ufc`, `tennis`, `darts`, `politics`, and `crypto`,
which is Bitcoin, the one coin Polymarket US lists. Of the thousands of
series Kalshi lists it takes only those classified, by ticker, each sport's
futures listed in `SPORTS` and its event series added from the
classifier's tables by their prefix, and for elections, which have a series
per state or district, by the shapes the classifier reads, `SENATEGA` or
`HOUSEAZ1`.
Kalshi's list of series, every category of it, is read once and kept ten
minutes, so a refresh of every sport reads it once. Kalshi is read through
its public REST catalog, paged under the rate limit, and each series brings
the exchange shard its markets trade on: shard 0 for football, hockey,
soccer, motorsport, UFC, darts, and elections, shard 3 for baseball,
basketball, and tennis. Polymarket US is read through its gateway, one
call per tag, deduplicated across tags, baseball through the `mlb` tag,
since `baseball` brings Korean and Japanese league games too, and Bitcoin
through `crypto` and `up-or-down`. A game's contract starts at its event's
kickoff: an event is a game when it has a game id, the venue's own or
Sportradar's, and a futures market on one is a race's when the event runs
two days or less, and otherwise an award's, which has no start. A Bitcoin
window starts at its `windowStart`. Polymarket US marks a window closed
until it opens, so the catalog of Bitcoin is refreshed just after each one
opens, see the engine.

**classify/** turns each contract into a `Bet`, a venue neutral statement
of what the contract is about: kind, season, the game's date and its two
sides for a bet on one event, subject, and a line. A future leaves the
date and sides empty, which is how the desks tell the two apart. Each venue
has its own parser, since the two describe the same thing very differently.
The bets on one event:

- Games of teams, in the five original sports, the WNBA, and men's college
  basketball: the winner, stated as the away team winning with Kalshi's
  home team market its complement, the spread, the total, a team's total,
  and player props, the player and an *at least N* line. Kalshi's event
  ticker holds the date and both teams, `KXNFLGAME-26SEP20CARATL`, and
  Polymarket US's event slug, `nfl-car-atl-2026-09-20`, whose markets'
  types say the kind. A spread on Polymarket US is the first team covering
  its signed line, so a positive line is the second team not winning by
  more than it, a market's *no* side. A doubleheader's games are left out,
  since a game is its date and teams.
- Soccer matches, in every league: the result, a club or the tie, the
  spread, the total, both teams to score, the exact score, each for the
  match and its halves, and the corners, all over 90 minutes and stoppage
  time on both venues.
- Matches between two people, in tennis, darts, and the UFC: the winner,
  the games and sets spreads and totals, a set's winner, the score in sets,
  whether a fight goes the distance, and the round it is won in. The two
  are named by their full names, the key's words in order, since Kalshi
  writes *Wang Cong* where Polymarket US writes *Cong Wang*; Kalshi's title
  for a winner gives only last names, so its two markets' subtitles name
  them.
- Races: F1's and NASCAR's winner and F1's top constructor, a race being
  its date, from Kalshi's rules and Polymarket US's slug.
- Bitcoin's 15 minute windows, whether the CF Benchmarks index ends the
  window at least where it began, the same rule on both venues, a window
  named by its start in UTC.

For a future, Kalshi gives each kind a series of its own, `KXSB` the Super
Bowl, or an event of its own within one, `KXEPLTOP-27TOP4` the Premier
League's top four, and the market ticker ends in the team, `KXSB-27-KC`.
Polymarket US tells kinds apart by the shape of the event slug with its
sport's prefix taken off and its date written as D, `nfl-afceast-D-w` being
a division winner, looked up with its prefix first where two sports share a
shape that means different things, `ucl-D-lastplace`. The kinds:

- Team futures: champions, conference and division winners, top seeds,
  playoff places and rounds, best and worst records, the last undefeated
  and winless teams, college football's unbeaten teams and the conference
  that wins its title, and in soccer the top two, four, and six,
  relegation, last place, and the Champions League's league phase.
- People: awards, a season's leaders in a stat, the NFL's, college
  football's by conference, the baseball postseason's, and soccer's goals
  and assists, and champions and title holders, F1 and NASCAR drivers, UFC
  title holders on December 31, the year-end No. 1 in men's and women's
  tennis, and darts' world champion. A person's market names them in its
  subtitle on Kalshi and its title on Polymarket US.
- Season totals: a team's wins or points at a line, and a player's
  passing, receiving, or rushing yards or touchdowns at a line, the line in
  the market on Kalshi and in the title or the event's shape on Polymarket
  US, `nfl-D-1000recyds` being 1,000 receiving yards or more.
- Elections: control of the House and the Senate, and each Senate,
  governor, and House race, by party, D or R, or by candidate. A race's
  season is its election year. In California and Washington, whose top two
  can be of one party, and Alaska, whose top four can, the venues read a
  party's win differently, Kalshi paying on any member of it taking the
  seat and Polymarket US on its nominee winning, so there only candidates
  are paired.
- Bitcoin: its price going above, or below, a strike by a deadline, the
  deadline being the last day it counts, *before Sep 1, 2026 at 12:00 AM
  ET* being August 31, and the $5,000 band it ends 2026 in.

Team aliases are resolved through `teams.py` and `aliases/`, which has a
file for each sport, since leagues reuse codes (DAL is the Cowboys and the
Mavericks), and an empty one for a sport whose futures name only people.
The new leagues' files list each venue's codes apart, and the Champions
League and Europa League are two sports, since Kalshi's VIK is Viking in
one and Plzen in the other. Player names are normalized to a key that
ignores accents, punctuation, and suffixes, reads a hyphen as a space, and
turns letters like ø into o, so Kalshi's *Ronald Acuña Jr.* is Polymarket
US's *Ronald Acuna* and *Martin Ødegaard* is *Martin Odegaard*, and the few
names the venues spell apart are in `PLAYER_ALIASES`. A market on no one,
Kalshi's *Vacant* title, is never paired. Lines are made strict, so *1000+*
on one venue and *over 999.5* on the other become the same bet. The venues
number seasons differently, so a future's season is the one it settles in,
or on Polymarket US its event's end when the slug's date is half a year
earlier, since the Champions League final's slug carries the wrong year.
Polymarket US's futures do not always use its games' team codes, gluing
city and nickname, `bufbil`, or borrowing Kalshi's, `gsw`, so a future's
team is the code whose team its market title could name. Contracts no
parser understands are counted and left out.

**match.py** groups a sport's bets whose identity agrees into a `Pair`,
whose label starts with the sport, for example `nfl champion 2027 KC`,
`nfl spread 2026-09-20 CAR@ATL ATL 3.5`, or
`politics senate_race 2026 GA D`. A pair only exists when both venues list
the bet, and it carries every contract that expresses it. In tennis, darts,
and the UFC the venues may date one match a day apart, Kalshi and
Polymarket US reading a match in Asia by different clocks. A date only one
venue lists for two people then joins the one a day off that only the other
lists. Two people can also meet on days in a row, as in a darts round
robin, so a date both venues list, and two dates one lists, stay matches of
their own. Kinds with settlement rules that differ between venues carry a
note from **notes.py**: how each venue treats a postponed game, overtime, a
retired tennis player, a fight's draw, a darts walkover, a driver who does
not finish, and Bitcoin's index, Polymarket US reading a trimmed mean of
its last minute where Kalshi reads the index itself; Polymarket US divides
the dollar among players or teams that tie for an award, a lead, or a
record, where Kalshi's rules do not always say; Kalshi reads a UFC title
holder and a tennis ranking at noon Eastern on December 31 and Polymarket
US at 11:59 PM; and Kalshi pays a race on the party of whoever is sworn in,
in January, and control of a house on February 1, Polymarket US on the
election. A note is a warning to read, not a bar.

**pipeline.py** runs fetch, classify, and match in one call. The live
process runs it every hour in a child process, so new games and futures
enter the pairs while it runs, and Bitcoin's alone just after each 15
minute window opens. A refresh of every sport takes some eight minutes,
the NFL's games and the elections the most of it. A dry run on October 4
paired some 5,000 game and event bets, 3,400 of them the NFL's, and 4,700
futures; 227 of the game pairs, every kind among them, were read against
both venues' own wording, and each named the same game, side, and line.

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
order id does not mean the order is done. Both venues trade in hundredths
of a contract: Polymarket US fills an order in pieces as small as one, 0.1
then 0.89 then 0.01 for a contract, and takes orders that small, and Kalshi
counts contracts to the hundredth too. So the pieces are added up and every
fill, holding, and order is counted to the hundredth (`orders.exact`). Both
clients also read the account's positions, which the live executor and
`tools/repair_fills.py` check the trades against. Kalshi turns away an
order whose market's shard lacks the cash, which counts as unfilled rather
than refused, since nothing traded. Polymarket US turns away an order the
account lacks the cash for, and Kalshi a sale it lacks the cash for, which
counts as `unfunded`, not refused either. A sale can need cash: each venue
keeps one position per market across all our trades, so selling the No one
trade holds, in a market where others hold more Yes, is buying Yes. So a
Kalshi sale is not reduce only, which would cancel such a sale unfilled
every time. Trading calls go over kept HTTPS connections from **http.py**,
since a new TLS connection costs round trips a race cannot spare, and are
never retried, since an order sent twice trades twice. The field names come
from the venues' published Python SDKs.

### Engine (`src/engine`)

**run.py** is the process that runs. Its `Session` wires the recorder, the
venue connections, the scanner, and a `Desk` for each mode it trades in
together, and a one-second timer ticks it, as [What happens
when](#what-happens-when) describes. A desk is one mode's executor, its
money, and its settler. `--execute` picks the desks: `paper`, the default,
`live`, or `both`, which runs a desk of each. The desks trade apart: the
paper desk the bets on one event, a pair with a game date, and the live
desk the futures, a pair without one, bar the sports given to `--not-live`,
`crypto` by default, whose futures are followed but traded by neither. On
one signal the live desk's real orders would take the contracts the paper
desk's simulated ones look for. Each desk still flattens and settles every
trade it holds. With `--live-in-play`, which needs `--execute live` or
`both`, the live desk is offered the bets on one event too, and trades them
once under way, see **trading/** below; with paper running too, both take
those signals, and paper is given back what live's orders took. The same loop starts the hourly catalog refresh in a
child process and applies the result to the live connections, and with
Bitcoin followed it refreshes Bitcoin's catalog alone 20 seconds after each
15 minute window opens, when both venues list it, so a window is traded for
most of its 15 minutes. One run trades every sport given to `--sport`,
comma separated as in `--sport nfl,ncaaf,mlb,nhl,nba`, or every one with
`--sport all`, since the money is one pool and a second process would spend
the same dollars. How long a game is expected to last is set for every
sport in `GAME_HOURS`, measured for the first five and an allowance for the
rest, a Bitcoin window's being its 15 minutes. The pieces it wires together
are in `engine/components/`, in three folders by what they do: `market/`
follows the venues, `trading/` makes the trades, and `money/` keeps the
cash. What they share is in `engine/helper/`: the settings, game timing,
pricing, and fees.

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
and the cheapest way to hold no across the pair's members, on two
different venues, including the venue's taker fee from **fees.py**. Two
contracts on one venue, such as a game's two teams on Kalshi, are priced
by the same traders, and a gap between them is gone before both orders
land: in the in-play test 52 trades with both legs on Kalshi matched 5 of
the 247 contracts they asked for, live and paper alike. An episode is a stretch
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
share, which is everything but how an order is filled. Live trades the
futures, which pay out a day or more away, and with `--live-in-play` the
games, matches, races, and windows once under way that pay within 24 hours
(`MAX_PAYOUT_HOURS`), below. Paper trades
the bets on one event (`in_play`), before and while they are played, that
pay within 24 hours (`MAX_PAYOUT_HOURS`), to see how they would do. A
signal needs a net edge of at least two cents per contract (`MIN_EDGE`,
five until 2026-10-04 16:03 UTC), for live a payout at least 24 hours away
(`MIN_PAYOUT_HOURS`), and a return of at least 50% a year on the money it
ties up until then (`MIN_ANNUAL_PCT`), which is what weighs an edge against
the time it ties the money up; the two cents only keep out the noise of a
cent or so. Two cents clears 50% a year for a bet paying within 14 days,
five cents within 38, ten within 81, twenty within 182. One limit order is
sent per leg, both at once, but in play Polymarket US's first, below. Both ladders are walked together and each leg's
limit is set at the deepest level that still leaves the minimum edge, so an
order sweeps every level above the floor rather than only the top one. A
trade asks for all of what those levels show (`FILL_SHARE`, half until
2026-10-04), as far as the cash free on each venue pays for, both legs from
one venue's cash when they share it, live as on paper, with no cap on
contracts. No cash is held back: trades may spend all that is free.

In **paper.py** each order goes the way a live one does. It takes a trip
to its venue and a trip back, each drawn from a lognormal with the median
and 90th percentile the live orders took (`PAPER_ORDER_MS`). The trip there
ends at the venue's own time on the order, the clock it stamps its books
with: Kalshi 12 ms, Polymarket US 59 for an opening order and 24 for a
sale, and back 8 and 31. The order fills against the book the venue had at
that moment, the newest it made by then on the contract's tape. Our copy of
a book runs behind the venue's, Kalshi's some 12 ms and Polymarket US's
some 85, so the order waits until a book the venue made later has reached
us, which shows every change up to then has, or `PAPER_FEED_SECONDS` when
none comes. The answer comes back after the trip back, and a leg that
filled short is sold back on the books we had seen by then, as live would,
the sale meeting the venue's book when it would arrive. `FILL_SHARE` of
the visible size at a level is asked for, all of it, and no order is
rejected at random (`PAPER_REJECT_PROBABILITY`), since none of the live
ones was: of 1,183, three lacked the cash and three came in Kalshi's
maintenance, which paper turns away as live does. A paper order leaves the
venue's book as it was, so paper remembers what it took from each level
and takes it off the books it later sizes, fills, and sells into, until
the level shrinks below that or goes. In **live.py** each order is a real
immediate or cancel limit order, sent on a thread pool of its own, and
stored in the orders table before it is sent and again with the venue's
answer. Either way a leg that filled short is flattened at once by selling
the excess back on its own venue, even when buying the missing side on the
other venue would cost less, since a sale frees the money now and a
purchase would hold it until the bet pays. A sale goes no lower than half
of what the contracts cost (`MIN_SALE_SHARE`): below that it would give
away most of what was paid, so the contracts are kept, a bet that may
still pay out, until a bid worth taking comes. Whatever stays exposed is
tried again on every tick until it is flat, the bet pays out, or the
settler settles it. A sale that filled nothing is tried again only a
minute later (`SALE_RETRY_SECONDS`), since a book can show a bid an
order never reaches. A sale its venue turned away for lack of cash is
tried again only once the cash there has grown, by a payout, a deposit, or
another sale, rather than sent again every second. A restart takes back
from the trades table whatever is still exposed, so a crash or a deploy
does not leave it unhedged. A live sale is
limited to the deepest price the books said it would reach, so a book that
moved leaves the rest for the next tick rather than filling far from its
price. Once a game has started, orders and flattening only use a book that
has changed within the last minute (`pricing.fresh`), as the scanner does,
since a market that has closed may stop changing rather than empty its
book, and its last book cannot be traded. Every trade is stored as soon as
it is sent and updated when it is done.

A trade opens in whole contracts, at least one, so no order opens a
fraction of a contract, but a leg may fill to the hundredth,
6.42 of 7 for one, and live trading counts it so: the other leg's 0.58
over is sold back like any excess, in an order for 0.58 of a contract. A
Kalshi order sent after Polymarket US's, in play, below, asks for what that
filled, to the hundredth.
Paper trading keeps to whole contracts, as its fills are worked out from
the books (`Executor.step`). Every `LIVE_POSITION_SECONDS`, 5 minutes, the
live executor reads each venue's positions and compares them with what
the open live trades hold of each contract (`check_positions`). A
difference of a hundredth or more means the records are wrong, so it is
logged once and left to a human, see `tools/repair_fills.py`. Nothing is
compared while a trade or a flatten is in flight, or when an order went
out while the positions were read.

A leg on Polymarket US trades only on a current book. That venue's books
reached us about 85 ms after it changed them at the median, 160 at the 90th
percentile, and Kalshi's in about 12, so after a score Kalshi's new price
could sit next to Polymarket US's old one for a moment, an edge already
gone there. Before a trade the executor compares the venues' own times for
the two books (`confirm_wait`). A Polymarket US book newer than the Kalshi
book's last change already shows any reaction to it. An older one is
trusted only once that change is `CONFIRM_SECONDS`, 0.3 seconds, old, long
enough for a reaction on Polymarket US to have reached us, and until then
the edge waits for the scanner to offer it again at the next change, or
when the wait ends, when the executor asks the scanner to price the pair
again. A Kalshi leg has no such wait, since its feed is fast. Both venues
stop every Thursday for maintenance they publish, Kalshi from 3 to 5 AM
Eastern and Polymarket US from 6 to 8 AM, while a feed may go on sending
books, so no trade is opened, and no order sent to flatten one, with a leg
on a venue in its window (`venues.is_maintenance`). A pause at any other
time is met by the brakes: the venue refuses the orders, and three refusals
in a row halt live trading. Live trading takes every sport the run does. A
Kalshi leg spends only the cash on its market's shard, football's and
hockey's on shard 0, baseball's and basketball's on 3, so
`tools/kalshi_shards.py` splits the Kalshi cash between the two, 90% to
shard 0, where the football futures with the long-lasting edges are, and
10% to shard 3 (`LIVE_SHARDS`). The live executor emails once when either
shard, or Polymarket US, falls under $5 (`LIVE_LOW_CASH`), and again only
after it has been back over.

**Edges that last.** Since 2026-10-05 live trades an edge, on a future or
a game, only once it has stayed at `MIN_EDGE` or more for half a second
(`LIVE_MIN_EDGE_SECONDS`), unbroken, timed from when the scanner's
episode last reached it (`Scanner.edge_since`, the stretch the
opportunities table keeps as `min_edge_seconds`). An edge that ends
sooner is never traded, and one still there is priced again the moment
the half second is up (`recheck`) and traded on the books then, sized and
checked as any other. The executor's summary line counts the pairs held
this way. Paper takes an edge when it first sees it, so with both running
they no longer trade the same signals.

**Games in play.** With `--live-in-play` live also trades the games,
matches, races, and windows under way, of every sport, Bitcoin's windows
included: once the game has started by any member's kickoff, since a
Kalshi contract gives none, paying within 24 hours (`MAX_PAYOUT_HOURS`),
sized as any trade, by the books and the cash. Its Polymarket US order
goes first and its Kalshi order only once that has answered, for what it
filled, and not at all when it filled nothing (`Executor.fill_legs`);
paper does the same in play. Sent at once the Kalshi leg lands first, in
some 12 ms against Polymarket US's 59, and another trader may take the
Polymarket US quote away once ours trades, leaving the Kalshi leg to sell
back at a loss. Sent first, a Polymarket US leg that misses leaves nothing
to sell back, at the cost of Kalshi's going out some 80 ms later. Futures,
and games before they start, still send both at once. With paper running
too, both take the same signals, and since our live orders are real and
take from the books paper's orders meet, each leaves a footprint
(**footprints.py**): from the venue's time on its answer and the book on
the tape just before then, what it took from each level. Paper adds that
back to every book the venue made after, until the level falls below what
our order left of it, as other takers or cancels would have taken ours
too, and waits up to 2 seconds for the answers of live orders sent before
its own. The brakes cover these trades as any live trade.

This follows the in-play test of 2026-10-04 17:49 to 10-05 01:27 UTC. Live
traded 200 games under way at no more than 5 contracts, each beside a
paper twin on the same signal with the same legs, limits, and size, the
trades with a leg on each venue sending both orders at once and Polymarket
US's first by turns. On those 147 trades live matched 111 of the 500
contracts asked for, paper 278: live's Polymarket US legs filled 56 times
and paper's 106, while their Kalshi legs filled alike, so paper is
optimistic in play, most of all on the NFL. Live's 69 sent Polymarket US
first made $3.84, matching about as many contracts as the 78 sent at once,
which lost $3.84 with 52 of them left on one leg to sell back, against 12.
The 52 trades with both legs on Kalshi lost $7.21, which is why the legs
are now always on two venues. The `twins` table holds the test, and
`tools/in_play_test.py` reports it.

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
  being slow, and one either venue turned away for lack of cash is
  unfunded, not refused: the venue works, and the low cash email tells a
  human.

It halts new trades, but goes on flattening what is exposed, when the
live trades decided in the last 6 hours lost more than 10% of the live
money, net. If the orders then fail as above, flattening stops as well.
That is checked after each trade, each settlement, and each retry that
sold something, not after a retry that sold nothing, which runs every
second while a trade is exposed.

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

`src/tools/summary.py` prints a short report from the database: its size
and the pairs of each sport in one line, and feed drops. It shows the
futures and the games in play apart, or one of them with `--market
futures` or `--market in-play`. For each it shows the opportunities within
the trading rules, an edge of `MIN_EDGE` or more and `MIN_ANNUAL_PCT` a
year or more, a whole contract or more fillable at that edge through its
longest stretch, on a future paying `MIN_PAYOUT_HOURS` or more out, or on
a game, match, race, or window under way paying within `MAX_PAYOUT_HOURS`: what they could
have taken and locked in at full size, how long the edge stayed at
`MIN_EDGE` or more, in seconds to the thousandth, at the median, the 90th
percentile, and the longest, the same by sport and kind, and the largest
five. An edge on less than a contract, a sliver of a Polymarket US level
that can last minutes, is left out, since a trade opens a whole contract
or more and so could never take it. Episodes before 2026-10-04 16:03 UTC
kept that stretch at five cents. It then shows, for paper and then live,
or one of them with `--mode paper` or `--mode live`, and in each for the
futures and then the games in play, the trades by outcome, filled,
partial, then failed, and by sport and kind in the window, the five
holding the most capital, the legs settled in it by venue, and
the open trades: in a few lines, how many, how many of them opened in the
last hour, day, and week and the capital those still hold, the capital
they all hold on each venue and the profit they are expected to return
with its rate a year weighted by capital, and the first, average, and last
dates they resolve, the first maybe past and waiting on a venue, the
average weighted by capital; then a table of each open trade opened in the
window, newest first, with what it holds, its capital, expected profit and
rates, and when it pays. Each live market ends with its real orders sent
by venue and what came back, the paper section with the paper money from
the ledger, and the live one with the live balances read from the venues now, with Kalshi's
shards. Each section is a title line with its details on indented lines
under it, and a long list, such as the pairs of each sport, wraps at 100
characters. `--hours` sets the window, `--sport nfl,ncaaf` narrows
everything to some sports, and `--no-live` leaves out the live balances.

`src/tools/live_check.py` reads both venues' balances with the keys in
`data/` and says when the Kalshi key's location attestation lapses, and
with `--email` sends a test email, without trading. Run it before a live
run.

`src/tools/kalshi_shards.py` splits the live Kalshi cash between the
exchange shards live trading uses by the percents `LIVE_SHARDS` gives them,
90% to shard 0 and 10% to shard 3. It reads each shard's cash and says what
it would move, and with `--apply` moves it, then sets Kalshi's own target
split to the same shares, which Kalshi keeps every 10 seconds, payouts
included. It does not refill a shard whose cash orders spend: at 50/50 on
2026-10-01, shard 0 had spent down to $6.94 of $51.77. So run it again when
one shard runs low. The money stays in the account, and nothing is traded.

`src/tools/repair_fills.py` repairs the live trades recorded under an
older reading of Polymarket US fills: before fills were added up, when an
order filled in pieces was stored as filled short or not at all, and
before they were counted to the hundredth, when 6.42 was stored as 6. It
reads every stored Polymarket US answer again as the client now does,
corrects the orders read differently, and works out each of their trades
again from its orders, as the executor does, and the live executor
sells back at its next start what a repaired trade holds on one side more
than the other. A bet whose orders sold more than it held is left for a
human. Last it compares what the trades hold with each venue's positions.
`--trade` names a trade whose record fell behind its orders for another
reason, to be worked out again from them too, as trade 48 was when a
sale of 0.42 of a contract went unrecorded. Without `--apply` it writes
nothing, so it also serves as a check that the live records match the
venues. Stop the recorder before `--apply`.

`src/tools/in_play_test.py` reports the in-play test of 2026-10-04 to
10-05: the live trades in play beside their paper twins on the same signals, for those sending both
orders at once and those sending Polymarket US's first apart, how many of
each filled in full, in part, on one leg, or not at all, the contracts
matched, how often each venue's leg filled of those sent, what was locked
in and what the sales back made, the result of those settled, each venue's
round trip, measured live and drawn on paper, how often the two matched
the same contracts, and the newest pairs one by one, `--recent 20` of them.

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
--sport all --execute live` from
`~/SportsArb/src` on boot and restarts it on any exit, and the process
starts a child for each venue's feed and each catalog refresh, which stop
with it. The venue API keys live in `data/`, which is
gitignored, and are copied to the instance by `scp` only. Deploying is
`git pull` on the instance, the tests, and a service restart only if they
pass, which refreshes the catalog for some eight minutes and then
resubscribes. Each run logs the commit it runs and every setting when it
starts, so the log says what produced its results. The instance was first
placed in Mexico to reach polymarket.com, which was then dropped as a venue
for legal reasons in favor of Polymarket US, and moved to us-east-1.

`commands.txt` holds the commands used to check the data, deploy, and
operate the instance, with the instance's address, key, and ids written
into them. It is gitignored, so it lives only on the machine that operates
the instance.

The process is light. The main process held some 14,000 books in about
260 MB of memory before the games came back, which double what it follows
to some 22,000, and each feed process its own venue's books.
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
100% and from that evening 30%, sell back a missed leg rather than buy the
other side, and size by the books and the cash alone. At 100% a year one
edge in the first 24 hours of live trading qualified, and it lasted an
instant.

**The first lasting trades.** At 30% a year live trading took its first
trades within seconds, on college football season win totals that had
held an edge for hours, several of them two Polymarket US markets on the
same line. In under three hours it had put about $100, most of its
Polymarket US cash, into 16 open trades expected to make about $9, 8% on
the capital or 46% a year, paying out from November 28. Those first hours
also found a bug. Polymarket US fills an order in pieces as small as a
hundredth of a contract, and each piece was cut to whole contracts on its
own, so 18 of the first 32 orders there, every one filled in full, were
stored as filled short or not at all, and trades were flattened against
legs they did not hold, one of them three times over. Fills are now added
up first, the stored answers were read again to repair the trades, and the
11 contracts they held on one side more than the other were sold back.
Since both venues trade in hundredths of a contract, fills are now counted
to the hundredth, so an order for 7 that fills 6.42 is recorded as 6.42
and its other leg's 0.58 over is sold back like any excess, and the live
executor checks the venues' positions against the trades every 5 minutes.
The first such sale, 0.42 of a contract on October 1, could not go through:
Polymarket US nets positions per market, so selling that No was buying
Yes, and the $0.16 free there could not pay for it. Three such refusals in
a row halted live trading, so an order turned away for lack of cash now
counts as unfunded rather than refused, and waits for the cash to grow.
Later that night live trading halted again, at 3 AM Eastern, when Kalshi
closed for its weekly maintenance while its feed went on: three trades
bought the Polymarket US leg, Kalshi refused the other, and two of those
legs were sold back at a cent each. Neither venue is traded in its
published maintenance window now, and a sale to flatten no longer gives
away a contract for under half of what it cost. On October 2 the return a
trade must make went up from 30% a year to 50%. That day showed that
Kalshi, too, keeps one position per market. Two trades had bought 5 Pitt
Yes there where another held 51 No, so the account held 46 No, and when
their Polymarket US legs missed, each sale of that Yes, sent reduce only,
was cancelled unfilled: 113,000 times in 16 hours, as the book kept
showing a bid. A Kalshi sale is no longer reduce only, and a sale that
fills nothing waits a minute before the next.

On October 4 a trade went from asking for half of what the books showed to
all of it. Of the 231 live trades to then that had a paper twin, 222 were
sized by the share rather than the cash, at a median of 2 contracts, while
every Kalshi order filled in full, Polymarket US orders of 10 or more
filled in full 45 times in 46, and Kalshi's public trades showed another
taker buying the same side at our price within 10 seconds of us on 4 of
140 orders. Selling back legs that missed had cost $5.59 against $144.98
locked in. The same day the catalog dropped games for futures only, and it
grew from five sports to 21 and the elections, the new ones traded on paper
only at first:
some 4,400 sports pairs and 887 election pairs, about 5,300 contracts a
venue to follow. Election races pay out on Kalshi when the winner is sworn
in, in January, and control of a house on February 1, so at 50% a year an
election edge must be some 11 to 14 cents. Two hundred of the new pairs,
every kind among them, were read against both venues' own wording, and
each named the same team, person, race, or line on both.

Asking for all of what the books showed made the two desks race for it.
Both acted on the same signal, and the live desk's real order took the
thinner leg's whole level, nearly always on Polymarket US, before the paper
desk's simulated order read the book, which in the first half hour was
45 to 383 ms after the signal. Paper twins of live trades that filled went
from failing 19 times in 107 on October 3 to 10 times in 13. So from then
the desks trade apart. That evening every sport's futures went live, and
the catalog took the games back, now of every sport, and Bitcoin, its 15
minute windows and its price by a date: paper trades the games, matches,
fights, races, and windows, in play too and however soon they pay, and
live the futures, Bitcoin's held out until they have shown what they do.

The same day showed why one live trade in six had gone out some 157 ms
after its signal while the rest went out within 20. Every second a trade
stayed exposed, the retry to flatten it ended with the loss check, whose
query joined the orders to their trades with no index on the trade, so
SQLite built one over every order each time: some 150 ms with the 116,000
orders the October 1 bug had left, during which the orders of a trade the
same tick had decided waited their turn. Deleting those orders brought
every live order since under 65 ms. The orders table now has that index,
a retry checks the losses only when it sold something, and an edge waiting
for a Polymarket US book is priced again the moment the wait ends rather
than at the next tick.

**Paper timed as live orders are.** Paper had drawn each order's arrival
around 50 ms on Kalshi and 60 on Polymarket US, guesses set above the round
trips, and filled it against our newest book then. But that book is late:
both venues stamp each order with their own clock, the one they stamp their
books with, and the 1,096 live orders from October 1 to 4 that carry it
show a Kalshi order reached the venue 12 ms after we sent it and answered 8
ms later, and a Polymarket US one 59 ms after, its median hour by hour from
34 to 79, and answered 31 later. A Polymarket US order met the venue's book
of some 60 ms after the signal, while paper read the venue's book of some
20 ms before it, our copy being some 85 ms behind, so paper missed the very
changes an edge goes in. From October 4 paper keeps every book of the
contracts its orders are in flight on, and an order meets the one the venue
had when it would have arrived, once a later one shows nothing is still on
its way. The same day the minimum edge went from five cents to two, the
return a year doing the weighing, and paper kept to the bets paying within
a day.

**Paper started again.** On October 4 at 16:42 UTC paper was reset to no
open trades and 10,000 dollars on each venue, so its results are all under
the new rules. Nothing was deleted: its 548 trades and 2 settlements until
then carry the mode `paper_reset_2026-10-04`, which neither the paper desk
nor `summary.py` reads, 447 of them still open, and a `transfer_in` on
each venue's ledger, 869.54 dollars on Kalshi and 1,744.29 on Polymarket
US, brought its cash back to 10,000. To undo it, set those rows' mode back
to `paper` and remove the two ledger rows.

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

`--sport ncaaf`, `--sport epl`, `--sport politics`, `--sport crypto`, and
the rest build or run one sport's markets, `--sport nfl,ncaaf,mlb` several
from one pool of money, and `--sport all` every one, as the instance does
with `--sport all --execute live --live-in-play --not-live none`. `--no-trade` scans without trading,
`--no-scan` only records, and `--seconds 120` runs a short test.
`--execute live` trades the futures with real money and `--execute both`
runs both desks, paper trading the bets on one event. `--not-live
crypto,politics` holds sports' futures out of live trading, `crypto` alone
by default, and `--not-live none` holds none. `--live-in-play`, with
`--execute live` or `both`, has live trade the games under way too. The settings a run is tuned
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
the email, and split the Kalshi cash across its shards, from `src/`:

```bash
python3 -m tools.live_check --email
python3 -m tools.kalshi_shards --apply
```

Then read the reports:

```bash
python3 src/tools/summary.py --hours 24
python3 src/tools/summary.py --mode paper --sport epl,ucl
python3 src/tools/summary.py --mode live --market in-play
python3 src/tools/latency_report.py --hours 4
python3 src/tools/in_play_test.py
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
  common/     paths, time and json helpers, the venue list and maintenance windows, the sports in groups, the logger,
              the timer background work runs on, how a child process starts, quantiles
  db/         models, the SQLite schema and its migrations, reads and writes
  engine/     run, the process that wires the components together
    components/
      market/    record, feeds, streams, scan, tape: the venues' books and the edges between them
      trading/   executor (what paper and live share), paper, live, footprints, brakes, notify
      money/     balances (what paper and live share), paper, live, settle
    helper/      config (the settings a run is tuned by), game (when a game is played and when its bets pay out), pricing, fees
  tools/      summary report, live_check, kalshi_shards, repair_fills, latency_report, feed_check, in_play_test
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
| when each venue is in maintenance | `common/venues.py` |
| which sports are treated alike: team sports, soccer, matches, races | `common/sports.py` |
| every tunable number | `engine/helper/config.py` |
| when a trade is taken and sized | `engine/components/trading/executor.py` |
| the Kalshi cash on each shard | `tools/kalshi_shards.py` |
| how an order fills | `trading/paper.py`, `trading/live.py` |
| when live trading halts | `engine/components/trading/brakes.py` |
| the email alerts | `engine/components/trading/notify.py` |
| how far behind the feeds run | `tools/latency_report.py`, `tools/feed_check.py` |
| how the processes are wired | `engine/run.py` |
| the report on the database | `tools/summary.py` |
| live trading games in play, Polymarket US first | `trading/executor.py` (`fill_legs`), `trading/live.py`, `trading/footprints.py` |
| the in-play test of 2026-10-04 to 10-05 | `tools/in_play_test.py` |
| live records against the venues' positions | `tools/repair_fills.py` |
