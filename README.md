# Glimpse Terminal

The Glimpse prediction market in your terminal. Read the market's forecast for every close,
price a range, buy it, watch your portfolio, run a bot. Works anywhere Python does, including
over SSH.

```
uv run glimpse-tui
```

No key is needed to look around: all market data is public. To trade, press `L` and paste an
API key. To get one, register at glimpse.markets, complete verification, then create a key
under Settings → Developer API Keys.

## Reading the screen

- **Left, markets.** One row per close, nearest first. `median` and `80% band` are the market's
  own forecast for that close. Read down the column to see where the market thinks price goes.
- **Right, ladder.** Price ranges of the selected market. `prob` is the market's probability,
  `odds` what one contract returns per sat staked, net of fees. `◂` marks spot, `●` a position.
- **Bottom, ticket.** What the selected range costs and pays at the current size, updated as
  you move. Cost includes the 2% fee. Payout is net of the 2% settlement fee.

## The forecast heatmap

Press `f`. Time runs across, price runs up, and every character is one price cell of one close. The
denser the glyph, the more probability the market puts there:

```
·  :  -  =  +  *     0.6% to 10% per bin, log spaced
#  %  @              10% up to certainty
```

in four colours, dim orange to white. Blank is untraded. The cyan rule is each close's median and `◂` on the right marks spot. Left of the orange `NOW` line is price
history on the same axis and time scale: 30-minute candles on hourly series (two under each close), daily candles
on daily series. The top line reads out the cell under the cursor with its probability and odds, and for that close
the market's σ and annualised implied volatility, read off its 80% band.

It is plain ASCII on purpose: a row is a few colour runs of ordinary characters, the drawn chart is
cached so a clock tick redraws nothing, and the market list refreshes once a minute, never twice at
once. Every live close loads: 168 on hourly BTC (seven days out) and about 172 on each daily series.
`<` zooms time out past one character a close, to 2, 3, 4, 6 or 12 hours a column (2 days or a week on
daily series); a zoomed-out cell draws its closes' average density and bets on all of them. `zf` fits the
whole forecast on screen.

### Keys

Every key for the view you are in is listed at the bottom of the screen, under a vim status line that
shows the mode (`NORMAL`, `VISUAL`, `SLIP`) and the keys typed so far. Terminals under 40 rows show two
lines of it and `?` for the rest.

| | |
|---|---|
| `h` `j` `k` `l`, with counts (`5j`, `12l`) | move |
| `{` `}` · `w` `W` | a day (a week on daily series) · next / previous midnight |
| `0` `^` `$` · `12\|` | first / last close · close no. 12 |
| `gg` `G` | top / bottom of that close's 80% band |
| `ctrl-d` `ctrl-u` · `ctrl-f` `ctrl-b` · `ctrl-e` `ctrl-y` | half page · page · scroll without moving the cursor |
| `zt` `zz` `zb` · `zh` `zl` `zH` `zL` | cursor row to top / centre / bottom · scroll sideways |
| `zf` · `zi` `zo` · `<` `>` · `za` · `zm` | whole forecast · zoom price · zoom time · auto-fit · median on / off |
| `v` · `V` · `o` · `gv` · `esc` | box select · select the 80% band · other corner · reselect · clear |
| `ma` `'a` · `''` | set mark a, jump to it · jump back |
| `/` · `:` | go to a price · command line: `:q` `:help` `:78000` `:size 50` `:size 500s` `:buy` `:series daily btc` `:noh` |
| `b` · `enter` · `tab` | BET · open the close in the ladder · bet slip |
| `q` `ZZ` `:q` | quit |

**If the colours look wrong, press `c`.** It switches between 24-bit colour and a palette built only
from colours a 256-colour terminal can draw exactly, and remembers your choice.

## The bet slip

On the right, as on the web. Top to bottom: **your prediction** (Low, High, From, To and Size, each
editable), then the **ticket** (cost, payout, profit, odds and ROI, all in the same plain bold text), then the
bet button. Nothing else.

Text size is your terminal's font size (Cmd/Ctrl and +): an app inside a terminal cannot change it.

`tab` moves onto it.

| | |
|---|---|
| `j` `k` | pick a field: Low, High, From, To (or Close, in the ladder view), Size |
| `h` `l` or `-` `+` | step it by one price cell, one close, or one contract. Edges cannot cross |
| `enter` | type a price or a size; prices snap to the market's bins |
| `b` | place the bet · `tab` or `esc` goes back to the chart |

Editing the slip moves the box on the chart, and moving the box updates the slip. Terminals
narrower than 118 columns keep a compact ticket under the chart instead.

A box across several closes is several independent bets. The ticket shows what each close pays
and the most the box can pay; `all land` is the chance every close hits, treating them as
independent. A box may span every loaded close; the terminal sends it as requests of 24 closes each and tells
you how many were sent if one fails.

## Keys

| | |
|---|---|
| `j` `k` `↓` `↑` `s` `w` | move · `gg` `G` top / bottom · `ctrl-d` `ctrl-u` half page |
| `h` `l` `←` `→` `a` `d` `tab` | switch pane · `zz` jump to the most likely range |
| `[` `]` | previous / next series: `BTC` (hourly), `BTC 1D`, `ETH`, `SOL`, `XAU`. Opens on BTC, then on whichever you viewed last. Every market is titled by its question: `BTC at 18 Sep 11:00 UTC` |
| `v` | start a range, move to extend it, `esc` to clear |
| `+` `-` | double / halve contracts · `S` exact size, or a budget like `500s` |
| `b` `enter` | buy, after a confirmation |
| `f` | forecast heatmap |
| `p` | portfolio · `x` sells the selected position |
| `B` | bots: `j` `k` move · `h` `l` category · `/` search · `S` sort · `i` how the model works · `tab` walk the closes ahead · `enter` run / stop · `e` budget · `x` stop · `L` log in to trade real sats |
| `L` `O` `r` `?` `q` | log in · log out · refresh · help · quit |

