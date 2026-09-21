# Glimpse Terminal — PLAN

A Glimpse terminal, in the terminal. View every market's forecast, price a range, buy it,
watch the portfolio, run a bot. Works over SSH. Python 3.12, Textual, `glimpse-markets` SDK.

Verified against the live API, SDK 0.1.0 source, `main_glimpse_service` and
`glimpse-v2-frontend` on 2026-09-17.

## 1. Screens

One screen, three panes, no menus. Everything is a key.

```
GLIMPSE  Daily BTC   ₿ 182,340   exp 12,000/100,000   key glp_live_…a1b2        14:02:11 UTC
┌ MARKETS ───────────────────────┐┌ 2026-09-18 · closes 9h 58m · vol ₿7,991 ─────────────────┐
│ close        median   80% band ││ range            prob   odds    ▏distribution            │
│▸18 Sep 00:00  76,551  76k–77k  ││ 78,000–79,000    2.1%  45.1×   ▏█                        │
│ 19 Sep 00:00  76,210  72k–80k  ││ 77,000–78,000    7.5%  12.6×   ▏████                     │
│ 20 Sep 00:00  75,900  70k–81k  ││▸76,000–77,000   60.8%  1.18×   ▏████████████████████████ │
│ …                              ││ 75,000–76,000    6.2%  15.2×   ▏███                      │
└────────────────────────────────┘└──────────────────────────────────────────────────────────┘
┌ TICKET  75,000–78,000 · 3 bins ────────────────────────────────────────────────────────────┐
│ 21 contracts   prob 74.5%   cost ₿2,042   payout ₿2,058   profit +₿16   odds 1.01×  +0.8%  │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
 j/k move  h/l pane  v select range  +/- size  b buy  p portfolio  B bots  L login  ? help  q quit
```

- **Markets** (left): nearest-expiry markets of the chosen batch, each with the market's median
  and 80% band. This column *is* the forecast of the future: read down it to see the path.
- **Ladder** (right): price bins of the focused market around the mode, with probability,
  decimal odds and a bar. The frontend's Odds Chain, which is already a text table.
- **Ticket** (bottom): live as the selection or size changes, priced locally, no network.
- **Portfolio** (`p`): positions with cost, value, P&L; `x` sells. **Bots** (`B`): start/stop, log.

## 2. Keys

| | |
|---|---|
| `j` `k` `↓` `↑` | move |
| `h` `l` `←` `→` `tab` | switch pane |
| `gg` `G` | top / bottom · `ctrl-d` `ctrl-u` half page · `zz` recentre on the mode |
| `[` `]` | previous / next series (BTC, BTC 1D, ETH, SOL, XAU) |
| `v` | start range selection; move to extend; `esc` clears |
| `+` `-` | contracts ×/÷ 2 · `s` type an exact size or a sat budget (`500s`) |
| `b` `enter` | buy (confirm with `y`) |
| `p` `B` `L` `r` `?` `q` | portfolio · bots · login · refresh · help · quit |

## 3. Facts the code rests on

| # | Fact | Source |
|---|---|---|
| F1 | `yes_price` is rounded to whole sats and reads 0 on most bins. Price from `shares` with LS-LMSR, `alpha = 2/(n ln n)`, payout 100. | Local ticket cost matched `POST /trades/estimate` to the millisat on 3 live tickets. |
| F2 | Fee is 2% **on top** of buy cost, 2% off sell proceeds, 2% off the winning payout (auto-paid at resolution). A winner nets 98 sats. | `handlers.go:1873`, `settlement/service.go:173` |
| F3 | The website ticket shows payout as gross 100/contract. **The terminal shows net 98**, so its ROI reads slightly lower than the site. Deliberate. | `NMarketTradeSidebar.tsx:117` vs F2 |
| F4 | Only 11 routes accept an API key: wallet balance, four portfolio reads, six enter/exit calls. Trade history, P&L chart and key management are JWT-only. Keys are minted on the website only. | `nmarket/api/routes.go:104-156` |
| F5 | Portfolio row values are millisats; `wallet-balance.balance` is millisats, its exposure fields are sats. | `portfolio/service.go:94-106`, `walletController.go:133` |
| F6 | 60 req / 60 s per key, 429 with `Retry-After`. Public reads are not limited by this service. | `ratelimit/limiter.go:19` |
| F7 | No slippage guard, no idempotency key. A timeout mid-trade may have filled. | grep of `internal/`; SDK `GlimpseAmbiguousTradeStateError` |
| F8 | v2 list endpoints page on `limit`/`offset`; the SDK's `page` args are ignored. v2 rows carry `shares`, so the whole market list is priced from one paged call. | live call |
| F9 | The SDK never retries, and its rate limiter blocks the thread. | `ratelimit.py`, `_base.py` |
| F10 | Websocket pushes only on trades; SDK stream has no reconnect. | 60 s live silence; `streaming.py` |

## 4. Decisions

- **D1 Textual + AsyncClient.** One event loop, no threads; F9 rules out the sync client.
- **D2 Poll, don't stream (v1).** Focused book every 5 s, market list 60 s, portfolio 15 s:
  about 16 keyed calls a minute, a quarter of F6. The websocket (F10) is a later accelerator.
- **D3 Local pricing for display, server estimate as the gate.** Before every buy the terminal
  calls the public estimate and refuses if it differs from the local ticket by more than
  0.5% + 5 sats (the book moved). This is the only slippage protection available (F7).
- **D4 Trades never retry.** On an ambiguous failure the terminal says so and reloads the
  portfolio rather than resubmitting (F7).
- **D5 Key storage.** `GLIMPSE_API_KEY`, else OS keychain, else `~/.config/glimpse/credentials`
  at 0600 for headless servers. Input is masked; the key is never logged, never in a traceback
  message, shown only as `glp_live_…last4`. Login validates by calling `wallet-balance`.
- **D6 Read-only without a key.** All market data is public, so the terminal is fully usable
  logged out; `b` explains how to get a key instead of failing.
- **D7 Bots run in-process, dry-run by default.** A bot is one function,
  `forecast(book, closes, hours) -> probs`, the same contract as `glimpse-bot`. The runner owns
  sizing (fractional Kelly, truthful caps), the estimate gate, a per-cycle and per-hour spend
  cap, and stop-before-close. Going live needs a key, the `m` toggle, and typing `LIVE`;
  the mode cannot change while the bot runs. Bots live in `~/.config/glimpse/bots/*.py`.
- **D7a Default series** is BTC (the hourly series); the last series viewed is remembered in
  `~/.config/glimpse/state.json` (UI state only, never the key).
- **D8 Units.** Sats everywhere on screen, `₿1,234`, matching the site's default. UTC everywhere.
- **D9 Display distribution** subtracts the subsidy floor before computing median and band
  (frontend `heatmapMath.computeRegion`); pricing never uses the adjusted numbers.

## 5. Layout

```
src/glimpse_tui/
  pricing.py   LS-LMSR, ticket, band. Pure.            (done, verified F1)
  auth.py      key storage.                            (done)
  api.py       SDK wrapper: paging, retry, records.    (done, public half verified live)
  fmt.py       sats, prices, odds, countdowns.
  app.py       Textual app, panes, keymap.
  bots.py      runner + built-in lognormal bot.
tests/         pricing goldens (recorded live estimates), fmt, runner sizing, app pilot.
```

## 6. Not in v1

Deposits and withdrawals, trade history and P&L chart (F4: JWT-only), the websocket, the
heatmap's brush selection, and past forecast columns under the history candles.

## 6a. Forecast heatmap (added 2026-09-17)

- **D10 Half blocks.** Each character is `▀`: foreground colours one price cell, background the
  cell below. Two cells per text row, so a one-character column is about square and 24 rows
  show 40+ price levels. Works in any truecolor terminal, including over SSH.
