# Glimpse Terminal

A keyboard-driven terminal for Glimpse's prediction markets and the world around them. It opens on one long page,
most important first: Bitcoin's last 24 hours and next 24 beside the odds on its next hour, then what every bot on
this machine expects of the next hour, day and three days, then gold and the odds on its next close, hashrate and
the difficulty adjustment, the news and world prices, dollar liquidity and the Treasury curve, currencies and
commodities. `j` and `k` walk down it, `enter` opens a window full screen, `f` is the forecast and `o` the odds.
Everything runs on data anyone can fetch for free. It needs no key, no account and no configuration to start. Free
and open source. Works anywhere Python does, including over SSH. It moves like spacemacs: vim keys, windows and
buffers, and `SPC` for the rest.

```
uv run glimpse-tui              # the front page: Bitcoin, the bots, gold, the chain, the news, the world
uv run glimpse-tui "CONS BTC"   # open on any command: "CONS BTC", "DIST BTC", "FCST XAU", NEWS, "LP MACRO"
uv run glimpse-tui DEMO         # a ninety-second tour
uv run glimpse-tui --markets    # open on the odds screen, as the terminal did before the front page
```

```
 GLIMPSE  TERMINAL   BTC  81,423 spot                               open-source forecast terminal · Glimpse API                                              READ-ONLY L log in │ 23:24:42 UTC
  SPC   t  FRONT PAGE   f  FORECAST   o  ODDS   B  BOTS   p  PORTFOLIO    SERIES  [  BTC  BTC 1D  ETH  SOL  XAU  ]                                                         ?  HELP   q  QUIT
 BTC 81,423 +0.7%  GOLD 4,365 −0.1% via PAXG  S&P 7,638 +1.1% d  NDX 29,644 +0.7% d  DXY 100.282 +0.2% d  10Y 5.01% +0.07 d  OIL 130.80 +7.9% d               NY 19:24  LDN 00:24  TYO 08:24
┌1  GP  Bitcoin · the last 24 hours and the next 24 hours ────────────────────────────────────────────── delayed ┐┌2  DIST  Bitcoin · odds on the next hour ─────────────────────────── live ┐
│ Now 81,423     In 24 hours  81,581 median +0.2%   77,000 – 86,000 likely (80%)                                 ││ 00:00 close  in 35m 17s     most likely 81,000 – 82,000  16%             │
│                                                   │                                                            ││                                                                          │
│                                                   │                                                ░░░         ││ 85,000 – 86,000  █▏                                        0.4%          │
│ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ │ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ░░░░░░░░░░░░░░░░░░░░░░░░┤85,000  ││ 84,000 – 85,000  █████▊                                    2.3%          │
│                                                   │                           ░░░░░░░░░░░░░░░░░░░░░░░░         ││ 83,000 – 84,000  █████████████████▌                        7.1%          │
│                                                   │            ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││ 82,000 – 83,000  ████████████████████████████████▎        13.0%          │
│                                                   │            ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││ 81,000 – 82,000  ████████████████████████████████████████ 16.2%  ◂ now   │
│                                                   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││ 80,000 – 81,000  █████████████████████████████████▋       13.6%          │
│ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░┤82,500  ││ 79,000 – 80,000  ███████████████████                       7.7%          │
│ ⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀│⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠤⠄┄◂81,423  ││ 78,000 – 79,000  ██████▋                                   2.7%          │
│                                                   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││ 77,000 – 78,000  █▍                                        0.5%          │
│                                                   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││ 76,000 – 77,000  ▎                                         0.1%          │
│ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░┤80,000  ││ 75,000 – 76,000  ▏                                         0.1%          │
│                                                   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││                                                                          │
│                                                   │░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││                                                                          │
│                                                   │░░░░░░░░░░░░░   ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░         ││                                                                          │
│ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ │ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ┄ ░░░░░ ┄ ░░░░░░░░░░░░░░░░░░░░┤77,500  ││                                                                          │
│                                                   │                       ░░░░░   ░░░░░░░░░░░░░░░░░░░░         ││                                                                          │
│                                                   │                                       ░░░░░░░░░░           ││                                                                          │
│                                                   │                                       ░░░░░░░░░░           ││                                                                          │
│                                                   │                                                            ││                                                                          │
│ 18 Sep 23:24       19 Sep 11:26                  NOW                  20 Sep 11:28       20 Sep 23:30          ││                                                                          │
└─────────────────────────────────────────────────────────────────────────────── [ ] window · L log · C candles ─┘└─────────────────────── o or enter to bet on these odds · f the forecast ─┘
┌3  CONS  Bitcoin · what the bots expect ────────────────────────────────────────────────────────────────── live ┐┌4  BOT  Bitcoin · one bot at a time ──────────────────────────────── live ┐
│ Now 81,423   17 of 17 bots priced the next hour   they agree 98%   apart from the market 71%                   ││ Random Walk (bootstrap)  Baseline  ▲ bullish                 bot 1 of 17 │
│                                                                                                                ││   History's own hourly moves, replayed at today's volatility             │
│ NEXT HOUR ───────────────────────────────────────────────────────────────────────────────── 00:00 · in 35m 17s ││                                                                          │
│   ▲  17 bullish  ·   0 neutral  ▼   0 bearish                                                                  ││   this bot         median    move   80% band             market          │
│   ██████████████████████████████████████████████████████████████████████████████████████████████████████████   ││   next hour    ▲   81,491   +0.1%   81,083 – 81,902      81,450          │
│   bots 81,491  +0.1%   81,078 – 81,904 (80%)    market 81,450   79,416 – 83,481    bots 0.1% above the market  ││   in 24 hours  ▼   81,184   −0.3%   79,156 – 83,273      81,581          │
│                                                                                                                ││   in 72 hours  ▼   80,876   −0.7%   78,079 – 83,903      81,647          │
│ IN 24 HOURS ─────────────────────────────────────────────────────────────────────────────── 23:00 · in 23h 35m ││                                                                          │
│   ▲   2 bullish  ·  10 neutral  ▼   5 bearish      of the neutral: 2 volatile · 1 sideways                     ││   price at close     bot  market                                         │
│   ████████████▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒███████████████████████████████    ││   84,000–85,000  <0.01%    3.6%  ░                                       │
│   bots 81,284  −0.2%   79,084 – 83,506 (80%)    market 81,581   77,194 – 85,948    bots 0.4% below the market  ││   83,000–84,000  <0.01%   11.1%  ░░░░                                    │
│                                                                                                                ││   82,000–83,000   0.47%   20.6%  ░░░░░░░░                                │
│ IN 72 HOURS ──────────────────────────────────────────────────────────────────────────────── 23:00 · in 1d 23h ││ ▸ 81,000–82,000   97.7%   25.5%  █████████████████████████████████████   │
│   ▲   2 bullish  ·   9 neutral  ▼   6 bearish      of the neutral: 3 volatile · 1 sideways                     ││   80,000–81,000    1.8%   21.4%  █░░░░░░░                                │
│   ████████████▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒█████████████████████████████████████    ││   79,000–80,000  <0.01%   12.1%  ░░░░░                                   │
│   bots 81,253  −0.2%   78,150 – 84,360 (80%)    market 81,647   75,427 – 87,853    bots 0.5% below the market  ││   78,000–79,000  <0.01%    4.2%  ░░                                      │
│                                                                                                                ││   █ both  █ the bot buys  ░ dearer than the bot                          │
│ WHO STANDS OUT ────────────────────────────────────────────────────────────── on the next hour · BOT opens one ││                                                                          │
 -- NORMAL --                                                                                                                                        screen 1 of 4 · j k scroll   ? every key
 MOVE     j k · ↓ ↑ down and up the page   h l · ← → across   g G top / bottom   1-9 window
 OPEN     enter this window full screen   esc back   f the forecast   o the odds   B bots   p portfolio
 FIND     : or SPC SPC search everything   ctrl-j ctrl-k choose   SPC menu   ? every key   q quit
```

