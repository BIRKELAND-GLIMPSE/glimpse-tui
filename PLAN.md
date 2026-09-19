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
  James disliked the shape. Now one plain character per cell from the ramp `·:-=+*#%@` on one absolute
  log scale of probability per bin (six steps 0.6%→10%, three 10%→100%) in four colour tiers. One cell
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
  stream), `httpx[socks]` which adds `socksio` (MIT: the one SOCKS5 setting), `segno` (BSD-3: QR codes for `WAL`), `embit`
  (MIT, pure Python: watch-only address derivation from xpubs and descriptors). numpy, pandas and scipy were already present.

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
- [x] `WAL`: the Glimpse account · barkd (`wallet/barkd.py`: balance, receive with QR, send with the fee first, the amount typed back, a final
      confirmation, the daily cap, a wrong-network refusal, never retried, boards, exits, history, VTXO expiry warning; the mnemonic and
      wallet-delete routes are refused in the client) · watch-only (`wallet/watch.py`: xpub, ypub, zpub, descriptors, single addresses,
      gap limit 20, embit; refuses private keys; no public lookup without `wallet.watch_public_ok`)
- [x] `OMON` hands a box to the heatmap (enter a range, `C` the call, `U` the put) and never orders · [x] `GIV`
- [ ] **barkd send and receive on signet or regtest: NOT exercised.** No `barkd` or `bitcoind` binary is on this machine, and none was
      installed. Everything is tested against a fake daemon that accepts only routes present in barkd 0.7.1's recorded OpenAPI
      (tests/test_funcs_wallet.py). Before trusting it with funds: run `barkd` on signet (SOURCES.md has the steps), `SET wallet.barkd
      http://127.0.0.1:3000`, press `t` in `WAL`, receive from the signet faucet, send a small amount back. No real funds were moved.
- [ ] barkd notifications on the status line (the long-poll route is in the client, not wired to the status line)
- [ ] Paying a Glimpse deposit invoice from barkd (waits on TERMINAL.md section 12 question 1)

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