- **D11 The forecast is an ASCII density heatmap** (2026-09-17, third renderer). First a filled half-block
  heatmap: it brought James's machine down mid-bet. Then an outline of band/extent/median: cheap, but
  James disliked the shape. Then one plain character per cell from the ramp `·:-=+*#%@` on one absolute
  log scale of probability per bin (six steps 0.6%→10%, three 10%→100%) in four colour tiers, which D63 replaced
  with `░▒▓█` on one orange for the same reason. One cell
  per text row, column levels cached per data version, chart body cached on its full state key.
  Measured at 200×58 on Hourly BTC: full redraw 7 ms, ~14 KB a frame, 0 redraws while idle.
- **D11f The slip is two blocks and a button** (2026-09-17). Your prediction (editable) on top, then the
  ticket: cost, payout, profit, odds, ROI as five plain bold rows. Block numerals were tried and rejected as
  illegible (a TUI cannot scale its font). A RISK block (breakeven, EV at market odds, price impact, fill, fee)
  was tried and rejected as clutter, and its pricing code was removed with it.
- **D11g History is 30-minute candles on hourly series.** Coinbase serves no 30-minute bar, so 15-minute bars
  are fetched and paired (`api.resample`). A close is `hm_w` characters and a candle `hm_w / 2`, so time runs at
  one scale on both sides of NOW; viewport maths is in characters (`hm_pos`). One-character candles use a heavy
  rule for the body so neighbours do not fuse. At an odd column width the view falls back to hourly candles.
  Daily series keep daily candles. The readout adds σ and annualised IV from the 80% band (±1.2816σ).
- **D11e Vim, fully.** Counts, `g`/`z`/`Z`/`m`/`'` chords, `V` `o` `gv`, marks, `''`, `ctrl-e/y/f/b`, `/` and a
  `:` command line. The footer is a vim status line (mode, messages, showcmd) over the complete keymap
  for the current view, generated from `KEYMAP`, so the legend cannot drift from the bindings' list.
  Zoom moved from `i`/`o` to `zi`/`zo` so `o` could mean "other corner" as in vim.
- **D11c Refresh discipline.** One 60 s list timer (there were two, 30 s and 60 s). A refresh for a
  series never overlaps or cancels one in flight: `exclusive=True` used to cancel and restart, so on
  a link slower than the timer it re-downloaded ~4 MB and rebuilt ~250k objects forever. Closes whose
  shares did not change keep their `RowView` and `data_version` does not bump, so caches hold. The
  500 bin names, ids and parsed bins are one shared tuple per series, not one per close per refresh.
  Every live close loads (D22). The chart body is
  cached on its full state key; only the one-line readout is rebuilt per paint.
- **D11d Two palettes**, truecolor and one of exactly-representable 256-palette colours, tested to
  survive Rich's downgrade unchanged. Depth is chosen before Textual imports (`TEXTUAL_COLOR_SYSTEM`)
  from an explicit choice, then `COLORTERM`, then known terminals; `c` flips it and is remembered.
- **D11a Chart furniture.** A ruled grid of plain line characters on round prices (`nice_step`; the view
  shifts one cell if needed so a rule falls mid-row), day/week rules, a ticked price axis, thin-wick candles.
- **D11b Bet slip on the right** (`slip.py`), like the web sidebar, replacing the ticket under the
  chart at 118+ columns. Fields step the selection's edges; the chart and slip edit the same state
  (`hm_anchor` + cursor, or `anchor` + `b_cur`). Odds and ROI are drawn in three-row block numerals
  because a TUI cannot set font size. Secondary text was lifted from #888/#555 to #bdbdbd/#949494.
- **D12 History.** Coinbase candles at the series cadence, closed buckets only, left of a
  one-character NOW divider. The in-progress candle is not drawn over the live column (the web
  does): with one colour per cell it would hide the prices being traded. Spot is tagged instead.
- **D13 Box = one request, many markets.** `enter-multi-topic-multi-leg`, one leg group per close.
  Before sending, every close is re-estimated on the server and the order is refused if the
  total moved more than 0.5% + 5 sats. The server fills each close independently, so the
  terminal reports "filled k of n" with the first rejection. A box may span every loaded close (the server sets no
  limit on topics per request); the terminal sends it as requests of 24 closes each, so a timeout leaves at most
  24 closes in doubt, and reports how many were sent if a later request fails.
- **D14 Public reads use their own SDK client** (240 req/min limiter). The SDK throttles every
  call through one 60/min window, but F6 applies to keyed routes only; splitting them keeps
  heatmap polling and N pre-trade estimates from queueing behind, or starving, keyed calls.
- The heatmap prices from the market list (refreshed every 30 s while open), not per-market
  quotes; D13's gate is what makes that safe.

## 6b. Questions, small bets and the bot marketplace (added 2026-09-17)

- **D15 Markets are named by their question**, `BTC at 18 Sep 11:00 UTC` (`fmt.question`), on the ladder, the
  slip, every confirmation, the portfolio and the bot log. Series are tickers: `BTC` is the hourly series,
  `BTC 1D` the daily one (Bitcoin has two series, so one of them has to carry a mark), then `ETH`, `SOL`, `XAU`.
  State files that remember "Hourly BTC" are mapped on load.
- **F11 There is no 100 sat minimum on the route the terminal trades through.** `minTradeSats = 100` lives in
  `executeTradeWithBalance` (`handlers.go:1881`), which serves `POST /trades`. The terminal, like the SDK, enters
  through `enter-multi-topic-multi-leg` → `enterTopicCore` (`handlers.go:2655`), which has no minimum ticket and
  instead floors the commission at 1,000 msat. The terminal's floor was an assumption carried over from the lab's
  notes; it is removed, and `pricing.ticket` charges `max(2%, 1 sat)`. The public estimate does not apply the
  1 sat floor; the 5 sat tolerance of the pre-trade gate covers the difference. Read from the local checkout of
  `main_glimpse_service`; not yet confirmed by a live sub-100 order (section 8).
- **D16 The zoo** (`src/glimpse_tui/zoo/`). Every Forecast Lab model that needs only `bars_1h, spot` is ported,
  with its lab id, family, factors and description. The lab's machinery (5,000 bootstrap paths, a feature
  database, per-horizon regression with shrinkage) is replaced by a density over the standardised move on a
  grid (`core.Z`): signal models use the lab's `picture: conviction` rule (percentile of the signal in its own
  history → drift in σ on the family's clock, plus the family's exponential tilt); volatility models scale the
  width within [0.7, 1.6]; views and option payoffs are minimum-relative-entropy tilts with the lab's
  effective-sample floor; shaped processes simulate 4,000 paths; named distributions are written onto the bins.
  Scale is the lab's EWMA variance with the hour-of-day profile and a partial first hour. One change from the
  lab: signal strength uses a mid-rank, so a signal that is silent (exactly zero) reads as neutral rather than
  as its lowest reading ever. Dependencies added: numpy, pandas, scipy, imported only when the bots load.
  Not ported: anything needing funding, derivatives, DVOL, sentiment, ETF flows, options, other models'
  forecasts or downloaded weights.
- **D17 Data** is public Coinbase hourly candles, 2,000 hours, paged 300 at a time, cached in
  `~/.cache/glimpse/`, complete bars only. One `Feed` per asset is shared by the bots screen and every runner;
  the volatility fit and each model's signals are cached until a new bar closes. Reading all 130 bots against
  the live market took 1.2 s.
- **D18 The bots screen is the marketplace without the performance numbers.** Tabs are the marketplace's
  categories computed live from each bot's picture of the nearest close (bullish, bearish, sideways, volatile),
  rows show lean, width and distance from the market, the detail pane draws the bot's distribution over the
  market's. No APY, no investors, no track record: the terminal has none to show. Deploy is `enter`, `enter`.
- **D19 The runner manages what it opens.** A ledger on this machine (`bot-ledger.json`) records each bot's
  legs per mode (live, paper). Open cost never exceeds the budget; Kelly's stake for a market is a target, so
  what is already held there is subtracted; a held leg is sold when the market's net proceeds exceed the
  picture's value by `exit_edge`. Dry run keeps the same ledger on paper, so it behaves as the live bot would
  instead of logging the same order every cycle. `glimpse-tui run <bot>` is the same runner without the screen.
