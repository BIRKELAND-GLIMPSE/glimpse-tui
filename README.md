# Glimpse Terminal

A keyboard-driven terminal for Bitcoin and the markets around it, in your terminal. It looks inside Bitcoin (the
mempool as it fills, the next block as it forms, every block, transaction and address), charts tens of thousands of
open on-chain series, follows gold, FX, indices, oil and rates, reads company filings, and trades the Glimpse
prediction market. It is built entirely on data anyone can fetch for free. It needs no key, no account and no
configuration to start. Free and open source. Works anywhere Python does, including over SSH.

```
uv run glimpse-tui              # the default launchpad
uv run glimpse-tui MEMP         # open on any GO bar command: MEMP, "LP MACRO", "TX <txid>", "GP BTC XAU SPX"
uv run glimpse-tui DEMO         # a two-minute tour
uv run glimpse-tui --markets    # open on the Glimpse markets and ladder, as the terminal did before launchpads
```

```
 GLIMPSE  TERMINAL   BTC 81,608 +2.1%  FEE 2 s/vB  #967,653 7m                    READ-ONLY   UTC 18:32  NY 14:32
 MEMP 38 MvB  SPX 6,612 +0.4% d  USDJPY 147.82 -0.2% d  XAU 3,684 proxy +0.8%  BRENT 68.40 -1.1% d  US10Y 4.12% d
 > BTC <GO>   BTC Bitcoin · CRYPTO   1 GP  2 MEMP  3 ONCH  4 OMON
┌1  GP  BTC · 1H ──────────────────────── coinbase · live ┐┌2  MEMP  mempool ──── mempool.space ws · live ┐
│ ━ BTC 81,608 +2.1%  to NOW, then Glimpse's median ━ … ░ ││ NEXT BLOCKS      fee s/vB    txs     eta     │
│                         ⢀⡠⠔⠒⠢⣀│░░░░░░░░░░░░░ ┤83,000     ││ #1  ████████████   3-40    3.4k     ~9m     │
│                   ⣀⡠⠔⠊⠉      │┄┄┄┄┄┄┄┄┄┄┄┄┄ ◂81,608     ││ #2  ███████████▌   2-3     3.9k    ~19m     │
│          ⢀⣀⠤⠔⠒⠉⠁             │░░░░░░░░░░░░░ ┤80,000     ││ NEXT BLOCK 3,412 tx · 998 kvB               │
│ ⠤⠤⠒⠒⠉⠁                       │                          ││ ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀ │
│ 17 Sep       18 Sep          NOW          20 Sep        ││ FEES next 3 30m 2 1h 2  POOL 38 MvB         │
└─────────────────────────────────────────────────────────┘└─────────────────────────────────────────────┘
```

## The GO bar