## Your key

Looked up in this order: `GLIMPSE_API_KEY`, the OS keychain, then
`~/.config/glimpse/credentials` (mode 0600) on servers with no keychain. It is validated
before it is saved, shown only as `glp_live_…last4`, and never written to a log. `O` removes it.

A Glimpse key can trade. It cannot withdraw, and it cannot create or list other keys.

## Safety

- Every order asks for confirmation and shows cost, payout and what you lose.
- Before an order is sent, the terminal asks the server for its own estimate and refuses if the
  price has moved more than 0.5%. The API has no slippage limit of its own, so this is the guard.
- Orders are never retried. If the connection drops mid-order the terminal tells you the fill is
  unknown and reloads the portfolio, rather than risk buying twice.

## Bots

Press `B`. The list is the model zoo from the Glimpse Forecast Lab: every model there that can run on
hourly candles alone (trend, mean reversion, volatility, calendar, regimes and jumps, pattern models, named
distributions, scenario views and option-payoff views), running on this computer from public Coinbase
candles. No server, no database, no GPU.

Each row says what that bot believes about the nearest close *right now*: bullish, bearish, sideways or
volatile, and the price it calls. The tabs
(`h` `l`) sort the zoo by that live view: Bullish, Bearish, Sideways, Volatile. The right pane is one model, top to bottom:
the idea in two lines; a ladder with two bars per price range, what the bot thinks and what the market thinks, with
`BUYS` next to the ranges the bot would bet on; and under it a time series chart. The chart runs the hourly candles the
model actually read (merged to the chart's time scale) into the model's own forecast: its median path `─` and 80% band
`░` at every close ahead, the market's median `·` through it for contrast, and the close the ladder shows marked `▒`.
`tab` walks the closes ahead, close by close. None of it is a track record, and the terminal shows none.

`i` opens the model's account of itself, written to be checked against the code: **idea** (what it believes and why),
**reads** (the candle columns, windows and how many bars it needs), **maths** (the signal formula with the parameters
the code uses, the estimator, and how a reading moves the picture), **trades** (which ranges it puts more weight on
than the market, and when it says nothing), **now** (its stance on the close shown: median, 80% band, lean against the
baseline in σ, width, and how much of its probability sits differently from the market), then the machinery every
picture shares (percentile strength, exponential tilt, timing clocks, or path simulation, named distributions, option
payoffs), the volatility clock that sizes every picture, the runner's buy and sell rules, and the reference. The same
text is on the command line: `glimpse-tui about <bot>`.

`enter` deploys the selected bot: type a budget in sats, or press `enter` again to accept the one shown.
Small budgets are fine; there is no minimum bet. Each cycle the runner

- buys only ranges the bot values above the market price after both fees, sized by quarter-Kelly against
  the budget, never paying past the price the bot's picture justifies;
- sells a position it bought once the market pays more for it than the picture says it is worth;
- never touches a position it did not open (what it owns is recorded in `~/.config/glimpse/bot-ledger.json`);
- checks the server's estimate before every order, never has more than the budget at risk, and stops at its
  per-cycle and per-hour caps.

There is no testnet: bots use the production API. With no API key loaded (`L` to log in) a bot can only
practise on paper, with a paper ledger, logging what it would do. With a key, deploying asks one more
question, and typing `LIVE` makes it trade real sats through that key. Several bots can run at once. They stop when the terminal closes; to
keep one running without the screen:

```sh
glimpse-tui bots [search]                                  # list them
glimpse-tui about ema_crossover                            # idea, data, maths, trades, machinery
glimpse-tui run ema_crossover --series BTC --budget 20000  # dry run in this shell; add --live for real sats
```

How the terminal's models differ from the lab's: the lab simulates thousands of paths against a feature
database and calibrates each signal on a year of history. The terminal keeps each model's signal, its
conviction rule and its shape, and draws the picture as a density on a grid, scaled by the same EWMA
volatility clock. Models that need funding, options, sentiment or downloaded weights are not included.
Each bot's `maths` says where it differs.

Your own bot is one function in `~/.config/glimpse/bots/<name>.py`, listed under the Mine tab:

```python
def forecast(book, closes, hours):
    """book.bins: [(low, high), ...]   closes: hourly prices, oldest first   hours: time to close
    Return one probability per bin, summing to 1."""
```

Defaults live in `~/.config/glimpse/bots.toml`; a bot's budget lowers the caps to a quarter of it per cycle
and half of it per hour:

```toml
markets = 2               # nearest closes traded each cycle
interval_s = 300
bankroll_sats = 20000     # default budget
kelly = 0.25
min_edge = 0.10           # the round trip costs ~4% in fees alone
exit_edge = 0.05          # sell when the market pays 5% more than the picture's value
max_per_cycle_sats = 2000
max_per_hour_sats = 5000
```

Bot files are Python that runs with your permissions. Only install ones you have read.

## Development

```
uv sync
uv run pytest        # offline; pricing is pinned to server estimates recorded live
uv run ruff check .
```

Design, verified API facts and open questions: [PLAN.md](PLAN.md).