Half of the top of that page is the machines: Glimpse is a market you can bring a model to, so the terminal opens
with what the models think. `CONS` runs every bot in the zoo against the live market at three horizons and counts
the bulls against the bears; `BOT` takes them one at a time, `[` and `]` walking the zoo, and shows what each one
would buy. Both run here, on your computer, from public candles.

## Moving around

Every page is a buffer; windows show buffers; the page is taller than the screen and scrolls. The key panel at the
bottom always lists what works where you are, and `?` explains everything.

| on the page | |
|---|---|
| `j` `k` · `↓` `↑` · `ctrl-j` `ctrl-k` | down and up the page, window by window; the page scrolls to follow |
| `h` `l` · `←` `→` | across |
| `g` `G` · `1`-`9` | top and bottom · jump to a window (its number is in its frame) |
| `enter` | open the window full screen: its frame turns green and the keys belong to its page |
| `esc` | back a step, and out to the page |
| `f` · `o` | the full forecast, or the odds on every outcome, on the series that window shows |
| `:` | search everything that can be opened. Type to filter, `ctrl-j` `ctrl-k` to choose, `enter` to open |

| `SPC`, the leader, on every screen | |
|---|---|
| `SPC SPC` · `SPC 1-9` · `SPC TAB` | search everything · window 1 to 9 · back a page |
| `SPC w /` · `SPC w -` · `SPC w d` · `SPC w m` · `SPC w =` | split right · split below · close · maximize · even sizes |
| `SPC b b` · `SPC b n` · `SPC b p` · `SPC b d` | list buffers · next · previous · close the buffer |
| `SPC t` · `SPC f` · `SPC o` · `SPC B` · `SPC p` | the front page · the forecast · the odds · bots · portfolio |

