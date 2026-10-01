# Environment: Python 3.14 on Windows 11 for binance-futures-bot

Verified 2026-09-30 (KST). No API keys exist in this project; only public, unauthenticated
GET market-data endpoints were called. No accounts, no signed endpoints, no orders.

## 1. Interpreter and venv

| Item | Value |
|---|---|
| OS / shell | Windows 11 Home 10.0.26200, Windows PowerShell 5.1.26100 |
| System Python | CPython 3.14.6 (MSC v.1944, 64-bit, AMD64) |
| `python` on PATH | `%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` (Python install manager alias) -> real interpreter `%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe`; `py -0p` lists only 3.14-64 |
| venv | `.venv\` created with `python -m venv .venv`; `pyvenv.cfg` `home` points at `pythoncore-3.14-64` (not the WindowsApps alias), so the venv is sound |
| pip in venv | upgraded 26.1.2 -> 26.2.1 |
| LongPathsEnabled | 1 (enabled) |
| Execution policy | LocalMachine/CurrentUser = Undefined (Windows client default = Restricted). `Activate.ps1` will not run in a normal PowerShell window |
| Locale encoding | `locale.getpreferredencoding()` = **cp949**, UTF-8 mode off |
| Windows time | W32Time running but **not synchronized** (source: Local CMOS Clock, leap indicator 3) |

## 2. Installed packages (all binary wheels, no local compile)

Installed with `pip install --only-binary ":all:" ...` and `--report`; every artifact in the
report is a `.whl`. Smoke-imported and exercised (numpy math, pandas kline parse + EWM,
yaml/dotenv parse, FastAPI `TestClient`, `responses` mock of `requests`, live uvicorn serve on
127.0.0.1, pytest run). `pip check`: no broken requirements.

| Package (requested) | Version | Wheel tag | Smoke test |
|---|---|---|---|
| requests | 2.34.2 | py3-none-any | OK (real GETs + mocked) |
| pandas | 3.0.6 | cp314-cp314-win_amd64 | OK |
| numpy | 2.5.3 | cp314-cp314-win_amd64 | OK (BLAS: scipy-openblas) |
| pyyaml | 6.0.3 | cp314-cp314-win_amd64 | OK |
| python-dotenv | 1.2.3 | py3-none-any | OK |
| fastapi | 0.142.2 | py3-none-any | OK (TestClient) |
| uvicorn | 0.54.0 | py3-none-any | OK (served /health 200) |
| pytest | 9.1.1 | py3-none-any | OK (2 passed, `-W error`) |
| responses | 0.26.3 | py3-none-any | OK |
| httpx | 0.28.1 | py3-none-any | OK |
| httpx2 (added, dev) | 2.13.1 | py3-none-any | OK; removes Starlette TestClient warning |

Notable transitive: pydantic 2.13.5 / pydantic_core 2.46.5 (cp314 wheel), starlette 1.7.0,
anyio 4.15.1, charset-normalizer 3.5.2 (cp314 wheel), urllib3 2.8.0, certifi 2026.7.22,
tzdata 2026.4, opentelemetry-api 1.45.0 (pulled by fastapi), httpcore2 2.13.1 + truststore
0.10.4 (pulled by httpx2). Full list: `requirements-lock.txt`.

Optional packages checked with a wheel-only dry run (not installed) - all have cp314 wheels:
websockets 17.1, pyarrow 25.0.1, httptools 0.8.0, watchfiles 1.3.0 (`uvicorn[standard]`
resolves wheel-only; uvloop is skipped on Windows).

Nothing failed on 3.14, so no fallback versions or alternative interpreters were needed.

## 3. Requirements files

- `requirements.txt` - runtime: requests, numpy, pandas, pyyaml, python-dotenv, fastapi, uvicorn
  (lower bounds = verified versions; `<3`/`<4` caps on requests/numpy/pandas majors).
- `requirements-dev.txt` - `-r requirements.txt` + pytest, responses, httpx, httpx2.
- `requirements-lock.txt` - exact pins of the verified venv.

Both `requirements-dev.txt` and `requirements-lock.txt` dry-run resolve to "already satisfied".

## 4. Connectivity (venv python + requests, no proxy env vars set)

| URL | HTTP | Geo-block | DNS | RTT | Notes |
|---|---|---|---|---|---|
| `https://fapi.binance.com/fapi/v1/time` | 200 | no | 13.225.117.x (CloudFront) | ~70-100 ms | |
| `https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=3` | 200 | no | same | ~40 ms | 3 rows x 12 fields |
| `https://testnet.binancefuture.com/fapi/v1/time` | 200 | no | 18.67.51.x (CloudFront) | ~50-80 ms | |
| `https://demo-fapi.binance.com/fapi/v1/time` | 200 | no | 18.179.135.188, 52.195.130.124, 52.197.167.147 (AWS, no CloudFront) | ~45-120 ms | |
| extra: `fapi` `premiumIndex`, `fundingRate`, `exchangeInfo` | 200 | no | | | |
| extra: `demo-fapi` / `testnet` `klines`, `exchangeInfo` | 200 | no | | | |

