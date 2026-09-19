# Glimpse Terminal: the Bitcoin terminal

A build brief for Claude Code running Claude Fable. It turns glimpse-tui into a keyboard-driven terminal for Bitcoin and the markets around it, in the spirit of the Bloomberg Terminal and Godel, built entirely on data anyone can fetch for free.

What it becomes:

- **A look inside Bitcoin.** The mempool as it fills, the next block as it forms, every block, transaction and address, mining, difficulty, Lightning, and a price read from the blockchain itself.
- **On-chain research.** Tens of thousands of open time series from the Bitcoin Research Kit, searchable and chartable from the command line, in the manner of Newhedge.
- **The markets around Bitcoin.** Gold, USD/JPY, the S&P 500, oil, rates and the dollar: live where an open feed exists, daily where only official data exists, and labelled either way.
- **Companies.** Filings, financials and Bitcoin holdings for treasury companies, miners and ETFs, straight from SEC EDGAR.
- **Glimpse itself.** The forecast heatmap, an options view of every close, the bet slip, the portfolio, the bots, and a wallet.

How to use this file:

1. Save it as `TERMINAL.md` in the root of glimpse-tui, next to `PLAN.md` (and `JEV_STARS.md` if you have it).
2. Open Claude Code in the repo with Fable selected: run `/model` inside a session and pick Fable, or launch with `claude --model claude-fable-5-1`.
3. Paste the Phase 0 prompt from section 13. Review each phase before pasting the next one.

Every number in the mockups is illustrative.

---

## 1. Ground truth

### 1.1 The terminal today (commit `8ca2b18`)

- Python 3.12, Textual, one event loop (PLAN D1). Screens: markets and ladder, the forecast heatmap (`f`), the portfolio (`p`) and the bots (`B`). A `:` command line lives in `app.py:command_line`. `KEYMAP` in `chrome.py` generates the key panel.
- `api.py` wraps the Glimpse API: batches, markets, book, estimate, wallet (balance, exposure, maximum exposure), positions, summary, buy, sell. `PAIRS` maps BTC, ETH, SOL and XAU (via PAXG) to Coinbase.
- PLAN F4: an API key reaches 11 routes (wallet balance, four portfolio reads, six enter and exit calls). Deposits, withdrawals, trade history and the P&L chart are JWT-only. A key can trade. It cannot withdraw.
- PLAN F6: 60 requests per 60 seconds per key. F10: the Glimpse websocket pushes only on trades.
- PLAN D3: every order is priced locally, then gated by the server's estimate. D8: amounts show as ₿ (sats), times in UTC.
- `heatmap.py` and `botsview.chart()` already draw price charts in text, with a truecolor palette and a palette of exact xterm-256 entries.
- 435 tests pass offline.
- `JEV_STARS.md`, if built, adds a Jev news hub, the Stars drawings and a one-shot `T` trade. This brief reuses all three and works without them.

### 1.2 Open data, checked on 19 Sep 2026