Whatever you open replaces the focused window's page, which stays open as a buffer: backspace brings it back and
`SPC b b` lists it. `ctrl-w` chords work as in vim. `LP MACRO` loads another layout and `LP SAVE desk` saves yours.
Shipped layouts: `BTC` (the default), `MACRO`, `TRADER`, `TREASURY`, `CHAIN`, `MINER`. Yours are TOML files in
`~/.config/glimpse/layouts/`.

## Commands

| you type after `:` | you get |
|---|---|
| `CONS BTC` · `BOT BTC` | what every bot expects · one bot at a time, `[` `]` to walk the zoo |
| `FCST BTC` · `FCST XAU` · `FCST BTC 1D` | a Glimpse forecast as a heatmap |
| `DIST BTC` · `DIST XAU` | the odds on every outcome of the next close, range by range |
| `NEWS` · `NEWS FED` | the headlines. enter reads a story here, as text; `n` `p` walk the list; backspace goes back |
| `GP BTC 24H` · `GP XAU 1D` · `GP SPX` | Bitcoin's next 24 hours · gold's next month · a chart of anything |
| `HM XAU` · `ODDS BTC` | the full forecast, or the odds, on a series |
| `QM GLOBAL` · `ECO` · `RATES` · `MACRO` | a watchlist, economic prints, the yield curve, dollar liquidity |
| `HELP` · `HELP DIST` · `F1` | every function · one function's page · help on the focused page |

## Functions

| | |
|---|---|
| **Bitcoin** | `BTC` the Bitcoin page · `MEMP` mempool and the next block · `FEES` fees and a calculator · `BLK` blocks · `TX` a transaction · `ADDR` an address · `RBF` live replacements · `MINE` pools · `HASH` hashrate and difficulty · `DIFF` the retarget · `HASHP` hashprice · `HALV` the halving · `SUPL` supply · `LN` Lightning · `ORCL` the price read from the chain alone |
| **On-chain** | `FLDS` search 60,000 Bitview series · `GP` chart anything · `ONCH` the cycle dashboard · `URPD` realized price distribution · `WAVE` HODL waves · `CYC` valuation bands · `CORR` rolling correlations |
| **Markets** | `NEWS` world, economy, markets, Fed, ECB and crypto headlines · `READ` a story, in the terminal · `QM` quote monitor · `WEI` world indices · `FX` currencies and the dollar index · `GLCO` commodities · `RATES` the Treasury curve · `MACRO` liquidity · `ECO` economic prints · `HMAP` performance heatmap · `RV` BTC against gold · `DVOL` implied volatility |
| **Companies** | `DES` description · `FA` financials · `CF` filings, read inside the pane · `CFS` full-text search · `TRSY` Bitcoin treasuries · `MINR` public miners · `ETF` spot ETFs · `N` news and filings · `TOP` the feed |
| **Glimpse** | `CONS` what the whole zoo expects · `BOT` one model against the market · `FCST` a forecast as a heatmap · `DIST` the odds on the next close · `ODDS` the odds screen, close by close · `HM` the forecast heatmap · `SLIP` the bet slip · `PORT` portfolio · `BOTS` the model zoo · `OMON` every close as an options chain · `GIV` Glimpse's implied volatility against Deribit |
| **Tools** | `AL` alerts · `NOTE` notes · `CALC` sats, fees, Kelly · `EXP` export · `SRC` every source's health · `SET` settings · `ASK` plain English · `HELP` · `FIND` · `DEMO` |