Bloomberg grammar: `<TICKER> [<CLASS>] <FUNCTION> <GO>`, where Enter is GO. Press `` ` `` or `:` to focus it.

| you type | you get |
|---|---|
| `BTC` | the Bitcoin page |
| `MEMP` | the mempool, with the next block drawn |
| `TX 8a4666…` · `ADDR bc1q…` · `BLK 967653` | a transaction, an address, a block. A bare txid, address or height works too |
| `USDJPY GP` · `GP BTC XAU SPX` · `GP mvrv` | a chart; several instruments rebased to compare; any Bitview series by name |
| `MSTR DES` · `MSTR FA` · `MSTR CF` | a company: description, financials, filings |
| `LP MACRO` · `LP SAVE desk` | load a launchpad · save the current layout |
| `HELP` · `HELP MEMP` · `F1` | every function by category · one function's page · help on the focused pane |
| `FIND gold` | search functions, instruments, companies and launchpads |
| `what is the fee right now` | anything that is not a command goes to `ASK`, which shows the command it chose before running it |

Tab completes, the arrows pick a suggestion, ↑ and ↓ walk a history kept between sessions. A bare function runs
against the focused pane's security. Digits pick the numbered menu items beside the bar. Classes work like
Bloomberg's yellow keys (`CRYPTO`, `CURNCY`, `CMDTY`, `INDEX`, `GOVT`, `EQUITY`, `ETF`, `SERIES`) and an unambiguous
ticker needs none.

## Panes and launchpads

Up to nine tiled panes, each running one function against its own security. Each pane's frame shows its number, the
function as an amber chip, its security, and on the right where the data came from and how fresh it is.

| | |
|---|---|
| `tab` `shift-tab` | next / previous pane |
| `ctrl-w` `h` `j` `k` `l` | move focus |
| `ctrl-w` `s` `v` | split: stacked, side by side |
| `ctrl-w` `q` · `o` · `=` | close · zoom one pane and back · even the sizes |
| `j` `k` `ctrl-d` `ctrl-u` `g` `G` | scroll the page, or move its row cursor |
| `enter` | open the row under the cursor |
| `EXP` | export the focused pane's table to `~/Downloads/glimpse/` as CSV, with its sources |

Shipped launchpads: `BTC` (the default), `CHAIN`, `MINER`, `MACRO`, `TRADER`, `TREASURY`. Yours are TOML files in
`~/.config/glimpse/layouts/`.

## Functions

| | |
|---|---|
| **Bitcoin** | `BTC` the Bitcoin page · `MEMP` mempool and the next block · `FEES` fees and a calculator · `BLK` blocks · `TX` a transaction · `ADDR` an address · `RBF` live replacements · `MINE` pools · `HASH` hashrate and difficulty · `DIFF` the retarget · `HASHP` hashprice · `HALV` the halving · `SUPL` supply · `LN` Lightning · `ORCL` the price read from the chain alone |
| **On-chain** | `FLDS` search 60,000 Bitview series · `GP` chart anything · `ONCH` the cycle dashboard · `URPD` realized price distribution · `WAVE` HODL waves · `CYC` valuation bands · `CORR` rolling correlations |
| **Markets** | `QM` quote monitor · `WEI` world indices · `FX` currencies and the dollar index · `GLCO` commodities · `RATES` the Treasury curve · `MACRO` liquidity · `ECO` economic prints · `HMAP` performance heatmap · `RV` BTC against gold · `DVOL` implied volatility |
| **Companies** | `DES` description · `FA` financials · `CF` filings, read inside the pane · `CFS` full-text search · `TRSY` Bitcoin treasuries · `MINR` public miners · `ETF` spot ETFs · `N` news and filings · `TOP` the feed |
| **Glimpse** | `MKT` markets and ladder · `HM` the forecast heatmap · `SLIP` the bet slip · `PORT` portfolio · `BOTS` the model zoo · `OMON` every close as an options chain · `GIV` Glimpse's implied volatility against Deribit |
| **Wallet** | `WAL` the Glimpse account, your own barkd Ark wallet, and watch-only keys |
| **Tools** | `AL` alerts · `NOTE` notes · `CALC` sats, fees, Kelly · `EXP` export · `SRC` every source's health · `SET` settings · `ASK` plain English · `HELP` · `FIND` · `DEMO` |

Every function has a `HELP` page: what it shows, where each number comes from, how often it refreshes, and what it
does when a source is down.

## Where the numbers come from

Every default source is public and needs no key. [SOURCES.md](SOURCES.md) records each one's terms, limits and a
sample response, all checked live.

- **Bitcoin:** mempool.space (REST and WebSocket), Bitview (the Bitcoin Research Kit), Blockstream Esplora as a fallback.
- **Prices:** Coinbase, Kraken and Bitstamp public tickers. BTC is the median of the three.
- **Macro:** FRED's keyless CSV, the US Treasury's yield curve, ECB reference rates, Deribit's public API.
- **Companies:** SEC EDGAR, once you give it a contact (below).

The terminal says what it knows and how well. Each panel names its source and its delay (`live`, `daily`, `weekly`,
`monthly`) and the date a daily value describes. A value it could not refresh stays on screen, dimmed, marked `stale`
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
says so the first time, and watch-only wallets never touch a public backend without your explicit consent.

**The SEC asks for a contact.** Company functions read SEC EDGAR, which refuses requests without a name and email in
the User-Agent. The terminal sends nothing to the SEC until you set one, and then the SEC receives it on every
request: `SET sec_user_agent Your Name you@example.com`.

Settings live in `~/.config/glimpse/terminal.toml`. Secrets (the Glimpse API key, the barkd token) live in the OS
keychain, never in that file, a log or a commit.

## Trading Glimpse

No key is needed to look around: all market data is public. To trade, press `L` and paste an API key (register at
glimpse.markets, complete verification, then Settings → Developer API Keys). A Glimpse key can trade. It cannot
withdraw, and it cannot create or list other keys.

`MKT`, `HM`, `SLIP`, `PORT` and `BOTS` are the screens the terminal has always had, with every key they have always
had. `f` `p` `B` reach them from a launchpad and `t` returns.

- **Markets and ladder (`MKT`).** One row per close with the market's median and 80% band; the ladder of price
  ranges with probability, odds net of fees, and your position. `j` `k` move, `h` `l` switch pane, `v` selects a
  range, `+` `-` size, `b` buys after a confirmation, `[` `]` change series (`BTC`, `BTC 1D`, `ETH`, `SOL`, `XAU`).
- **The forecast heatmap (`HM`, `f`).** Time runs across, price runs up, each character is one price cell of one
  close, denser where the market puts more probability (`·:-=+*#%@`), candles left of `NOW`, the median in cyan.
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

## Wallets (`WAL`)

- **The Glimpse account:** balance, exposure against its cap, open positions valued locally. Deposits and
  withdrawals stay on the website, because an API key cannot move funds.
- **Your own Ark wallet through [barkd](https://gitlab.com/ark-bitcoin/bark):** a self-custodial Ark, Lightning and
  on-chain wallet daemon you run. barkd alone holds the seed. The terminal only calls its API, and never the routes
  that reveal or delete a wallet. `SET wallet.barkd http://127.0.0.1:3000`, then `t` in `WAL` stores its token in the
  keychain. Receive with a QR code. A send shows the fee first, asks for the amount to be typed back, asks for a
  final confirmation, enforces a daily cap, refuses a destination on the wrong network, and is never retried. Try
  it on signet first.
- **Watch-only:** an xpub, ypub, zpub or output descriptor. Addresses are derived on this machine (gap limit 20) and
  looked up only on your own backend or through Tor, unless you explicitly allow otherwise.

## Bots (`BOTS`, `B`)

The model zoo from the Glimpse Forecast Lab: 130 forecasting models that run on this computer from public Coinbase
candles. Each row says what that bot believes about the nearest close right now. `i` opens a model's account of
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