No 451/403 and no DNS failures from this machine/network (Korea, KST).

### Kline layout (`GET /fapi/v1/klines`) - JSON array of arrays, 12 fields per row

| idx | field | JSON type | example |
|---|---|---|---|
| 0 | open_time (ms, UTC) | int | 1790769600000 |
| 1 | open | **string** | "83883.00" |
| 2 | high | string | "83916.50" |
| 3 | low | string | "83730.00" |
| 4 | close | string | "83750.40" |
| 5 | volume (base asset) | string | "1223.909" |
| 6 | close_time (ms, = open_time + interval - 1) | int | 1790773199999 |
| 7 | quote_asset_volume | string | "102577263.36790" |
| 8 | number_of_trades | int | 33000 |
| 9 | taker_buy_base_volume | string | "426.751" |
| 10 | taker_buy_quote_volume | string | "35763805.98130" |
| 11 | ignore | string | "0" |

Rows are ascending by open_time. **The last row is the still-forming candle**
(its close_time was in the future at request time).

### Other observations (public data)

| | mainnet `fapi` | demo `demo-fapi` / `testnet.binancefuture` |
|---|---|---|
| symbols in exchangeInfo | 919 | 741 |
| REQUEST_WEIGHT limit | 2400 / 1m | 6000 / 1m |
| ORDERS limits | 1200/1m, 300/10s | 1200/1m, 300/10s |
| BTCUSDT tickSize | 0.10 | 0.10 |
| BTCUSDT stepSize / minQty | 0.001 | **0.0001** |
| BTCUSDT quantityPrecision | 3 | **4** |
| BTCUSDT MIN_NOTIONAL | 50 | 50 |
| BTCUSDT MAX_NUM_ORDERS | 200 | 10000 |
| BTCUSDT marketTakeBound | 0.05 | 0.30 |
| BTCUSDT liquidationFee | 0.0125 | 0.02 |

- `testnet.binancefuture.com` and `demo-fapi.binance.com` returned identical klines and matching
  exchangeInfo summaries (741 symbols, 6000 weight, same BTCUSDT LOT_SIZE): they appear to be
  the same demo backend. Its prices and
  volumes differ from mainnet (e.g. 1h volume 23200 vs 1224 BTC for the same bar).
- `premiumIndex` BTCUSDT: markPrice, indexPrice, lastFundingRate, nextFundingTime (8h cycle).
  `fundingRate` rows: symbol, fundingTime, fundingRate, markPrice, rateType. All numbers are strings.
- Mainnet `X-MBX-USED-WEIGHT-1M` was already ~51 on the first call from this IP.

### Clock offset (5 samples per host, midpoint method)

Binance serverTime minus local clock = **+3.78 s** (median; all three hosts agree within 50 ms).
The local PC clock is ~3.8 s behind Binance.

## 5. How to run things (PowerShell, from the project folder)

Call the venv interpreter directly - no activation needed, no execution-policy issue.

```powershell
# one-time: create venv (use the real 3.14 interpreter explicitly)
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# install (wheels only, guarantees no C compiler is ever needed)
.\.venv\Scripts\python.exe -m pip install --only-binary ":all:" -r requirements-dev.txt
# or exact reproduction
.\.venv\Scripts\python.exe -m pip install --only-binary ":all:" -r requirements-lock.txt

# check
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import requests,pandas,numpy,yaml,dotenv,fastapi,uvicorn;print('ok')"

# tests
.\.venv\Scripts\python.exe -m pytest -q

# local API server (replace module path with the real app module)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# quick public reachability probe (single quotes inside, see gotcha 2)
.\.venv\Scripts\python.exe -c "import requests;r=requests.get('https://demo-fapi.binance.com/fapi/v1/time',timeout=10);print(r.status_code,r.text)"

# recommended for this machine (cp949 locale): force UTF-8 for this shell
$env:PYTHONUTF8 = "1"
```