Every function has a `HELP` page: what it shows, where each number comes from, how often it refreshes, and what it
does when a source is down.

## Where the numbers come from

Every default source is public and needs no key. [SOURCES.md](SOURCES.md) records each one's terms, limits and a
sample response, all checked live.

- **Bitcoin:** mempool.space (REST and WebSocket), Bitview (the Bitcoin Research Kit), Blockstream Esplora as a fallback.
- **Prices:** Coinbase, Kraken and Bitstamp public tickers. BTC is the median of the three.
- **Markets:** Yahoo Finance's public chart API (the endpoints `yfinance` reads) for index levels, the VIX, the dollar
  index, Treasury yields, COMEX and NYMEX futures, FX and US shares. `SET sources.yahoo false` turns it off and every
  pane falls back to the official daily sources below.
- **Macro:** FRED's keyless CSV, the US Treasury's yield curve, ECB reference rates, Deribit's public API.
- **News:** public RSS from BBC World, CNBC (international, economy, finance), the Federal Reserve, the ECB and CoinDesk.
  A story is fetched from the outlet only when you open it, and shown as text.
- **Companies:** SEC EDGAR, once you give it a contact (below).

The terminal says what it knows and how well. Each window's frame says how old its numbers are (`live`, `daily`,
`weekly`, `monthly`) and the date a daily value describes, but never which vendor served them: `SRC` lists every
source and each function's `HELP` page says where its numbers come from, which is where that belongs. A value it could not refresh stays on screen, dimmed, marked `stale`
with its age. Where no open feed carries an instrument, a labelled proxy stands in: gold shows through PAXG, and a
few shares and ETFs through Kraken's tokenised versions, each marked `proxy` with the stand-in's ticker. A proxy is
never presented as the instrument. Several live feeds the original design hoped for now need paid keys, so indices,
USD/JPY, oil and rates are official daily values, and say so.

The terminal is a good citizen: per-host rate limits, shared connections, caching (memory, and SQLite at
`~/.cache/glimpse/terminal.db`), exponential backoff with jitter, and a descriptive User-Agent. `SRC` shows every
source's health, latency, budget and last error.

## Your own node, Tor, and privacy

Every Bitcoin backend is a URL. Point the terminal at your own and it takes effect at once, no restart:

```
SET bitcoin.mempool http://umbrel.local:3006      a self-hosted mempool
SET bitcoin.bitview http://localhost:3110         bitviewd
SET bitcoin.electrum ssl://your-server:50002      Electrum, never a public server by default
SET socks5 socks5h://127.0.0.1:9050               every request through Tor
```

Looking up an address or a transaction on a public backend tells that backend what you care about. The terminal
says so the first time.

**The SEC asks for a contact.** Company functions read SEC EDGAR, which refuses requests without a name and email in
the User-Agent. The terminal sends nothing to the SEC until you set one, and then the SEC receives it on every
request: `SET sec_user_agent Your Name you@example.com`.

Settings live in `~/.config/glimpse/terminal.toml`. Secrets (the Glimpse API key, optional data keys) live in the OS
keychain, never in that file, a log or a commit.

## Trading Glimpse

No key is needed to look around: all market data is public. Trading is through a Glimpse API key and nothing else.
The terminal holds no wallet: deposits and withdrawals stay on the website. To trade, press `L` and paste an API key (register at
glimpse.markets, complete verification, then Settings → Developer API Keys). A Glimpse key can trade. It cannot
withdraw, and it cannot create or list other keys.