- **D20 The market rides on the bot's bars (2026-09-17).** Each price range in the bots detail pane shows the bot's
  chance, the market's price (raw price/100, what a contract costs, which is what `decide` compares against, not
  the normalised ladder figure), the gap in points, and one bar carrying both: orange where they agree, green where
  the bot is above the price, red shading where the price is above the bot. `BUYS` marks ranges that clear
  `decide`'s edge test at the default `min_edge`.
- **D21 Keys (2026-09-17).** `w a s d` are the arrow keys on every screen (remapped at the top of `on_key`, except
  as the second key of a chord, so `ma`, `'a`, `za` still work). That moved next / prev midnight from `w W` to
  `) (`, and set size and bot sort from `s` to `S`. The bottom key panel wraps a long group onto a second line
  instead of cutting it with `…`; on short terminals whole groups drop and `?` lists everything. The slip reads
  market probability, then your prediction, then the ticket and button pinned to the bottom edge.

- **D22 A week ahead, zoomed out (2026-09-17).** James wants the full seven days of hourly closes and every daily
  close, and to zoom out far enough to see all of it. The list loads every live close (`CLOSES_MAX` 400; live:
  168 hourly, ~172 per daily series), which replaces the 72-close default and the load-more-at-the-edge step. Summarising
  170 closes takes ~0.04 s and a full-screen redraw stays under 8 ms. `<` continues past one character a close into
  clock-aligned columns of 2/3/4/6/12 closes (hourly) or 2 days / a Monday week (daily): the column draws the mean
  per-bin probability of its closes and the middle close's median, the cursor steps a column at a time, and the
  cursor cell is a ticket on every close in it (readout `N CLOSES`). History resamples to the same span from
  Coinbase hourly candles (300 h) so it reaches back as far as the forecast reaches forward. Vertical rules step up
  from days to Mondays to month starts so they stay at least ~6 characters apart. `zf` picks the most detailed
  time scale that fits every close with a little history, and a price zoom holding every close's 80% band.

## 7. Open questions for James

1. F4 means a key cannot read trade history or the P&L series. Worth exposing
   `ended-trades` and `pnl-chart` to API keys so the terminal can show them?
2. F3: keep net-of-fee payout in the terminal, or match the site's gross figure?
3. Public sign-up URL to print on the login screen (assumed `https://glimpse.markets`).
4. The ladder's `prob` is the price normalised across all bins (prices sum to ~133, not 100).
   The website shows raw price/100, so its figure for the same bin reads higher (81% vs 61%
   on tomorrow's modal bin). Normalised is a true probability; raw is the site's number. Which
   should the terminal lead with? The ticket shows `breakeven` either way, which is the number
   a trader needs.

## 8. Status (2026-09-17)

Built and verified: everything in section 5 plus the heatmap (`heatmap.py`, `theme.py`), the zoo (130 models), the bots screen (`botsview.py`), the runner with its ledger, and the `bots` / `run` commands. 427 offline tests, ruff clean. All 130 models were run against live Coinbase candles and the live BTC market (no errors, 1.2 s for the pass), and a dry-run bot cycle priced real sub-100-sat tickets. Read-only paths,
pricing, paging and the dry-run bot were exercised against the live API. Layout checked at
132×36 and 80×24.

**Not yet exercised against the live API, because no key was used during the build:** login,
wallet, portfolio, buy (ladder and heatmap box), sell. They are covered by tests against a fake API whose shapes come
from the backend source (F4, F5), not from a live response. First live use should be one
small ticket, which also settles F11.

## 6d. The Bitcoin terminal (TERMINAL.md, branch `terminal`, started 2026-09-19)

### Revision (2026-09-20, eighth session): every position before the order, and three navigation bugs

James, on the seventh session's build: the power law is drawn too far out to read the present; `SPC b p` does
nothing on the front page; `j` and `k` sometimes move two windows instead of one; and, the main one, the bet slip
has no way to see each individual prediction. "The main priority is that all positions are visible before the
order is placed."

- **D73 Every position, priced, before anything is sent.** A box across the forecast was always several orders,
  one a close, each at that close's own price, but only their sum was ever shown. `app.legs` now returns a `Leg`
  per close (`hm_parts` keeps what `P.combine` used to throw away). The slip grows a `POSITIONS` section listing
  each close with its cost, payout, profit and ROI (`[` and `]` walk it, and the slip widens from 42 to 58
  columns to hold the extra ones); `P` opens `OrderPreview`, a scrolling table of every column of every
  position — chance, contracts, cost, payout, profit, odds, ROI — with a `TOTAL` row; and **the same table is
  the confirmation `b` asks for**, on one market and on many, so nothing can be bought that has not been seen
  priced line by line. `order_table` builds it once for both, so the preview and the confirmation cannot drift.
  The old `Confirm` dialog stays for logging out and for selling.
- **D74 The power law opens on the present.** The fit is still the whole history; how much of it is *drawn* is
  now separate (`<` and `>`, and `@4y` in a saved launchpad). The default is the last four years, the y range is
  framed on the price and the fitted line rather than on a ±2σ channel four times as tall, a yellow rule marks
  the price now, and the outer channel drops out when zoomed because five shallow braille lines read as hatching.
  The frame says both halves: "fit on 5,880 closes from 16 Aug 2010 · showing the last 4y of it".
- **D75 `j` and `k` move exactly one row.** `Workspace.move` added the horizontal and vertical distances between
  window centres. A cell is about three times wider than it is tall, so from `FEES` the key `k` scored `RATES`
  (two rows up, nearly above) better than `FX` (one row up, half a page across) and skipped a row. It now ranks
  the gap along the way you are going first and the distance across it second, which is what "up one" means.
  Checked at six widths from 80 to 240 columns.
- **D76 `SPC b p` works where there are buffers to walk.** `cycle_buffer` only offered buffers no window was
  showing, so on the front page — every buffer in a window — it said "No other buffer is open" to someone with
  sixteen of them on screen. It now walks the whole ring; `_put` already swapped two windows' pages when the
  target was on screen, which keeps one rectangle per page.

Tests: 711 pass offline.

### Revision (2026-09-20, seventh session): the odds are the front door, the bots leave the front page, everything can be priced in Bitcoin, and the power law

James: get rid of the bots on the terminal screen and order it Bitcoin forecast, Bitcoin odds, gold forecast, gold
odds, then hashrate, difficulty, news, global and the rest. The first page should be the odds on Bitcoin's next
hour, not the terminal, because "terminal is the secret world behind the curtain if people choose to investigate;
start easy with the odds page first". `?` could not be scrolled. Commodities, indices and equities should be
priced in Bitcoin ("gold should be XAUBTC or ₿5,440,000"). And a power law: a log-log chart of BTCUSD and XAUBTC
with the linear regression that produces it, showing the power law historically.

- **D65 The terminal opens on the odds.** `glimpse-tui` now opens the odds screen on Bitcoin's nearest hourly
  close with the cursor already on the ladder (`Terminal.pane` starts at 1, not 0), so the first thing anyone sees
  is one row an outcome with its chance and its payout, and `tab` is the bet slip. The front page is behind `t`,
  `--terminal`/`-t`, or `opens_on = "terminal"` in `terminal.toml`; `--odds` (and the old `--markets`) forces the
  odds back. The decision is `glimpse_tui.opening_launchpad(argv, cfg)`, which a test drives without a screen.
- **D66 The bots are off the front page, not out of the terminal.** The front page is now fourteen windows of
  market and two of power law, in James's order: `GP BTC 24H` + `DIST BTC`, `GP XAU 1D` + `DIST XAU`, `HASH` +
  `DIFF`, `NEWS` + `QM GLOBAL`, `PL BTC` + `PL XAUBTC`, `MACRO`/`RATES`/`ECO`, `FX` + `GLCO`, `FEES`. `CONS` and
  `BOT` keep their functions and move to a new shipped launchpad, `LP BOTS` (both on Bitcoin, both on gold, and
  the Bitcoin chart and odds under them); `B`, `BOTS <id>` and `glimpse-tui run <bot>` are unchanged, and
  `tests/test_swarm.py` drives `LP BOTS`.
- **D67 Anything can be priced in Bitcoin.** `Book.get` synthesises `<ticker>BTC` for any instrument it knows
  (`XAUBTC`, `SPXBTC`, `NVDABTC`, also `XAU/BTC`), with the single source `computed:ratio:<leg>`; the quote board
  divides the two legs into satoshis *after* the proxy pass, so gold through PAXG still works, and a ratio history
  is the two daily histories divided on the days both of them closed. Yields and on-chain series are excluded: a
  percentage does not divide. `$` on `QM`, `WEI` and `GLCO` swaps the whole table into that form, drops Bitcoin's
  own row and puts the bitcoin price on the head; the unit survives a saved launchpad as the word `SATS`, never
  `BTC`, because `QM BTC` is already a one-instrument watchlist. The front page's commodities window ships as
  `GLCO SATS`, so gold reads ₿5,453,863 an ounce without pressing anything; one word in `btc.toml` reverts it. `fmt.in_btc` is satoshis up to a coin and whole
  bitcoin above it. A ratio row has no day range and no sparkline of its own, and a monthly IMF average shows a
  dash for the change rather than one computed against the wrong month's bitcoin price.
- **D68 `PL`, the power law.** `funcs/powerlaw.py` fits `price = 10^a · days^n` by least squares on log10 of both,
  days counted from the genesis block, and draws it on log-log axes: the price in orange, the fit in cyan, dotted
  rules at ±1σ and ±2σ of the log residual (the outer pair drops out under fourteen rows, where five lines are mush). Above the chart: the exponent, R², the residual spread as a
  multiplier, the price now, the fitted value, and the gap in percent and in σ. `[` `]` move where the fit starts
  and the exponent moves with it (only the starts the loaded history actually reaches back past are offered); `T`
  adds what the line alone reads 1, 2, 4 and 10 years out. Live: BTC is n≈5.6, R²≈0.96 over 5,880 closes from
  16 Aug 2010; XAUBTC is n≈−3.6, R²≈0.70 over 2,513 closes from 20 Sep 2016. A fit needs both `MIN_POINTS` (200)
  and `MIN_DECADES` (0.15 of a decade of age); under that the page says how little history it has instead of
  printing a slope, which is what a year of PAXG against BTC would otherwise have produced (a confident n=+8.4).
- **D69 The chart grew a log x axis.** `charts.Plot(xlog=True)` maps x through log, `x_ticks` replaces the date
  axis with explicit labels (years, on `PL`) and `x_grid` draws vertical rules under the data. A test asserts that
  `10^a·d^n` comes out straight on log-log axes and bent on a linear one.
- **D70 Bitcoin's history past an exchange comes off the chain.** `quotes.history` for more than 700 days (Kraken
  serves 720 daily candles, Coinbase 300) reads Bitview's `price_close` from 2009 instead. Under that nothing
  changes, so `RV` and the charts are still exchange prices; `PL`'s `HELP` says the oracle is not a ticker.
  Recorded as `tests/fixtures/sources/bitview/series_price_close_day1_start0.json` (47 KB, whole history).
