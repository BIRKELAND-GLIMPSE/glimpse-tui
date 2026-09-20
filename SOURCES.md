# Glimpse Terminal: data sources

Every source the terminal reads, checked live on 19 Sep 2026 between 18:50 and 19:20 UTC (chain tip 967,731 to 967,732).
For each one: what it serves, how it is reached, its limits and terms, the verdict, and a sample response.
The samples are saved under `tests/fixtures/sources/<source>/`. They are real responses. No value was altered. Long
arrays were cut, and each source's `_manifest.json` records the URL, status, latency and any trimming for every file.
No test touches the network: `tests/offline.py` answers every request from these files.

The rule for keeping a source: open, free, no key on first launch, and terms that allow a free open-source terminal
to fetch the data on the user's own machine and show it to that user. Everything else is dropped or made optional.

All requests used the User-Agent `glimpse-tui/0.1 (+https://github.com/BIRKELAND-GLIMPSE/glimpse-tui)`, at most one
request a second per host. No 429 was received except where noted.

## Verdicts at a glance

| source | verdict | serves | delay on screen |
|---|---|---|---|
| mempool.space REST and WebSocket | keep, primary Bitcoin backend | blocks, transactions, addresses, projected blocks, fees, RBF, mining, difficulty, Lightning | live |
| Bitview (Bitcoin Research Kit) | keep | 61,949 on-chain series, URPD, the on-chain price oracle, mempool-compatible explorer paths | daily for series, live for explorer paths |
| Blockstream Esplora | keep, third fallback | blocks, transactions, addresses, fee estimates | live, polled |
| Electrum protocol | keep as a user-configured backend, never a public default | headers, fee histogram, scripthash history | live |
| Bitcoin Core RPC | keep as a user-configured backend | anything the user's node answers | live |
| Bitnodes | dropped from defaults | reachable nodes | n/a |
| Coinbase Exchange, Kraken, Bitstamp | keep | spot crypto; Kraken also spot FX and tokenised shares | live |
| Pyth Hermes and Benchmarks | dropped as a default, optional with a key | 1,870 feeds across equities, crypto, FX, metals, commodities and rates; no eco feeds, no grains, no official index levels | n/a without a key |
| FRED keyless CSV | keep | rates, the Fed balance sheet, M2, spot oil, index and FX daily closes | daily, weekly, monthly |
| US Treasury XML | keep | the official par yield curve, real yields, bill rates | daily |
| ECB reference rates | keep | official daily FX fixes | daily |
| Deribit public API | keep | option summaries, DVOL, the BTC index | live |
| alternative.me Fear and Greed | keep, with attribution beside the number | the index | daily |
| CFTC Commitments of Traders | keep | CME Bitcoin futures positioning | weekly |
| SEC EDGAR | keep, but unverified: blocked in Phase 0, and it needs the user's contact before the first request | tickers, filings, XBRL facts, full-text search | as filed |
| ETF issuers' holdings files | dropped | n/a | n/a |
| GDELT | dropped from defaults | news search | n/a |
| Stooq | dropped | n/a | n/a |
| Yahoo Finance chart API | keep, added 2026-09-19, default on (`SET sources.yahoo false`) | index levels, the VIX, the dollar index, Treasury yields, COMEX and NYMEX futures, FX, US shares and ETFs | delayed (its own last print) |
| News RSS: BBC World, CNBC (international, economy, finance), Federal Reserve, ECB, CoinDesk | keep, added 2026-09-19 | headlines: title, time, link | delayed, polled every 5 min |
| barkd (Second's Bark) | removed 2026-09-19: no Ark wallet in the terminal yet, trading is by Glimpse API key only | n/a | n/a |

## Bitcoin sources

### mempool.space REST

- **Serves.** Blocks, transactions, addresses, projected blocks, recommended and precise fees, RBF replacements, mining pools, hashrate, difficulty, rewards, audit scores and Lightning statistics.
- **Access.** `https://mempool.space/api/...`, no key. Self-hostable (AGPL-3.0). The terminal reads its base URL from `bitcoin.mempool`.
- **Latency.** Median 82 ms, p90 574 ms, worst 1.29 s over 53 calls.
- **Limits and terms.** Numbers are not published. The API docs say: "Note that we enforce rate limits. If you exceed these limits, you will get an HTTP 429 error. If you repeatedly exceed the limits, you may be banned from accessing the service altogether. Consider an enterprise sponsorship if you need higher API limits." The licence excludes trademark and logo rights. The terminal keeps to one request a second, streams where it can, and parks the host on a 429 for its `Retry-After`.
- **Verdict.** Keep. Primary backend.
- **Endpoints used**, all 200 unless noted, each with a fixture named after it in `tests/fixtures/sources/mempool/`:
  `/api/v1/fees/recommended`, `/api/v1/fees/precise`, `/api/v1/fees/mempool-blocks`, `/api/mempool`, `/api/v1/blocks`, `/api/v1/block/{hash}`, `/api/block/{hash}/txs/{start}`, `/api/blocks/tip/height` (text), `/api/tx/{txid}`, `/api/tx/{txid}/outspends`, `/api/v1/cpfp/{txid}`, `/api/v1/tx/{txid}/rbf`, `/api/v1/transaction-times`, `/api/address/{a}`, `/txs`, `/utxo`, `/api/v1/replacements`, `/api/v1/fullrbf/replacements`, `/api/v1/mining/pools/{period}`, `/api/v1/mining/pool/{slug}`, `/pool/{slug}/blocks`, `/pool/{slug}/hashrate`, `/api/v1/mining/hashrate/{period}`, `/api/v1/difficulty-adjustment`, `/api/v1/mining/difficulty-adjustments/{period}`, `/api/v1/mining/blocks/fee-rates/{period}`, `/blocks/rewards/{period}`, `/blocks/fees/{period}`, `/api/v1/mining/reward-stats/{n}`, `/api/v1/block/{hash}/audit-summary`, `/api/v1/mining/blocks/audit/scores`, `/api/v1/lightning/statistics/latest`, `/statistics/{period}`, `/nodes/rankings`, `/nodes/countries`, `/nodes/isp-ranking`, `/api/v1/prices`.
- **Sample.** `GET /api/v1/fees/recommended` returned `{"fastestFee":4,"halfHourFee":4,"hourFee":1,"economyFee":1,"minimumFee":1}`.
- **Quirks the client handles.**
  - The difficulty adjustment mixes milliseconds (`estimatedRetargetDate`, `remainingTime`, `timeAvg`) and seconds (`previousTime`).
  - `difficulty-adjustments` rows are `[time, height, difficulty, ratio]`. `reward-stats` totals are strings.
  - Audit health is `extras.matchRate` on a block. `audit-summary` is 721 KB, so it is fetched only for one block on request.
  - `/address/{a}/utxo` answers HTTP 400 in plain text above 500 UTXOs.
  - `transaction-times` returns 0 for confirmed transactions. It only knows recently seen ones.
  - `fee-rates` percentiles are whole sat/vB, so low percentiles read 0 when fees are under 1 sat/vB. `fees/precise` and a block's `extras.feeRange` carry the decimals.
  - `/lightning/nodes/country` with no country answers HTTP 500. The list is `/nodes/countries`.
  - Pool rows carry no `share` field. The terminal computes it from block counts.
  - Lightning statistics were 20 days old on the day of the check (latest 30 Aug 2026, and `backend-info` said `lightning: false`). `LN` shows the as-of date and marks it stale.

### mempool.space WebSocket

- **Access.** `wss://mempool.space/api/v1/ws`. Send `{"action":"init"}`, then `{"action":"want","data":["blocks","mempool-blocks","stats"]}`. Connect took about 250 ms and the socket held for 15 minutes without a drop.
- **Limits.** "Note that usage limits apply to our WebSocket API." No numbers are published. The terminal holds one socket.
- **Messages.** The reply to `want` carries `blocks`, `mempool-blocks`, `mempoolInfo`, `vBytesPerSecond`, `fees` and `da`. Every one to three seconds a message of about 4 KB repeats `mempoolInfo`, `vBytesPerSecond`, `transactions`, `da`, `fees` and `mempool-blocks`. There is no `stats` key in any message, despite the subscription name.
- **The next block.** `{"track-mempool-block": 0}` returns `projected-block-transactions` with `blockTransactions`, 4,355 rows and 508 KB on the day, highest fee rate first. After that each periodic message carries a `delta` of `added`, `removed` and `changed`. A row is `[txid, fee, vsize, value, rate, flags, first_seen]`. A changed row is `[txid, rate, flags, acc]`. The layout was confirmed against `backend/src/mempool.interfaces.ts`.
- **A new block** arrives as one message with a singular `block` in the `/api/v1/blocks` shape and a full replacement `blockTransactions`, not a delta.
- **RBF.** `{"track-rbf": "all"}` sends the 25 latest replacement trees, about 75 KB, again on every replacement: about 2.5 MB a minute against 0.25 MB for everything else. The terminal subscribes only while an `RBF` pane is open.
- **Fixtures.** `ws_init.json`, `ws_want_reply.json`, `ws_stats_update.json`, `ws_projected_block_full.json`, `ws_projected_block_delta.json`, `ws_rbf_latest.json`.

### Bitview (Bitcoin Research Kit)

- **Serves.** On-chain time series, the UTXO realized price distribution, a price oracle computed from chain data alone, and explorer paths in mempool's shape.
- **Access.** `https://bitview.space`, no key. MIT. Self-host with `bitviewd` on `localhost:3110`. The terminal reads its base URL from `bitcoin.bitview`.
- **Latency.** Median 206 ms, p90 393 ms, worst 2.3 s over 113 calls.
- **Limits and terms.** None published (checked `llms.txt`, the OpenAPI `info`, and the repository). The README says: "The official free hosted instance is bitview.space." Responses carry an ETag. An undocumented `weight_exceeded` error exists, so the terminal asks for bounded ranges. Budget: two requests a second, `/api/series/bulk` for several series.
- **Verdict.** Keep.
- **Series workflow.** `/api/series/search?q=` returns an array of names. `/api/series/{name}` returns `{description, indexes, type}`. `/api/series/{name}/{index}` returns the data:
  `{"version":…,"index":"day1","type":"Dollars","start":6071,"end":6471,"stamp":"2026-09-19T19:00:39Z","data":[…]}`.
  `start` and `end` are positions and `end` is exclusive. There are no timestamps inside. On `day1`, position 0 is 2009-01-01, and the last point is today, still forming. Parameters: `start`, `end`, `limit`, `format=json|csv`, each accepting a position, a negative position or a date. `/api/series/bulk?series=a,b&index=day1` returns the envelopes in request order.
- **URPD.** `/api/urpd` lists 92 cohorts. `/api/urpd/{cohort}/dates`, `/api/urpd/{cohort}` (latest) and `/api/urpd/{cohort}/{date}` return `{cohort, date, close, total_supply, buckets:[{price_floor, supply, realized_cap, unrealized_pnl}]}`. The default aggregation is raw: 9,057 buckets and 893 KB. The terminal always passes `agg` (for example `log200`).
- **Oracle.** `/api/oracle/price` is a bare float (81337.07 on the day, against 81,424 on the exchanges). Histograms are 2,400 bins, `bin = round(log10(sats) × 200)`. The round-dollar spikes are clear in `/api/oracle/histogram/payments/live` and buried under dust in `/outputs/live`, so `ORCL` draws the payments histogram.
- **Explorer paths.** `fees/recommended`, `fees/mempool-blocks`, `blocks/tip/height`, `block/{hash}`, `tx/{txid}`, `mempool`, `difficulty-adjustment` and `mining/pools/{period}` answer in mempool's shape. Two differences matter. Its `difficultyChange` read -24.9% when mempool.space said -6.0% at the same moment, so `DIFF` does not fall back to it silently. `/api/v1/mempool/block-template` is 9.7 MB and 2.3 s, too heavy to poll, so the fallback for `MEMP` is `fees/mempool-blocks`.
- **Sample.** `GET /api/series/mvrv/day1?start=-3` returned `{"version":…,"index":"day1","type":"StoredF32","start":6468,"end":6471,…,"data":[1.5201,1.5262,1.528034]}`.

### The on-chain series behind `ONCH`, `CYC` and `WAVE`

Each identifier was found with Bitview's own search, inspected through `/api/series/{name}`, fetched at `day1` and
sanity-checked. None was invented. They live in `src/glimpse_tui/data/onchain.toml` with a comment on why each fits.

| metric | series | value on 19 Sep 2026 |
|---|---|---|
| price, daily close | `price_close` | 81,355.05 |
| 200-day average | `price_sma_200d` | 70,438.36 |
| Mayer multiple | `price_sma_200d_ratio` | 1.155 (price over its 200-day average) |
| realized price | `realized_price` | 53,241.66 |
| MVRV | `mvrv` | 1.528 |
| NUPL | `nupl` | 0.346 |
| SOPR | `sopr_24h` | 1.0075 (bare `sopr` is height-only with three-value rows, unusable daily) |
| supply in profit | `supply_in_profit`, `supply_in_profit_share` | 14,567,797 BTC, 72.53% |
| short-term holder cost basis | `sth_realized_price` | 71,605.11 (coins younger than 150 days) |
| long-term holder cost basis | `lth_realized_price` | 49,358.27 |
| Puell multiple | `puell_multiple` | 1.008 |
| liveliness | `liveliness` | 0.634 |
| market cap, realized cap | `market_cap`, `realized_cap` | $1.634T, $1.069T |
| circulating supply | `circulating_supply` | 20,086,432 BTC |
| UTXO count | `utxo_count` | 165,266,372 |
| hashrate, difficulty | `hash_rate`, `difficulty` | 9.50e20 H/s; difficulty matches mempool.space exactly |

HODL waves use 23 disjoint age bands, `utxos_<band>_old_supply_dominance`, which summed to 100.0000% on the day. One
bulk call fetches all of them.

### Blockstream Esplora

- **Access.** `https://blockstream.info/api`, no key, no WebSocket. Median latency 263 ms.
- **Limits and terms.** None published. The API document only says: "You can also self-host the Esplora API server, which provides better privacy and security."
- **Verdict.** Keep as the third fallback for tip, fee estimates, blocks, transactions and addresses.
- **Sample.** `GET /fee-estimates` returns a map of confirmation target to sat/vB, for example `{"1":4.1,"2":4.1,"3":3.2,…}`.

### Electrum servers

- **Checked.** `electrum.blockstream.info:50002`, `bitcoin.lu.ke:50002` and `electrum.emzy.de:50002` answered `server.version`, `blockchain.headers.subscribe`, `mempool.get_fee_histogram` and `blockchain.estimatefee` over TLS in 80 to 700 ms.
- **Finding.** Two of the three use self-signed certificates. Limits and terms are not published anywhere.
- **Verdict.** The protocol stays, as a backend the user configures (`bitcoin.electrum`). No public Electrum server is a default. `estimatefee` is BTC per kB. The fee histogram is `[[sat/vB, vsize], …]`.

### Bitnodes

- **Finding.** `bitnodes.io/api/...` now redirects to a different host, `btcnodes.io`, which describes itself as a standalone deployment and does not name its operator. Its page says: "Requests originating from the same IP address are limited to a maximum of 10 requests per day." Snapshots no longer carry a country field.
- **Verdict.** Dropped from the defaults. `NODE` is not built. The question is recorded for James in PLAN.md.

## Market and macro sources

Checked on Saturday 19 Sep 2026, about 18:55 to 19:15 UTC, from a residential US connection. No key, cookie or
account was used. Latency is the total time of one request. A Saturday is the worst case for FX and equity liquidity.

What works without a key: crypto on three exchanges, five FX majors on Kraken, daily FX from the ECB and FRED, the
Treasury curve, FRED macro series and daily index closes, Deribit, the CFTC report and Fear and Greed. There is no
keyless live source for equities, ETFs, spot gold, silver, USD/JPY, USD/SEK, USD/CNH or any commodity.

### Coinbase Exchange

- **Serves.** Spot crypto tickers, 24 hour stats, candles and a WebSocket feed. It has BTC-USD, ETH-USD, SOL-USD and PAXG-USD. It has no fiat FX pairs against USD except USDT-USD.
- **Access.** `https://api.exchange.coinbase.com` and `wss://ws-feed.exchange.coinbase.com`, no key.
- **Latency.** REST 84 to 108 ms. WebSocket connect 215 ms, first tick 239 ms.
- **Limits and terms.** https://docs.cdp.coinbase.com/exchange/introduction/rate-limits-overview gives public REST "Requests per second per IP: 10" with bursts "Up to 15". The WebSocket allows "Requests per second per IP: 8", bursts "Up to 20", and "Messages sent by the client every second per IP: 100". Responses carry `cache-control: public, max-age=1`. The Market Data Terms of Use at https://www.coinbase.com/legal/market_data answered 403 to an automated fetch. They were read through a search excerpt, so the wording is unverified. The excerpt forbids redistribution or display "to any third party outside of your organization" and use "to create indexes, fixings, other benchmarks, generic or fair value prices". A terminal that fetches on the user's machine and shows the user is inside that. The composite BTC price is computed locally, labelled, shown on screen only and never published.
- **Verdict.** Keep. Primary BTC stream.
- **Endpoints used**, all 200, fixtures in `tests/fixtures/sources/coinbase/`: `/products/BTC-USD/ticker` (also ETH-USD, SOL-USD, PAXG-USD), `/products/BTC-USD/stats`, `/products/BTC-USD/candles?granularity=86400`, `/products/BTC-USD`, and the WebSocket `ticker` and `heartbeat` channels.
- **Sample.** `GET /products/BTC-USD/ticker` returned `{"ask":"81423.45","bid":"81423.44","volume":"4779.54637332","trade_id":1095325422,"price":"81423.45",…}`.
- **Quirks.**
  - Numbers are strings. `time` is RFC 3339 with nanoseconds. Candle rows are `[time, low, high, open, close, volume]`, newest first, as numbers. The daily fixture holds 350 rows, untrimmed.
  - Subscribe with `{"type":"subscribe","product_ids":["BTC-USD"],"channels":["ticker","heartbeat"]}`. The first message is `{"type":"subscriptions",...}`.
  - One tick arrives per trade, so several a second. The client coalesces before it redraws. `heartbeat` comes once a second per product. Coinbase's docs say to subscribe within 5 s of connecting. That was not tested.

### Kraken

- **Serves.** Spot crypto, spot fiat FX and tokenised US equities (xStocks), over REST and WebSocket v2.
- **Access.** `https://api.kraken.com/0/public` and `wss://ws.kraken.com/v2`, no key.
- **Latency.** REST 150 to 190 ms. WebSocket connect 241 ms, with the snapshot immediately after.
- **Limits and terms.** No number is published for public REST. Kraken support (https://support.kraken.com/hc/en-us/articles/206548367) says: "The Spot public endpoints are rate limited by IP address and currency pair for calls to Trades and OHLC, and by IP address only for calls to all other public endpoints". About one request a second is safe. The WebSocket guide (https://docs.kraken.com/api/docs/guides/spot-ws-intro/) says: "approximately 150 attempts per rolling 10 minutes. If the reconnection rate limit is exceeded, the IP address is banned for 10 minutes." No public market-data display terms were found.
- **Verdict.** Keep. Second BTC source, and the only keyless live FX.
- **Endpoints used**, all 200, fixtures in `tests/fixtures/sources/kraken/`: `/Ticker?pair=XBTUSD`, `/Ticker` with several pairs in one call, `/OHLC?pair=XBTUSD&interval=1440`, `/AssetPairs` (674 KB, trimmed to 10 of 1,450), `/AssetPairs?aclass_base=tokenized_asset` (18 of 354), `/Ticker?pair=SPYxUSD,...&asset_class=tokenized_asset`, and the WebSocket v2 `ticker` channel.
- **Sample.** The WebSocket snapshot began `{"channel":"ticker","type":"snapshot","data":[{"symbol":"BTC/USD","bid":81413.2,"bid_qty":0.26067066,"ask":81413.3,…}]}`.
- **Quirks.**
  - The REST result key is not the pair requested. `XBTUSD` returns `XXBTZUSD`, `EURUSD` returns `ZEURZUSD`, `SOLUSD` returns `SOLUSD`. The client maps by `altname`. REST numbers are strings. WebSocket v2 numbers are JSON floats.
  - OHLC rows are `[time, open, high, low, close, vwap, volume, count]`, oldest first. 720 bars is the maximum, with no deeper history.
  - WebSocket symbols are `wsname` values: `BTC/USD`, not `XBT/USD`. The order is a `status` message, the subscribe ack, a `snapshot`, then `update` messages and bare heartbeats. The server closes a socket after about a minute of inactivity. The client sends `{"method":"ping"}`.
  - Kraken's docs offer `"event_trigger":"bbo"`, which suits FX. That was not tested. xStocks REST calls need `asset_class=tokenized_asset`.

Kraken FX quality on the day:

| pair | spread | trades in 24 h | usable |
|---|---|---|---|
| EURUSD | 0.7 bp | 90,102 | yes |
| GBPUSD | 1.2 bp | 16,260 | yes |
| AUDUSD | 1.7 bp | 3,236 | yes |
| USDCHF | 2.1 bp | 2,481 | yes |
| USDCAD | 2.5 bp | 8,015 | yes |
| USDJPY | 199 bp | 39 | no. The fixture shows bid 154.619 and ask 157.700 |

EURJPY, XBTJPY and USDTJPY are just as thin, so USD/JPY cannot be crossed either. There are no SEK or CNH pairs.

xStocks are tokenised trackers whose base names end in `x`. Online with tight spreads on the day: SPYx 0.1 bp, QQQx
0.1 bp, and NVDAx, AAPLx, MSTRx, TSLAx and GOOGLx all under 3 bp. GLDx was online but thin: 17 bp and 9 trades.
`post_only` or empty books: AMZNx, AVGOx, CLSKx, COINx, IWMx, MARAx, METAx, MSFTx, RIOTx, SLVx. There is no IBIT,
FBTC, GBTC, ARKB or BITB token. They are tokens, not the listed share. The terminal uses them only as a labelled
proxy, behind a gate: `status == "online"` and a spread under 50 bp.

### Bitstamp

- **Serves.** Spot crypto plus eurusd and gbpusd. It has btcusd, ethusd, solusd and paxgusd.
- **Access.** `https://www.bitstamp.net/api/v2` and `wss://ws.bitstamp.net`, no key.
- **Latency.** REST 340 to 570 ms, the slowest of the three. WebSocket connect 477 ms. Trades arrive every few seconds.
- **Limits and terms.** https://www.bitstamp.net/api/ says: "all clients can make 400 requests per second. There is a default limit threshold of 10,000 requests per 10 minutes in place." A breach returns error code `400.002`. The same page asks companies that use the data commercially to contact Bitstamp "to receive and sign a commercial use Data License Agreement". A free terminal showing the user their own fetch is not that.
- **Verdict.** Keep. Third BTC source.
- **Endpoints used**, all 200, fixtures in `tests/fixtures/sources/bitstamp/`: `/ticker/btcusd/`, `/ohlc/btcusd/?step=86400&limit=400`, and the WebSocket channel `live_trades_btcusd`.
- **Sample.** `GET /ticker/btcusd/` returned `{"timestamp":"1789844293","open":"80878.78","high":"81913.69","low":"80823.13","last":"81425.16",…}`.
- **Quirks.**
  - All ticker fields are strings. `timestamp` is unix seconds. OHLC is `data.ohlc[]` of `{timestamp,open,high,low,close,volume}` strings, oldest first. `limit` has a maximum of 1000.
  - Subscribe with `{"event":"bts:subscribe","data":{"channel":"live_trades_btcusd"}}`. In a trade, `type` 0 is a buy and 1 is a sell. The client reads `price_str`.
  - There is no ticker channel. Day stats come from REST. Bitstamp's docs describe a `bts:request_reconnect` event. It was not seen on the day.

### Pyth Hermes and Benchmarks

- **Serves.** Real-time prices for 1,870 feeds: 1,243 equity, 421 crypto, 39 FX, 34 commodities, 11 metal and 19 rates, plus redemption-rate, NAV and funding-rate feeds.
- **Access.** `https://hermes.pyth.network`, its new home `https://pyth.dourolabs.app/hermes`, and `https://benchmarks.pyth.network`. The feed catalogue is public. Prices need `Authorization: Bearer <key>`.
- **Latency.** Catalogue 120 to 250 ms. It is 958 KB unfiltered.
- **Limits and terms.** Since the "Pyth Core upgrade" on 2026-08-26 16:00 UTC every Hermes price endpoint answers `401 unauthorized` without a key. https://docs.pyth.network/price-feeds/core/upgrade/preparing says: "Hermes now requires an API Key", "All Hermes users need a Pyth API Key" and "a free trial is included, paid plans cover ongoing use". The rate-limit page at docs.pyth.network/price-feeds/core/rate-limits now only says: "Pyth Pro does not impose hard technical rate limits on API requests. Instead, subscription parameters ... are defined by your service agreement." Display and redistribution terms were not found on the public docs. A web search result quoted paid plans from $500 per month. That figure is unverified.
- **Verdict.** Dropped as a default, because every default source must work with no key. Kept as an optional keyed source. The feed ids are real and sit under `keyed = [...]` in `instruments.toml`.
- **Endpoints checked**, fixtures in `tests/fixtures/sources/pyth/`:

| path | status | note |
|---|---|---|
| `GET /v2/price_feeds?query=&asset_type=` | 200 | trimmed to the 99 rows used. The same response comes from the new host and from `benchmarks.pyth.network/v1/price_feeds/` |
| `GET /v2/updates/price/latest?ids[]=..&parsed=true` | 401 | also 401 on the new host and on `hermes-beta` |
| `GET /v2/updates/price/stream?ids[]=..` (SSE) | 401 | no event was ever received, so the event shape is unverified |
| `GET benchmarks /v1/shims/tradingview/history` | 404, empty body | `/symbols`, `/config` and `/search` also 404. The shim is gone |
| `GET benchmarks /v1/updates/price/{timestamp}` | 401 | |

- **Sample.** A catalogue row: `{"id":"e62df6c8…415b43","market_hours":{"is_open":true,…},"attributes":{"asset_type":"Crypto","display_symbol":"BTCUSD","symbol":"Crypto.BTC/USD",…}}`.
- **Catalogue facts that stay useful.**
  - `asset_type=rates` and `asset_type=eco` both return `[]`. The rate feeds exist but carry an empty `asset_type`. They are found by the symbol prefix `InterestRate.` in the unfiltered catalogue. There are no eco feeds at all. Rates that exist: `InterestRate.US2Y/3Y/5Y/7Y/10Y/20Y/30Y`, the same with `/USD` for price, and `SOFR`, `EFFR`, `OBFR`, `BGCR`, `TGCR`. Their session is 02:30 to 17:00 New York, Monday to Friday.
  - `market_hours.is_open`, `next_open` and `next_close` give the session for each feed. The schedule says equities publish only from 09:30 to 16:00 New York. Off-session freshness could not be measured without a key. The index feeds are Pyth synthetics, not official index levels: `Equity.Index.US500/USD`, `US100/USD`, `US30/USD`, `KR200/KRW`, `JP225/JPY`. There is nothing for the Russell 2000, Euro Stoxx 50, DAX, FTSE 100, Nifty 50, Hang Seng or the KOSPI composite.
  - ETFs present include SPY, QQQ, IWM, DIA, EZU, EWG, EWJ, EWY, EWH, `Equity.IN.NIFTYBEES/INR`, `Equity.KR.069500/KRW` and `Equity.JP.1321/JPY`. Absent: FEZ, EWU, 2800.HK. Commodities are dated futures plus synthetics. Dated contracts are named root, month code, year digit, such as `Commodities.BRENTX6/USD` ("BRENT 30 SEPTEMBER 2026"). They expire, so a client would have to roll. Raw sugar is quoted in US cents (`USc`). Constant one-month synthetics exist, such as `Commodities.Index.BRENT1M/USD` and `WTI1M`. No soybeans, corn or wheat feeds exist.
  - `FX.USDXY` "US DOLLAR INDEX" exists. All FX pairs the brief names exist, including `FX.USD/SEK` and `FX.USD/CNH`.
  - The keyed response shapes come from Pyth's docs and were not observed. A 401 means "no key configured". The client marks the capability unavailable and does not retry.

### Deribit public API

- **Serves.** Option and future book summaries, DVOL, the index price, instruments and historical volatility. JSON-RPC over HTTP GET.
- **Access.** `https://www.deribit.com/api/v2/public/`, no key.
- **Latency.** 150 to 250 ms. The option summary is 434 KB (974 rows). The instruments list is 851 KB.
- **Limits and terms.** https://docs.deribit.com/articles/rate-limits says: "Public, non-authorized API requests are rate-limited on a per-IP basis". It gives no number for anonymous use. The documented credit system charges 500 credits a request against a pool of 50,000 that refills at 10,000 a second. `public/get_instruments` costs 10,000. The terminal stays at one request a second and caches `get_instruments` for an hour. No market-data display clause was found for public data.
- **Verdict.** Keep.
- **Endpoints used**, all 200, fixtures in `tests/fixtures/sources/deribit/`: `get_book_summary_by_currency?currency=BTC&kind=option` (trimmed to 80 rows of the two nearest expiries, of 974), the same with `kind=future`, `get_volatility_index_data`, `get_index_price?index_name=btc_usd`, `get_instruments?currency=BTC&kind=option&expired=false` (trimmed to 40 of 974), `get_historical_volatility?currency=BTC`.
- **Sample.** The last DVOL 1D bar was `[1789776000000, 35.46, 36.52, 34.28, 36.31]`. `get_index_price` returned `{"estimated_delivery_price":81413.6,"index_price":81413.6}`.
- **Quirks.**
  - The envelope is `{"jsonrpc":"2.0","result":...,"usIn":..,"usOut":..,"usDiff":..,"testnet":false}`. Errors are HTTP 400 with `{"error":{"code":-32602,"message":"Invalid params",…}}`.
  - Timestamps are milliseconds. `get_volatility_index_data` requires both timestamps and pages backwards through `result.continuation`. Resolutions `60`, `3600`, `43200` and `1D` answered.
  - Instrument names parse as `BTC-<DDMMMYY>-<strike>-<C|P>`. `mark_iv` is in percent. Option prices are in BTC. `underlying_price` is the forward for that expiry. `high`, `low` and `price_change` can be null.

### FRED keyless CSV

- **Serves.** Every series the brief lists. All 40 returned 200.
- **Access.** `https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>&cosd=YYYY-MM-DD[&coed=YYYY-MM-DD]`, no key. Akamai bot cookies are set, and none is needed.
- **Latency.** 240 to 900 ms, with an occasional 2.5 s. Responses carry `cache-control: public, max-age=600`.
- **Limits and terms.** No rate limit is published for the graph CSV. The terminal keeps to one request a second and caches for a day. https://fred.stlouisfed.org/legal/ defines three licence classes, and each series page names its class:
  - "Public Domain: Citation requested": "may be used without permission".
  - "Copyrighted: Citation required": "you may use these data series with proper attribution of the source and acknowledgment that you obtained the data from FRED."
  - "Copyrighted: Pre-approval required": "Without first obtaining the express written permission of the copyright holder ... these series may only be used for non-commercial educational or personal use." Also: "BEFORE USING DATA SERIES OWNED BY THIRD PARTIES FOR ANYTHING OTHER THAN YOUR OWN PERSONAL USE, YOU MUST CONTACT THE DATA OWNER TO OBTAIN PERMISSION."
  - Prohibited: data gathering "methods that are disruptive".
  - The notice "This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis." is required of API apps. The graph CSV is not the API, but `SRC` shows the line anyway.
- **The copyright reading for SP500, DJIA, NASDAQCOM, NASDAQ100 and NIKKEI225.** The first four are pre-approval series. NIKKEI225 is citation required. A free open-source terminal may show them to its own user when the user's machine fetches them. That is personal non-commercial use. The provenance note names the owner: "© S&P Dow Jones Indices LLC, via FRED", "© Nasdaq, Inc., via FRED", "Nikkei, via FRED". The project must not bundle, mirror, cache server-side or re-serve these values. The SP500, NASDAQ100 and NIKKEI225 fixtures in this repo stay small test data. If Glimpse ever ships a hosted or commercial build, these five series drop out. The S&P and Dow Jones series also carry only 10 years of history on FRED: `cosd=2016-01-01` returns rows from 2016-09-19.
- **Verdict.** Keep.
- **Sample.** `DGS10` ended `2026-09-16,5.01` then `2026-09-17,4.94`. `WALCL` ended `2026-09-16,6746548`.
- **Quirks.**
  - The header is `observation_date,<ID>`. Dates are `YYYY-MM-DD`. Line endings are LF. A missing value is an empty field (`2024-01-15,`), not `"."`. The client treats both `""` and `"."` as missing.
  - Several ids in one request give one wide CSV, but then `cosd` must be given once per series. A single `cosd` is ignored and the full history comes back. Mixed frequencies return a ZIP, not CSV. The terminal asks for one id per request.
  - An unknown id answers HTTP 404 with an HTML body. IMF commodity values are long floats, such as `13542.820869565219`.
  - Net liquidity on the latest values: WALCL 6,746,548 (millions) minus WTREGEN 877,028 (millions) minus RRPONTSYD 0.576 (billions, so 576 millions) gives 5,868,944 millions.

| series | units | frequency | latest on 19 Sep 2026 | licence class |
|---|---|---|---|---|
| DGS10, DGS2, DGS3MO, DGS30, DGS5, DFII10 | percent | daily | 2026-09-17 | public domain |
| T10Y2Y, T10YIE | percent | daily | 2026-09-18 | citation required (St. Louis Fed) |
| DFF | percent | daily, 7-day | 2026-09-17 | public domain |
| SOFR | percent | daily | 2026-09-17 | citation required (NY Fed) |
| WALCL | millions of USD | weekly, as of Wednesday | 2026-09-16 | public domain |
| WTREGEN | millions of USD | weekly, week average ending Wednesday | 2026-09-16 | public domain |
| RRPONTSYD | billions of USD | daily | 2026-09-18 | citation required (NY Fed) |
| M2SL | billions of USD, seasonally adjusted | monthly | 2026-07-01 | public domain |
| SP500, DJIA | index | daily close | 2026-09-17, 2026-09-18 | pre-approval (S&P Dow Jones Indices LLC) |
| NASDAQCOM, NASDAQ100 | index | daily close | 2026-09-18 | pre-approval (Nasdaq, Inc.) |
| NIKKEI225 | index | daily close | 2026-09-18 | citation required (Nikkei) |
| DCOILBRENTEU, DCOILWTICO | USD per barrel | daily | 2026-09-15 | public domain (EIA) |
| DHHNGSP | USD per MMBtu | daily | 2026-09-15 | public domain (EIA) |
| DEXJPUS, DEXUSEU, DEXUSUK, DEXCAUS, DEXSZUS, DEXSDUS, DEXUSAL, DEXCHUS | as named, note the direction | daily, released weekly | 2026-09-11 | public domain |
| DTWEXBGS | index, Jan 2006 = 100 | daily, released weekly | 2026-09-11 | public domain |
| PCOPPUSDM, PWHEAMTUSDM, PMAIZMTUSDM, PSOYBUSDM | USD per metric ton | monthly | 2026-07-01 | citation required (IMF) |
| PSOYBUSDQ | USD per metric ton | quarterly | 2026-04-01 | citation required (IMF) |
| PSUGAISAUSDM | US cents per pound | monthly | 2026-07-01 | citation required (IMF) |
| CPIAUCSL, UNRATE, PAYEMS | index, percent, thousands | monthly | 2026-08-01 | public domain (BLS) |
| GDP | billions, annual rate | quarterly | 2026-04-01 | public domain (BEA) |

Fixtures in `tests/fixtures/sources/fred/`: about 400 rows each of `DGS10`, `SP500`, `RRPONTSYD`, `WALCL`, `WTREGEN`
and `M2SL`, short files for the others used, and `multi_DGS10_DGS2.csv`. Responses are whole, limited only by `cosd`.

### US Treasury XML

- **Serves.** The official daily par yield curve, the real yield curve and bill rates.
- **Access.** `https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=<set>&field_tdr_date_value=<yyyy>` or `&field_tdr_date_value_month=<yyyymm>`, no key.
- **Latency.** 17 to 19 seconds per request. The 20 KB month file is as slow as the 279 KB year file. The terminal fetches it in a background job with a 60 s timeout, never on a `<GO>`.
- **Limits and terms.** Not published. US government work. The terminal makes a few requests a day.
- **Verdict.** Keep. It had 2026-09-18 while FRED `DGS10` still ended 2026-09-17.
- **Endpoints used**, all 200, fixtures in `tests/fixtures/sources/treasury/`: `daily_treasury_yield_curve` for the month (21 KB, 13 entries) and for the year 2026 (279 KB, 180 entries, whole), `daily_treasury_real_yield_curve`, `daily_treasury_bill_rates`.
- **Sample.** 2026-09-18: 3M 4.14, 2Y 4.76, 10Y 5.01, 30Y 5.34.
- **Quirks.**
  - It is an Atom feed. The values sit at `feed/entry/content/m:properties/d:*`, as `Edm.Double` text in percent. Entries are oldest first.
  - Curve fields are `d:NEW_DATE` and `d:BC_1MONTH` to `d:BC_30YEAR`, plus `BC_30YEARDISPLAY`. `d:BC_1_5MONTH` is new. The client iterates over `BC_*` and does not hard-code the list. Real curve fields are `TC_5YEAR` to `TC_30YEAR`. Bill rates use `INDEX_DATE` as the date field, not `NEW_DATE`.
  - Fields can be absent or carry `m:null="true"` in older years. For "a year ago" the prior year file is fetched once and cached for good.

### ECB reference rates

- **Serves.** Official daily euro FX fixes for 29 currencies, including USD, JPY, GBP, SEK, CHF, CAD, AUD and CNY. CNY is onshore. There is no CNH.
- **Access.** `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml` and `eurofxref-hist-90d.xml` (65 days, 71 KB), no key. `eurofxref-hist.xml` holds the full history and was not fetched.
- **Latency.** 600 ms.
- **Limits and terms.** https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html says the rates are "usually updated at around 16:00 CET every working day, except on TARGET closing days". They are based on a concertation "around 14:10 CET". They are "published for information purposes only. Using the rates for transaction purposes is strongly discouraged." ECB statistics are free to reuse with the source named. No rate limit is published.
- **Verdict.** Keep, as the daily FX fallback. It is the only keyless SEK source apart from FRED.
- **Sample.** The fix of 2026-09-18 began `<Cube currency='USD' rate='1.1460'/>` and `<Cube currency='JPY' rate='180.94'/>`.
- **Quirks.**
  - Rates are units of currency per 1 EUR. The crosses are `EURUSD = USD`, `USDJPY = JPY / USD`, `GBPUSD = USD / GBP`, `USDCAD = CAD / USD`, `USDSEK = SEK / USD`, `USDCHF = CHF / USD`, `AUDUSD = USD / AUD`. The daily file uses single quotes. The 90-day file uses double quotes and no whitespace, and is newest first. The client parses XML and never uses a regex.
  - The client polls hourly between 15:00 and 17:00 CET, and once a day otherwise. Both fixtures in `tests/fixtures/sources/ecb/` are whole.

### alternative.me Fear and Greed

- **Access.** `GET https://api.alternative.me/fng/?limit=30`, no key, 118 ms. The fixture `alternative_me/fng_limit30.json` is whole.
- **Limits and terms.** https://alternative.me/crypto/fear-and-greed-index/ says: "You must properly acknowledge the source of the data and prominently reference it accordingly. Commercial use is allowed as long as the attribution is given right next to the display of the data. You may not use our data to impersonate us or to create a service that could be confused with our offering." No rate limit is published.
- **Verdict.** Keep, with "Source: alternative.me" printed beside the number. The client polls once per `time_until_update`.
- **Sample.** `{"value":"71","value_classification":"Greed","timestamp":"1789776000","time_until_update":"17570"}`.
- **Quirks.** All values are strings. Rows are newest first. Only the first row has `time_until_update`, in seconds. `timestamp` is midnight UTC of the day described.

### CFTC Commitments of Traders

- **Access.** Socrata, no key: `https://publicreporting.cftc.gov/resource/gpe5-46if.json`, the "Traders in Financial Futures, futures only" report. CME Bitcoin is `cftc_contract_market_code=133741`. Micro Bitcoin is `133742`. Latency 280 to 490 ms.
- **Query used.** `?cftc_contract_market_code=133741&$order=report_date_as_yyyy_mm_dd DESC&$limit=12`. The fixture `cftc/tff_futures_only_133741.json` holds 12 weeks of whole rows.
- **Limits and terms.** US government data. Socrata throttles anonymous clients that have no app token. No number is published. The terminal fetches once a week.
- **Verdict.** Keep.
- **Sample.** Report of 2026-09-15: `open_interest_all` 20773, `lev_money_positions_long` 5545, `lev_money_positions_short` 11899.
- **Quirks.** Every value is a string. The report date is a Tuesday. The latest report was published on Friday 2026-09-18 at 19:30 UTC, per `Last-Modified`. Fields cover dealers, asset managers, leveraged money, other reportables and non-reportables, with `change_in_*`, `pct_of_oi_*` and `traders_*` companions.

### GDELT DOC 2.0

- **Finding.** `GET https://api.gdeltproject.org/api/v2/doc/doc?query=bitcoin&mode=artlist&format=json&maxrecords=25&sort=datedesc` returned HTTP 429 on the very first request, after 10.7 s. The body began "Please limit requests to one every 5 seconds". The check stopped there. No success body was recorded. The fixture `gdelt/doc_artlist_429.txt` is the 429 body.
- **Terms.** https://www.gdeltproject.org/about.html says the data is "available for unlimited and unrestricted use for any academic, commercial, or governmental use of any kind without fee", and "any use or redistribution of the data must include a citation to the GDELT Project and a link to this website".
- **Verdict.** Dropped from the defaults until someone records a 200. If it returns as an option: one request per 5 s at most, a 15 minute cache, and a 429 treated as normal.

### Stooq

- **Finding.** `GET https://stooq.com/q/d/l/?s=^spx&i=d` returns HTTP 200 `text/html`. The body is a JavaScript proof-of-work page ("This site requires JavaScript to verify your browser"), not CSV. The fixture is `stooq/spx_d_js_challenge.html`.
- **Verdict.** Dropped. Working around the challenge would be scraping. It is not offered as an option unless its key flow is verified by hand.

### The dollar index formula

The source is ICE Futures U.S., "U.S. Dollar Index Contracts FAQ" (June 2015), page 2,
https://www.ice.com/publicdocs/futures_us/ICE_Dollar_Index_FAQ.pdf:

> USDX = 50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 × USDCAD^0.091 × USDSEK^0.042 × USDCHF^0.036

The weights are EUR 0.576, JPY 0.136, GBP 0.119, CAD 0.091, SEK 0.042 and CHF 0.036. The FAQ says: "There are no
regularly scheduled adjustments or rebalancings." The base is March 1973 = 100. A check with the ECB fix of 2026-09-18
(EURUSD 1.1460, USDJPY 157.89, GBPUSD 1.3344, USDCAD 1.4010, USDSEK 9.8530, USDCHF 0.8257) gives 100.52. Without a
Pyth key the JPY and SEK legs are daily, so the label reads "computed, EUR GBP CAD CHF live (Kraken), JPY SEK daily
(ECB)". `fred:DTWEXBGS` is the Fed broad index, a different basket. It is never shown as DXY.

## Instrument map

Every instrument in `src/glimpse_tui/data/instruments.toml`, grouped by its category lists. "Live" means a keyless
source that streams or polls intraday. The "Pyth" column says whether a feed id sits in the instrument's `keyed`
array, usable only with a key. `sources` holds only keyless sources that answered on the day. `GLOBAL` (BTC, XAU,
SPX, NDX, USDJPY, EURUSD, BRENT, US10Y, DXY, XAG) and `DXY_LEGS` draw on the rows below.

| ticker | class | live source | daily fallback | proxy | Pyth | status |
|---|---|---|---|---|---|---|
| **CRYPTO** | | | | | | |
| BTC | CRYPTO | coinbase:BTC-USD, kraken:XBTUSD, bitstamp:btcusd | exchange candles | | yes | live |
| ETH | CRYPTO | coinbase:ETH-USD, kraken:ETHUSD, bitstamp:ethusd | | | yes | live |
| SOL | CRYPTO | coinbase:SOL-USD, kraken:SOLUSD, bitstamp:solusd | | | yes | live |
| PAXG | CRYPTO | coinbase:PAXG-USD, kraken:PAXGUSD, bitstamp:paxgusd | | | yes | live. A token, not spot gold |
| **FX** | | | | | | |
| EURUSD | CURNCY | kraken:EURUSD | ecb:USD, fred:DEXUSEU | | yes | live |
| GBPUSD | CURNCY | kraken:GBPUSD | ecb:GBP, fred:DEXUSUK | | yes | live |
| USDCAD | CURNCY | kraken:USDCAD | ecb:CAD, fred:DEXCAUS | | yes | live |
| USDCHF | CURNCY | kraken:USDCHF | ecb:CHF, fred:DEXSZUS | | yes | live |
| AUDUSD | CURNCY | kraken:AUDUSD | ecb:AUD, fred:DEXUSAL | | yes | live |
| USDJPY | CURNCY | none, Kraken is too thin | ecb:JPY, fred:DEXJPUS | | yes | daily |
| USDSEK | CURNCY | none | ecb:SEK, fred:DEXSDUS | | yes | daily |
| USDCNH | CURNCY | none | none | USDCNY | yes | unavailable. USDCNY is the labelled stand-in |
| USDCNY (in no list) | CURNCY | none | ecb:CNY, fred:DEXCHUS | | yes | daily. Onshore, not CNH |
| **Dollar index** | | | | | | |
| DXY | INDEX | computed:dxy, 4 live legs and 2 daily | computed from the ECB fix | | yes (FX.USDXY) | live, mixed freshness |
| **RATES** | | | | | | |
| US3M | GOVT | none | treasury:BC_3MONTH, fred:DGS3MO | | no | daily |
| US2Y | GOVT | none | treasury:BC_2YEAR, fred:DGS2 | | yes | daily |
| US5Y | GOVT | none | treasury:BC_5YEAR, fred:DGS5 | | yes | daily |
| US10Y | GOVT | none | treasury:BC_10YEAR, fred:DGS10 | | yes | daily |
| US30Y | GOVT | none | treasury:BC_30YEAR, fred:DGS30 | | yes | daily |
| **INDICES** | | | | | | |
| SPX | INDEX | none | fred:SP500 | SPY, then SPYx | yes (synthetic) | daily, personal-use licence |
| NDX | INDEX | none | fred:NASDAQ100 | QQQ, then QQQx | yes (synthetic) | daily, personal-use licence |
| NKY | INDEX | none | fred:NIKKEI225 | EWJ | yes (synthetic) | daily, citation required |
| RUT | INDEX | none | none | IWM | no | unavailable |
| SX5E | INDEX | none | none | EZU | no | unavailable |
| DAX | INDEX | none | none | EWG | no | unavailable |
| UKX | INDEX | none | none | none | no | unavailable |
| NIFTY | INDEX | none | none | NIFTYBEES | no | unavailable |
| KOSPI | INDEX | none | none | EWY | yes (KR200, the KOSPI 200) | unavailable |
| HSI | INDEX | none | none | EWH | no | unavailable |
| DJI (in no list) | INDEX | none | fred:DJIA | DIA | yes (synthetic) | daily, personal-use licence |
| **STOCKS** | | | | | | |
| NVDA, AAPL, GOOGL, TSLA, MSTR | EQUITY | none | none | NVDAx, AAPLx, GOOGLx, TSLAx, MSTRx | yes | proxy |
| MSFT, AMZN, META, AVGO, COIN | EQUITY | none | none | none. The Kraken token was post_only or illiquid | yes | unavailable |
| **MINERS** | | | | | | |
| MARA, RIOT, CLSK, CORZ, IREN, WULF, HUT, CIFR | EQUITY | none | none | none | yes | unavailable |
| **BTC_ETFS** | | | | | | |
| IBIT, FBTC, GBTC, ARKB, BITB | ETF | none | none | none | yes | unavailable |
| **Proxy ETFs (in no list)** | | | | | | |
| SPY, QQQ, GLD | ETF | none | none | SPYx, QQQx, GLDx | yes | proxy |
| IWM, DIA, SLV, USO, BNO, CPER, EZU, EWG, EWY, EWH, EWJ, NIFTYBEES | ETF | none | none | none | yes | unavailable |
| **xStocks tokens (in no list)** | | | | | | |
| SPYx, QQQx, GLDx | ETF | `kraken:<ticker>USD` | | | no | live, behind the status and spread gate. GLDx is thin |
| NVDAx, AAPLx, GOOGLx, TSLAx, MSTRx | EQUITY | `kraken:<ticker>USD` | | | no | live, behind the status and spread gate |
| **COMMODITIES** | | | | | | |
| XAU | CMDTY | none | none | PAXG | yes | proxy |
| XAG | CMDTY | none | none | none | yes | unavailable |
| BRENT | CMDTY | none | fred:DCOILBRENTEU | | yes (BRENT1M or dated) | daily, about 4 days late |
| WTI | CMDTY | none | fred:DCOILWTICO | | yes | daily |
| NATGAS | CMDTY | none | fred:DHHNGSP | | yes | daily |
| COPPER | CMDTY | none | fred:PCOPPUSDM | | yes | daily slot, monthly data |
| SOYBEANS | CMDTY | none | fred:PSOYBUSDM | | no, not on Pyth | daily slot, monthly data |
| CORN | CMDTY | none | fred:PMAIZMTUSDM | | no, not on Pyth | daily slot, monthly data |
| WHEAT | CMDTY | none | fred:PWHEAMTUSDM | | no, not on Pyth | daily slot, monthly data |
| SUGAR | CMDTY | none | fred:PSUGAISAUSDM | | yes (dated, US cents) | daily slot, monthly data |

What is unavailable without a key, and why. No open source publishes live equity or ETF quotes, so all ten `STOCKS`,
all eight `MINERS` and all five `BTC_ETFS` have no real quote. Five stocks and three ETFs have a Kraken xStocks token
as a labelled proxy, and a token is not the listed share. Seven of the ten `INDICES` have no source of any kind: no
open index level exists, and the ETF proxies themselves need the Pyth key. Spot gold has no open feed, so PAXG stands
in. Silver has no keyless source at all. USD/JPY and USD/SEK are daily because Kraken's yen book is too thin and it
lists no krona pair. USD/CNH has no open feed. Oil and gas are daily EIA spot series. Copper, the grains and sugar
are IMF monthly averages. Pyth Hermes lists feeds for most of these gaps, but it has needed a key since 2026-08-26.
If a free Pyth key is judged acceptable, the client can put `keyed` in front of `sources`. Soybeans, corn and wheat
have no Pyth feed. RUT, SX5E, DAX, UKX, NIFTY and HSI have no Pyth index feed either, so a key gives them an ETF
proxy at best, and UKX has none.

### Yahoo Finance chart API

- **Access.** `GET https://query1.finance.yahoo.com/v7/finance/spark?symbols=<up to 20>&range=1mo&interval=1d` for quotes with 30 daily
  closes, and `/v8/finance/chart/<symbol>?range=2y&interval=1d` for history. Keyless. Both answered 200 on 2026-09-19 with the
  terminal's own User-Agent. A browser User-Agent was refused (the body was not JSON), so the terminal sends its own, as it does
  everywhere. `spark` refuses more than 20 symbols (HTTP 400 at 25); the client batches in twenties.
- **What it carries that nothing else open does.** The S&P 500, Nasdaq 100, Dow, Russell, Euro Stoxx, DAX, FTSE, Nikkei, Hang Seng,
  Kospi and Nifty as levels rather than official daily closes; the VIX; the ICE dollar index itself (`DX-Y.NYB`); Treasury yields
  (`^TNX`, `^IRX`, `^FVX`, `^TYX`); COMEX gold, silver and copper, NYMEX crude and gas, CBOT grains and ICE sugar as front-month
  futures; FX crosses; and every US share and ETF (MSTR, IBIT, the miners), which had no keyless source at all before.
- **Terms.** Yahoo's terms allow personal, non-commercial use. The terminal fetches for the person running it, names `yahoo` on every
  number it draws, caches for 60 s, keeps within one request per second, and stores nothing beyond its cache. `SET sources.yahoo false`
  turns it off, and every pane falls back to the official daily sources (FRED, the Treasury, the ECB) as before. A user who cannot
  accept those terms loses nothing but freshness.
- **Verdict.** Keep, default on, because the alternative for a dozen instruments is nothing at all.
- **Sample.** `^GSPC`: `regularMarketPrice` 7650.5, `previousClose` 7637.76, day range 7610.52 to 7657.17, `regularMarketTime`
  1789766988, 22 daily closes. `GC=F` (gold, Dec 26) 4424.9. `^TNX` 4.998, already in percent.
- **Quirks.** `chartPreviousClose` is the previous *session's* close, which on an intraday range is not yesterday's: the client
  prefers the second-to-last daily close when the last one is today's. A symbol with no print in half an hour is marked closed. The
  v7 `quote` endpoint now needs a crumb (401) and is not used. Futures are the front month, so gold sits a few dollars above spot;
  Glimpse's own gold market settles on PAXG, and the forecast pages say so.
- **Fixture.** `tests/fixtures/sources/yahoo/spark_all.json` holds all 72 symbols the instrument map names, recorded in four
  requests and merged by symbol; `chart_gspc_2y.json` is two years of the S&P 500. The offline transport answers any spark request
  with the recorded rows for the symbols asked for.

## News sources

Checked live on 2026-09-19 with the terminal's own User-Agent (`glimpse-tui/0.1.0 (+https://github.com/…)`): every feed
answered 200 with RSS 2.0. Fixtures are under `tests/fixtures/sources/news/`. The terminal shows each item's title, time
and link, and opens the link in the browser on request. It never copies an article's text. Each feed is its own source
on `SRC`, polled at most every five minutes.

| key | outlet · tag | URL | items on the day |
|---|---|---|---|
| `bbc_world` | BBC · WORLD | `https://feeds.bbci.co.uk/news/world/rss.xml` | 24 |
| `cnbc_world` | CNBC International · WORLD | `https://www.cnbc.com/id/100727362/device/rss/rss.html` | 30 |
| `cnbc_econ` | CNBC Economy · ECONOMY | `https://www.cnbc.com/id/20910258/device/rss/rss.html` | 30 |
| `cnbc_finance` | CNBC Finance · MARKETS | `https://www.cnbc.com/id/10000664/device/rss/rss.html` | 30 |
| `fed_press` | Federal Reserve press releases · FED | `https://www.federalreserve.gov/feeds/press_all.xml` | 20 |
| `ecb_press` | ECB press releases, speeches · ECB | `https://www.ecb.europa.eu/rss/press.html` | 15 |
| `coindesk` | CoinDesk · CRYPTO | `https://www.coindesk.com/arc/outboundfeeds/rss` (the `/rss/` form redirects here) | 25 |

- **Tried and not used.** MarketWatch top stories (`feeds.content.dowjones.io/public/rss/mw_topstories`) answered, but on the
  day it was mostly personal finance. BBC Business was UK-centred. FT and the Guardian redirect to pages that are not feeds.
  Google News topic feeds answered but aggregate other publishers' headlines through Google's redirect links.
- **Quirks.** `pubDate` is RFC 822 with `GMT`, `+0000` or `+0200`. CNBC's feeds overlap: the same story is shown once, by title.
  The ECB feed puts every item on one line.

## Company sources

Checked on 19 Sep 2026 with curl from the development machine.

### SEC EDGAR

- **Serves.** Company tickers, filing indexes, XBRL company facts, full-text search and filing archives.
- **Access.** `https://www.sec.gov`, `https://data.sec.gov` and `https://efts.sec.gov`. JSON, no key.
- **Status: blocked in Phase 0.** Every SEC host answered HTTP 403 to a User-Agent without a contact email. No personal email was sent. No SEC fixture and no XBRL element could be verified. Nothing about SEC response shapes in this document comes from a live response.
- **What happened.** Eight requests went out, at least one second apart, with two User-Agent strings whose only contact was the project URL. All eight were refused in 0.07 to 0.19 s by Akamai. `www.sec.gov` answered with the page "Request Rate Threshold Exceeded". The rate limit was never approached, so that is the generic refusal page. `data.sec.gov` and `efts.sec.gov` answered "Your Request Originates from an Undeclared Automated Tool", which names the real cause. No browser string and no invented email was tried.

| path | status | fixture |
|---|---|---|
| `www.sec.gov/files/company_tickers.json` | 403 | `sec/error_403_www.html` |
| `www.sec.gov/files/company_tickers_exchange.json` | 403 | none |
| `data.sec.gov/submissions/CIK##########.json` | 403 | none |
| `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` | 403 | none |
| `efts.sec.gov/LATEST/search-index` | 403 | none |
| `www.sec.gov/os/accessing-edgar-data` (the policy page) | 403 | none |
| `www.sec.gov/Archives/edgar/data/...` | not tried | none |

- **Limits and terms.** The only SEC text reachable was the 403 body. It says: "Please declare your traffic by updating your user agent to include company specific information." It says: "Current guidelines limit users to a total of no more than 10 requests per second, regardless of the number of machines used to submit requests." It says: "We reserve the right to block IP addresses that submit excessive requests." It also says: "Note: We do not offer technical support for developing or debugging scripted downloading processes." The page points to `https://www.sec.gov/developer`.
- **Verdict.** Keep, but unverified. SEC is the only open, official source for the company functions. The terminal reads `sec_user_agent` from config and asks for it on first use. The check shows that this prompt is not optional. Two things stay unknown until someone runs one request with a real contact string: whether a name and email is accepted from this network, and every response shape.
- **To unblock.** One person with a contact address they are willing to publish to SEC runs `curl -A "Glimpse Terminal <contact address>" --compressed https://www.sec.gov/files/company_tickers.json`. If that returns JSON, the check is rerun with the string supplied through an environment variable, so that it never lands in a fixture, a log or the repository.
- **Sample.** The refusal reads: `SEC.gov | Request Rate Threshold Exceeded ... Please visit www.sec.gov/developer for more developer resources and Fair Access guidelines.`
- **What the evidence does support.** A refusal is HTTP 403 with `content-type: text/html`, gzip encoded, never JSON. The client checks status and content type before it parses. It shows "SEC refused the request: set sec_user_agent". It never retries a 403 in a loop.
- **XBRL elements.** None is recommended. No companyfacts file could be read. The names in the brief remain candidates only. The `xbrl_element` fields in `treasuries.toml`, `miners.toml` and `etfs.toml` are empty. Six CIKs come from the brief and are marked `brief`, unconfirmed. BITB's CIK comes from the issuer's own fund site and is marked `issuer-page`. No CIK was filled from memory.
- **One holdings figure was verified elsewhere.** Metaplanet (3350.T) showed 43,000 BTC on its own home page `https://metaplanet.jp/en` on the day. The page gives no as-of date, so `as_of` is the fetch date, and the figure may be rounded.
- **Fixtures still owed.** `sec/company_tickers.json`, `sec/company_tickers_exchange.json`, `sec/submissions_CIK0001050446.json`, `sec/companyfacts_CIK0001050446.json` (plus MARA and COIN), `sec/search_index_bitcoin_8k.json`, `sec/doc_mstr_8k.htm`. `sec.py` is written only after they exist.

### ETF issuers' holdings files

Dropped, all of them. No issuer was cleared. The IBIT page was fetched once to find the terms link, and nothing from
it was saved as a fixture.

| issuer | terms URL | clause | verdict |
|---|---|---|---|
| BlackRock iShares (IBIT). The fund page links a `latest-holdings.csv` | `https://www.blackrock.com/corporate/compliance/terms-and-conditions` | Users may not "Use any robot, spider, intelligent agent, other automatic device, or manual process to search, monitor or copy this Website or the reports, data, information, content, software, products services, or other materials on, generated by or obtained from this Website, whether through links or otherwise (collectively, "Materials"), without BlackRock's permission, provided that generally available third-party web browsers may be used without such permission" | forbidden |
| Fidelity (FBTC) | `https://www.fidelity.com/terms-of-use` | "You may not access the Fidelity Websites using devices, services, platforms, or software ("Third-Party Access Tools") designed to provide high-speed, automated, or repeated access to Fidelity Websites." Third-party content "shall not be distributed or redistributed in any manner". | forbidden |
| Bitwise (BITB) | `https://bitwiseinvestments.com/terms-of-service` | "you may not reproduce, display, publicly perform, make a derivative version of, distribute, or otherwise use Bitwise Content in any way for any public or commercial purpose without Bitwise's prior written consent. The use or posting of any of Bitwise Content on any other website or in a networked computer environment for any purpose is expressly prohibited." | forbidden |
| Grayscale (GBTC, BTC) | `https://www.grayscale.com/terms-of-use` | HTTP 429 on the terms page and on robots.txt. Not read. | unclear, so not permitted |
| ARK / 21Shares (ARKB) | `https://www.ark-funds.com/terms-of-use` | HTTP 403 on the terms page and on robots.txt. Not read. | unclear, so not permitted |
| VanEck, CoinShares, Franklin, Invesco, WisdomTree | not checked | not checked | unclear, so not permitted |

The iShares `robots.txt` does not disallow the product pages, but robots.txt does not override the terms. The `ETF`
pane shows holdings or NAV only from the trusts' SEC filings, with the as-of date of the filing. Flows and the daily
premium or discount are not available from a cleared source.

### barkd (Second's Bark)

Removed on 2026-09-19 with the `WAL` page, the `wallet/` package and its fixture: the terminal has no Ark wallet yet, and
trades through a Glimpse API key only. The research that was here (barkd 0.7.1, its routes and auth) is in git history
at commit `90956d8` for when a wallet returns.

### Library choices

`uv.lock` already holds httpx 0.28.1 and websockets 17.1, both pulled in by `glimpse-markets`. socksio, segno and
embit are absent. httpx and websockets are declared directly once the terminal imports them.

| need | choice | version on PyPI | licence | reason |
|---|---|---|---|---|
| address derivation | **embit** | 0.8.0 (2024-05-30). The repository has tags up to v0.8.2, so PyPI lags it | MIT | Pure Python, sdist only, no dependencies, installs on every platform. It handles xpub, ypub, zpub, output descriptors and miniscript. It is the library inside Specter and SeedSigner. Watch-only derivation never touches private keys |
| QR codes | **segno** | 1.6.6 (2025-03-12) | BSD | Pure wheel with no dependencies. `segno.make(data).matrix` gives the module matrix to draw in half blocks |
| SOCKS5 | **socksio** through `httpx[socks]` | 1.0.0 (2020-04-17) | MIT | The `socks` extra of httpx 0.28.1 is exactly `socksio==1.*`. Old but tiny and stable. `proxy="socks5h://127.0.0.1:9050"` resolves DNS through Tor. It covers HTTP only |
| WebSocket | **websockets** | 17.1 (2026-08-26) | BSD-3-Clause | Already locked, with wheels for macOS, manylinux x86_64 and aarch64, musllinux and Windows. It has no SOCKS support of its own |

Rejected for address derivation: bdkpython 3.1.0 (MIT OR Apache-2.0) is the strongest engine, but it has no Linux
aarch64 wheel, no cp314 wheel and no sdist, so it is the second choice. bip-utils 2.12.2 (MIT) has a heavy multi-coin
dependency tree and no output descriptor support. python-bitcointx 1.1.5 is LGPLv3+ and is rejected on licence.
WebSockets through Tor need a separate answer, for example `python-socks` (Apache-2.0, not checked here). That is
flagged for PLAN.md.

## Where the brief and the live sources disagree

Items 1 to 18 cover the Bitcoin sources, 19 to 34 markets and macro, and 35 to 44 companies and the wallet.

1. Bitview is v0.12.2, not v0.11.2.
2. Bitview's catalogue reported 61,949 series, not 61,764. The number is never hard-coded.
3. Bitview's OpenAPI has 114 operations. 17 are deprecated, which leaves 97.
4. The repository `bitcoinresearchkit/brk` redirects to `bitcoinresearchkit/mono`. It is still MIT.
5. The daily index is `day1`.
6. URPD has a fourth path, plus `agg` and `weight` parameters. It lists 92 cohorts.
7. The oracle's round-dollar spikes show in the payments histogram, not the outputs histogram. The oracle price is a bare number.
8. Bitview's `block-template` is 9.7 MB, too heavy to poll.
9. Bitview's `difficultyChange` disagreed with mempool.space at the same moment: -24.9% against -6.0%.
10. The Mayer multiple exists as a series, `price_sma_200d_ratio`. `sopr` is height-only, so the terminal uses `sopr_24h`.
11. mempool.space `/lightning/nodes/country` with no country answers HTTP 500.
12. mempool.space Lightning data was 20 days stale on the day.
13. `/address/{a}/utxo` returns HTTP 400 above 500 UTXOs.
14. `transaction-times` returns 0 for confirmed transactions.
15. `fee-rates` percentiles are integers.
16. The WebSocket has no `stats` message key. `track-rbf` costs about 2.5 MB a minute.
17. Mining pool rows have no `share` field.
18. Bitnodes moved host and now limits each IP to 10 requests a day. Public Electrum servers are mostly self-signed.
19. Section 1.2 lists Pyth as "no key". That has been false since 2026-08-26. `latest`, the SSE stream and Benchmarks all answer 401. Only `/v2/price_feeds` is public. Sections 4.2, 4.3 and 7 (`QM`, `WEI`, `FX`, `GLCO`, `ECO`) and rule 1 all depend on it.
20. Section 1.2 lists Benchmarks for history. The TradingView shim returns 404. Keyless history exists only for crypto (exchange candles) and the FRED, ECB and Treasury dailies.
21. Section 1.2 names the Pyth asset types `rates` and `eco`. Both return `[]`. Rate feeds exist with an empty `asset_type`. There are no eco feeds, so `ECO` has nothing to read from Pyth even with a key.
22. Section 4.4, category lists. With no key there is no live source for the S&P 500, Nasdaq 100, USD/JPY, Brent, silver or gold in `GLOBAL`. Seven of ten `INDICES` have no source of any kind. All ten `STOCKS` have no real quote, and five have a tokenised proxy. The grains and sugar are monthly.
23. Section 4.4 says to show an ETF proxy during its session. The ETF proxies themselves need the Pyth key. The only keyless stand-ins are Kraken xStocks tokens, which trade 24x7 and are not the ETF.
24. Section 4.4, the dollar index. It is computable, but the JPY and SEK legs, 17.8% of the weight, are daily without a key. Kraken's USD/JPY book is too thin to use. With a key, Pyth carries `FX.USDXY` directly.
25. Section 7 `GLCO` calls the FRED copper and grain series spot. They are IMF monthly averages in USD per metric ton, two months late. Pyth has no soybeans, corn or wheat. Pyth's Brent, WTI, gas and sugar are dated futures that expire, or Pyth synthetics, and sugar is quoted in US cents.
26. Section 7 `WEI` says "Pyth where a feed exists". Pyth's US500, US100, US30, JP225 and KR200 are its own 24/7 synthetics, not official index levels. KR200 is the KOSPI 200, not the KOSPI.
27. Section 1.2, FRED. The keyless graph CSV marks a missing day with an empty field, not `"."`. Multi-id requests need one `cosd` per id. Mixed frequencies come back as a ZIP.
28. Section 1.2, FRED third-party series. SP500 and DJIA are "pre-approval required" and hold only 10 years of history. The NASDAQ series are pre-approval too. Personal display is fine. Bundling or re-serving is not. `CORR` over more than 10 years of S&P 500 cannot use FRED.
29. Section 7, net liquidity. The units are confirmed. But `WTREGEN` is a weekly average ending Wednesday, while `WALCL` is the Wednesday level. The Wednesday-level series is `WDTGAL`. The panel uses it or says "week average".
30. Section 4.3, home.treasury.gov. The budget is fine, but each request takes 17 to 19 s. It must be a background job with a long timeout.
31. Section 1.2, Stooq. A plain client gets a JavaScript proof-of-work page, not a key prompt. It is dropped.
32. Section 1.2 candidates, GDELT. It answered 429 on the first request. It is not reliable enough for a default.
33. Section 5, the `BTC` composite price. Coinbase's market data terms forbid using its data "to create indexes, fixings, other benchmarks, generic or fair value prices". An on-screen, labelled, never-published blend for the user is a fair reading, but James should know.
34. The FRED FX series (`DEX*`) and `DTWEXBGS` are published weekly, so they lag the ECB by up to a week. The ECB comes before FRED in every FX source list in `instruments.toml`.
35. Section 1.2, the SEC row, says "checked on 19 Sep 2026". On that date, from this machine, every SEC host refused the request. A descriptive User-Agent whose contact is a project URL is not enough. It is unknown whether a name and email is enough from this network. The paths and the 10 requests per second figure match SEC's own refusal text. The shapes remain unverified.
36. Section 9 says the SEC User-Agent comes from config and is asked for the first time. That is now mandatory, because there is no usable default. `DES`, `FA`, `CF`, `CFS` and the SEC-backed columns of `TRSY`, `MINR` and `ETF` do nothing until the user types a contact. That contact goes to SEC on every request, and the prompt says so. Fixtures must be recorded with a contact that never enters the repository.
37. Section 9, the `ETF` row, says "holdings and flows where an issuer's terms allow". No issuer's terms allow it. Holdings and flows from issuer files are dropped from the plan, not deferred.
38. Section 1.2, the barkd row, lists seven tags. The 0.7.1 spec has ten: those seven plus `history`, `message` and `bitcoin`. History moved to `GET /api/v1/history`. The old `/api/v1/wallet/history` and `/wallet/movements` are deprecated.
39. Section 10 describes one balance: spendable, pending and on-chain. barkd has two balance calls. The Ark balance has one spendable field and five kinds of pending. The on-chain balance is a separate endpoint with six fields. The pane needs both.
40. Section 10, the send flow. "Send on-chain" is two operations in barkd: from the Ark balance (`/wallet/send-onchain`, an offboard, with a fee from `/fees/send-onchain`) and from the on-chain wallet (`/onchain/send`, with fee rates from `/fees/onchain` and no per-send estimate). The Ark-to-Ark send has no fee estimate endpoint. The pane must pick the source of funds and handle the case with no estimate.
41. Section 10, notifications. The WebSocket is authenticated by a single-use ticket in the query string, not the bearer header, and its path is absent from the OpenAPI file. The long poll is simpler and enough for a status line.
42. Section 10, security. barkd exposes a mnemonic endpoint (off by default) and a wallet delete endpoint under the same bearer token. The client does not wrap those routes at all.
43. Section 10, watch-only over Tor. httpx needs the `socks` extra, which is absent from `uv.lock`. `websockets` has no SOCKS support. Rule 4's single SOCKS5 proxy setting needs one more dependency for WebSocket backends.
44. Section 4.4, the ticker `BTC`. The Grayscale Bitcoin Mini Trust trades as `BTC`, which collides with the terminal's Bitcoin instrument code.