If activation is really wanted in one window only: `Set-ExecutionPolicy -Scope Process Bypass`
then `.\.venv\Scripts\Activate.ps1` (process scope, not persisted).

## 6. Gotchas

1. **Clock is ~3.8 s behind Binance and Windows Time is not syncing.** Signed futures requests
   fail with `-1021` if `timestamp < serverTime - recvWindow` or `timestamp > serverTime + 1000`.
   With the default recvWindow 5000 ms only ~1.2 s of margin is left. The user should sync the
   Windows clock (Settings > Time & language > Date & time > Sync now). Independently, the bot
   must fetch `/fapi/v1/time` at startup and periodically, keep `offset = serverTime - local_ms`,
   and sign with `local_ms + offset`.
2. **PowerShell 5.1 strips embedded double quotes** when passing arguments to native programs:
   `python -c "print(\"x\")"` arrives broken (SyntaxError, reproduced). Use single quotes inside
   `-c` code or put code in a `.py` file.
3. **Activate.ps1 is blocked** by the default Restricted policy. Call `.venv\Scripts\python.exe`
   directly (all commands above do).
4. **cp949 default encoding.** `open()` without `encoding=` reads/writes cp949. Always pass
   `encoding="utf-8"` for YAML/config/log/CSV files or set `PYTHONUTF8=1`. Native Windows tool
   output (e.g. `w32tm`) may appear garbled when captured.
5. **pandas 3 semantics.** Kline string fields become dtype `str` (not `object`); convert with
   `pd.to_numeric`/`astype("float64")`. Copy-on-Write is always on: chained assignment
   (`df["close"][0] = x`) never modifies `df`; use `.loc`. `pd.to_datetime(ms, unit="ms", utc=True)`
   yields `datetime64[ms, UTC]` (not ns), so `.astype("int64")` returns milliseconds - do not
   assume nanoseconds.
6. **Drop the open candle.** The last kline row is still forming; use only rows with
   `close_time < now` in signals/backtests to avoid look-ahead and repainting.
7. **Prices/quantities are strings.** Keep them as `Decimal` for order price/qty and round to
   `tickSize`/`stepSize`; floats are fine for indicators only.
8. **Filters differ per environment** (BTCUSDT stepSize 0.001 mainnet vs 0.0001 demo, weight
   limit 2400 vs 6000, 919 vs 741 symbols). Always load `exchangeInfo` from the same base URL
   you trade on; never hardcode precision.
9. **Demo data is not market data.** testnet/demo klines differ from mainnet. Do research and
   backtests on mainnet public klines; use demo only for order-flow testing.
   `demo-fapi.binance.com` and `testnet.binancefuture.com` served the same backend; prefer
   `demo-fapi.binance.com` as the demo base URL (key compatibility not tested: no keys).
10. **Rate limits.** Read `X-MBX-USED-WEIGHT-1M` on every response; back off on HTTP 429 and
    stop on 418 (IP ban). Mainnet weight was already ~51/2400 on the first call.
11. **Starlette 1.7 TestClient** warns when only `httpx` is present; `httpx2` (in
    requirements-dev) removes the warning. `pytest -W error` passes with httpx2 installed.
12. **uvloop does not exist on Windows**; uvicorn uses the default asyncio loop. `uvicorn[standard]`
    (httptools, websockets, watchfiles) is available as cp314 wheels if needed later.
13. **The venv is not relocatable.** It embeds absolute paths; if the project folder is moved,
    delete `.venv` and recreate it with the commands above.
14. **Secrets.** Keep API keys only in `.env` (load with python-dotenv); add `.env` and `.venv/`
    to `.gitignore`. Use demo keys first; mainnet keys should have withdrawals disabled and an
    IP whitelist.
15. **`pytest` with no tests exits with code 5**, which scripts may treat as failure.