- **D71 Yahoo's `range=max` is not always daily.** With a symbol whose past is long (gold futures start in 1975)
  Yahoo answers a `max`+`1d` request with *monthly* points, which had silently turned XAUBTC into 166 monthly
  observations. `Yahoo.history` now checks the median gap and asks again over ten years when it is coarser than
  daily. Both responses are recorded (`chart_gcf_max.json` 268 points at 31-day gaps, `chart_gcf_10y.json` 2,516
  points at 1-day gaps), which is the evidence for the check.
- **D72 `?` scrolls.** The help modal holds its body in a `VerticalScroll`: `j` `k`, the arrows, `ctrl-j`/`ctrl-k`,
  `ctrl-d`/`ctrl-u`, page keys and `g`/`G` move it, a hint line at the bottom says so, and only `esc`, `q` or `?`
  close it, so a scroll key can never dismiss the page it is scrolling. `About` already worked this way; `Help`
  dismissed on any key, which is why James could not read past the first screen.

Tests: 698 pass offline, including `tests/test_funcs_powerlaw.py` and the priced-in-Bitcoin cases in
`tests/test_funcs_markets.py`.

### Revision (2026-09-19, sixth session): the forecast is one orange, and the odds show the whole distribution

James on the full forecast (`f`) against the front page's chart: the front page is the elegant one, and the
heatmap has too many symbols in too many colours. And the odds window shows four outcomes where it could show
the shape of the distribution.

- **D63 The heatmap is four shade blocks in one hue.** The ramp `·:-=+*#%@` in four colour tiers (faint, warm,
  hot, white) is now `░ ▒ ▓ █` — thin to solid — on one orange: `░` from the 0.6% seed floor, `▒` from 1.5%,
  `▓` from 3.9%, `█` above 10% a bin (`KNEE` 3 of 4 instead of 6 of 9; the log scale either side of the knee is
  unchanged, so the same probability still draws the same way in every column and at every zoom). Nothing reaches
  white: a bright cell is the mode, not an alarm. `heatmap.BAND` is tier 0 as hex, and `GP` paints its 80% band
  with it, so the front page's forecast and the full one are literally the same orange. The median stays cyan and
  `NOW` stays the orange rule, which is the pairing James liked. `FCST` inherits all of it; the legend, both help
  pages, the README and Appendix C say the new ramp. Drawing cost is unchanged: still a handful of colour runs a row.
- **D64 `DIST` fills the pane with outcomes.** The window was the 80% band plus half its width, which on hourly
  Bitcoin is four rows whatever the pane's height. It is now as fine as the market's own bins and as tall as the
  pane — twelve outcomes in the front page's window, sixteen full screen — centred on the band and the price now,
  coarsening to a round multiple of a bin only when the band would not otherwise fit. It stops at the band widened
  twice over, and at anything anyone has traded, so a market whose bins are coarser than its own distribution does
  not fill the pane with identical rows of untraded seed. Every row now carries the odds beside the probability
  (the pane is the odds window, and a 0.1% tail is worth reading at 1,500×), dropped when the pane is too narrow,
  and a non-zero row always keeps at least a sliver of bar.

### Revision (2026-09-19, fifth session): the front page reads top to bottom, and half of it is bots

James's review of the scrolling page: the ladder key `l` had to go, the sources on every frame looked like
advertising, gold was drawn unlike Bitcoin, the order of the page did not follow what matters, and the thing that
makes Glimpse Glimpse — that you can bring a machine to the market — was nowhere near the top.

- **D58 `f` is the forecast, `o` is the odds.** `MKT` is now `ODDS` ("the odds on every outcome, close by close"),
  reached with `o` from anywhere, and `l` no longer opens anything, so it only ever moves right. `f` still opens the
  forecast. On the page, `f` and `o` open the full screen for whatever series the focused window shows (`FCST`,
  `DIST`, `CONS`, `BOT`, and `GP` when its chart runs into a Glimpse market); `o` still belongs to a page that has an
  `o` of its own (`ORCL`). `registry.ALIASES` keeps `MKT` and `LADDER` working, and `o` on the odds screen is still
  "the other end of the range". The nav bar, the leader menu (`SPC t f o B p`), the key panel and both help pages say
  the same thing.
- **D59 The page is ordered by how much it matters** (16 windows, 8 rows): Bitcoin's next 24 hours beside the odds on
  its next hour · what the bots expect beside one bot at a time · gold beside the odds on its next close · hashrate
  and the difficulty epoch · the news beside world prices · dollar liquidity, the Treasury curve and the economic
  prints · currencies and commodities · fees last, where a trailing number belongs. `MAX_PANES` is 20, so the page
  filling sixteen of them still leaves room to split a window.
- **D60 The bots are half the top of the page.** `term/swarm.py` runs the whole zoo on this machine against one
  series and keeps one pass per asset per hourly bar; `CONS` reads the counts and the consensus, `BOT` reads one
  model at a time. The horizons are the next hour, 24 hours and 72 hours on an hourly series, and the next close, a
  week and a month on a daily one; the close nearest each horizon is taken once. The consensus is the mean of every
  model's distribution, with its median and 80% band beside the market's own; `they agree` is one minus the mean
  distance from a model to that consensus, and the same distance against the market's prices says how far apart the
  two are. A pass over 130 models and three closes costs about a second, because the models cache their signals per
  bar. `BOTS <model id>` opens the zoo on one model, which is what `enter` does on either window. The tests run the
  real models on a synthetic frame (`tests/conftest.py` replaces `swarm.make_feed` and narrows `swarm.catalog`), so
  the suite still reaches no network.
