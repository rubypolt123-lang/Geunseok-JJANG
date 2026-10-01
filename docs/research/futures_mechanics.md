# Binance USDⓈ-M Perpetual Futures: Mechanics for the Backtester and Risk Module

Research date: 2026-09-30. Scope: USDⓈ-M perpetuals (e.g. BTCUSDT), **isolated margin, one-way position mode**.

How it was checked:
- Official Binance pages (FAQ, fee FAQ, developer docs, change log). Links are in [Sources](#sources).
- Unauthenticated public GET calls to `fapi.binance.com`: `time`, `exchangeInfo`, `klines`, `markPriceKlines`, `fundingRate`, `fundingInfo`, `premiumIndex`.
- HEAD and listing requests to `data.binance.vision`. No zip files were downloaded.
- No API keys were used. No signed endpoints were called and no orders were placed.

Tags used below: **[verified-live]** means the fact was observed in a public API response on 2026-09-30. **[doc]** means it comes from official Binance documentation. **[derived]** means it is algebra from documented formulas. **[assumption]** means it is a modelling choice for this project.

---

## 1. Trading fees (USDⓈ-M, Regular user / VIP 0)

| Item | Value | Source |
|---|---|---|
| Maker | **0.0200 %** of notional | [doc] Binance Futures Fee Structure FAQ (updated 2026-05-01) |
| Taker | **0.0500 %** of notional | [doc] same |
| BNB fee deduction | **10 % off** USDⓈ-M fees, so 0.0180 % maker / 0.0450 % taker. It only applies when BNB is held in the USDⓈ-M futures wallet. Otherwise the fee is taken in USDT. | [doc] same |
| Fee base | `fee = qty × fill_price × rate`, charged on **both** the opening fill and the closing fill | [doc] same |

Backtester defaults **[assumption]**:
- `maker_fee = 0.0002` and `taker_fee = 0.0005`, with no BNB discount, so results stay conservative.
- Market entries, stop-market exits and liquidations are charged at the **taker** rate. Only resting limit orders that actually rest get the maker rate. When unsure, use taker.
- A taker round trip costs **0.10 % of notional**. At 10x leverage that is about **1 % of the margin used** per round trip, before slippage and funding.
- Account-specific rates will be available from `GET /fapi/v1/commissionRate` once API keys exist. That endpoint is signed and was not called here. USDC-margined contracts often run promotional fees that differ from these.

---

## 2. Funding

### 2.1 Interval and schedule
- **Default: every 8 h at 00:00, 08:00 and 16:00 UTC.** Only positions **open at the exact funding timestamp** pay or receive funding. Closing before the timestamp avoids it. [doc] Funding-rate FAQ
- **Many symbols no longer use 8 h.** A live `GET /fapi/v1/fundingInfo` call (weight 0) returned 803 symbols: **333 at 8 h, 469 at 4 h, 1 at 1 h** [verified-live]. BTCUSDT and ETHUSDT returned `fundingIntervalHours=8`, `adjustedFundingRateCap=0.003` and `Floor=-0.003`. LPTUSDT history showed 4 h spacing.
- Per the docs, `fundingInfo` only lists symbols whose cap, floor or interval has been adjusted. **A symbol that is missing from it should be treated as 8 h.** [doc]
- Other detection options:
  - `premiumIndex.nextFundingTime`.
  - The spacing between consecutive `fundingTime` values in the history.
- **Binance can change a symbol's interval over time.** A backtest must never assume a fixed interval. It should charge funding at the **actual `fundingTime` timestamps** from the history. [assumption, best practice]

### 2.2 Historical rates
`GET /fapi/v1/fundingRate` [doc + verified-live]

**Parameters:** `symbol`, `startTime`, `endTime` (both inclusive, in ms), and `limit`. The default limit is 100 and the **maximum is 1000**.

**Behaviour:**
- With no start or end time, the endpoint returns the most recent 200 records.
- If there are more records than `limit`, it returns `startTime + limit` records.
- Results are in **ascending** order.

**Rate limit:** it shares a **500 requests / 5 min / IP** limit with `/fapi/v1/fundingInfo`.

**Pagination:** set `startTime = last.fundingTime + 1` and repeat until fewer than `limit` rows come back. A live call returned 1000 rows starting 2024-01-01.

**Response fields:** `symbol`, `fundingTime`, `fundingRate`, `markPrice`, and `rateType` (for example `"Regular"`).

`markPrice` is the mark price used for that settlement, so use it for the payment notional.

**Bulk history:** monthly zip files at `data.binance.vision/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-YYYY-MM.zip` (see §5). There are **no daily** fundingRate files; the daily URL returned 404.

### 2.3 Payment formula and sign
```
funding_payment = position_qty_signed × mark_price_at_funding × funding_rate
  position_qty_signed > 0 for long, < 0 for short
  payment > 0  => the position PAYS   (wallet -= payment)
  payment < 0  => the position RECEIVES
```
- A **positive rate means longs pay shorts**. A negative rate means shorts pay longs. [doc]
- The FAQ states the formula as "Funding Amount = Nominal Value of Positions × Funding Rate", with nominal value taken at the mark price. [doc]

**Rate construction, for reference [doc]:**
- `F = Premium Index + clamp(Interest − Premium Index, ±0.05%)`.
- Interest is 0.03 %/day, i.e. 0.01 % per 8 h interval. `premiumIndex.interestRate = 0.0001` [verified-live].
- The rate is capped and floored. BTCUSDT: cap and floor are ±0.3 % [verified-live via `fundingInfo`].

**Isolated margin:** a funding settlement changes that position's isolated wallet balance, so it **moves the liquidation price**. [doc]

**Backtest charging rule [assumption]:** charge funding at timestamp `ft` when `entry_ts < ft ≤ exit_ts`.
- A fill at a bar open that equals `ft`, such as 00:00:00, happens just after settlement.
- So an entry at `ft` does **not** pay, and an exit at `ft` **does** pay.

**Sample, verified live:** the last 5 BTCUSDT rates ranged from -0.0000244 to +0.0000770 per 8 h, i.e. -0.0024 % to +0.0077 %.

---

## 3. Isolated margin in one-way mode: margin and liquidation

### 3.1 Initial and maintenance margin
```
notional      = qty × price                  (mark price for MM and risk, entry price for IM)
IM (initial)  = notional_at_entry / leverage
MM (maint.)   = notional_at_mark × MMR_bracket − cum_bracket          [doc]
liquidation when  isolated_margin_balance = WB + UPNL(mark) ≤ MM
```
- `WB` is the isolated wallet balance of the position: IM, plus any added margin, plus or minus funding. It is the "isolatedWalletBalance" in Binance's formula. [doc]
- **Leverage brackets.** `GET /fapi/v1/leverageBracket` returns per symbol, per tier: `bracket`, `initialLeverage` (the tier's max leverage), `notionalFloor`, `notionalCap`, `maintMarginRatio`, and `cum` (the "maintenance amount" used for quick calculation). [doc]
  - **This endpoint is signed (USER_DATA), so it cannot be called without keys.**
  - Until keys exist, keep brackets in a config file copied by hand from binance.com → Futures → Trading Rules → Leverage & Margin. That page renders with JS and could not be scraped.
  - Refresh the config periodically, because Binance changes tiers by announcement.
  - The documented example tier is `{bracket:1, initialLeverage:75, notionalCap:10000, notionalFloor:0, maintMarginRatio:0.0065, cum:0}`. It is an example only and not BTC's real table.
- **Why `cum` exists [derived].** It keeps MM continuous at tier boundaries: `cum_k = cum_{k-1} + notionalFloor_k × (MMR_k − MMR_{k-1})`, with `cum_1 = 0`. This lets you sanity-check a hand-copied table.
- `exchangeInfo` also returns `maintMarginPercent` (BTCUSDT 2.5), `requiredMarginPercent` (5.0) and `liquidationFee` (0.0125 = **1.25 %**) [verified-live]. `maintMarginPercent` is a legacy field. Use the brackets for MM. `liquidationFee` is the clearance fee charged on liquidation.

### 3.2 Official liquidation price formula

The Binance FAQ publishes the formula as an image. Its variables are defined in the text: WB, TMM1, UPNL1, cumB/L/S, Side1BOTH, Position1BOTH, EP1BOTH, MMRB/L/S. Written out:
```
LP = ( WB − TMM1 + UPNL1 + cumB + cumL + cumS
       − Side1BOTH × Position1BOTH × EP1BOTH
       − Position1LONG × EP1LONG + Position1SHORT × EP1SHORT )
   / ( Position1BOTH × MMRB + Position1LONG × MMRL + Position1SHORT × MMRS
       − Side1BOTH × Position1BOTH − Position1LONG + Position1SHORT )
```
The FAQ also states: "In Isolated margin mode, WB is isolatedWalletBalance of the isolated position, TMM=0, UPNL=0". In one-way mode the LONG and SHORT terms are 0.

Setting `side = +1` for long and `−1` for short, and `Q = |qty|`, gives [derived]:
```
LP = (WB + cum − side·Q·EP) / (Q·MMR − side·Q)

LONG :  LP = (Q·EP − WB − cum) / (Q · (1 − MMR))
SHORT:  LP = (Q·EP + WB + cum) / (Q · (1 + MMR))
```
This comes from setting `WB + side·Q·(LP − EP) = Q·LP·MMR − cum`. It matches an independent open-source implementation (the gist in Sources).

**Bracket selection:** compute LP with each tier's `(MMR, cum)` and keep the tier whose `[notionalFloor, notionalCap)` contains `LP × Q`.

**Worked example [derived]:** BTC long, Q = 1, EP = 84,000, 10x leverage, so WB = IM = 8,400. Illustrative MMR = 0.004 and cum = 0.
- Long: LP = (84,000 − 8,400) / 0.996 = **75,903.61**, which is −9.64 %.
- Short with the same inputs: LP = (84,000 + 8,400) / 1.004 = **92,031.87**, which is +9.56 %.

### 3.3 Conservative approximation for the backtester [assumption]
With `WB = IM = Q·EP/L`, **setting cum = 0 is always conservative**. For a long it raises LP, and for a short it lowers LP. So use:
```
LONG :  LP ≈ EP · (1 − 1/L) / (1 − MMR*)
SHORT:  LP ≈ EP · (1 + 1/L) / (1 + MMR*)
MMR* = MMR of the tier that contains the position notional, plus a buffer (e.g. +0.5 %) for fees and slippage
```
Rules:
- A position is **liquidated in the backtest** if the bar's adverse extreme reaches LP. That is the low for a long and the high for a short. Use `markPriceKlines` to be exact, or last-price klines as a stricter proxy.
- When liquidated, assume the **whole isolated margin is lost**, i.e. PnL = −WB. That already covers the 1.25 % clearance fee.
- **Risk-module invariant:** the stop-loss must sit well inside the liquidation price. Reject any setup where `|EP − LP| < k × |EP − SL|`, with k ≥ 2.
  - With 10x leverage and a 2 % stop, LP is about 9.6 % away, so the setup is fine.
  - With 50x leverage, LP is about 1.6 % away and a 2 % stop is impossible.
- A cruder rule of thumb gives `LONG LP ≈ EP(1 − 1/L + MMR)`. It is slightly conservative for longs but **not** for shorts, so use the exact cum = 0 form above.

---

## 4. Mark price vs last price, and modelling stops

**Mark price** = median(Price 1, Price 2, last contract price) [doc]:
- Price 1 is the index plus a funding basis.
- Price 2 is the index plus a 30-s moving-average basis.

Mark price drives **unrealized PnL and liquidation** [doc].

**Stop orders:**
- Stop orders trigger on `workingType`: `CONTRACT_PRICE` (the default, i.e. last price) or `MARK_PRICE` [doc].
- Trigger rules [doc]:
  - **STOP / STOP_MARKET:** a BUY triggers when price ≥ triggerPrice. A SELL triggers when price ≤ triggerPrice.
  - **TAKE_PROFIT(_MARKET):** a BUY triggers when price ≤ triggerPrice. A SELL triggers when price ≥ triggerPrice.
- **`priceProtect=true` can block a trigger.** The trigger is blocked if the mark and last prices differ by more than the symbol's `triggerProtect` (BTCUSDT 0.05 = 5 %) [doc + verified-live]. During a dislocation the stop **may not fire**. Accept this or leave `priceProtect` off, and keep the liquidation distance large.
- **Since 2025-12-09, conditional order types go through the Algo Order service.** This covers STOP_MARKET, TAKE_PROFIT_MARKET, STOP, TAKE_PROFIT and TRAILING_STOP_MARKET.
  - Endpoint: `POST /fapi/v1/algoOrder` with `algoType=CONDITIONAL`, `triggerPrice`, `workingType`, `closePosition` and `clientAlgoId`.
  - `POST /fapi/v1/order` rejects these order types with **-4120 STOP_ORDER_SWITCH_ALGO**.
  - Order state updates arrive in the new user-stream event `ALGO_UPDATE`. [doc change log 2025-11-06]
- `workingType` choice [assumption]:
  - `CONTRACT_PRICE` is triggered by last-price wicks, which matches a backtest run on last-price klines.
  - `MARK_PRICE` ignores isolated wicks. A backtest for it should trigger on `markPriceKlines` and still fill at last price plus slippage.

**Stop-market fill model for the backtester [assumption]:**
```
LONG stop (sell) at S:
  if open ≤ S:            fill = open            # gap through the stop
  elif low ≤ S:           fill = S
  fill *= (1 − slip)      # e.g. slip = 0.0005–0.001 (5–10 bp) on BTC; more on alts
SHORT stop (buy) at S:
  if open ≥ S:            fill = open
  elif high ≥ S:          fill = S
  fill *= (1 + slip)
=> equivalently: fill = worse(trigger, bar_open) then apply adverse slippage; fee = taker
```
- **Take-profit as a resting LIMIT order:** it fills only if price trades **through** the level. Use `high > TP` (strict) for a long and `low < TP` for a short. It fills at TP with the maker fee.
- **Take-profit as a TAKE_PROFIT_MARKET order:** it fills at `max(TP, open)` for a long, then slippage and the taker fee apply.
- **Market entry on the next bar:** fill at `open × (1 ± slip)` with the taker fee.

---

## 5. Kline data

### 5.1 REST: `GET /fapi/v1/klines` [doc + verified-live]

**Parameters:** `symbol`, `interval`, `startTime`, `endTime`, `limit`. The default limit is 500 and the **maximum is 1500**. `limit=2000` returned HTTP 400 `-1130` [verified-live].

**Request weight measured from the `X-MBX-USED-WEIGHT-1M` header [verified-live]:**

| limit | weight |
|---|---|
| ≤ 100 | 1 |
| 101–500 | 2 |
| 501–1000 | 5 |
| > 1000 | 10 |

The docs describe the table as `[1,100)`: 1, `[100,500)`: 2, `[500,1000]`: 5, `>1000`: 10. The measured boundaries at 100 and 500 were slightly more lenient. Plan for the doc table.

**Row format:** each row has 12 fields.
`[openTime, open, high, low, close, volume, closeTime, quoteVolume, trades, takerBuyBase, takerBuyQuote, ignore]`

Prices are **strings**, so parse them as Decimal or float deliberately. `closeTime = openTime + interval − 1 ms`.

**The last row is the still-forming candle.** A live check found `closeTime > now` on the latest 1 h bar. **Always drop rows whose `closeTime ≥ serverTime`.**

**Pagination:**
- Set `startTime = last_openTime + interval_ms` and use `limit=1500`.
- `startTime` is inclusive: a 2024-01-01 00:00 start returned that bar first.
- `endTime` is inclusive of the bar that opens at `endTime`.
- Stop when fewer than `limit` rows come back or `startTime > end`.
- Deduplicate on `openTime`.
- Check for gaps: bars are missing during exchange maintenance, so flag them rather than forward-fill silently.

**Related public endpoints with the same shape:**
- `GET /fapi/v1/markPriceKlines`: volume fields are 0 [verified-live]. Use it for liquidation and mark-trigger checks.
- `GET /fapi/v1/indexPriceKlines` and `GET /fapi/v1/premiumIndexKlines`.

**Rate limits from `exchangeInfo` [verified-live]:**
- REQUEST_WEIGHT **2400 / min / IP**.
- ORDERS **1200 / min** and **300 / 10 s**.

### 5.2 Bulk data: data.binance.vision [verified-live via HEAD and S3 listing]
```
Monthly klines : https://data.binance.vision/data/futures/um/monthly/klines/{SYM}/{INT}/{SYM}-{INT}-{YYYY}-{MM}.zip
Daily klines   : https://data.binance.vision/data/futures/um/daily/klines/{SYM}/{INT}/{SYM}-{INT}-{YYYY}-{MM}-{DD}.zip
Funding (month): https://data.binance.vision/data/futures/um/monthly/fundingRate/{SYM}/{SYM}-fundingRate-{YYYY}-{MM}.zip
Mark/Index/Premium klines: replace "klines" with markPriceKlines | indexPriceKlines | premiumIndexKlines
Checksum       : same URL + ".CHECKSUM"  -> "<sha256hex>  <filename>.zip"
Listing (XML)  : https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/futures/um/monthly/klines/BTCUSDT/1h/
```
Folders that exist:
- **Monthly:** aggTrades, bookTicker, fundingRate, indexPriceKlines, klines, markPriceKlines, premiumIndexKlines, trades.
- **Daily:** aggTrades, bookDepth, bookTicker, indexPriceKlines, klines, markPriceKlines, metrics, premiumIndexKlines, trades. There is **no daily fundingRate**.

BTCUSDT monthly fundingRate files run from 2020-01 to 2026-08.

Notes:
- New daily files appear the next day. Monthly files appear on the first Monday of the following month [doc].
- Recommended plan: monthly zips for whole past months, daily zips for the current month, then REST for the last hours.
- The CSV column order is the same as the REST response.
- **Handle an optional header row**: skip the first line if it is non-numeric, because newer files include headers.
- **Timestamp units:** spot data from 2025-01-01 is in **microseconds** [doc]. Futures data is documented as ms, but detect defensively: if `ts > 1e14`, divide by 1000.
- Verify each download against its `.CHECKSUM` (SHA-256).

---

## 6. Backtest pitfalls and best-practice checklist

1. **No look-ahead.** Compute signals only from **closed** bars. Fill at the **next bar's open**. Stops and targets set at entry are evaluated from the entry bar onward, using bars after the fill.
2. **Indicator warm-up.** Drop the first `max(lookback) × ~3` bars. EMA and ATR need several periods to converge. Never trade on NaN or partial values.
3. **Intrabar SL/TP ambiguity.** If a bar touches both the SL and the TP, **assume the SL came first**. Optionally resolve it with 1 m bars.
4. **Fees on both legs:** taker on market and stop fills, maker only for true resting limit orders.
5. **Slippage.** Apply adverse slippage in basis points to every market and stop fill. Also apply gap fills per §4.
6. **Funding.** Charge it at actual `fundingTime` timestamps using the §2.3 rule and the history's `markPrice`.
7. **Liquidation check** per §3.3, using a conservative LP.
8. **Sizing from current equity**, not initial equity:
   ```
   risk_$        = equity × risk_pct                         (e.g. 0.5–1 %)
   per_unit_loss = |entry − stop| + entry×taker + stop×taker + entry×slip
   qty_raw       = risk_$ / per_unit_loss
   qty           = floor_to_step( min(qty_raw, equity × max_lev / entry, bracket_cap / entry) )
   skip trade if qty < minQty or qty × entry < MIN_NOTIONAL
   ```
9. **Exchange rules in the simulation.** Floor qty to `stepSize`, round prices to `tickSize`, and apply MIN_NOTIONAL. For BTCUSDT on 2026-09-30 [verified-live]:
   - tickSize 0.10, stepSize 0.001, minQty 0.001.
   - MIN_NOTIONAL **50 USDT**. ETHUSDT is 20.
   - MARKET_LOT_SIZE maxQty 120.
10. **Overfitting control:**
    - Split in time: in-sample, then out-of-sample and walk-forward.
    - Keep the parameter set small and report every variant you tried.
    - Prefer parameter plateaus to single peaks.
    - Test on several symbols and regimes.
    - Consider the Deflated Sharpe Ratio and the Probability of Backtest Overfitting (Bailey & López de Prado).
11. **Survivorship.** Delisted symbols disappear from `exchangeInfo`. Note the survivorship bias if the universe is built from today's list.
12. **Metric definitions:**

| Metric | Definition |
|---|---|
| Total return | `E_end / E_start − 1` |
| CAGR | `(E_end / E_start)^(365.25 d / days_elapsed) − 1` |
| Max drawdown | `max_t (1 − E_t / max_{s≤t} E_s)` on the **mark-to-market** equity curve, not only on closed trades |
| Sharpe | `mean(r) / std(r) × sqrt(N)` on per-bar equity returns, including flat bars, with rf ≈ 0. Crypto trades 24/7, so N per year is: 1m 525,600 · 5m 105,120 · 15m 35,040 · 1h 8,760 · 4h 2,190 · 1d 365. Better: resample to daily returns and use √365. |
| Sortino | same as Sharpe but with downside deviation |
| Win rate | `#(net PnL > 0) / #trades`, net of fees and funding |
| Profit factor | `Σ winning net PnL / |Σ losing net PnL|` |
| Expectancy | `mean(net PnL per trade)` = `WR·avgWin − (1−WR)·|avgLoss|`. Also report it in R multiples. |
| Exposure | `bars_in_position / total_bars` (time in market) |
| Also report | #trades, average holding time, total fees, total funding, liquidations (should be 0), worst trade, longest drawdown duration |

---

## 7. Live-bot robustness checklist
*(for later, when keys exist. Everything below requires signed endpoints and was **not** exercised.)*

1. **Server time offset.**
   - Measure `offset = serverTime − local_now` at startup and every few minutes via `GET /fapi/v1/time`.
   - On this machine, on 2026-09-30, the local clock was **~3.7 s behind** Binance [verified-live].
   - Signed requests are rejected (−1021) unless `timestamp < serverTime + 1000` **and** `serverTime − timestamp ≤ recvWindow`. The default `recvWindow` is 5000 ms. Keep it small, e.g. 5000, and sync the Windows clock.
2. **Candle-close timing.**
   - Act only on closed candles: WebSocket kline field `x == true`, or REST rows with `closeTime < serverTime`.
   - After the boundary, wait about 1–3 s, fetch, and check that the expected `openTime` is present. Retry if it is not.
   - Record the processed bar's `openTime` so the same signal is never acted on twice.
3. **WebSocket URLs changed in 2026.**
   - Market streams (klines, markPrice, aggTrade) use `wss://fstream.binance.com/market/...`.
   - Book streams use `/public`.
   - User data uses `/private/ws?listenKey=...&events=...`.
   - Legacy URLs were retired on 2026-04-23 [doc].
   - A listenKey expires after 60 min without a keepalive.
   - Connections are recycled roughly every 24 h. Reconnect with backoff and backfill via REST.
4. **Idempotent orders.**
   - Use a deterministic `newClientOrderId` or `clientAlgoId`, e.g. `b1-BTC-1h-<barOpenTs>-E`. It must match `^[\.A-Z\:/a-z0-9_-]{1,36}$` and is unique **among open orders** only [doc].
   - After a timeout or HTTP 503 "Unknown error...", the execution status is **unknown** [doc]. Query by client ID before any retry. Never blindly resend.
5. **Restart reconciliation.** On boot:
   - Load local state from a DB.
   - Fetch positions, open orders **and open algo orders**.
   - Diff them against local state, then fix:
     - Position without a stop: place the stop immediately.
     - Stop without a position: cancel it.
     - Quantity mismatch: adopt the exchange state and alert.
   - Check position mode is one-way, and check margin type and leverage for each symbol.
6. **Partial fills.**
   - Use `executedQty` and `avgPrice` from `newOrderRespType=RESULT` or from `ORDER_TRADE_UPDATE`.
   - Size protective orders from the **actual** position.
   - `closePosition=true` stops avoid quantity mismatch.
   - Market orders can partially fill or expire because of `marketTakeBound` (5 % on BTC).
7. **Protective orders on the exchange.**
   - Place the SL, a STOP_MARKET with `closePosition=true` via `/fapi/v1/algoOrder`, **immediately after the entry fill**, so it survives a bot crash.
   - Never rely on a client-side stop.
   - If the SL placement fails, flatten the position with a reduceOnly market order.
8. **Detect manual intervention.**
   - Look for orders or positions whose client ID lacks the bot prefix, and for position quantity that changed without a bot fill.
   - On detection: pause new entries and alert.
9. **Rate limits.**
   - Track the `X-MBX-USED-WEIGHT-1M` and `X-MBX-ORDER-COUNT-*` headers.
   - Back off on 429. A 418 means an IP ban of 2 min up to 3 days.
   - Prefer WebSockets over polling.
10. **Kill switch.**
    - Triggers: max daily loss, max drawdown, N consecutive losses, repeated API errors, stale market data, clock skew above threshold, or reconciliation mismatch.
    - Actions: halt new entries. Optionally cancel entries and flatten with reduceOnly orders. **Keep the SLs** unless the position is flattened.
    - Add a manual stop file or flag as well.
11. **Precision.**
    - Use `Decimal`.
    - Quantity: **floor** to `stepSize`. Use `MARKET_LOT_SIZE` for market orders.
    - Prices: round to `tickSize`. Round the SL **toward entry** (tighter), never looser.
    - Respect `PERCENT_PRICE` (±5 % on BTC) for limit prices.
12. **Min notional.**
    - Opening orders need `qty × price ≥ MIN_NOTIONAL` (BTCUSDT 50 USDT). Otherwise the error is −4164. The error text says reduce-only orders are exempt. [from memory: verify on testnet]
    - Re-read `exchangeInfo` daily, because filters change.
13. **Safety defaults.**
    - Start on the testnet and in paper mode. The testnet REST host is `demo-fapi.binance.com` and its WS host is `stream.binancefuture.com`. [unverified: re-check both in the General Info docs before use]
    - Use keys **with withdrawals disabled and an IP whitelist**.
    - Load keys from environment variables or `.env`, never from code or git.

---

## Sources
- Binance Futures Fee Structure & Fee Calculations (updated 2026-05-01): https://www.binance.com/en/support/faq/binance-futures-fee-structure-fee-calculations-360033544231
- Introduction to Binance Futures Funding Rates: https://www.binance.com/en/support/faq/introduction-to-binance-futures-funding-rates-360033525031
- How to Calculate Liquidation Price of USDⓈ-M Futures Contracts (updated 2025-12-31): https://www.binance.com/en/support/faq/b3c689c1f50a44cabb3a84e663b81d93
- Leverage and Margin of USDⓈ-M Futures: https://www.binance.com/en/support/faq/leverage-and-margin-of-usd%E2%93%A2-m-futures-360033162192
- Leverage & Margin tier tables (JS-rendered, copy by hand): https://www.binance.com/en/futures/trading-rules/perpetual/leverage-margin
- Binance Futures Liquidation Protocols: https://www.binance.com/en/support/faq/binance-futures-liquidation-protocols-360033525271
- What Is the Mark Price and Price Index: https://www.binance.com/en/support/faq/what-is-the-mark-price-and-price-index-360033525071
- API docs, Funding Rate History: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History
- API docs, Funding Rate Info: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-Info
- API docs, Notional and Leverage Brackets (USER_DATA): https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets
- API docs, New Order: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order
- API docs, New Algo Order: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
- API docs, General Info (limits, timing, 503 semantics): https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
- API docs, WebSocket change notice: https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Important-WebSocket-Change-Notice
- API docs, User Data Streams: https://developers.binance.com/docs/derivatives/usds-margined-futures/user-data-streams
- Derivatives change log (algo-order migration 2025-12-09, WS URL change 2026, etc.): https://developers.binance.com/docs/derivatives/change-log
- Binance public data (bulk files, CHECKSUM, timestamp note): https://github.com/binance/binance-public-data and https://data.binance.vision/
- Independent liquidation-formula implementation (cross-check): https://gist.github.com/highfestiva/b71e76f51eed84d56c1be8ebbcc286b5
- Bailey & López de Prado, The Deflated Sharpe Ratio: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Bailey, Borwein, López de Prado & Zhu, The Probability of Backtest Overfitting (SSRN 2326253)
- Live public API responses from `https://fapi.binance.com` on 2026-09-30: `/fapi/v1/time`, `/exchangeInfo`, `/klines`, `/markPriceKlines`, `/fundingRate`, `/fundingInfo`, `/premiumIndex`
