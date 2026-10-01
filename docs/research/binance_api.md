# Binance USDⓈ-M Futures API — research notes for the Python bot

Research date: **2026-09-30**. Scope: USDⓈ-M perpetual futures (`/fapi`), REST + WebSocket market/user streams.

Safety note: No API keys were used. No signed/private endpoint was called and no order was placed. Only public
unauthenticated GET market-data endpoints (and public WebSocket market streams) were hit for verification.

## Source legend

Every fact below has a source tag. Tags:

| Tag | URL / meaning |
|---|---|
| `[GI]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info |
| `[CL]` | https://developers.binance.com/docs/derivatives/change-log (combined derivatives changelog) |
| `[WS]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams |
| `[WSN]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice |
| `[WSN2]` | Mirror of Binance notice "USDⓈ-M Futures WebSocket System Upgrade Notice (2026-03-06)": https://www.treeofalpha.com/preview_article?id=1772787601395 |
| `[WSAPI]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-api-general-info |
| `[QS]` | https://developers.binance.com/docs/derivatives/quick-start |
| `[FAQ]` | https://www.binance.com/en/support/faq/how-to-test-my-functions-on-binance-testnet-ab78f9a1b8824cf0a106b4229c76496d |
| `[ALGO]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order |
| `[QALGO]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Query-Algo-Order |
| `[ALGOUPD]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams/Event-Algo-Order-Update |
| `[ERR]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/error-code |
| `[CD]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/common-definition |
| `[EXI]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information |
| `[KL]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data |
| `[FR]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History |
| `[MP]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price |
| `[LB]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets |
| `[ACC3]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V3 |
| `[INC]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Get-Income-History |
| `[UT]` | https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Account-Trade-List |
| `[SDK]` | Official Binance Python SDK `binance-sdk-derivatives-trading-usds-futures` v17.5.0 (2026-09-23), generated from Binance's OpenAPI spec: https://github.com/binance/binance-connector-python/tree/master/clients/derivatives_trading_usds_futures (files: `src/.../rest_api/rest_api.py` docstrings, `rest_api/api/*.py` paths, `rest_api/models/*.py` field lists, `CHANGELOG.md`) |
| `[SDK-C]` | https://github.com/binance/binance-connector-python/blob/master/common/src/binance_common/constants.py |
| `[SDK-U]` | https://github.com/binance/binance-connector-python/blob/master/common/src/binance_common/utils.py (signing, retries, rate-limit header parsing) |
| `[LIVE]` | Verified by a live public request from this machine on 2026-09-30 |
| `[3P]` | Third-party (GitHub issues / repos) — used only as corroboration |

Note on the docs site: developers.binance.com now renders many per-endpoint pages as a large combined "catalog" page;
automated fetches were sometimes truncated or summarized. Where the docs page could not be read verbatim, field lists
were taken from the official SDK models (`[SDK]`), which are generated from Binance's own OpenAPI spec. Such cases are
marked.

---

## 0. What changed recently (must-know before coding)

1. **Conditional orders moved to the Algo Order API (effective 2025-12-09).** `STOP`, `STOP_MARKET`, `TAKE_PROFIT`,
   `TAKE_PROFIT_MARKET`, `TRAILING_STOP_MARKET` must be placed with `POST /fapi/v1/algoOrder` (`algoType=CONDITIONAL`,
   `triggerPrice` instead of `stopPrice`). Sending them to `POST /fapi/v1/order` (and batchOrders / WS `order.place`)
   returns **`-4120 STOP_ORDER_SWITCH_ALGO`** "Order type not supported for this endpoint. Please use the Algo Order API
   endpoints instead." `[CL 2025-11-06]` `[ERR]` `[3P freqtrade#12610, nautilus_trader#3287]`
2. `CONDITIONAL_ORDER_TRIGGER_REJECT` user-stream event deprecated 2025-12-15; rejections now come in `ALGO_UPDATE`. `[CL 2025-12-10]`
3. `MAX_NUM_ALGO_ORDERS` filter removed from exchangeInfo (2025-12-29); conditional-order limit is **200 across all
   symbols** (account-wide). `[CL 2025-12-29]` Verified: no symbol has `MAX_NUM_ALGO_ORDERS` today. `[LIVE]`
4. **WebSocket URL routing (legacy URLs retired 2026-04-23):** market streams must use
   `wss://fstream.binance.com/market/...` (klines, markPrice, aggTrade, tickers, forceOrder...) or `/public/...`
   (bookTicker, depth); user data uses `/private/...`. An un-routed URL only gets `/public` streams. `[WS]` `[WSN]` `[CL 2026-04-02, 2026-03-05]`
   Verified: `wss://fstream.binance.com/ws/btcusdt@kline_1m` delivered **nothing** in 6 s, while
   `wss://fstream.binance.com/market/ws/btcusdt@kline_1m` delivered kline events immediately. `[LIVE]`
5. **Futures testnet = Demo Trading.** Docs list testnet REST `https://demo-fapi.binance.com` and WS
   `wss://demo-fstream.binance.com`. `[GI]` The old web UI `https://testnet.binancefuture.com/` now returns
   **301 → `https://demo.binance.com/en/futures/BTCUSDT`**. `[LIVE]`
6. `avgPrice` / `cumQuote` removed from the **response** of `POST/DELETE /fapi/v1/order` per SDK spec 14.0.0
   (2026-07-15) `[SDK CHANGELOG]`; the docs response example still shows them `[docs trade page]`. Treat as optional. (see §9)
7. `GET /fapi/v1/userTrades` history reduced to **3 months** (2026-08-26). `GET /fapi/v1/allOrders` `symbol` now optional. `[CL 2026-08-26]`
8. `GET /fapi/v1/fundingRate` response gained `rateType` ("Regular" / "Special"). `[CL 2026-07-23]` `[LIVE]`
9. `POST /fapi/v1/positionSide/dual` now flips UM **and** CM together (shared `dualSidePosition`); new error `-4531`. `[SDK]` `[CL 2026-05-11]`

---

## 1. Environments / base URLs