- **D61 A frame says how old its numbers are, never who served them.** Vendor names are out of the pane frames and
  out of the market tables (`QM`, `WEI`, `FX`, `GLCO`, `RATES`, `ECO`). `SRC` lists every source with its health, each
  function's `HELP` page says where its numbers come from, exports keep the source column, and the privacy line still
  names the host a lookup would go to. The frame keeps `live`/`daily`/`stale` and the dimming.
- **D62 Gold reads like Bitcoin.** `GP` draws the Glimpse forecast for any series whose closes the terminal has, on
  the window that matches how often that series closes: hourly for Bitcoin (`24H`, `1H`), 45 days back and 31 ahead
  for gold (`1D`). Charting gold charts PAXG, which is what the Glimpse market settles on, titled `Gold`, so the
  price line and the band are on one scale. `DIST XAU` sits beside it for the odds on the next close.

Tests: 656 pass offline, including `tests/test_swarm.py`.


### Revision (2026-09-19, second session): the terminal as a place to watch the world

James's review of the first build: too hard to navigate, too much Bitcoin plumbing, a header unlike every other screen, and
an Ark wallet with nothing behind it yet. What changed:

- **D43 No wallet.** `WAL`, `wallet/barkd.py`, `wallet/watch.py`, the `[wallet]` settings, `ADDR`'s `w` (watch an address, which
  only `WAL` read), and the `segno` and `embit` dependencies are gone. Trading is by Glimpse API key only (`L`), and `PORT` is the
  account. The barkd research is in git history at `90956d8`.
- **D44 One header.** The terminal uses `chrome.header`, the brand bar and function bar every other screen has. Under it one
  line, `tape.strip`, carries the markets that matter (BTC, gold, S&P 500, Nasdaq, DXY, 10Y, oil, EUR/USD, USD/JPY, ETH) and the
  clocks. Fee, mempool size and block height left the top of the screen; `MEMP` and `CHAIN` still have them.