| source | what it gives | access | notes |
|---|---|---|---|
| mempool.space | blocks, transactions, addresses, projected blocks, fees, RBF, mining, difficulty, Lightning | REST and WebSocket, no key | Rate-limited: HTTP 429, and repeat offenders are banned. Self-hostable (AGPL, one-click on Umbrel, Start9 and others). WebSocket: `{"action": "want", "data": ["blocks", "mempool-blocks", "stats"]}`, plus `track-tx`, `track-address`, `track-addresses`, `track-mempool-block` and `track-rbf`. |
| Bitview (Bitcoin Research Kit) | 61,764 on-chain series and 97 API operations at v0.11.2; mempool-compatible explorer paths; URPD; a price oracle computed from chain data alone | REST at `https://bitview.space`, no key | MIT. OpenAPI at `/openapi.json`. Zero-dependency Python client `bitview-client`. Self-host with `bitviewd` (Bitcoin Core with RPC and readable `blk*.dat`, about 290 GiB, 16 GB RAM recommended, serves `localhost:3110`). Series workflow: `/api/series/search?q=`, then `/api/series/<name>`, then `/api/series/<name>/<index>`. The project says never to invent a series identifier. |
| Blockstream Esplora | blocks, transactions, addresses, fees | REST, no key | No WebSocket, so poll. Self-hostable. |
| SEC EDGAR | company tickers, filings, XBRL financial facts, full-text search | JSON, no key | A descriptive User-Agent with contact details is required (HTTP 403 otherwise). At most 10 requests per second. `www.sec.gov/files/company_tickers.json`, `data.sec.gov/submissions/CIK##########.json`, `data.sec.gov/api/xbrl/companyfacts/CIK##########.json`, `efts.sec.gov/LATEST/search-index?q=`. |
| FRED | US macro series: rates, the Fed balance sheet, M2, spot oil, dollar indexes | CSV at `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>`, no key | A free key unlocks the API and the release calendar. Some third-party series carry their own copyright terms; FRED marks them. |
| US Treasury | the official daily par yield curve, bills, real yields | XML at `home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=<yyyy>` | Official, no key. |
| Pyth | real-time prices for crypto, FX, equities, metals, rates, commodities and economic data; history through Benchmarks | Hermes (`hermes.pyth.network/v2/price_feeds`, latest prices, a server-sent-events stream) and Benchmarks (`benchmarks.pyth.network`), no key | Asset types include `crypto`, `fx`, `equity`, `metal`, `rates`, `commodities` and `eco`. Check coverage and public rate limits in Phase 0. |
| barkd (Second's Bark) | a self-custodial Ark, Lightning and on-chain wallet daemon | REST on localhost, bearer token | MIT. Tags: wallet, lightning, onchain, boards, exits, fees, notifications. OpenAPI in the bark repository at `bark-rest/openapi.json`. |
| Stooq | delayed quotes and history | CSV | Now needs a key obtained through a CAPTCHA and enforces a daily quota. Optional only, never a default. |

Candidates to verify in Phase 0, and to drop if they fail: Coinbase Exchange, Kraken and Bitstamp public tickers and WebSockets; the Deribit public API (option summaries and the DVOL index); ECB reference rates; public Electrum servers; Bitnodes; the alternative.me Fear and Greed index (asks for attribution); CFTC Commitments of Traders; GDELT; ETF issuers' holdings files and their terms; Pyth's feed IDs for every instrument in section 4.4.

---

## 2. Rules for every phase

1. **Open by default.** Every default source is public and needs no key. The terminal works on first launch with nothing configured. Optional keys (FRED, TypeSafe, Stooq and others) only add depth.
2. **Say where every number came from.** Each panel shows its source, its delay (`live`, `delayed`, `daily close`, `weekly`) and its as-of time. A stale value is dimmed and says it is stale.
3. **Be a good citizen of every source.** Per-host rate limits, shared connection pools, caching, exponential backoff with jitter on 429 and 5xx, a descriptive User-Agent, and attribution where a source asks for it. No scraping of sites whose terms forbid it. No Binance endpoints. No paid APIs in the defaults.
4. **Self-hosting is first class.** Every Bitcoin backend is a URL in config: mempool, Bitview, Esplora, Electrum and Bitcoin Core RPC. One SOCKS5 proxy setting (Tor) covers all of them.
5. **Privacy.** Looking up an address or a transaction on a public backend tells that backend what you care about. The terminal says so the first time and offers the private route (your own node, or Tor). Watch-only wallets never touch a public backend without explicit consent.
6. **One event loop.** All network work is async. Parsing heavier than a few milliseconds runs in a worker thread. Panels render from cached state and never wait on the network.
7. **Money paths do not change.** Glimpse orders go through the existing Confirm and estimate gate. Wallet sends need the amount typed back and a final confirmation. Secrets follow the keychain pattern of `auth.py` and never reach a log. The terminal never holds a seed.
8. **Units.** Sats show as ₿ with USD beside them. Fees in sat/vB. Hashrate in EH/s. Time in UTC, with world clocks in the status bar.
9. **Offline tests.** Every source has recorded fixtures under `tests/fixtures/sources/<source>/`. No test touches the network.
10. **Budgets.** A panel redraw stays under 10 ms. `<GO>` paints from cache within 100 ms. A full launchpad stays under 400 MB resident.
11. **House style for new text.** Plain sentences, one idea per sentence, no em-dashes.
12. **Ask before guessing.** When this brief, the code and a live source disagree, stop and say so.
13. **Permissive dependencies.** New dependencies must carry MIT, BSD or Apache licences. Record each one and its reason in PLAN.md.

Work on a branch named `terminal`. Commit once per phase, after the full suite and `ruff` pass.

---

## 3. How it feels

### 3.1 The GO bar

Bloomberg grammar: `<TICKER> [<CLASS>] <FUNCTION> <GO>`, where Enter is GO.

- Focus it with `` ` `` (as in Godel) or `:` (as today). Esc leaves it.
- Examples: `BTC` opens the Bitcoin page. `MEMP` opens the mempool. `TX 8a4666…` opens a transaction. `ADDR bc1q…` opens an address. `BLK 967653` opens a block. `MSTR DES`, `MSTR FA` and `MSTR CF` open a company. `USDJPY GP` charts USD/JPY. `GP BTC XAU SPX` compares three assets on one normalised chart.
- Classes work like Bloomberg's yellow keys: `CRYPTO`, `CURNCY`, `CMDTY`, `INDEX`, `GOVT`, `EQUITY`, `ETF`, `SERIES`. An unambiguous ticker needs no class.
- Autocomplete as you type across instruments, SEC company tickers, Bitview series, functions and layouts. Tab completes, the arrows pick, Enter goes.
- History with ↑ and ↓, kept between sessions.
- A bare function code runs against the pane's current security.
- Digits pick numbered menu items on the focused pane, as on Bloomberg function pages.
- `HELP` lists every function by category. `HELP <FUNC>` opens its page. F1 works anywhere.
- `FIND <text>` searches everything the autocomplete searches, with more room.
- Input that parses as nothing goes to `ASK` (section 11).

### 3.2 Panes and launchpads

- Up to nine panes, tiled. Each pane runs one function against its own security.
- `ctrl-w` then `h` `j` `k` `l` moves focus; `s` and `v` split; `q` closes; `o` zooms one pane and back; `=` evens the sizes. Tab cycles panes, as in Godel.
- Pane header: the pane number, the function code as an amber chip, the security, then source and delay on the right.
- `LP <name>` loads a launchpad. `LP SAVE <name>` writes the current layout to `~/.config/glimpse/layouts/<name>.toml`.
- Shipped launchpads: `BTC` (the default), `CHAIN`, `MINER`, `MACRO`, `TRADER` and `TREASURY`.
- Today's screens become functions: `MKT` (markets and ladder), `HM` (the heatmap), `SLIP`, `PORT` and `BOTS`. Their keys keep working inside their panes.

### 3.3 Status bar and tape

- Top line: the brand, BTC price and change, the fastest fee, the tip height and its age, the Glimpse balance, and clocks for UTC and New York. Wider terminals add London and Tokyo and the session state of NYSE, CME and Tokyo.
- Second line: a ticker tape of the instruments in the tape list.
- A new block makes the height flash for two seconds.

### 3.4 Look

- Black background. Glimpse orange (#FF7D08) plays the role of Bloomberg amber. White for values, green and red for change, cyan for the market's median, violet for the Stars.
- Dense tables with right-aligned numbers and thin rules, like Bloomberg function pages.
- Charts in text: candles and lines as today, braille dots for high-resolution lines (two by four dots per cell), sparklines (`▁▂▃▄▅▆▇█`) inside tables, and half-block pixels (`▀` with separate foreground and background colours) for block pictures.
- Both palettes stay: truecolor, and exact xterm-256 entries.

### 3.5 The default launchpad (`BTC <GO>`)

```
 GLIMPSE  TERMINAL   BTC 81,608 +2.1%  FEE 2 s/vB  #967,653 7m       ₿182,340   UTC 18:32  NY 14:32 
 MEMP 38 MvB  SPX 6,612 +0.4%  USDJPY 147.82 -0.2%  XAU 3,684 +0.8%  BRENT 68.40 -1.1%  US10Y 4.12% 
 > BTC <GO>   BTC Bitcoin · CRYPTO   1 DES  2 MEMP  3 ONCH  4 TOP  OMON  FEES  BLK  MINE  TRSY      
┌1  GP  BTC · 1H ────────────────── coinbase+kraken · live ┐┌2  MEMP  mempool ────────── ws · live ┐
│ hourly to NOW, then the Glimpse market's median and 80%  ││ NEXT BLOCKS   fee s/vB    txs    eta │
│                                                          ││ #1 ████████  3-40    3.4k    ~9m     │
│                               │         ░░░░░░░░ ┤83,000 ││ #2 ███████▌  2-3     3.9k   ~19m     │
│                               │  ░░░░░░░░░░░░░░░         ││ #3 ██████▊   2       4.0k   ~29m     │
│                              ⢀│───────────────── ◂81,608 ││ +9 ███▏      1-2      41k    2h+     │
│                           ⣤⡤⠏⠉│░░░░░░░░░░░░░░░░░         ││                                      │
│                      ⢀⣀⣤⡟⠋⠁   │  ░░░░░░░░░░░░░░░         ││ FEES  next 3  30m 2  1h 2  eco 1     │
│                   ⡏⠙⠚⠉        │         ░░░░░░░░ ┤80,000 ││ POOL  38 MvB  52,114 tx  min 1.0     │
│         ⣤⡀ ⢀⣠⠏⠷⠶⠶⠚⠁           │                          ││ TIP   #967,653  7m  Foundry USA      │
│  ⣀⡀⣠⣄⣠⠏⠉⠁⠙⠋⠉                  │                  ┤79,000 ││       3,288 tx  fees ₿2.1M           │
│ ⠚⠁⠛⠁⠈⠁                        │                          ││ DIFF  1,204 blk  est +1.8%           │
│ 17 Sep 18:00        19 Sep 06:00  NOW       20 Sep 06:00 ││                                      │
└──────────────────────────────────────────────────────────┘└──────────────────────────────────────┘
┌3  ONCH  on-chain ───────────────── bitview.space · daily ┐┌4  TOP  news · filings ──── jev · sec ┐
│ realized price 51,840   MVRV 1.57   NUPL 0.36  Puell 1.08││ 12m ▲ ETF inflows extend to day five │
│ STH cost basis 76,210   SOPR 1.01   Mayer 1.15           ││ 33m ▲ MSTR 8-K: adds BTC to treasury │
│ chain oracle   81,470   read from the chain alone        ││  1h · Fed speaker: path is data-led  │
│ URPD · share of supply by the price it last moved at     ││  2h ▼ Brent jumps on supply outage   │
│ 85k ▏██████▌                                             ││  3h ▲ Hashrate prints a new record   │
│ 80k ▏████████████████████████████████▌   ◂ 81,608 spot   ││                                      │
│ 75k ▏████████████████████                                ││ CF  MSTR 8-K 33m  MARA 8-K 3h        │
│ 70k ▏█████████████▌                                      ││     COIN 10-Q 1d  IBIT N-PORT 2d     │
│ 65k ▏███████████████████▌                                ││                                      │
│ 60k ▏█████████                                           ││                                      │
└──────────────────────────────────────────────────────────┘└──────────────────────────────────────┘
 F1 help  ` GO  ctrl-w hjkl panes  ctrl-w o zoom  LP layouts  AL alerts  1-4 menu  T trade          
```

---

## 4. Architecture

### 4.1 Layout

```
src/glimpse_tui/
  term/
    gobar.py        parser, autocomplete, history, HELP, FIND
    registry.py     the function registry
    instruments.py  instruments, classes, resolution, the category lists
    panes.py        tiling, focus, zoom, launchpads
    tape.py         status bar, ticker tape, clocks, session states
    alerts.py       AL: rules, evaluation, notifications
  data/
    core.py         Source base: token buckets, two-level cache, backoff, provenance, health
    http.py         pooled clients per host, User-Agent, SOCKS5
    mempool.py      mempool-compatible REST and WebSocket (mempool.space, Bitview, self-hosted)
    esplora.py      the Esplora subset
    electrum.py     Electrum protocol over TLS: headers, fee histogram, scripthash subscriptions
    core_rpc.py     Bitcoin Core JSON-RPC, optional
    bitview.py      series catalog, series data, URPD, oracle
    prices.py       exchange tickers and WebSockets; a composite BTC price
    pyth.py         Hermes latest prices and stream; Benchmarks history
    fred.py         keyless CSV; the API when a key exists
    treasury.py     the official yield curve
    fx_ref.py       ECB reference rates
    sec.py          EDGAR tickers, submissions, company facts, full-text search, documents
    deribit.py      public option summaries and DVOL
  funcs/            one module per family: btc, chain, mining, onchain, markets, companies, glimpse, wallet, tools
  charts.py         candles, lines, braille, bars, sparklines, histograms, half-block pixels
  wallet/           the Glimpse account, barkd, watch-only
```

`heatmap.py` and `botsview.chart()` move onto `charts.py` where they share code, with no change in what they draw.

### 4.2 The data contract

```python
@dataclass(frozen=True)
class Provenance:
    source: str        # "mempool.space", "bitview.space", "pyth", "fred:DGS10", "sec:0001050446"
    fetched_at: float  # unix seconds
    as_of: float       # the time the value describes
    delay: str         # "live" | "delayed" | "daily" | "weekly" | "monthly"
    note: str = ""     # a licence or attribution line, when a source asks for one
```

- `Source` gives `get(path, params, ttl)` and `stream(...)`, a token bucket per host, a memory cache, a SQLite cache at `~/.cache/glimpse/terminal.db` for series and history, backoff, and health counters.
- Functions ask for capabilities such as `fees()`, `mempool_blocks()`, `block(h)`, `tx(id)`, `series(name, index)` and `quote(instrument)`. A resolver picks the first healthy provider in the user's order and records which one answered.
- Default Bitcoin order: mempool.space, then Bitview, then Esplora. With a node configured, Bitcoin Core RPC and Electrum come first for what they can answer.
- Streams: the mempool WebSocket for blocks, projected blocks and stats; an exchange WebSocket for BTC; Pyth's stream for other quotes. Streams reconnect with backoff and fall back to polling.
- Offline: the last good value stays on screen, dimmed, with its age.

### 4.3 Default request budgets

Start here and tune in Phase 0. Streams replace polling wherever a source offers one.

| host | budget | cache |
|---|---|---|
| mempool.space | 1 request per second sustained | blocks forever, a tip-dependent view until the next block |
| bitview.space | 2 requests per second; `/api/series/bulk` for several series | daily series for an hour, height series until the next block |
| sec.gov | 5 requests per second (half the SEC ceiling) | tickers for a day, filings for an hour, company facts for a day |
| fred.stlouisfed.org | 1 request per second | a day |
| home.treasury.gov | a few requests per day | until the next business day |
| hermes.pyth.network | one stream; REST at 1 request per second | live |
| exchange REST | 1 request per second per exchange | live |

### 4.4 Instruments

- `instruments.toml` ships in the package. The user's `~/.config/glimpse/instruments.toml` overrides it.
- Each instrument has a ticker, name, class, quote currency, decimals, trading session, and an ordered source list such as `pyth:<feed id>`, `ecb:JPY`, `fred:DEXJPUS`.
- Default category lists of up to ten:
  - `GLOBAL`: BTC, gold, S&P 500, Nasdaq 100, USD/JPY, EUR/USD, Brent, US 10-year, the dollar index, silver.
  - `INDICES`: S&P 500, Nasdaq 100, Russell 2000, Euro Stoxx 50, DAX, FTSE 100, Nifty 50, KOSPI, Hang Seng, Nikkei 225.
  - `STOCKS`: NVDA, AAPL, MSFT, GOOGL, AMZN, META, TSLA, AVGO, MSTR, COIN.
  - `COMMODITIES`: gold, silver, Brent, WTI, natural gas, copper, soybeans, corn, wheat, sugar.
  - `CRYPTO`: BTC, ETH, SOL and PAXG, plus whatever `instruments.toml` adds.
  - `TREASURIES` and `MINERS`: the company lists behind `TRSY` and `MINR` (section 9).
- Where no open feed carries an index level, show an ETF proxy during its session and label it (for example SPY beside FRED's daily S&P 500 close). Never pass a proxy off as the index.
- The dollar index is computed from its six currencies with ICE's published weights and labelled `computed`.

### 4.5 The function registry

```python
@dataclass(frozen=True)
class Function:
    code: str                    # "MEMP"
    name: str                    # "Mempool"
    category: str                # Bitcoin | On-chain | Markets | Companies | Glimpse | Wallet | Tools
    takes: tuple[str, ...]       # instrument classes it accepts; () for none
    summary: str                 # one line for HELP and autocomplete
    needs: tuple[str, ...]       # capabilities it reads, for SRC and graceful degradation
    make: Callable[..., Widget]
```

Every function has a `HELP` page in the zoo's documentation standard: what it shows, where each number comes from, how often it refreshes, and what it does when a source is down.

---

## 5. Bitcoin functions (Phase 2)

| code | shows | sources |
|---|---|---|
| `BTC` | The Bitcoin page: composite price (Coinbase, Kraken, Bitstamp) and the on-chain oracle price, 24-hour range, distance from the all-time high, market cap, supply, halving countdown, tip, hashrate, difficulty and the next adjustment, fees, mempool size, Lightning capacity, and the Glimpse market's median and 80% band for the next close | exchanges, mempool, Bitview, Glimpse |
| `MEMP` | Projected blocks as a live train with fee range, transaction count and ETA; the next block as a picture of every transaction in it; the fee histogram; mempool size, count and minimum fee | mempool WebSocket (`mempool-blocks`, `track-mempool-block`), Bitview `/api/v1/mempool/block-template` as fallback |
| `FEES` | Recommended and precise fees; fee rates of recent blocks by percentile; a calculator for a transaction of N inputs and M outputs by script type | `/api/v1/fees/recommended`, `/api/v1/fees/precise`, `/api/v1/mining/blocks/fee-rates/{period}` |
| `BLK [height or hash]` | Recent blocks: height, age, pool, transactions, size, weight, total fees, median fee, reward, audit health. One block: its fee-rate percentiles, its coinbase tag, and its transactions paged | `/api/v1/blocks`, `/api/v1/block/{hash}`, `/api/block/{hash}/txs/{start}`, audit endpoints where offered |
| `TX <txid>` | Inputs and outputs with script types, amounts, fee, fee rate, vsize, weight, RBF signalling and replacement timeline, CPFP package, confirmations, first-seen time, outspends, OP_RETURN payload shown as text when it decodes | `/api/tx/{txid}`, `/api/v1/cpfp/{txid}`, `/api/v1/tx/{txid}/rbf`, `/api/v1/transaction-times`, `/api/tx/{txid}/outspends` |
| `ADDR <address>` | Balance, received and sent totals, transaction history (confirmed and mempool), UTXOs, first and last seen; a button to watch it | `/api/address/{a}`, `/txs`, `/utxo` |
| `RBF` | Live replacements, with the fee bump and the time between versions | WebSocket `track-rbf`, `/api/v1/replacements`, `/api/v1/fullrbf/replacements` |
| `MINE` | Pools by share over 24 hours to 3 years: blocks, share, hashrate, empty blocks; one pool's recent blocks | `/api/v1/mining/pools/{period}`, `/api/v1/mining/pool/{slug}` |
| `HASH` | Network hashrate and difficulty history | `/api/v1/mining/hashrate/{period}` |
| `DIFF` | The current epoch: progress, blocks left, ETA, estimated change, previous change; past adjustments | `/api/v1/difficulty-adjustment`, `/api/v1/mining/difficulty-adjustments/{period}` |
| `HASHP` | Hashprice and miner revenue: subsidy, fees, fee share of revenue | computed (below) from blocks, rewards and price |
| `HALV` | Countdown to the next halving in blocks and estimated days; the issuance schedule | computed from the tip |
| `SUPL` | Circulating supply, issuance per day, annual inflation rate, UTXO set statistics | computed from the tip; Bitview series |
| `LN` | Lightning capacity, channels and nodes over time; nodes by country and ISP; top nodes | mempool Lightning endpoints |
| `NODE` | Reachable nodes by version and country | Bitnodes, if Phase 0 clears it |
| `ORCL` | The on-chain price: Bitview's oracle derives BTC/USD from round-dollar output amounts in each block. Shows it beside the exchange price, and draws the live output histogram with its spikes at $10, $20, $50 and $100 | `/api/oracle/price`, `/api/oracle/histogram/outputs/live` |

Details that matter:

- **The next block, drawn.** `track-mempool-block` streams the transactions of a projected block. Draw them as half-block pixels in template order: each pixel is a fixed slice of vsize, coloured by fee rate on a ramp from low to high. This is mempool.space's signature picture, in a terminal. When a block is mined, flash the tip and start the next picture.
- **Hashprice.** Revenue per unit of hashrate per day: `(subsidy + mean fees per block) × blocks per day × price ÷ hashrate`, shown in USD per PH/s per day and in ₿ per PH/s per day. Use the observed blocks per day over the window.
- **Supply.** Compute it from the tip height and the halving schedule. Label it as the protocol maximum issued, since some coins are provably unspendable.
- **Addresses and privacy.** The first lookup on a public backend shows one line: "This lookup goes to mempool.space. Use your own node or Tor to keep it private (`SET`)."

---

## 6. On-chain research (Phase 3)

| code | shows | sources |
|---|---|---|
| `FLDS <words>` | Series search, like Bloomberg's field search: name, description, indexes, latest value. Enter charts the series | `/api/series/search`, `/api/series/{name}` |
| `GP <series or tickers>` | Charts any series or instrument: line or candles, log or linear, height, daily, weekly or monthly index, several series on one chart, a secondary axis | Bitview, price sources |
| `ONCH` | A dashboard of the classic cycle metrics: realized price, MVRV, NUPL, SOPR, supply in profit, short-term and long-term holder cost basis, Puell multiple, Mayer multiple, liveliness | Bitview, via `onchain.toml` |
| `URPD` | The UTXO realized price distribution: the share of supply by the price at which it last moved, with spot marked. Cohorts and dates are selectable | `/api/urpd`, `/api/urpd/{cohort}/dates`, `/api/urpd/{cohort}/{date}` |
| `WAVE` | HODL waves: supply by age band over time, drawn as stacked bands | Bitview age-range series |
| `CYC` | Valuation bands on the price chart: realized price, holder cost bases, the 200-day moving average | Bitview, prices |
| `CORR` | Rolling correlations of BTC with the S&P 500, gold, the dollar index, the 10-year yield and the Nasdaq 100 | computed from daily closes |

- Never invent a series identifier. In Phase 0, search Bitview for each metric `ONCH` needs, inspect the candidates, and write the chosen identifiers into `onchain.toml` with a comment on why each one fits.
- Mayer multiple is price over its 200-day simple moving average. Compute it locally if no series carries it.
- Every on-chain panel says `daily` and names the day it describes.

---

## 7. Markets and macro (Phase 4)

| code | shows | sources |
|---|---|---|
| `QM [list]` | Quote monitor: watchlists with last, change, percent change, day range, a sparkline, source and delay. Lists live in config | Pyth, exchanges, FRED |
| `WEI` | World equity indices by region, with change, percent change and year to date | Pyth where a feed exists, ETF proxies, FRED daily closes |
| `FX` | Major pairs including USD/JPY, the computed dollar index, and BTC priced in each currency | Pyth FX, ECB reference rates |
| `GLCO` | Commodities: gold, silver, Brent, WTI, natural gas, copper and the grains; gold and silver priced in BTC | Pyth metals and commodities, FRED spot series, PAXG |
| `RATES` | The Treasury curve today against a week, a month and a year ago; the 2s10s spread; fed funds; SOFR; real yields; 10-year breakevens | Treasury XML, FRED |
| `MACRO` | Liquidity: the Fed balance sheet, the Treasury General Account, overnight reverse repo, M2, and net liquidity | FRED `WALCL`, `WTREGEN`, `RRPONTSYD`, `M2SL` |
| `ECO` | Latest economic prints; with a FRED key, the release calendar | Pyth `eco` feeds, FRED |
| `HMAP` | A performance heatmap across the five category lists over a day, a week, a month and the year to date | computed |
| `RV` | Relative value: BTC against gold, BTC market cap against gold's, BTC in ounces | computed; the gold stock is a sourced constant in config |
| `DVOL` | BTC implied volatility from Deribit against the volatility implied by Glimpse's closes | Deribit public API, Glimpse |

- Net liquidity is `WALCL − WTREGEN − RRPONTSYD`. FRED publishes `WALCL` and `WTREGEN` in millions of dollars and `RRPONTSYD` in billions. Convert before subtracting, and cover it with a test.
- FX and index data from official daily sources arrive a day or more late. The panel says so.
- A proxy is never presented as the instrument it stands in for.

---

## 8. Glimpse functions (Phases 1 and 6)

| code | shows |
|---|---|
| `MKT` | Today's markets and ladder screen |
| `HM` | Today's forecast heatmap, with the Stars when `JEV_STARS.md` is built |
| `SLIP`, `PORT`, `BOTS` | Today's bet slip, portfolio and bots |
| `OMON` | An options chain read off each close's distribution: for every strike on a bin edge, the digital call price P(close above K) and the digital put price, and range prices between strikes. An implied volatility per close from a lognormal fitted to the distribution, a term structure across closes, and the Greeks of each digital under that fit. Deribit's implied volatility for the nearest expiry sits beside it. Enter on a strike preselects the matching range on the heatmap for the normal bet slip |
| `GIV` | Glimpse's implied volatility term structure against Deribit's DVOL |
| `WAL` | Wallets (section 10) |

`OMON` hands its selection to the heatmap as a box, and the existing Confirm and estimate gate handles the order.

---

## 9. Companies (Phase 5)

| code | shows | sources |
|---|---|---|
| `DES <ticker>` | Name, SIC industry, exchange, fiscal year end, shares outstanding, price, market cap, Bitcoin held when known, key financials, the latest filings | SEC submissions and company facts, Pyth |
| `FA <ticker>` | Standardised income statement, balance sheet and cash flow, annual and quarterly, each line tied to the filing it came from | SEC company facts |
| `CF <ticker>` | Filings list (10-K, 10-Q, 8-K, S-1, DEF 14A, 13F, Form 4), sortable and filterable. Enter opens the primary document as clean text inside the pane | SEC submissions and archives |
| `CFS <words>` | Full-text search across filings, with form and date filters | `efts.sec.gov/LATEST/search-index` |
| `TRSY` | Bitcoin treasury companies: BTC held, the as-of date and the filing it came from, BTC value, market cap, mNAV, BTC per share, 30-day sparkline | curated `treasuries.toml`, SEC, Pyth |
| `MINR` | Public miners: price, market cap, BTC held, self-reported hashrate from monthly updates, performance against BTC | curated `miners.toml`, SEC, Pyth |
| `ETF` | US spot Bitcoin ETFs: price, volume, premium or discount where NAV is available, holdings and flows where an issuer's terms allow | Pyth, issuer files that Phase 0 clears |
| `N [ticker]` | News for a ticker or a topic, plus its filings as they land | the Jev news hub, SEC |

- mNAV is market cap divided by the value of the Bitcoin held. Market cap is shares outstanding (the `dei` shares-outstanding fact from the latest filing) times price. Show the as-of date of each input.
- Crypto holdings under the post-2023 US GAAP rules appear as their own XBRL elements. Find the elements by searching real company-facts files (start with MSTR's), and record the chosen elements in `sec.py` with a test on a fixture.
- `treasuries.toml` and `miners.toml` list each company, its CIK or foreign exchange code, and where its holdings are disclosed. Non-US companies keep a manual entry with the source link and date until an open feed exists.
- The SEC User-Agent comes from config (`sec_user_agent = "Name email"`). The terminal asks for it the first time a company function runs and explains why.

---

## 10. Wallet (Phase 6)

`WAL` shows three kinds of wallet side by side.

**The Glimpse account** (API key): balance, exposure and its cap, open positions with value and P&L computed locally. Deposits and withdrawals are JWT-only (F4), so the pane links to the website for them. Section 12 lists the backend change that would bring deposits into the terminal.

**A self-custodial Ark wallet through barkd.** The terminal talks to a barkd daemon the user runs: a base URL and a bearer token, stored like the Glimpse key.

- Balance: spendable, pending, and on-chain.
- Receive: an Ark address, a Lightning invoice for an amount, or an on-chain address, each with a QR code drawn in half blocks (for example with `segno`, BSD-licensed).
- Send: paste a Lightning invoice, an Ark address or an on-chain address. The terminal shows the fee estimate first, asks for the amount to be typed back, then asks for a final confirmation. A per-day send cap lives in config.
- Boards and exits: status, and warnings as VTXO expiry approaches.
- History, and barkd's notifications on the status line.
- barkd alone holds the wallet's seed. The terminal only calls its API.

**Watch-only.** An xpub, zpub or output descriptor tracked through the user's Electrum server or node. Addresses are derived locally with a well-reviewed, permissively licensed library, with a gap limit of 20. Shows balance, UTXOs and the last movement. Derived addresses never go to a public backend without consent.

A deposit invoice from Glimpse, once the API can issue one, can be paid from barkd in one confirmed step.

---

## 11. Tools (Phase 7)

| code | does |
|---|---|
| `AL` | Alerts on price, fees, blocks, difficulty, mempool size, filings, news and Glimpse odds. Examples: "BTC below 75,000", "next-block fee under 3 sat/vB", "a block from a named pool", "MSTR files an 8-K", "odds on 80,000 to 82,000 at the 16:00 close above 20%". Fires on the status line with the terminal bell |
| `NOTE` | Notes attached to a security or a pane |
| `CALC` | Sats and USD, transaction fee from size and rate, Kelly stake from probability and odds |
| `EXP` | Exports the focused pane's table or series to CSV under `~/Downloads/glimpse/` |
| `SRC` | Every source: health, latency, budget left, last error, what it serves, and the switch to a self-hosted URL |
| `SET` | Backends, optional keys, Tor, tape list, theme, clocks |
| `ASK <words>` | Plain English to a function. With a TypeSafe key, route through Jev's function-calling pattern; without one, a keyword router. It always shows the command it chose before running it |

---

## 12. Open questions for James and the backend

1. Can API keys gain a deposit-only scope that creates a Lightning invoice? Withdrawals would stay on the website.
2. Can API keys read `ended-trades` and `pnl-chart` (PLAN open question 1), so `PORT` can show history?
3. Should the terminal compute the Conviction Report's index live? It needs the report's exact definitions of concentration, agreement and persistence.
4. Which ETF issuers' holdings files may the terminal read, and at what frequency?
5. Default tape list and launchpad order.

---

## 13. Phases and prompts

Each phase ends with a check, a full test run, `ruff`, a commit, and a stop for review.

**Phase 0: orient and verify.** Read everything in section 1.1. Run the baseline. Fetch every source in section 1.2 and every candidate live; record endpoint, auth, limits, terms, latency and a sample response in `SOURCES.md`, and save the samples as test fixtures. Map every instrument in section 4.4 to at least one source, or mark it unavailable. Search Bitview for the `ONCH` metrics and draft `onchain.toml`. Append "6d. The Bitcoin terminal" to PLAN.md with decisions from D30. Change no behaviour.

**Phase 1: the shell.** The GO bar, the registry, `HELP` and `FIND`, panes and launchpads, the status bar and tape, `SRC` and `SET`, the data core with provenance, and today's screens ported as `MKT`, `HM`, `SLIP`, `PORT` and `BOTS` with no change in behaviour. Done when the old keys still work inside their panes, `LP BTC` opens four panes, and the tape ticks from live sources.

**Phase 2: inside Bitcoin.** The mempool, Bitview, Esplora, Electrum and Core RPC clients and the functions of section 5. Done when `MEMP` streams the next block as a picture, `TX` explains any transaction, `BLK` pages through a block, and switching the backend to a self-hosted URL works without a restart.

**Phase 3: on-chain research.** `charts.py`, `FLDS`, `GP`, `ONCH`, `URPD`, `WAVE`, `CYC` and `CORR`. Done when any Bitview series can be found, charted and compared from the GO bar.

**Phase 4: markets and macro.** Pyth, FRED, Treasury, ECB and Deribit clients and the functions of section 7. Done when `LP MACRO` shows the curve, liquidity, FX, commodities and indices with honest labels for every delay.

**Phase 5: companies.** The EDGAR client and the functions of section 9, with the in-pane filing reader. Done when `MSTR DES`, `MSTR FA`, `MSTR CF` and `TRSY` work from the GO bar, with every number's source and date visible.

**Phase 6: wallet and Glimpse depth.** `WAL` with the Glimpse account, barkd and watch-only; `OMON` and `GIV`. Done when a signet or regtest barkd can receive and send through the terminal with every confirmation in place, and `OMON` hands a box to the heatmap.

**Phase 7: tools and polish.** `AL`, `NOTE`, `CALC`, `EXP`, `ASK`, both palettes checked at 80×24, 132×36 and 200×58, the README rewritten around the GO bar, and a demo script that walks a new user through `BTC`, `MEMP`, `TX`, `ONCH`, `MACRO`, `TRSY` and `HM` in two minutes.

### Prompts

**Phase 0**

```
You are working in the glimpse-tui repository. Read TERMINAL.md in the repo root first, then README.md and PLAN.md (and JEV_STARS.md if present).

Do Phase 0 of TERMINAL.md and nothing else:
1. Run uv sync, uv run pytest and uv run ruff check . and record the baseline.
2. Fetch every source in section 1.2 and every candidate listed under it. For each, record endpoint, auth, limits, terms, latency and a sample response in SOURCES.md, and save the samples under tests/fixtures/sources/. Drop anything that fails or whose terms forbid this use, and say why.
3. Map every instrument in section 4.4 to at least one working source, or mark it unavailable.
4. Search Bitview for each ONCH metric and draft onchain.toml. Never invent a series identifier.
5. Append section 6d to PLAN.md: decisions from D30, verified facts, open questions.

Change no behaviour. Work on a branch called terminal. When you finish, stop and show me SOURCES.md, the PLAN.md diff, the instrument map, and anything in the brief that disagrees with what you found.
```

**Phase 1**

```
Continue with Phase 1 of TERMINAL.md: the shell. Build the GO bar, the function registry, HELP and FIND, panes and launchpads, the status bar and tape, SRC and SET, and the data core with provenance. Port today's screens as MKT, HM, SLIP, PORT and BOTS with no change in behaviour. Follow every rule in section 2. Stop at the Phase 1 check, run the full suite and ruff, commit, and show me LP BTC at 132x36.
```

**Phase 2**

```
Continue with Phase 2 of TERMINAL.md: inside Bitcoin. Build the mempool, Bitview, Esplora, Electrum and Core RPC clients and every function in section 5, including the live next-block picture in MEMP. Record fixtures for every endpoint you use. Stop at the Phase 2 check, run the full suite and ruff, commit, and show me MEMP, TX on a recent transaction, and BLK on the tip.
```

**Phase 3**

```
Continue with Phase 3 of TERMINAL.md: on-chain research. Build charts.py, FLDS, GP, ONCH, URPD, WAVE, CYC and CORR on Bitview. Use only identifiers from onchain.toml or from a live search. Stop at the Phase 3 check, run the full suite and ruff, and commit.
```

**Phase 4**

```
Continue with Phase 4 of TERMINAL.md: markets and macro. Build the Pyth, FRED, Treasury, ECB and Deribit clients and every function in section 7. Label every delay and every proxy. Stop at the Phase 4 check, run the full suite and ruff, commit, and show me LP MACRO.
```

**Phase 5**

```
Continue with Phase 5 of TERMINAL.md: companies. Build the EDGAR client with the configured User-Agent and a 5 requests per second budget, then DES, FA, CF with the in-pane filing reader, CFS, TRSY, MINR, ETF and N. Stop at the Phase 5 check, run the full suite and ruff, commit, and show me MSTR DES, MSTR FA and TRSY.
```

**Phase 6**

```
Continue with Phase 6 of TERMINAL.md: WAL with the Glimpse account, barkd and watch-only, then OMON and GIV. Test barkd against signet or regtest only. Every send needs the fee shown, the amount typed back and a final confirmation. Stop at the Phase 6 check, run the full suite and ruff, and commit.
```

**Phase 7**

```
Continue with Phase 7 of TERMINAL.md: AL, NOTE, CALC, EXP and ASK, then polish. Check both palettes at 80x24, 132x36 and 200x58, rewrite the README around the GO bar, and write the two-minute demo script. Run the full suite and ruff, and commit.
```

---

## Appendix A: more mockups (illustrative numbers)

**The GO bar, autocompleting `M`.**

```
 > M▏
   MSTR    Strategy Inc · EQUITY · Nasdaq                          DES  FA  CF  GP  N  TRSY
   MSFT    Microsoft Corp · EQUITY · Nasdaq                        DES  FA  CF  GP  N
   MARA    MARA Holdings · EQUITY · Nasdaq                         DES  FA  CF  GP  MINR
   MEMP    function · the mempool: next blocks, fee bands, RBF
   MINE    function · mining pools by share, blocks and hashrate
   MACRO   function · Fed balance sheet, TGA, reverse repo, M2, net liquidity
   MKT     function · Glimpse markets and the ladder
```

**`TRSY`.**

```
┌1  TRSY  Bitcoin treasury companies ────────────── holdings: filings · prices: pyth · delayed ┐
│  #  company              ticker    BTC held   as of    BTC value    mkt cap   mNAV   30d     │
│  1  Strategy             MSTR       640,000   15 Sep     $52.2B     $91.4B    1.75  ▁▂▃▅▆▇   │
│  2  Twenty One Capital   XXI         43,500   30 Jun      $3.5B      $6.0B    1.69  ▅▄▃▃▂▂   │
│  3  Metaplanet           3350 TYO    20,100   01 Sep      $1.6B      $4.1B    2.50  ▃▂▂▁▂▃   │
│                                                                                              │
│  enter opens the filing behind the holding · D opens DES · each row shows its own as-of date │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

**`WAL`.**

```
┌1  WAL  wallets ──────────────────────────────────────────────────────────────────────────────┐
│ GLIMPSE ACCOUNT  key glp_live_…a1b2   balance ₿182,340 ($148.80)   at risk ₿12,000 of ₿100k  │
│   3 open positions · value ₿14,210 · P&L +₿2,210         deposits and withdrawals: website   │
│                                                                                              │
│ ARK WALLET  barkd at localhost:3535   spendable ₿1,250,000   pending ₿0   on-chain ₿310,000  │
│   r receive  s send  b board  x exits  h history                 next VTXO expiry in 21 days │
│                                                                                              │
│ WATCH-ONLY  cold storage · zpub…9f3k · your Electrum server over Tor                         │
│   ₿52,110,000 ($42,526)   14 UTXOs   last movement 412 days ago                              │
│                                                                                              │
│ RECEIVE  Lightning · ₿50,000 ($40.80) · expires in 59m                                       │
│   █▀▀▀▀▀█ ▀▄█▀ █▀▀▀▀▀█   lnbc500u1p…q9x8                                                     │
│   █ ███ █ █▄▀▄ █ ███ █   c copy   esc close                                                  │
│   █ ▀▀▀ █ ▄▀█▄ █ ▀▀▀ █                                                                       │
│   ▀▀▀▀▀▀▀ █ ▀ ▀▀▀▀▀▀▀                                                                        │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Appendix B: configuration

```toml
# ~/.config/glimpse/terminal.toml
tape = ["BTC", "MEMP", "SPX", "USDJPY", "XAU", "BRENT", "US10Y", "DXY"]
clocks = ["UTC", "America/New_York", "Europe/London", "Asia/Tokyo"]
default_launchpad = "BTC"
socks5 = ""                                # "socks5h://127.0.0.1:9050" routes every request through Tor
sec_user_agent = ""                        # "Your Name you@example.com", asked for on first use

[bitcoin]
order = ["mempool", "bitview", "esplora"]  # prepend "core" and "electrum" when you run a node
mempool = "https://mempool.space"
bitview = "https://bitview.space"
esplora = "https://blockstream.info"
electrum = ""                              # "ssl://your-server:50002"
core_rpc = ""                              # "http://127.0.0.1:8332", cookie auth by default

[keys]                                     # optional; the keychain holds the values
fred = false
typesafe = false
stooq = false

[wallet]
barkd = ""                                 # "http://127.0.0.1:3535"; the token lives in the keychain
send_cap_sats_per_day = 1_000_000
watch = []                                 # output descriptors or xpubs, with labels
```

---

## Appendix C: glyphs

| glyph | use |
|---|---|
| `⠁` to `⣿` | braille line charts, two by four dots per cell |
| `▁▂▃▄▅▆▇█` | sparklines and bars |
| `▀` | half-block pixels for block pictures and QR codes |
| `░` `▒` | forecast bands and selections |
| `▲` `▼` | direction |
| `◂` | spot on a price axis |
| `✦` `✧` | the Stars (from `JEV_STARS.md`) |

Every glyph must measure one cell in Rich's `cell_len`. Add a test that fails on any double-width character in the terminal's output.