`ODDS`, `HM`, `SLIP`, `PORT` and `BOTS` are the screens the terminal has always had, with every key they have always
had. `f` opens the forecast and `o` the odds from anywhere, `B` the bots and `p` the portfolio, and `t` returns to the
front page (`SPC f` `SPC o` `SPC B` `SPC p` `SPC t` do the same). `ODDS` was called `MKT`, and the ladder, and that name
still opens it. `PORT` (`p`) is the account: balance, exposure against its cap, open positions valued locally.

- **The odds (`ODDS`, `o`).** One row per close with the market's median and 80% band; beside it the odds on every
  outcome of the close you are on: each price range with its probability, what it pays net of fees, and your
  position in it. `j` `k` move, `h` `l` switch pane, `v` selects a
  range, `+` `-` size, `b` buys after a confirmation, `[` `]` change series (`BTC`, `BTC 1D`, `ETH`, `SOL`, `XAU`).
- **The forecast (`HM`, `f`).** Time runs across, price runs up, each character is one price cell of one
  close, one orange growing solid where the market puts more probability (`░▒▓█`), candles left of `NOW`, the
  median in cyan.
  Full vim motion: counts, `{` `}` a day, `gg` `G`, `zf` the whole forecast, `zi` `zo` zoom price, `<` `>` zoom
  time, `v` box select, `V` the 80% band, marks, `/` go to a price. `tab` opens the bet slip, `b` bets.
- **Options on every close (`OMON`).** Digital calls, puts and ranges read off each close's distribution, an implied
  volatility from a lognormal fit, and the Greeks. Enter hands the range to the heatmap as a box. `OMON` never places
  an order itself.
- **If the colours look wrong, press `c`.** It switches between 24-bit colour and a palette built only from colours a
  256-colour terminal draws exactly, and remembers your choice.

### Safety

- Every order asks for confirmation and shows cost, payout and what you lose.
- Before an order is sent, the terminal asks the server for its own estimate and refuses if the price has moved more
  than 0.5%. The API has no slippage limit of its own, so this is the guard.
- Orders are never retried. If the connection drops mid-order the terminal says the fill is unknown and reloads the
  portfolio, rather than risk buying twice.

## Bots (`CONS`, `BOT`, `BOTS`, `B`)

The model zoo from the Glimpse Forecast Lab: 130 forecasting models that run on this computer from public Coinbase
candles. Two windows of the front page read them: `CONS` counts how many lean bullish, bearish or neither at three
horizons and draws the one picture they make together against the market's own, and `BOT` shows a single model's
picture of the nearest close beside what the market charges for the same ranges, `[` and `]` walking the zoo. A pass
over the whole zoo is recomputed when a new hourly bar closes, and nothing about it leaves the machine. `BOTS` (`B`)
is the full screen: each row says what that bot believes about the nearest close right now. `i` opens a model's account of
itself (idea, data, maths, trades). `enter` runs one on paper; with a key and the word `LIVE`, with real sats,
sized by quarter-Kelly, never above its budget, checking the server's estimate before every order.

```sh
glimpse-tui bots [search]                                  # list them
glimpse-tui about ema_crossover                            # idea, data, maths, trades, machinery
glimpse-tui run ema_crossover --series BTC --budget 20000  # dry run in this shell; add --live for real sats
```

Your own bot is one function in `~/.config/glimpse/bots/<name>.py`:

```python
def forecast(book, closes, hours):
    """book.bins: [(low, high), ...]   closes: hourly prices, oldest first   hours: time to close
    Return one probability per bin, summing to 1."""
```

Defaults live in `~/.config/glimpse/bots.toml`. Bot files are Python that runs with your permissions. Only install
ones you have read.

## Development

```
uv sync
uv run pytest        # offline: every source is replayed from tests/fixtures/sources/, no test touches the network
uv run ruff check .
```

- [TERMINAL.md](TERMINAL.md): the build brief. [PLAN.md](PLAN.md): decisions, verified facts, open questions, and the
  checklist of what is built. [SOURCES.md](SOURCES.md): every data source, checked live.
- A function page is one class: `src/glimpse_tui/funcs/__init__.py` explains how to write one, and
  `funcs/btc.py` is the worked example.