| Purpose | Mainnet | Demo (= "testnet") | Source |
|---|---|---|---|
| REST | `https://fapi.binance.com` | `https://demo-fapi.binance.com` | `[GI]` `[SDK-C]` `[LIVE]` |
| REST (legacy testnet alias) | — | `https://testnet.binancefuture.com` (serves same data as demo-fapi) | `[SDK-C]` `[LIVE]` |
| WS market streams | `wss://fstream.binance.com/market/ws/<stream>` or `/market/stream?streams=a/b` | `wss://demo-fstream.binance.com/market/ws/<stream>` | `[WS]` `[WSN]` `[GI]` `[LIVE]` |
| WS high-freq public (bookTicker/depth) | `wss://fstream.binance.com/public/ws/<stream>` | `wss://demo-fstream.binance.com/public/...` (assumed) | `[WSN]` `[LIVE mainnet]` |
| WS user data | `wss://fstream.binance.com/private/ws?listenKey=<key>&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE` | `wss://demo-fstream.binance.com/private/ws?listenKey=...` (assumed, see confidence) | `[WSN]` `[WSN2]` |
| WS API (trading over WS) | `wss://ws-fapi.binance.com/ws-fapi/v1` | `wss://testnet.binancefuture.com/ws-fapi/v1` | `[WSAPI]` `[SDK-C]` |

Live evidence `[LIVE]`:
- `GET https://fapi.binance.com/fapi/v1/time` → `{"serverTime":1790770782215}`; demo-fapi and testnet.binancefuture.com both 200.
- `GET /fapi/v1/exchangeInfo` on demo-fapi and testnet.binancefuture.com returned byte-identical size (901,331 bytes) and a shared
  `x-mbx-used-weight-1m` counter (3 → 4) ⇒ same backend. Mainnet exchangeInfo is different (919 symbols vs 741 on demo).
- `wss://demo-fstream.binance.com/market/ws/btcusdt@kline_1m`, `wss://demo-fstream.binance.com/ws/btcusdt@kline_1m`,
  `wss://fstream.binancefuture.com/ws/btcusdt@kline_1m` all streamed the **same** demo kline (first trade id `f=542681087`),
  i.e. the demo market is its own matching engine (volumes differ from mainnet; prices track mainnet closely).

Confidence notes:
- Demo `/private` user-data path: not testable without a key. Demo WS host accepts both routed (`/market/ws`) and legacy
  (`/ws`) paths today `[LIVE]`, so try `/private/ws?listenKey=...&events=...` first and fall back to `/ws/<listenKey>`. **Medium.**
- Whether `events=` is mandatory on `/private`: notice examples always pass it `[WSN2]`. Pass it explicitly. **Medium.**
- Demo exchange rules differ from mainnet (e.g. BTCUSDT stepSize 0.0001 on demo vs 0.001 mainnet; MAX_NUM_ORDERS 10000 vs 200;
  marketTakeBound 0.30 vs 0.05; REQUEST_WEIGHT limit 6000/min vs 2400/min) `[LIVE]` ⇒ always load filters from the host you trade on.

### WebSocket connection rules `[WS]` `[WSAPI]`
- Connection valid max **24 h** → reconnect proactively.
- Server sends ping every **3 min**; if no pong for **10 min** the connection is dropped (websockets libs auto-pong).
- Max **1024 streams** per connection `[WS]` `[CL 2025-07-02]`; incoming (client→server) messages max **10/s**.
- Combined stream payload: `{"stream":"<name>","data":<payload>}`. Live-verified `[LIVE]`.
- Stream names are lowercase: `btcusdt@kline_1m`, `btcusdt@markPrice@1s`, `btcusdt@bookTicker`.
- Stream → path mapping `[WSN]`: **/public** = `@bookTicker`, `!bookTicker`, `@depth<levels>`, `@depth`;
  **/market** = `@aggTrade`, `@markPrice`, `@kline_<i>`, `@continuousKline`, `@miniTicker`, `@ticker`, `@forceOrder`,
  composite index, contract info, asset index; **/private** = user data (listenKey).

### Kline stream payload (live) `[LIVE]` `[SDK]`
```json
{"e":"kline","E":1790771371858,"s":"BTCUSDT","k":{"t":1790771340000,"T":1790771399999,"s":"BTCUSDT","i":"1m",
 "f":8132503391,"L":8132505633,"o":"83896.40","c":"83860.20","h":"83920.00","l":"83860.10","v":"114.602","n":2239,
 "x":false,"q":"9615538.66780","V":"48.488","Q":"4068722.10360","B":"0"}}
```
`k.x` = is this kline closed. Only act on `x == true` for closed-bar strategies. Update speed 250 ms. `[SDK]`
Mark price stream fields: `e, E, s, p (mark), i (index), P (est. settle), r (funding rate), ap (moving-avg mark, added 2026-03-16), T (next funding), st`. `[SDK]` `[CL 2026-03-16]`

---

## 2. Getting test (Demo Trading) API keys — step by step

1. Log in with a normal Binance account at **https://demo.binance.com/en/futures** (the demo environment; create/activate
   the demo account if prompted). `[FAQ]` `[QS]` (legacy `testnet.binancefuture.com` redirects here `[LIVE]`)
2. Open **API Management**: account icon (top right) → API Management, or directly
   **https://demo.binance.com/en/my/settings/api-management**. `[FAQ]`
3. Click **Create API**, give it a label, generate. Save API key + secret immediately (secret shown once). `[FAQ]`
4. Use these keys **only** with `https://demo-fapi.binance.com` / `wss://demo-fstream.binance.com`. Demo keys and
   mainnet keys are separate and not interchangeable. `[GI]` `[3P gunbot/ccxt]`
5. Recommended: HMAC (system-generated) keys for simplicity; restrict by IP if possible; never enable withdrawals. `[QS]`

Confidence: exact UI labels (e.g. key-type choice HMAC/Ed25519/RSA on the demo site) could not be confirmed — **medium**.
The FAQ page `[FAQ]` also references Spot testnet (`testnet.binance.vision`), which is a different environment.

---

## 3. Signed request format

### Security types `[GI]` `[SDK]`
- `NONE` (market data): no key.
- `USER_STREAM` (listenKey endpoints): **API key header only**, no signature.
- `TRADE` / `USER_DATA`: API key header **and** `timestamp` + `signature`.

### HMAC-SHA256 `[GI]`
- Header: `X-MBX-APIKEY: <apiKey>`.
- `signature = hex(HMAC_SHA256(secret, totalParams))` where `totalParams` = query string concatenated with request body.
- Params: `timestamp` (ms, required), `recvWindow` (optional, default **5000**, max **60000** `[LB]` `[INC]`).
- Server check: `if timestamp < serverTime + 1000 and serverTime - timestamp <= recvWindow: process else reject` → `-1021`. `[GI]`
- GET: query string only. POST/PUT/DELETE: query string or `application/x-www-form-urlencoded` body; may mix; if a
  param is duplicated, query string wins. `[GI]`