- **D45 Two layers of keys.** On the wall, `h j k l` and the arrows move between panes, `1-9` jump to one, `J K` scroll it,
  `enter` zooms in. Zoomed, the pane's own keys work (`j k`, `enter`, `[ ]`, the digits for its menu) and `esc` comes back.
  `f` on a forecast pane opens the full heatmap on that series. `ctrl-w` chords are unchanged. The GO bar is now the command
  line at the bottom, opened with `:` (or `` ` ``), as in vim; the status line shows NORMAL, ZOOM or GO. `?` on the terminal opens
  its own help page. The key panel lists the zoomed pane's hints and menu.
- **D46 The default wall.** `LP BTC` is `FCST BTC` (the lead pane), `FCST XAU`, `QM WATCH` and `NEWS`.
- **D47 `FCST`.** A Glimpse series as a heatmap pane: Coinbase history left of NOW, then the next 48 hourly or 31 daily closes,
  each cell a glyph by the chance the close lands in that cell (per cell, not per bin as on the full heatmap, so a thinly traded
  series such as gold still shows where its probability sits). It reads the app's own loaded closes when the app is on that series
  and fetches its own otherwise. `HM <series>` opens the full heatmap on a series.
- **D48 `NEWS`.** Seven public RSS feeds (SOURCES.md, News sources), fifteen newest per feed, deduplicated by title, tagged
  WORLD, ECONOMY, MARKETS, FED, ECB, CRYPTO. Enter opens the link in the browser. Titles and links only.

Tests: 640 pass offline. Checked by running the app against the live Glimpse API and feeds at 130×38 and 200×55.

### Revision (2026-09-19, fourth session): the whole situation on one page

James: the default should highlight Glimpse and then let him monitor global finance, scrolling with vim keys, with data from
yfinance and other open APIs.

- **D54 Yahoo Finance** (`data/yahoo.py`, SOURCES.md): the endpoints `yfinance` reads, called directly through the terminal's own
  source layer (rate limits, cache, offline fixtures, `SRC` health) rather than adding the library and its dependencies. One `spark`
  request quotes up to 20 symbols with 30 daily closes. It is first in `sources` for indices, yields, futures, FX crosses with no
  exchange feed, and every US share and ETF; a live exchange (Kraken, Coinbase) still comes first where there is one. Default on,
  `SET sources.yahoo false` falls back to FRED, the Treasury and the ECB, and the test suite runs on that path except where marked
  `@pytest.mark.yahoo`. Gold is now COMEX `GC=F`, so `FCST`/`DIST` read PAXG for spot instead, which is what the Glimpse market settles on.
- **D55 The page scrolls.** A layout may set `row_height` on a stacked root: the page is then taller than the screen and
  `Workspace.page_y` scrolls it. j k walk the windows and the page follows (in `TileLayout.arrange`, where the true size is known),
  g G go to the ends, the wheel scrolls, and the status line says which screen of how many. `MAX_PANES` is 16.
- **D56 The default page** (13 windows): the next hour's odds and the 24-hour chart, then the Bitcoin heatmap and the news, then
  gold and world markets, rates and dollar liquidity, currencies and commodities, world indices, hashrate and the difficulty epoch.
  `GP … 24H` is a new window: the last 24 hours to NOW, then Glimpse's median and 80% band, with one line saying where the market
  expects Bitcoin in 24 hours. `GLCO` reads futures when Yahoo is on.
- **D57 Simpler keys.** `enter` opens a window full screen (no separate maximize step), `esc` comes back; `ctrl-j`/`ctrl-k` are down
  and up everywhere, including the `:` list; the key panel is three lines; a page's staleness is judged against its own refresh
  interval, so a half-hourly page no longer says "stale" at ten minutes.

### Revision (2026-09-19, third session): spacemacs

James's second review: h j k l did not move the highlight, the default was crowded, the ladder shared `f` with the forecast,
the news only led to the browser, and `:` was a blank line. What changed:

- **D49 The highlight bug.** Moving focus changed `Workspace.focused` but a pane whose size did not change never redrew, so the
  orange frame stayed put. `relayout` now bumps every window. The tests check the drawn frame colour, not just the state.
- **D50 Windows and buffers.** Every page is a buffer (a mounted `FuncPane`); a window (`Leaf`) shows one and keeps a `back`
  list. Opening something replaces the focused window's page and keeps the old one open (at most 24 buffers; the oldest hidden
  ones close). `:` reuses a hidden buffer with the same command. Two modes: on the windows, h j k l, arrows and 1-9 move; `enter`
  goes inside (green frame, keys to the page); `esc` / `backspace` go back a page, then out. `enter` no longer maximizes:
  `SPC w m` and `ctrl-w o` do.
- **D51 SPC, the leader.** On every screen, with a which-key menu: `SPC SPC` open anything, `SPC 1-9`, `SPC TAB` back,
  `SPC w` windows (`/` `-` split, `d` close, `m` maximize, `=`, h j k l), `SPC b` buffers (`b` list, `n` `p`, `d`, `r`), and the
  screens `SPC t f l B p`. The ladder is `SPC l`; the function bar shows `SPC` then each screen's key. A split opens an empty
  window with the list up to fill it.
- **D52 `:` is a list.** Everything that can be opened: open buffers, the forecasts and odds, the screens, every function
  (those needing a ticker keep the line open for it), the layouts. Type to filter, ↑ ↓ or ctrl-n ctrl-p to choose, ↑ at the
  top walks the history.
- **D53 The default.** `FCST BTC` over `DIST BTC` (new: the next close's odds as a ladder of round price ranges), with
  `FCST XAU` beside. `FCST`'s summary is one line: the price now and the next close's likely (80%) range. NEWS and QM left the
  default. `NEWS` enter now opens `READ`, the story as text in the same window (a stdlib HTML reader: paragraphs and subheads
  outside nav, header, footer and scripts, boilerplate dropped), fetched from the outlet on request only; backspace returns.
  Checked live on BBC, CNBC, the Fed, the ECB and CoinDesk pages.


### Summary (written 2026-09-19, when the first build session stopped). A fresh session starts here.

**State.** Branch `terminal`, eight local commits on top of `8ca2b18` (Phases 0, 1, 2, 3, 4, 6, 7, then 5: the companies family
finished last). Nothing is pushed. `uv run pytest`: 650 passed (435 before). `uv run ruff check .`: clean. No test touches the network.
59 functions are registered. `uv run glimpse-tui` opens the `BTC` launchpad; `uv run glimpse-tui --markets` opens the old first screen.

**What works.** The shell (GO bar with grammar, autocomplete and history; nine tiled panes; six launchpads; status bar, tape, clocks;
`HELP`, `FIND`, `SRC`, `SET`, `EXP`), every Bitcoin function except `NODE`, every on-chain, markets, Glimpse, wallet and tools function,
and the companies functions on synthetic data. Every key and screen that existed before still works and is tested (D31 to D33).
Order and wallet code keeps every confirmation: Glimpse orders are untouched, `OMON` only hands a box to the heatmap, and a barkd send
needs the fee shown, the amount typed back and a final yes.

**Verified live on 2026-09-19.** Every source in SOURCES.md. The hub: the mempool WebSocket (fees, tip, projected blocks, the next
block's transactions), the BTC composite from three exchanges, Kraken FX, FRED, the ECB, the computed dollar index, gold through its
labelled proxy. The `BTC` launchpad at 132×36: `GP BTC` drawing live candles into the real Glimpse forecast, `MEMP` drawing every
transaction of the next block, `ONCH` from Bitview, `TOP` showing blocks as mined.

**Not verified live.** Anything SEC (blocked on a contact email, D38). barkd (no daemon on this machine: nothing was sent or received on
signet, and no real funds were moved). Electrum and Core RPC against a real server. The pages agents built (`BLK`, `TX`, `ADDR`, `RBF`,
mining, markets, on-chain beyond `ONCH`) were driven in the app on recorded data and have not been looked at live by a person. The
authenticated Glimpse paths remain as section 8 left them.

**What is left.** `NODE` (no source). Electrum and Core RPC wired into `bitcoin.order`. An optional keyed Pyth client. barkd on signet,
its notifications on the status line. `ASK` through Jev with a TypeSafe key. `heatmap.py` and `botsview.chart()` moved onto `charts.py`.
Tiling today's screens into launchpads. Real SEC fixtures. A look at every page at 200×58 and in the 256-colour palette by eye.
The checklist below has each item with its reason.

**Open questions.** Ten, listed below. The three that unblock the most: the SEC contact (1), whether a Pyth key is acceptable (2), and
the gold stock constant, which was written from memory and needs confirming (10).

### Baseline (2026-09-19, commit `8ca2b18`)

`uv sync` clean. `uv run pytest`: 435 passed in 50 s. `uv run ruff check .`: all checks passed.

### Decisions

- **D30 Sources are recorded in SOURCES.md and replayed in tests.** Every source was fetched live on 2026-09-19. Samples live
  under `tests/fixtures/sources/<source>/` with a `_manifest.json` (URL, status, latency, trimming). `tests/offline.py`
  answers every request from them through an httpx mock transport that `conftest.py` installs for every test, so a
  request with no fixture fails with a 599 instead of reaching the network. WebSocket streams are off in tests.
- **D31 Today's screens run as functions without being rebuilt.** `MKT`, `HM`, `SLIP`, `PORT` and `BOTS` switch the app to the
  view they always had, full stage, with every key they always had. The launchpad is a fifth view (`term`) beside them. They
  are single-instance and do not tile into a launchpad yet: the heatmap's state is app-wide, and splitting it was judged a
  larger risk to the money paths than the brief's tiling is worth today. Recorded as open question 6.
- **D32 Launch.** `glimpse-tui` opens on the default launchpad (`default_launchpad`, `BTC`). `Terminal()` with no argument
  still opens on the markets and ladder, which is what the existing tests drive. `t` reaches the launchpad from any old
  screen; `f`, `p` and `B` reach the old screens from the launchpad.
- **D33 The GO bar.** `` ` `` and `:` focus it in the launchpad. On the old screens `:` still opens the vim command line, which
  now hands anything it does not know to the GO bar (`:MEMP`, `:LP MACRO`). `` ` `` focuses the GO bar on every old screen
  except the heatmap, where it remains the jump-to-mark chord it has always been (brief and code disagreed: existing
  behaviour kept). A bare number is a block height in the GO bar and a price in the vim command line, as before.
- **D34 Panes draw their own frame.** A `FuncPane` renders its border, number, amber function chip, title, and the source and
  delay on the right of the top edge, because Textual's border titles cannot put two labels on one edge. Tiling is a custom
  Textual `Layout` over a split tree, so splitting, closing, zooming and evening out never re-mount a widget.
- **D35 Rendering never waits.** `load()` runs in a worker and stores results on the pane. `draw()` reads only that. A failed
  load keeps the last picture, names the error in the frame, and the frame dims and says `stale` when any provenance is older
  than its kind allows (live 90 s, daily four days). The rendered text is cached on (size, data version, cursor, focus, a
  five-second clock tick).
- **D36 Pyth is optional, not a default.** Since 2026-08-26 every Hermes price endpoint answers 401 without an API key (SOURCES.md).
  Rule 1 forbids keys on first launch, so the defaults are: exchange tickers for crypto, Kraken's public spot FX for five
  majors, Kraken's tokenised shares (xStocks) as labelled proxies for a few equities and ETFs, FRED, the Treasury and the ECB
  for daily closes, and a computed dollar index. The verified Pyth feed ids stay in `instruments.toml` under `keyed` for a
  future optional client. Consequence: no keyless live S&P 500, Nasdaq, USD/JPY, Brent, gold or silver. Each shows its daily
  official value, or a labelled proxy (PAXG for gold, SPYx for SPY), and says which.
- **D37 BTC is a composite.** The median of Coinbase, Kraken and Bitstamp last trades, shown on screen only and never
  published. One venue alone still gives a price and the pane names the venues that answered. See open question 9.
- **D38 SEC EDGAR waits for the user's contact.** The SEC refuses a User-Agent without contact details, and the session rules
  forbid sending James's email to a service he did not name. No SEC request was made with a contact, so no SEC fixture was
  recorded and no XBRL element could be verified live. The EDGAR client is built against the SEC's published API
  documentation, its test fixtures are hand-built in that documented shape and marked synthetic, and the crypto XBRL elements
  are discovered at run time by searching the company-facts file (name matches `crypto`, unit BTC or USD) rather than
  hard-coded, so nothing is invented. The first company function asks for `sec_user_agent` and explains that the SEC
  receives it on every request.
- **D39 Slow official servers never hold the tape.** The Treasury takes 17 to 19 s to answer. Daily sources get six seconds
  inside a quote refresh, then finish in the background and land in the cache (SQLite, `~/.cache/glimpse/terminal.db`).
- **D40 Heavy stream subscriptions are reference-counted.** `track-mempool-block` (the next block's transactions, 500 KB then
  deltas) is held only while a `MEMP` pane is open, `track-rbf` (2.5 MB a minute) only while an `RBF` pane is open.
- **D41 No public Electrum or Bitnodes default.** Two of three well-known Electrum servers use self-signed certificates and none
  publishes terms. Bitnodes moved to an unnamed operator with ten requests a day. Electrum and Core RPC are backends the
  user configures. `NODE` is not built.
- **D42 Dependencies added** (rule 13): `websockets` (BSD-3, already in the lock through the SDK, now direct: the mempool
  stream), `httpx[socks]` which adds `socksio` (MIT: the one SOCKS5 setting), `segno` (BSD-3: QR codes for `WAL`) and `embit`
  (MIT, pure Python: watch-only address derivation from xpubs and descriptors), both removed again by D43. numpy, pandas and scipy were already present.

### Verified facts (2026-09-19, live)

| # | Fact |
|---|---|
| T1 | The mempool WebSocket has no `stats` message. After `want` it pushes `mempoolInfo`, `fees`, `da`, `mempool-blocks` every 1 to 3 s. A new block is one message with a singular `block` and a full replacement of the projected block. |
| T2 | A projected-block row is `[txid, fee, vsize, value, rate, flags, first_seen]`; deltas are `added`, `removed` (txids) and `changed` (`[txid, rate, flags, acc]`). |
| T3 | Bitview v0.12.2 serves 61,949 series. The daily index is `day1`, position 0 is 2009-01-01, series carry no timestamps, and the last point is today, still forming. |
| T4 | Every `ONCH` metric exists as a Bitview series, including the Mayer multiple (`price_sma_200d_ratio`). `sopr` is height-only; the daily one is `sopr_24h`. 23 age bands sum to 100%. Identifiers are in `data/onchain.toml`. |
| T5 | URPD must be asked for with `agg` (raw is 893 KB). The oracle price is a bare float, and the round-dollar spikes are in the payments histogram, not outputs. |
| T6 | Pyth Hermes and Benchmarks need an API key. Only the feed catalogue is public. |
| T7 | Kraken serves spot FX (EURUSD, GBPUSD, USDCAD, USDCHF, AUDUSD usable; USDJPY too thin) and tokenised shares, which need `asset_class=tokenized_asset`. Result keys differ from the pair asked for (`XXBTZUSD`). |
| T8 | FRED's keyless CSV marks a missing value with an empty field. `WALCL` and `WTREGEN` are millions, `RRPONTSYD` billions. S&P, Dow, Nasdaq and Nikkei series are third-party copyright: shown to the user who fetched them, owner named, never bundled. |
| T9 | The Treasury XML is fresher than FRED's DGS series and takes 17 to 19 s a request. |
| T10 | ICE dollar index: 50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 × USDCAD^0.091 × USDSEK^0.042 × USDCHF^0.036. The ECB fix of 2026-09-18 gives 100.52. |
| T11 | No ETF issuer's terms allow automated reading of its holdings file (BlackRock, Fidelity and Bitwise forbid it; the rest are unclear, which is treated as no). |
| T12 | barkd 0.7.1: `http://127.0.0.1:3000/api/v1`, bearer token, MIT, networks mainnet, signet, mutinynet and regtest. Balance is two endpoints. There is no fee estimate for an Ark-to-Ark send or for `/onchain/send`. The mnemonic and wallet-delete routes sit under the same token, and the terminal never calls them. |
| T13 | mempool.space Lightning statistics were 20 days old on the day. |

### Open questions for James

1. **SEC contact.** Set `sec_user_agent = "Your Name you@example.com"` (or `SET sec_user_agent …`). Then the SEC fixtures can be
   recorded and the XBRL crypto elements confirmed on MSTR's real file. Until then Phase 5 runs on documented shapes (D38).
2. **Pyth.** Is a free-trial Pyth key acceptable as an optional upgrade? It would bring live indices, USD/JPY, metals, oil and
   real equity quotes. The ids are already verified in `instruments.toml`.
3. **Bitnodes.** `NODE` needs a source. The old host now redirects to an unnamed operator with ten requests a day.
4. **Lightning.** mempool.space's Lightning data is stale. Keep `LN` with a stale label, or drop it from launchpads?
5. **ETF holdings and flows.** No issuer permits automated access. `ETF` shows what SEC filings and open quotes give. Is there an
   issuer relationship that would change that?
6. **Tiling today's screens** into launchpads (D31): worth the refactor of the heatmap's app-wide state?
7. TERMINAL.md section 12 questions 1 to 3 and 5 stand as written (deposit-scoped keys, `ended-trades` for keys, the Conviction
   index, default tape and launchpad order).
8. **Tokenised shares as proxies.** Kraken xStocks track the share but are not the share. They are labelled `proxy`. Acceptable?
9. **Coinbase's market-data terms** discourage building "indexes" from its data. The BTC composite is an on-screen median that is
   never published. Acceptable, or should BTC show one venue?
10. **Gold stock constant** for `RV`: 216,265 tonnes (World Gold Council, above-ground stock, end 2024) sits in config as
    `gold_stock_tonnes`. It was written from memory and not re-verified live. Please confirm or replace.

### Checklist

Legend: `[x]` built, tested offline and committed · `[~]` built with a stated gap · `[ ]` not built. Each function lists the
command that opens it.

**Phase 0: orient and verify**
- [x] Baseline recorded
- [x] Every source in TERMINAL.md 1.2 and every candidate fetched live, recorded in SOURCES.md, samples saved as fixtures
- [x] Sources filtered to open, free and permitted; drops explained (Pyth, Bitnodes, ETF issuer files, GDELT, Stooq, public Electrum)
- [x] Instrument map: `src/glimpse_tui/data/instruments.toml` (86 instruments, every list of 4.4)
- [x] On-chain series found by catalogue search: `src/glimpse_tui/data/onchain.toml` (19 metrics, 23 age bands)
- [~] SEC EDGAR blocked on the contact email (D38, question 1)

**Phase 1: the shell**
- [x] Data core: `data/core.py` (provenance, token buckets, memory and SQLite cache, backoff, health), `data/http.py` (pools, User-Agent, SOCKS5)
- [x] Function registry `term/registry.py`; instruments `term/instruments.py`; config `term/config.py`; the hub `term/hub.py`; the shell `term/shell.py`
- [x] GO bar `term/gobar.py`: grammar, classes, autocomplete, history kept between sessions
- [x] Panes and launchpads `term/panes.py`: tiling, focus, zoom, even, `LP <name>`, `LP SAVE <name>`, six shipped launchpads
- [x] Status bar and tape `term/tape.py`: clocks, session states, block flash
- [x] Live loops `data/feeds.py`: the mempool WebSocket with polling fallback, and the quote board (`data/quotes.py`)
- [x] `HELP` · `HELP <FUNC>` · F1
- [x] `FIND <text>`
- [x] `SRC`
- [x] `SET` · `SET <setting> <value>` (takes effect without a restart)
- [x] `MKT`, `HM`, `SLIP`, `PORT`, `BOTS` open today's screens with every key unchanged (D31)
- [x] Phase check: old keys work (tests/test_term_shell.py), `LP BTC` opens four panes, the tape ticked from live sources on 2026-09-19
      (BTC composite, fees and tip over the WebSocket, SPX, USDJPY, XAU by proxy, BRENT, DXY computed)
- Launch: `uv run glimpse-tui` (default launchpad) · `uv run glimpse-tui MEMP` (any GO command) · `uv run glimpse-tui --markets` (the old opening screen)

**Phase 2: inside Bitcoin** (section 5)
- [x] Clients: `data/mempool.py` (REST and WebSocket), `data/bitview.py`, `data/esplora.py`; arithmetic in `data/btcmath.py`
- [~] `data/electrum.py` and `data/core_rpc.py` are built and unit-tested as standalone clients (TLS, `ssl+insecure://` for self-signed,
      SOCKS5, cookie auth, credentials never in errors). They are NOT yet wired into `bitcoin.order`: the resolver walks mempool-shaped
      backends only (mempool, bitview, esplora). Next step: adapters exposing `fees()`, `tip_height()`, `block()`, `tx()`, `address()`.
      The Electrum `scripthash.*` reply shapes are untested against a server (no public server is contacted, D41).
- [x] `BTC` · [x] `MEMP` (the next block as a picture, from `track-mempool-block`; fee bands when polling) · [x] `FEES [in out script]`
- [x] `BLK [height or hash]` (pages with `]` `[`) · [x] `TX <txid>` · [x] `ADDR <address>` (`w` watches it) · [x] `RBF` (`f` full-RBF only)
- [x] `MINE [period | slug]` · [x] `HASH` · [x] `DIFF` (never falls back to Bitview silently) · [x] `HASHP` · [x] `HALV` · [x] `SUPL` · [x] `LN` (says STALE with its date) · [x] `ORCL`
- [ ] `NODE` (no source: D41)
- [x] Privacy line on the first public lookup (`privacy_ack`); backend switch without a restart (`SET bitcoin.mempool …`, tested)
- Verified live on 2026-09-19: the WebSocket streamed fees, tip and projected blocks into the status bar and `MEMP`; `GP BTC` drew live
  Coinbase candles into the real Glimpse forecast. The agents that built `BLK`, `TX`, `ADDR`, `RBF` and the mining pages drove them in the
  app on recorded data; they have not been eyeballed live by James.

**Phase 3: on-chain research** (section 6)
- [x] `charts.py`: braille lines, candles, bands, bars, sparklines, histograms, half-block pixels, stacked bands, QR, both palettes
- [~] `heatmap.py` and `botsview.chart()` were NOT moved onto `charts.py` (brief 4.1). They draw what they always drew; moving them
      risks the heatmap's tuned redraw cost for no visible gain. Left as a follow-up.
- [x] `FLDS <words>` (fills the GO bar's series autocomplete) · [x] `GP <tickers or series> [1H|1D|1Y|MAX] [LOG] [CANDLES]` (`GP BTC XAU SPX`,
      `GP mvrv`, `GP BTC realized_price`; `GP BTC` hourly continues into the Glimpse forecast)
- [x] `ONCH` · [x] `URPD [cohort] [date]` (`c` cohort, `[` `]` dates, `l` log or linear) · [x] `WAVE` · [x] `CYC` · [x] `CORR`
- Facts found while building: a Bitview bulk request is refused above a weight of 320,000 (`weight_exceeded`), so all-history HODL waves
  use the weekly index. Requests use `start=-61` and `-401` so 60 and 400 complete days remain after today's forming point is dropped.
  The dollar index has no history of its own, so `CORR` computes it from the six FX legs.

**Phase 4: markets and macro** (section 7)
- [x] Clients: `data/prices.py`, `data/fred.py`, `data/treasury.py`, `data/fx_ref.py`, `data/deribit.py`, `data/sentiment.py`, `data/quotes.py`
- [ ] `data/pyth.py`: not built. Pyth needs a key (D36). The verified feed ids wait in `instruments.toml` under `keyed`.
- [x] `QM [list]` · [x] `WEI` · [x] `FX` · [x] `GLCO` · [x] `RATES` · [x] `MACRO` · [x] `ECO` · [x] `HMAP` · [x] `RV` · [x] `DVOL`
- Every row carries one of: `live`, `closed`, `daily <date>`, `monthly <month>`, `proxy <ticker>`, `computed`, `no open source`.
  `LP MACRO` was run in the app on recorded data at 132×36 (each pane is 40×12 inside). `ECO` takes real GDP from FRED `GDPC1`,
  which answered live but is not in SOURCES.md's series table. The release calendar needs a FRED key and is not shown.

**Phase 5: companies** (section 9)
All of Phase 5 is `[~]`: built and tested on hand-built fixtures in the SEC's documented shapes (each marked `_synthetic`), never run against
the live SEC (D38). It sends nothing to the SEC until `sec_user_agent` is set, and a 403 is reported and not retried.
- [~] Client: `data/sec.py` (tickers, submissions, company facts, full-text search, documents as clean text, statements with Q4 and
      year-to-date cash flows de-cumulated, crypto holdings discovered at run time, one 5 requests a second budget across the three hosts)
- [~] `DES <ticker>` (a bare equity ticker opens it) · [~] `FA <ticker>` (`a` annual, `q` quarterly, `s` statement) · [~] `CF <ticker>` with the
      in-pane reader (`f` form filter, enter reads, backspace or esc returns) · [~] `CFS <words> [form:8-K] [since:2026-01-01]`
- [~] `TRSY` · [~] `MINR` · [~] `ETF` (no issuer permits reading its holdings file, so no flows) · [~] `N [ticker]` (filings only: the Jev news hub
      is not in this repository) · [x] `TOP` (blocks and alerts work today; filings join once the contact is set)
- To verify live, James: `SET sec_user_agent <name> <email>`, then `MSTR <GO>` and check BITCOIN HELD names the right XBRL element and unit
  against the latest 10-Q; check shares outstanding (MSTR has two classes); `FA MSTR` `q` and a derived Q4; `CF MSTR` enter on a 10-K;
  `CFS bitcoin treasury form:8-K`; `TRSY` for tickers missing from company_tickers.json (XXI, XYZ, SMLR). Then record real fixtures with the
  contact passed through an environment variable, and replace the synthetic ones.
- Known: in `FA`, `q` means quarterly and so does not quit while that pane is focused. Most equities have no open quote without a key, so
  price, market cap and mNAV show a dash and the reason, or a labelled Kraken xStocks proxy (MSTR via MSTRx).

**Phase 6: wallet and Glimpse depth** (sections 8 and 10)
- [-] `WAL` (Glimpse account, barkd, watch-only): built, then removed on 2026-09-19 (D43). Trading is by API key only; `PORT` is the
      account. The code is at `90956d8`.
- [x] `OMON` hands a box to the heatmap (enter a range, `C` the call, `U` the put) and never orders · [x] `GIV`

**Phase 8: the watching terminal** (2026-09-19, D43 to D48)
- [x] One header on every screen, the market strip under it, no fee or block height up top
- [x] `h j k l` between panes, `1-9` to jump, `enter` zoom, `esc` back, `:` command line at the bottom, `?` terminal help
- [x] `FCST BTC` · `FCST XAU` heatmap panes; `HM <series>`; `f` on a forecast pane trades that series
- [x] `NEWS` from seven RSS feeds; `QM WATCH`; the default wall `FCST BTC · FCST XAU · QM WATCH · NEWS`
- [ ] Looked at by James

**Phase 9: spacemacs** (2026-09-19, D49 to D53)
- [x] The highlight moves with h j k l (D49) · [x] windows and buffers with back · [x] SPC leader with which-key · [x] `:` list
- [x] `DIST` · [x] default `FCST BTC` / `DIST BTC` / `FCST XAU` · [x] `READ`, news in the terminal
- [ ] Looked at by James

**Phase 10: the whole situation** (2026-09-19, D54 to D57)
- [x] Yahoo Finance source, instrument map rewired, off switch, fixtures for all 72 symbols (D54)
- [x] The page scrolls: `row_height`, `page_y`, j k g G, the wheel, "screen n of m" (D55)
- [x] The default page: odds, 24h chart, heatmap, news, gold, markets, rates, liquidity, FX, commodities, indices, hashrate, difficulty
- [x] `GP 24H` · [x] `GLCO` on futures · [x] staleness judged against each page's own refresh
- [ ] Looked at by James

**Phase 11: the front page** (2026-09-19, D58 to D62)
- [x] `f` the forecast, `o` the odds; `MKT` renamed `ODDS` with aliases; `l` freed; nav bar, leader, key panel and help
- [x] The page reordered, most significant first, 16 windows; `MAX_PANES` 20
- [x] `CONS` and `BOT` on `term/swarm.py`; `BOTS <model id>`; the zoo runs on this machine, one pass per hourly bar
- [x] Vendor names out of the frames and the market tables; `SRC` and `HELP` keep them
- [x] Gold on the same picture as Bitcoin (`GP XAU 1D` + `DIST XAU`)
- [ ] Looked at by James

**Phase 7: tools and polish** (section 11)
- [x] `AL [rule]` (price, fee, mempool, block, difficulty, Glimpse odds; evaluated each second from cached state; bell and status line)
- [~] `AL` filing rules fire only if something emits `filing` events into `hub.events` (check `TOP`/`N` once the SEC contact is set);
      news rules wait for the Jev hub, which is not in this repository
- [x] `NOTE [ticker] [text]` · [x] `CALC <expression>` · [x] `EXP`
- [~] `ASK <words>`: the keyword router is built and always shows the command before running it. Routing through Jev's function calling
      with a TypeSafe key is NOT built.
- [x] `DEMO`: the two-minute tour (`uv run glimpse-tui DEMO`), any key stops it
- [~] Sizes: pages are tested for no line wider than the pane from 40 to 196 columns, and the default launchpad was looked at on
      recorded data at 80×24 and live at 132×36. 200×58 and the 256-colour palette were covered by tests (`charts.snap`), not by eye.
- [x] README rewritten around the GO bar