- Signature is case-insensitive; must be URL-encoded if it contains `/` or `=` (relevant for RSA/Ed25519 base64). `[GI]`
- The official SDK puts **all** params (including `signature`) in the **query string**, even for POST/DELETE, signing
  `urlencode(params)` in insertion order `[SDK-U]` — simplest safe approach: build `urlencode(params)`, sign exactly
  that string, append `&signature=...`, send everything as query string.
- Symbols with non-ASCII (Chinese) names must be UTF-8 percent-encoded. `[CL 2025-10-09]`

### Official HMAC test vector `[GI]` (verified locally `[LIVE-compute]`)
```
apiKey    = dbefbc809e3e83c283a984c3a1459732ea7db1360ca80c5c2c8867408d28cc83
secretKey = 2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9
payload   = symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943
signature = 3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9
```
Confidence: **high** — recomputed with .NET HMACSHA256 and matched exactly. Note the docs page displays the query with
`timeInForce` before `quantity`, but the `echo | openssl` command and the published signature use the order above
(`...type=LIMIT&quantity=1&price=9000&timeInForce=GTC...`). With the other order the HMAC is
`ec11dcc17e67e47f0d3c3f513dfe9062307e37619c5c82ebaa8fe0bdf3d59519`. Use the exact payload above in unit tests.
The docs' "mixed query+body" example signature could not be reproduced from the text as extracted — **low confidence**;
avoid mixing in our client.

### RSA / Ed25519
- RSA (PKCS#8): sign payload with RSASSA-PKCS1-v1_5 + SHA-256, base64, URL-encode. Documented for fapi REST. `[GI]`
- Ed25519: **required** for WebSocket API `session.logon` `[WSAPI]`. For REST, the official SDK signs with Ed25519 when
  given an Ed25519 private key (`eddsa.new(key,"rfc8032")`, base64 signature over the same query string) `[SDK-U]`
  `signature.py`; the futures general-info page only shows HMAC and RSA examples. **Medium** confidence that Ed25519 works
  on fapi REST. Recommendation: use HMAC for v1.

### Time sync
- Maintain `offset = serverTime - localTime` from `GET /fapi/v1/time` (weight 1) at startup and every few minutes; use
  `timestamp = now_ms + offset`. On `-1021` (or `-5028` "outside of the ME recvWindow"), resync and retry once. `[GI]` `[ERR]`

---

## 4. Endpoints

Legend: W = IP request weight (per 1-min `REQUEST_WEIGHT`), OC = order-count cost. Paths/methods from `[SDK]` api files
unless noted; weights from `[SDK]` docstrings and docs pages.

### 4.1 Market data (public, no key)

| Endpoint | W | Params | Notes / response | Source |
|---|---|---|---|---|
| `GET /fapi/v1/ping` | 1 | – | `{}` | `[SDK]` |
| `GET /fapi/v1/time` | 1 | – | `{"serverTime": int}` | `[SDK]` `[LIVE]` |
| `GET /fapi/v1/exchangeInfo` | 1 | – | `timezone, serverTime, futuresType, rateLimits[], exchangeFilters[], assets[], symbols[]` | `[EXI]` `[LIVE]` |
| `GET /fapi/v1/klines` | 1/2/5/10 by limit | `symbol, interval, startTime?, endTime?, limit?` | array of arrays (below) | `[KL]` `[SDK]` `[LIVE]` |
| `GET /fapi/v1/fundingRate` | own limit: 500 req / 5 min / IP shared with `/fapi/v1/fundingInfo` | `symbol?, startTime?, endTime?, limit?` (default 100, max 1000) | `[{symbol, fundingTime, fundingRate, markPrice, rateType}]` ascending | `[FR]` `[SDK]` `[LIVE]` |
| `GET /fapi/v1/fundingInfo` | 0 (same 500/5min pool) | – | `[{symbol, adjustedFundingRateCap, adjustedFundingRateFloor, fundingIntervalHours, disclaimer, updateTime}]` — only symbols with non-default settings | `[SDK]` `[LIVE]` |
| `GET /fapi/v1/premiumIndex` | 1 with symbol, 10 without | `symbol?` | `{symbol, markPrice, indexPrice, estimatedSettlePrice, lastFundingRate, interestRate, nextFundingTime, time}` (array without symbol) | `[MP]` `[SDK]` `[LIVE]` |
| `GET /fapi/v1/markPriceKlines` | as klines | `symbol, interval, ...` | mark-price candles | `[SDK]` |

**exchangeInfo `rateLimits` (live)** `[LIVE]`:
mainnet `[{"REQUEST_WEIGHT","MINUTE",1,2400},{"ORDERS","MINUTE",1,1200},{"ORDERS","SECOND",10,300}]`;
demo: REQUEST_WEIGHT **6000**/min, ORDERS 1200/min, 300/10s.

**Symbol fields** (live BTCUSDT) `[LIVE]` `[EXI]`: `symbol, pair, contractType ("PERPETUAL"), deliveryDate, onboardDate,
status ("TRADING"), maintMarginPercent, requiredMarginPercent, baseAsset, quoteAsset, marginAsset, pricePrecision,
quantityPrecision, baseAssetPrecision, quotePrecision, underlyingType, underlyingSubType, triggerProtect ("0.0500"),
liquidationFee, marketTakeBound ("0.05"), maxMoveOrderLimit, filters, orderTypes, timeInForce, permissionSets`.
`pricePrecision`/`quantityPrecision` are NOT tickSize/stepSize — always use filters. `[EXI]`
Filter statuses to trade: `status == "TRADING"`, `contractType == "PERPETUAL"`. Other contractTypes seen:
`CURRENT_QUARTER, NEXT_QUARTER, TRADIFI_PERPETUAL`; statuses `PENDING_TRADING, SETTLING, ...` `[LIVE]`.

**Filters** (live mainnet BTCUSDT) `[LIVE]`:
```json
[{"filterType":"PRICE_FILTER","tickSize":"0.10","minPrice":"556.80","maxPrice":"4529764"},
 {"filterType":"LOT_SIZE","stepSize":"0.001","maxQty":"1000","minQty":"0.001"},
 {"filterType":"MARKET_LOT_SIZE","minQty":"0.001","stepSize":"0.001","maxQty":"120"},
 {"filterType":"MAX_NUM_ORDERS","limit":200},
 {"filterType":"MIN_NOTIONAL","notional":"50"},
 {"filterType":"PERCENT_PRICE","multiplierUp":"1.0500","multiplierDown":"0.9500","multiplierDecimal":"4"},
 {"filterType":"POSITION_RISK_CONTROL","positionControlSide":"NONE"}]
```
(ETHUSDT MIN_NOTIONAL = 20.) Filter types present across all symbols today: `PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE,
MAX_NUM_ORDERS, MIN_NOTIONAL, PERCENT_PRICE, POSITION_RISK_CONTROL` `[LIVE]`.

Validation rules `[CD]` `[EXI]`:
- PRICE_FILTER: `minPrice <= price <= maxPrice`, `(price - minPrice) % tickSize == 0` (in practice: round to tickSize with Decimal).
- LOT_SIZE (LIMIT/others): `minQty <= qty <= maxQty`, `(qty - minQty) % stepSize == 0`.
- MARKET_LOT_SIZE: same rules, applies to MARKET orders (smaller maxQty, e.g. 120 BTC).
- MIN_NOTIONAL: `price * qty >= notional` (key is **`notional`**, not `minNotional`); for MARKET use mark/last price;
  reduce-only orders are exempt per `-4164` message. `[ERR]`
- PERCENT_PRICE (limit orders): BUY `price <= markPrice * multiplierUp`; SELL `price >= markPrice * multiplierDown`.
- `marketTakeBound`: max deviation (from mark) a MARKET order may execute at → `-4131` if book is too thin. `[EXI]` `[ERR]`
- Use `decimal.Decimal` and round **down** qty to stepSize; round prices to tickSize (stops: round away from entry conservatively).

**Klines** `[KL]` `[SDK]` `[LIVE]`:
- `interval` enum: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M`. `1s` → `-1120 Invalid interval.` on mainnet today `[LIVE]`.
- `limit`: default **500**, max **1500** (1501 → `-1130 Data sent for parameter 'limit' is not valid.`) `[LIVE]`.
- Weight by limit: `[1,100)`=1, `[100,500)`=2, `[500,1000]`=5, `>1000`=10 `[SDK]` (live check: limit 1500 cost 10 `[LIVE]`).
- No start/end → most recent klines. `startTime` only → klines from startTime ascending (verified). Klines identified by open time.
- Row layout (12 fields): `[0 openTime, 1 open, 2 high, 3 low, 4 close, 5 volume, 6 closeTime, 7 quoteVolume, 8 trades,
  9 takerBuyBaseVol, 10 takerBuyQuoteVol, 11 ignore]` — numbers are strings except times/trades.
- **The last row is the still-open candle**: live 1h request at t=1790770820448 returned last row open 1790769600000,
  close 1790773199999 (> now) `[LIVE]`. Drop rows with `closeTime >= serverTime` for closed-bar logic.
- Pagination for history: loop `startTime = lastOpenTime + intervalMs`, `limit=1500`.

### 4.2 Account (signed)

| Endpoint | W | Params | Response fields we need | Source |
|---|---|---|---|---|
| `GET /fapi/v3/account` (**use this**) | 5 | `recvWindow?, timestamp` | top: `totalInitialMargin, totalMaintMargin, totalWalletBalance, totalUnrealizedProfit, totalMarginBalance, totalPositionInitialMargin, totalOpenOrderInitialMargin, totalCrossWalletBalance, totalCrossUnPnl, availableBalance, maxWithdrawAmount, assets[], positions[]`; `assets[]`: `asset, walletBalance, unrealizedProfit, marginBalance, maintMargin, initialMargin, positionInitialMargin, openOrderInitialMargin, crossWalletBalance, crossUnPnl, availableBalance, maxWithdrawAmount, updateTime`; `positions[]`: `symbol, positionSide, positionAmt, unrealizedProfit, isolatedMargin, notional, isolatedWallet, initialMargin, maintMargin, updateTime` | `[ACC3]` `[SDK]` |
| `GET /fapi/v2/account` | 5 | same | legacy; returns all symbols. v3 only returns symbols with positions/open orders `[CL 2024-07-24]` | `[SDK]` |
| `GET /fapi/v3/balance` | 5 | `recvWindow?, timestamp` | array: `accountAlias, asset, balance, crossWalletBalance, crossUnPnl, availableBalance, maxWithdrawAmount, marginAvailable, updateTime` | `[SDK]` |
| `GET /fapi/v1/accountConfig` | 5 | – | `feeTier, canTrade, canDeposit, canWithdraw, dualSidePosition, updateTime, multiAssetsMargin, tradeGroupId` (one call gives both modes) | `[SDK]` |
| `GET /fapi/v1/symbolConfig` | 5 | `symbol?` | array: `symbol, marginType, isAutoAddMargin, leverage, maxNotionalValue` (**leverage + marginType live here now**) | `[SDK]` |
| `GET /fapi/v1/leverageBracket` | 1 | `symbol?` | `[{symbol, notionalCoef, brackets:[{bracket, initialLeverage, notionalCap, notionalFloor, maintMarginRatio, cum}]}]`; SDK models both list and single-object shapes → handle both | `[LB]` `[SDK]` |
| `GET /fapi/v1/positionSide/dual` | 30 | – | `{"dualSidePosition": bool}` (true = Hedge) | `[SDK]` |
| `GET /fapi/v1/multiAssetsMargin` | 30 | – | `{"multiAssetsMargin": bool}` | `[SDK]` |
| `GET /fapi/v1/income` | 30 | `symbol?, incomeType?, startTime?, endTime?, page?, limit?` (default 100, max 1000) | `[{symbol, incomeType, income, asset, info, time, tranId, tradeId}]`; no times → last 7 days; 3 months retention | `[INC]` `[SDK]` |
| `GET /fapi/v1/commissionRate` | – | `symbol` | maker/taker rates (not researched in depth) | `[SDK]` |

incomeType enum `[SDK]`: `TRANSFER, WELCOME_BONUS, REALIZED_PNL, FUNDING_FEE, COMMISSION, INSURANCE_CLEAR,
REFERRAL_KICKBACK, COMMISSION_REBATE, API_REBATE, CONTEST_REWARD, CROSS_COLLATERAL_TRANSFER, OPTIONS_PREMIUM_FEE,
OPTIONS_SETTLE_PROFIT, INTERNAL_TRANSFER, AUTO_EXCHANGE, DELIVERED_SETTELMENT, COIN_SWAP_DEPOSIT, COIN_SWAP_WITHDRAW,
POSITION_LIMIT_INCREASE_FEE, STRATEGY_UMFUTURES_TRANSFER, FEE_RETURN, BFUSD_REWARD, SPECIAL_FUNDING_FEE`.

### 4.3 Positions / settings (signed)

| Endpoint | W | Params | Response / notes | Source |
|---|---|---|---|---|
| `GET /fapi/v3/positionRisk` (**use this**) | 5 | `symbol?` | array (only symbols with position or open orders): `symbol, positionSide, positionAmt, entryPrice, breakEvenPrice, markPrice, unRealizedProfit, liquidationPrice, isolatedMargin, notional, marginAsset, isolatedWallet, initialMargin, maintMargin, positionInitialMargin, openOrderInitialMargin, adl, bidNotional, askNotional, updateTime`. **No `leverage`/`marginType`** → use `/fapi/v1/symbolConfig`. In one-way mode `positionSide="BOTH"` and sign of `positionAmt` = direction. | `[SDK]` (model) `[3P]` |
| `GET /fapi/v2/positionRisk` | 5 | `symbol?` | legacy (all symbols, includes leverage/marginType) | `[SDK]` |
| `POST /fapi/v1/positionSide/dual` | 1 | `dualSidePosition="true"|"false"` | `{"code":200,"msg":"success"}`; already set → `-4059`; open orders/positions (UM **or** CM) → `-4067`/`-4068`; `-4531` UM/CM sync | `[SDK]` `[ERR]` `[CL 2026-05-11]` |
| `POST /fapi/v1/multiAssetsMargin` | 1 | `multiAssetsMargin="true"|"false"` | `{code,msg}`; already set → `-4171` | `[SDK]` `[ERR]` |
| `POST /fapi/v1/leverage` | 1 | `symbol, leverage` (int) | `{leverage, maxNotionalValue, symbol}`; invalid → `-4028`; isolated w/ position reducing lev → `-4161` | `[SDK]` `[ERR]` |
| `POST /fapi/v1/marginType` | 1 | `symbol, marginType="ISOLATED"|"CROSSED"` | `{code,msg}`; **already set → `-4046 NO_NEED_TO_CHANGE_MARGIN_TYPE`** (treat as success); open orders → `-4047`; open position → `-4048` | `[SDK]` `[ERR]` |

### 4.4 Orders (signed)

| Endpoint | Cost | Params | Response | Source |
|---|---|---|---|---|
| `POST /fapi/v1/order` | OC 1 (10s) + 1 (1m); IP W 0 | `symbol, side (BUY/SELL), type (LIMIT/MARKET only in practice), positionSide? (BOTH default; LONG/SHORT required in Hedge), timeInForce (LIMIT: required), quantity, price (LIMIT), reduceOnly ("true"/"false"; not allowed in Hedge), newClientOrderId (^[.A-Z:/a-z0-9_-]{1,36}$, unique among open orders), newOrderRespType (ACK default / RESULT), priceMatch?, selfTradePreventionMode? (default EXPIRE_MAKER), goodTillDate?, recvWindow?, timestamp` | `clientOrderId, cumQty, executedQty, orderId, origQty, price, reduceOnly, side, positionSide, status, stopPrice, closePosition, symbol, timeInForce, type, origType, updateTime, workingType, priceProtect, priceMatch, selfTradePreventionMode, goodTillDate` (+ possibly `avgPrice`, `cumQuote`, see §9) | `[SDK]` |
| `POST /fapi/v1/order/test` | – | same as new order | validates without matching engine (use for LIMIT/MARKET only) | `[SDK]` |
| `DELETE /fapi/v1/order` | W 1 | `symbol, orderId | origClientOrderId` | same shape as order | `[SDK]` |
| `DELETE /fapi/v1/allOpenOrders` | W 1 | `symbol` | `{code,msg}` — **regular orders only** (algo orders need `DELETE /fapi/v1/algoOpenOrders`) | `[SDK]` |
| `GET /fapi/v1/openOrders` | W 1 with symbol / 40 without | `symbol?` | array of orders — **excludes algo orders** | `[SDK]` |
| `GET /fapi/v1/openOrder` | W 1 | `symbol, orderId|origClientOrderId` | order; filled/cancelled → "Order does not exist" | `[SDK]` |
| `GET /fapi/v1/order` | W 1 | `symbol, orderId|origClientOrderId` | `avgPrice, clientOrderId, cumQuote, executedQty, orderId, origQty, origType, price, reduceOnly, side, positionSide, status, stopPrice, closePosition, symbol, time, timeInForce, type, activatePrice, priceRate, updateTime, workingType, priceProtect, priceMatch, selfTradePreventionMode, goodTillDate`. Not found if CANCELED/EXPIRED w/o fills and >3 days old, or >90 days old. | `[SDK]` |
| `GET /fapi/v1/allOrders` | W 5 | `symbol? (optional since 2026-08-26), orderId?, startTime?, endTime?, limit? (500/1000)` | window < 7 days | `[SDK]` `[UT]` `[CL]` |
| `GET /fapi/v1/userTrades` | W 5 | `symbol, orderId?, startTime?, endTime?, fromId?, limit? (default 500, max 1000)` | `[{buyer, commission, commissionAsset, id, maker, orderId, price, qty, quoteQty, baseQty, marginAsset, realizedPnl, side, positionSide, symbol, pair, time}]`; no times → last 7 days; window ≤ 7 days; `fromId` not with times; **only past 3 months** | `[UT]` `[SDK]` `[CL 2026-08-26]` |
| `POST /fapi/v1/batchOrders` | OC 5 (10s) + 1 (1m); W 5 | `batchOrders` (JSON list, max 5) | per-order results/errors in list order | `[SDK]` |
| `POST /fapi/v1/countdownCancelAll` | W 10 | `symbol, countdownTime` (ms; 0 disables) | dead-man switch for regular open orders (call every ~30 s with 120000) | `[SDK]` |

Order status enum `[CD]`: `NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, EXPIRED, EXPIRED_IN_MATCH`.
`newOrderRespType=RESULT`: MARKET returns final FILLED result directly; IOC/FOK LIMIT returns final status. `[SDK]`
Idempotency: always send our own `newClientOrderId`; on timeout/unknown (HTTP 503 "Unknown error" / `-1007`) query
`GET /fapi/v1/order?origClientOrderId=...` before retrying — never blind-retry POST. `[GI]` `[SDK-U]` (SDK only auto-retries GET/DELETE on 5xx).

---

## 5. Conditional / protective orders — Algo Order API (current way)

Effective **2025-12-09** `[CL 2025-11-06]`. POST weight changed 2026-06-20 to order-rate-limit only (IP weight 0) `[CL 2026-06-20]`.

### Endpoints `[SDK]` `[ALGO]` `[QALGO]`
| Action | Method + path | Cost | Params |
|---|---|---|---|
| Place | `POST /fapi/v1/algoOrder` | OC 1 (10s) + 1 (1m); IP 0 | see below |
| Query one | `GET /fapi/v1/algoOrder` | W 1 | `algoId` or `clientAlgoId` |
| Cancel one | `DELETE /fapi/v1/algoOrder` | W 1 | `algoId` or `clientAlgoId` (camelCase; SDK fixed its lowercase `algoid` in v6.1.0, 2026-01-19 `[SDK CHANGELOG]`) |
| Cancel all on symbol | `DELETE /fapi/v1/algoOpenOrders` | W 1 | `symbol` |
| List open | `GET /fapi/v1/openAlgoOrders` | W 1 with symbol / 40 without | `algoType?, symbol?, algoId?` |
| History | `GET /fapi/v1/allAlgoOrders` | W 5 | `symbol, algoId?, startTime?, endTime?, limit?` (7-day window; `page` param removed 2026-04-20) |
| WS API | `algoOrder.place`, `algoOrder.cancel` | | `[CL 2025-11-06]` |

### Place parameters (`POST /fapi/v1/algoOrder`) `[ALGO]` `[SDK]`
`algoType=CONDITIONAL` (required), `symbol`, `side`, `type` ∈ `STOP_MARKET | TAKE_PROFIT_MARKET | STOP | TAKE_PROFIT | TRAILING_STOP_MARKET`,
`positionSide?` (BOTH default; must send LONG/SHORT in Hedge), `timeInForce?` (default GTC), `quantity?`, `price?` (STOP/TAKE_PROFIT limit price),
**`triggerPrice`** (replaces old `stopPrice`), `workingType?` (`MARK_PRICE` | `CONTRACT_PRICE`, default CONTRACT_PRICE),
`priceMatch?`, `closePosition?` ("true"/"false"; only with STOP_MARKET/TAKE_PROFIT_MARKET), `priceProtect?` ("true"/"false"),
`reduceOnly?` ("true"/"false"; not in Hedge Mode, not with closePosition=true), `activatePrice?` (trailing; renamed from
`activationPrice` 2025-12-22), `callbackRate?` (trailing, 0.1–10), `clientAlgoId?` (`^[.A-Z:/a-z0-9_-]{1,36}$`),
`newOrderRespType?` (ACK/RESULT), `selfTradePreventionMode?`, `goodTillDate?`, `recvWindow?`, `timestamp`.

Rules `[ALGO]` `[SDK]`:
- `closePosition=true`: on trigger closes **all** of the long (if SELL) / short (if BUY) position; cannot send `quantity`
  (`-4137`) nor `reduceOnly`; in Hedge Mode cannot be BUY+LONG or SELL+SHORT.
- Trigger: STOP/STOP_MARKET BUY when price ≥ triggerPrice, SELL when price ≤ triggerPrice; TAKE_PROFIT(_MARKET) BUY when
  price ≤ triggerPrice, SELL when price ≥ triggerPrice ("price" = MARK_PRICE or CONTRACT_PRICE per `workingType`).
- If it would trigger immediately → `-2021 Order would immediately trigger` (also possible: `-4142 ... will be triggered immediately`). `[ERR]`
- `priceProtect=true`: at trigger, |mark − last|/mark must be ≤ symbol `triggerProtect` (exchangeInfo).
- `triggerPrice` must satisfy PRICE_FILTER tickSize (`-1111` / `-4014` otherwise) `[3P MankhongGarden]`.
- Limit: **200 open conditional orders per account across all symbols** `[CL 2025-12-29]`; overflow → `-4045 MAX_STOP_ORDER_EXCEEDED` `[ERR]`.

Recommended protective stop for a one-way LONG:
```
POST /fapi/v1/algoOrder
algoType=CONDITIONAL&symbol=BTCUSDT&side=SELL&type=STOP_MARKET&triggerPrice=80000.0
&closePosition=true&workingType=MARK_PRICE&priceProtect=true&clientAlgoId=bot-sl-<uuid>&timestamp=...
```
Take-profit: same with `type=TAKE_PROFIT_MARKET`. Alternative (partial exits): `quantity=<q>&reduceOnly=true` instead of `closePosition`.

### Response (place) `[ALGO]` `[SDK]`
`algoId` (int64), `clientAlgoId`, `algoType`, `orderType`, `symbol`, `side`, `positionSide`, `timeInForce`, `quantity`,
`algoStatus`, `triggerPrice`, `price`, `icebergQuantity`, `selfTradePreventionMode`, `workingType`, `priceMatch`,
`closePosition`, `priceProtect`, `reduceOnly`, `activatePrice`, `callbackRate`, `createTime`, `updateTime`, `triggerTime`, `goodTillDate`.
Query/open-list responses add: `actualOrderId` (regular orderId created on trigger), `actualPrice`, `actualType`, `actualQty`,
`tpOrderType` (+ `tpTriggerPrice, tpPrice, slTriggerPrice, slPrice` still in openAlgoOrders/allAlgoOrders models). `[SDK]`
Cancel response: `{algoId, clientAlgoId, code, msg}`; cancel-all: `{code, msg}`. `[SDK]`

### algoStatus lifecycle `[ALGOUPD]` (via search summary of the official page) `[QALGO]`
`NEW` (in Algo Service, not triggered) → `TRIGGERING` (condition met, forwarding) → `TRIGGERED` (placed in matching engine) →
`FINISHED` (filled or cancelled in ME). Also `CANCELED`, `REJECTED` (ME rejected, e.g. margin check), `EXPIRED` (system
cancel, e.g. close-position/GTE_GTC order after the position is closed).

### ALGO_UPDATE user-stream event `[SDK]` `[CL]`
Top: `e="ALGO_UPDATE"`, `T`, `E`, `o{}`. `o` fields: `caid` clientAlgoId, `aid` algoId, `at` algoType, `o` orderType,
`s` symbol, `S` side, `ps` positionSide, `f` timeInForce, `q` qty, `X` algoStatus, `ai` actual orderId, `ap` avg fill price
(after trigger), `aq` executed qty, `act` actual order type, `tp` triggerPrice, `p` price, `V` STP, `wt` workingType,
`pm` priceMatch, `cp` closePosition, `pP` priceProtect, `R` reduceOnly, `tt` triggerTime, `gtd`, `rm` failure reason,
`ia` activated (trailing; live since 2026-08-21, trailing may send two pushes).

### Old endpoint behaviour
`POST /fapi/v1/order` / `POST /fapi/v1/batchOrders` / WS `order.place` with those 5 types → `-4120`
`{"code":-4120,"msg":"Order type not supported for this endpoint. Please use the Algo Order API endpoints instead."}`
`[ERR]` `[3P freqtrade#12610]`. SDK removed `stopPrice, closePosition, workingType, priceProtect, activationPrice,
callbackRate` from `new_order()` in v5.0.0 (2025-12-22). `[SDK CHANGELOG]` Note: docs "New Order" enum still lists the
conditional types (stale) — do not rely on it.

### Demo/testnet support
Third-party evidence that `/fapi/v1/algoOrder` exists on the futures testnet (unauthenticated probe returns `-2014`
rather than 404; a testnet smoke test places/lists/cancels STOP_MARKET via algo endpoints) `[3P]`. Official SDK uses the
same paths for testnet/demo base URLs. **Medium-high** confidence; verify in our own demo smoke test.

Confidence notes:
- One third-party repo claims the algo endpoint "doesn't honour closePosition" — contradicts official docs and SDK, which
  document `closePosition` for algo STOP_MARKET/TAKE_PROFIT_MARKET. Treat official as correct but **verify on demo**;
  keep a fallback path (`quantity` + `reduceOnly=true`). **Medium.**
- Same repo claims "10 algo orders per symbol"; official changelog says 200 account-wide. Trust official. **Medium-high.**

---

## 6. Rate limits and headers

- Limits (mainnet, live) `[LIVE]`: REQUEST_WEIGHT 2400/min per IP; ORDERS 1200/min and 300/10 s per account. Demo: 6000/min weight.
- Response headers `[GI]` `[SDK-U]`: `X-MBX-USED-WEIGHT-1M` (served lowercase as `x-mbx-used-weight-1m` `[LIVE]`),
  `X-MBX-ORDER-COUNT-10S`, `X-MBX-ORDER-COUNT-1M` (order endpoints). Parse case-insensitively with regex
  `x-mbx-(used-weight|order-count)-(\d+)([smhd])`.
- `/fapi/v1/fundingRate` returns **no** `x-mbx-used-weight-1m` header (own 500/5min/IP pool) `[LIVE]` `[FR]`.
- HTTP **429**: limit exceeded → stop and back off; honour `Retry-After` (seconds) if present. `[GI]` `[SDK-U]`
- HTTP **418**: IP auto-banned after continued 429s; ban grows **2 min → 3 days**; honour `Retry-After`. `[GI]`
- HTTP **503**: (a) "Unknown error, please check your request or try again later" = execution status UNKNOWN → verify
  (query order / user stream) before resubmitting; (b) "Service Unavailable" = not executed → retry with backoff
  200→400→800 ms; (c) `-1008` "Request throttled by system-level protection" → reduce concurrency; reduce-only /
  close-position orders are exempt. `[GI]`
- 4XX = client error (don't retry blindly); 5XX = server-side, status may be unknown. `[GI]`
- WS: max 10 incoming msgs/s per connection; violation disconnects, repeated → IP ban. `[WS]`
- Weight budget tips: `premiumIndex` without symbol = 10; `openOrders`/`openAlgoOrders` without symbol = 40;
  `positionSide/dual` & `multiAssetsMargin` GET = 30 (call once at startup, or use `accountConfig` = 5); `income` = 30.
- `GET /fapi/v1/rateLimit/order` (W 1) returns the account's current order-rate usage. `[SDK]`

---

## 7. Error codes and recommended handling `[ERR]`

| Code | Name / message | Bot handling |
|---|---|---|
| -1000 | UNKNOWN | treat as unknown status; verify then retry |
| -1001 | DISCONNECTED internal error | retry with backoff (idempotent via clientOrderId) |
| -1003 | TOO_MANY_REQUESTS | back off; lower request rate |
| -1007 | TIMEOUT "execution status unknown" | query by clientOrderId before any retry |
| -1008 | Request throttled (server overload) | back off; reduce concurrency |
| -1015 | TOO_MANY_ORDERS | back off on order rate |
| -1021 | INVALID_TIMESTAMP "outside of the recvWindow" | resync server time offset, retry once |
| -1022 | INVALID_SIGNATURE | bug/config → halt |
| -1102 / -1106 / -1128 / -1130 | param missing / not required / bad combo / invalid | bug → halt, log |
| -1111 | BAD_PRECISION | round to tickSize/stepSize (Decimal) — bug if seen |
| -1116 | INVALID_ORDER_TYPE | bug |
| -1120 | Invalid interval (e.g. `1s`) | bug `[LIVE]` |
| -2010 / -2011 | NEW_ORDER_REJECTED / CANCEL_REJECTED | log; cancel-rejected often = already filled → reconcile |
| -2013 | NO_SUCH_ORDER | reconcile state (probably filled/cancelled) |
| -2014 / -2015 | bad API key format / invalid key, IP or permission | halt, alert |
| -2018 / -2019 | balance / **MARGIN_NOT_SUFFICIEN** "Margin is insufficient." | skip trade, reduce size, alert |
| -2021 | ORDER_WOULD_IMMEDIATELY_TRIGGER | stop/TP on wrong side of price: if protecting an open position, close at market instead |
| -2022 | REDUCE_ONLY_REJECT | position already flat/smaller → reconcile position, don't retry |
| -2025 | MAX_OPEN_ORDER_EXCEEDED | cancel stale orders |
| -2027 / -2028 | max position at leverage / leverage too small | lower size or leverage |
| -4003 | QTY_LESS_THAN_ZERO | size rounded to 0 → skip trade |
| -4004 / -4005 | qty < minQty / > maxQty | clamp/skip |
| -4013 / -4014 / -4016 / -4024 | price < min / not tick multiple / above multiplierUp / below multiplierDown | fix rounding / PERCENT_PRICE |
| -4015 | invalid client order id | fix id format (≤36 chars, allowed charset) |
| -4023 | QTY_NOT_INCREASED_BY_STEP_SIZE | round qty down to stepSize |
| -4028 | INVALID_LEVERAGE | use bracket max |
| -4045 | MAX_STOP_ORDER_EXCEEDED | cancel orphan algo orders (200 account-wide) |
| **-4046** | NO_NEED_TO_CHANGE_MARGIN_TYPE | **treat as success** |
| -4047 / -4048 | margin type change blocked by open orders / position | skip, warn |
| **-4059** | NO_NEED_TO_CHANGE_POSITION_SIDE | **treat as success** |
| -4061 | POSITION_SIDE_NOT_MATCH | positionSide vs account mode mismatch → re-read mode |
| -4067 / -4068 | position mode change blocked by open orders / position | skip, warn |
| -4109 | INACTIVE_ACCOUNT | activate futures account (demo: open demo futures once) |
| -4116 | DUPLICATED_CLIENT_ORDER_ID | an earlier attempt succeeded → query it |
| -4118 | REDUCE_ONLY_MARGIN_CHECK_FAILED | reconcile position/open orders |
| **-4120** | STOP_ORDER_SWITCH_ALGO | conditional type sent to /fapi/v1/order → use /fapi/v1/algoOrder (bug) |
| -4131 | MARKET_ORDER_REJECT (PERCENT_PRICE / thin book) | retry smaller or as limit |
| -4137 / -4138 | quantity with closePosition / reduceOnly must be true | fix params |
| -4140 | INVALID_OPENING_POSITION_STATUS | symbol not open for new positions → skip |
| -4142 | take profit or stop would trigger immediately | as -2021 |
| **-4164** | MIN_NOTIONAL "Order's notional must be no smaller than 5.0 (unless you choose reduce only)" | size up to exchangeInfo `notional` (50 USDT for BTCUSDT today) or skip; message value is generic — use the filter |
| -4171 | NO_NEED_TO_CHANGE_JOINT_MARGIN (multi-assets already set) | treat as success |
| -4192 | COOLING_OFF_PERIOD | halt trading |
| -4400 / -4401 | quantitative / large-position rules → only reduceOnly allowed | stop opening, alert |
| -4425 | cannot switch to multi-asset (signal lead portfolio) | skip |
| -4531 | position mode UM/CM sync blocked (open CM positions/orders) | skip, warn |
| -5021 / -5022 | FOK / GTX (post-only) rejected | expected; re-price |
| -5028 | ME_RECVWINDOW_REJECT | as -1021 |

---

## 8. User data stream (listenKey) `[SDK]` `[WSN]`

- `POST /fapi/v1/listenKey` (W 1, header `X-MBX-APIKEY` only) → `{"listenKey": "..."}`; if one is active it is returned
  and extended 60 min.
- `PUT /fapi/v1/listenKey` (W 1) keepalive — stream closes after **60 min** without it; send every ~30–50 min.
- `DELETE /fapi/v1/listenKey` (W 1) close.
- Connect: `wss://fstream.binance.com/private/ws?listenKey=<key>&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE/ALGO_UPDATE`
  (demo: `wss://demo-fstream.binance.com/...`). Handle `listenKeyExpired` → recreate key + reconnect; reconnect every < 24 h.
- `ORDER_TRADE_UPDATE.o` fields `[SDK]`: `s, c (clientOrderId), S, o (type), f (TIF), q, p, ap (avg price), sp, x (exec type),
  X (status), i (orderId), M (modifyId), l (last qty), z (cum qty), L (last price), N (fee asset), n (fee), T, t (tradeId), b, a,
  m (maker), R (reduceOnly), wt, ot (orig type), ps, cp, AP, cr, pP, si, ss, rp (realized pnl), V, pm, gtd, er (expire reason)`.
- `ACCOUNT_UPDATE.a` `[SDK]`: `m` (reason), `B[]{a, wb, cw, bc}`, `P[]{s, pa, ep, bep, cr, up, mt, iw, ps}`, `S` (symbol for FUNDING_FEE, added 2026-08-07).

---

## 9. Discrepancies / open questions (verify on Demo before mainnet)

1. **`avgPrice` / `cumQuote` in `POST /fapi/v1/order` response** — Official SDK spec (v14.0.0, 2026-07-15) deleted both from
   new/cancel order responses; the changelog has matching Portfolio-Margin notices (2026-07-13/2026-08-03); the docs
   New Order example still shows them. Confidence that they're gone for `/fapi`: **medium**. Implementation: treat as
   optional; derive fill price from `GET /fapi/v1/order` (`avgPrice` still in the model), `userTrades`, or `ORDER_TRADE_UPDATE.ap`.
2. **closePosition on algo orders** — official: supported. Third-party: not honoured. Verify on demo (see §5).
3. **Ed25519 on REST** — SDK supports, fapi docs show HMAC/RSA only. Use HMAC. **Medium.**
4. **Demo `/private` path & `events` param** — assumed identical to mainnet. **Medium.**
5. **leverageBracket shape** — object vs array when `symbol` sent; SDK models both → handle both. **Medium.**
6. **Klines weight at limit=100** — docs `[100,500)`=2; one live reading looked like 1 (header shared with other traffic, noisy). Budget per docs.
7. **Docs "New Order" and "Test Order" pages still list STOP/TP types and `stopPrice`** — stale; the live API returns -4120.
8. Algo order history/lookup retention: not found if CANCELED/EXPIRED w/o fills and >3 days old, or >90 days. `[SDK]`

---

## 10. Implementation checklist (derived)

- Config: `BASE_URL` (fapi vs demo-fapi) + `WS_BASE` (fstream vs demo-fstream) switch; never mix keys/hosts.
- Startup: `GET /fapi/v1/time` (offset) → `GET /fapi/v1/exchangeInfo` (filters, cache & refresh hourly) →
  `GET /fapi/v1/accountConfig` (dualSidePosition, multiAssetsMargin) → set one-way mode (`-4059` ok) → set margin type
  (`-4046` ok) → set leverage → `GET /fapi/v3/account`, `GET /fapi/v3/positionRisk`, `GET /fapi/v1/openOrders?symbol=`,
  `GET /fapi/v1/openAlgoOrders?symbol=` → reconcile.
- Entry: MARKET via `/fapi/v1/order` with `newClientOrderId`, `newOrderRespType=RESULT`; then immediately place SL/TP via
  `/fapi/v1/algoOrder` (`closePosition=true`, `workingType=MARK_PRICE`, `priceProtect=true`, own `clientAlgoId`).
  If the SL placement fails (e.g. -2021), flatten with MARKET `reduceOnly=true`.
- Exit/cleanup: cancel both regular (`DELETE /fapi/v1/allOpenOrders`) and algo (`DELETE /fapi/v1/algoOpenOrders`) orders.
- Data: closed-bar logic drops last kline if `closeTime >= serverTime`; WS kline only when `k.x == true`.
- Rate limiting: track `x-mbx-used-weight-1m`, keep < ~70% of limit; handle 429/418 with `Retry-After`.
