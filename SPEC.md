# binance-futures-bot — Implementation Specification (v1)

Status: authoritative. Date: 2026-09-30. Audience: engineers implementing the bot, working in parallel without talking to each other.
If this document and your intuition disagree, this document wins. If this document is silent, choose the most conservative (safest) behaviour and leave a `# SPEC-GAP:` comment.

Research that this spec is based on (read for background, not required to implement):
`docs/research/binance_api.md`, `docs/research/futures_mechanics.md`, `docs/research/environment.md`.

---

## 0. Global rules (apply to every unit)

### 0.1 Safety rules for implementers
- There are NO API keys in this project. Never create accounts, never enter credentials, never call signed endpoints against a real host, never place orders. Unit tests must never touch the network (enforced by an autouse fixture, §14.1).
- Only public unauthenticated GET endpoints (`/fapi/v1/time`, `exchangeInfo`, `klines`, `fundingRate`, `premiumIndex`) may be hit, and only by tests marked `@pytest.mark.network` or by the integrator's E2E run in **paper** mode.
- Default mode is `paper`. Paper mode builds its REST client **without** credentials, so a signed call is impossible by construction (§6.1 raises `AuthError` locally before any HTTP call).

### 0.2 Conventions
| Topic | Rule |
|---|---|
| Python | 3.14, `from __future__ import annotations` at the top of every module. Type hints everywhere. `enum.StrEnum` for enums with **explicit string values** (`BUY = "BUY"`); `enum.auto()` is forbidden (on StrEnum it yields lower-case values that Binance rejects). |
| Banned APIs | `datetime.utcnow()`, `datetime.utcfromtimestamp()`, `asyncio.get_event_loop()` (deprecated; `pytest.ini` turns DeprecationWarnings from `bot.*` into errors). Use `datetime.fromtimestamp(ms / 1000, tz=UTC)` (`from datetime import UTC`) or `time.strftime(fmt, time.gmtime(s))`. Never `dataclasses.asdict()` / `copy.deepcopy()` on config objects (they contain `MappingProxyType`, which cannot be pickled). |
| Time | All timestamps are **int milliseconds since epoch, UTC**. Variables end in `_ms` or are named `*_time`. Never naive datetimes. The dashboard converts to KST for display only. Bar/signal/order times use **server time**; status/heartbeat/event timestamps (`BotStatus.updated_at/started_at`, `touch_heartbeat`, `log_event`, `created_at`) use the **local** `now_ms(clock)` (the dashboard computes heartbeat age with local time). |
| numpy boundary | Every value that leaves pandas/numpy and enters a model dataclass, `Storage`, JSON, a log message format arg or a client id **must be a native `int`/`float`/`bool`** (`int(df.open_time.iloc[-1])`, `float(...)`). `numpy.int64` is stored by sqlite3 as an 8-byte BLOB and is rejected by `json.dumps`; `df.iloc[i]` on a mixed-dtype row returns `open_time` as `numpy.float64`. Defences (required): `Candle`/`Signal` coerce in `__post_init__`, `to_jsonable` converts `numpy.generic` via `.item()`, `storage.py` registers sqlite3 adapters (§5). |
| Money | USDT amounts are `float`. Fractions (returns, drawdown, win rate) are `float` fractions (0.12 = 12 %), never percent, except config keys ending in `_pct` which are percent. |
| Exchange precision | Prices/quantities sent to Binance are `decimal.Decimal`, rounded with `bot/exchange/filters.py`, formatted with `format_decimal`. Indicators and simulations use `float`. |
| Files | Always `open(..., encoding="utf-8")` (machine default is cp949). CSV: `newline=""`. Atomic writes: write `<file>.tmp` then **`bot.fsutil.atomic_replace(tmp, target)`** (never a bare `os.replace`: on Windows it raises `PermissionError` while e.g. Excel holds the target open; the helper retries and then raises a Korean-hinted `DataError`, §4.4). |
| pandas 3 | Kline strings -> `pd.to_numeric`. Copy-on-write: never chained assignment; use `.loc`. Time columns stay int64 ms (do not convert to datetime inside the library). |
| Logging | `logger = logging.getLogger(__name__)` in every module. Log messages in English. Never log API keys, secrets, signatures, or full signed URLs. |
| User-facing text | README, dashboard labels, CLI metric table: Korean. Identifiers: English. Comments: English or short Korean. |
| Errors | Raise the exception classes from `bot/errors.py` only (plus `ValueError`/`TypeError` for programming errors). |
| Imports | No circular imports. Bottom layer, in this exact order: `errors` <- `timeutil` <- `fsutil` <- `models` <- `config`. **`errors.py` imports nothing from `bot` at runtime** (it annotates with `if TYPE_CHECKING: from bot.models import OpenOutcome, PositionClosure`); `timeutil` and `fsutil` may import `bot.errors`; `models` may import `bot.errors`, `bot.timeutil`; `config` imports all four. Above that: `config` <- `logging_setup, storage` <- `exchange.*` <- `data.*`; `strategy.*`, `risk` import only `models/errors/config/timeutil/exchange.filters`; `fillmodel` imports `models/config`; `backtest.*` imports `strategy, risk, fillmodel, storage, models, config, timeutil, fsutil, exchange.filters`; `broker.*` imports `exchange.*, fillmodel, storage, models, config, errors, timeutil` (not `risk`); `trader` imports everything below it; `cli` imports everything; `dashboard` imports only `config, storage, models, timeutil`. |

### 0.3 Supported values
- Symbols: USDT-M perpetuals only, regex `^[A-Z0-9]{2,20}USDT$`. Default `BTCUSDT`.
- Intervals (trade + backtest): `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d`. (`3d 1w 1M` are rejected: they do not align to `floor(t / interval)`.)
- Hosts (constants in `bot/config.py`):
  - `MAINNET_REST_URL = "https://fapi.binance.com"` — paper (public data only) and live.
  - `TESTNET_REST_URL = "https://demo-fapi.binance.com"` — testnet (= Binance "Demo Trading"). `testnet.binancefuture.com` is a legacy alias; do not use it.
- WebSocket: **not used in v1.** REST polling aligned to candle close is sufficient for closed-candle strategies at ≥1m intervals; protective orders live on the exchange so the bot does not need millisecond fill events. (Future: market streams need routed URLs `wss://fstream.binance.com/market/ws/<stream>`.)

---

## 1. File tree and ownership

Unit codes (U1..U7) are defined in §17. Every file has exactly one owner.

```
binance-futures-bot/
├── .env.example                      U1
├── .gitignore                        U1
├── config.example.yaml               U1
├── config.yaml                       U1  (initial byte-identical copy of config.example.yaml; the user edits it; tests never read it)
├── pytest.ini                        U1
├── requirements.txt                  U1  (already exists and final; keep, see §2)
├── requirements-dev.txt              U1  (already exists and final; keep)
├── requirements-lock.txt             U1  (already exists and final; keep)
├── README.md                         U7  (Korean)
├── docs/
│   ├── SPEC.md                       (this file; read-only)
│   └── research/*.md                 (read-only)
├── bot/
│   ├── __init__.py                   U1  (__version__ = "0.1.0")
│   ├── __main__.py                   U6
│   ├── cli.py                        U6
│   ├── config.py                     U1
│   ├── errors.py                     U1
│   ├── models.py                     U1
│   ├── timeutil.py                   U1
│   ├── fsutil.py                     U1  (atomic file replace/write helpers, §4.4)
│   ├── logging_setup.py              U1
│   ├── storage.py                    U1
│   ├── exchange/
│   │   ├── __init__.py               U2
│   │   ├── rest.py                   U2
│   │   ├── filters.py                U2
│   │   └── market.py                 U2
│   ├── data/
│   │   ├── __init__.py               U2
│   │   └── downloader.py             U2
│   ├── strategy/
│   │   ├── __init__.py               U3  (imports ma_cross so it registers)
│   │   ├── base.py                   U3
│   │   ├── registry.py               U3
│   │   ├── indicators.py             U3
│   │   └── ma_cross.py               U3
│   ├── risk.py                       U3
│   ├── fillmodel.py                  U4
│   ├── backtest/
│   │   ├── __init__.py               U4
│   │   ├── engine.py                 U4
│   │   ├── metrics.py                U4
│   │   └── report.py                 U4
│   ├── broker/
│   │   ├── __init__.py               U5
│   │   ├── base.py                   U5
│   │   ├── paper.py                  U5
│   │   └── exchange_broker.py        U5
│   ├── trader.py                     U6
│   └── dashboard/
│       ├── __init__.py               U7
│       ├── app.py                    U7
│       └── static/
│           ├── index.html            U7
│           ├── app.js                U7
│           └── style.css             U7
├── tests/
│   ├── __init__.py                   U1  (empty)
│   ├── conftest.py                   U1
│   ├── data/exchange_info_btcusdt.json  U1
│   ├── test_config.py  test_models.py  test_timeutil.py  test_fsutil.py  test_storage.py
│   │   test_logging.py  test_conftest_network.py                                             U1
│   ├── test_rest.py  test_filters.py  test_market.py  test_downloader.py  test_network.py    U2
│   ├── test_indicators.py  test_strategy.py  test_risk.py                                  U3
│   ├── test_fillmodel.py  test_backtest_engine.py  test_metrics.py  test_report.py         U4
│   ├── test_paper_broker.py  test_exchange_broker.py                                        U5
│   ├── test_trader.py  test_cli.py                                                          U6
│   └── test_dashboard.py                                                                    U7
├── data/        (runtime, git-ignored: klines/, funding/, exchange_info/, backtests/, bot.db, STOP, trader.lock)
└── logs/        (runtime, git-ignored)
```
A unit may add private helper test modules named `tests/_helpers_u<N>.py` (owned by that unit). No other new files without a `# SPEC-GAP` justification.

---

## 2. Dependencies

`requirements.txt` (runtime) — present and final (do not edit):
`requests>=2.34.2,<3`, `numpy>=2.5.3,<3`, `pandas>=3.0.6,<4`, `pyyaml>=6.0.3`, `python-dotenv>=1.2.3`, `fastapi>=0.142.2` (read-only local dashboard), `uvicorn>=0.54.0`.

`requirements-dev.txt` — present and final: `-r requirements.txt`, `pytest>=9.1.1`, `responses>=0.26.3`, `httpx2>=2.13.1` (the HTTP client Starlette 1.7's `TestClient` imports first; plain `httpx` is **not** a dependency — no code imports it, and it was removed from the venv and the lock file; `pip check` clean, `TestClient` verified under `-W error`).

`requirements-lock.txt` — exact pins of the verified venv (no `httpx`/`httpcore`).

Not allowed: websockets, pyarrow, ccxt, python-binance, any Binance SDK, sqlalchemy, pydantic models of our own (FastAPI may use pydantic internally). Stdlib: `sqlite3, hmac, hashlib, decimal, dataclasses, argparse, logging.handlers, urllib.parse, json, csv, msvcrt/fcntl`.

Install: `.\.venv\Scripts\python.exe -m pip install --only-binary ":all:" -r requirements-dev.txt`.

---

## 3. Configuration

### 3.1 `config.example.yaml` (verbatim; U1 also writes `config.yaml` as an initial identical copy, which the user may edit freely)

```yaml
# ============================================================
# binance-futures-bot 설정 파일
# - 비밀값(API 키)은 절대 여기에 넣지 마세요. API 키는 .env 에만 둡니다.
# - 경로는 이 파일이 있는 폴더 기준 상대경로입니다.
# ============================================================

# 실행 모드: paper(기본, 로컬 가상 체결, 키 불필요) | testnet(바이낸스 데모 트레이딩) | live(실거래)
# live 는 이 값이 live 이고 동시에 환경변수 CONFIRM_LIVE_TRADING=YES 일 때만 실행됩니다(이중 확인).
mode: paper

# 거래 심볼 (USDT-M 무기한 선물)
symbol: BTCUSDT
# 캔들 간격: 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d
interval: 1h

strategy:
  name: ma_cross            # 등록된 전략 이름 (python -m bot strategies 로 목록 확인)
  extra_modules: []         # 사용자 전략 모듈 import 경로 (예: ["user_strategies.my_rsi"])
  params:
    fast_period: 20         # 단기 이동평균 기간
    slow_period: 50         # 장기 이동평균 기간 (fast_period 보다 커야 함)
    ma_type: EMA            # SMA | EMA
    allow_short: true       # false 면 데드크로스에서 롱 청산만 하고 숏 진입 안 함

risk:
  leverage: 3                     # 레버리지 (max_leverage 이하)
  max_leverage: 10                # 허용 레버리지 상한 (코드 절대 상한 20)
  risk_per_trade_pct: 1.0         # 1회 거래 위험 = 자산의 1% (손절 시 예상 손실액)
  stop_loss:
    mode: atr                     # percent | atr
    percent: 2.0                  # mode=percent: 진입가 대비 손절 거리 %
    atr_period: 14                # mode=atr: ATR 기간
    atr_multiple: 2.0             # mode=atr: 손절 거리 = ATR x 배수
  take_profit_r: 2.0              # 익절가 = 손절거리 x R (null 이면 익절 주문 없음)
  max_position_notional: 20000    # 포지션 명목가치 절대 상한 (USDT). 자산이 커져도 이 값 이상으로는 커지지 않음
  max_margin_fraction: 0.9        # 증거금 사용 상한 (자산 대비 비율)
  max_daily_loss_pct: 5.0         # 일일(UTC 기준) 손실 한도 %, 도달 시 킬스위치 (0 = 비활성)
  kill_switch_flatten: true       # 킬스위치 발동 시 보유 포지션 즉시 시장가 청산
  cooldown_bars_after_stop: 3     # 손절/강제청산 후 신규 진입 금지 봉 수
  min_liq_distance_multiple: 2.0  # (진입가~청산가 거리) >= (손절거리 x 이 값) 이어야 진입
  maint_margin_rate: 0.004        # 유지증거금률 (청산가 근사용)
  liq_mmr_buffer: 0.005           # 청산가 근사에 더하는 보수적 버퍼

execution:
  fees:
    maker: 0.0002                 # 0.02 %
    taker: 0.0005                 # 0.05 % (시장가·스탑 체결에 사용)
  slippage_bps: 5                 # 시장가·스탑 체결의 불리한 슬리피지 (1bp = 0.01%)
  working_type: MARK_PRICE        # 보호주문 트리거 기준: MARK_PRICE | CONTRACT_PRICE
  price_protect: false            # true 면 마크가/최종가 괴리(BTC 5%) 시 손절이 발동하지 않을 수 있음 (기본 false 권장)
  protective_mode: close_position # close_position | reduce_only (테스트넷 검증용 대체 방식)
  candle_close_delay_sec: 3       # 캔들 마감 후 대기 시간(초)
  kline_limit: 500                # 신호 계산용 캔들 수 (부족하면 자동으로 워밍업 x2 로 늘림, 최대 1500)
  recv_window_ms: 5000            # 서명 요청 허용 시간창(ms)
  heartbeat_sec: 30               # 하트비트 기록 및 보호주문 점검 주기(초)
  bot_id: mab1                    # 주문 ID 접두사 (영문/숫자 1~8자). 같은 계정에서 봇을 여러 개 돌리면 봇마다 다르게

paper:
  initial_balance: 10000          # 페이퍼 모드 시작 잔고 (USDT)
  include_funding: true           # 페이퍼 모드에서 실제 펀딩비 반영

backtest:
  start: "2024-01-01"             # 백테스트 시작일 (UTC)
  end: null                       # 종료일, null = 현재
  initial_balance: 10000
  include_funding: true           # 과거 펀딩비 반영
  results_dir: data/backtests

data:
  cache_dir: data                 # data/klines, data/funding, data/exchange_info 에 저장

storage:
  db_path: data/bot.db

dashboard:
  host: 127.0.0.1                 # 로컬 전용 (다른 주소는 거부됨)
  port: 8000
  refresh_sec: 10                 # 자동 새로고침 주기(초)

logging:
  level: INFO
  dir: logs
  max_bytes: 5242880              # 로그 파일 최대 크기 (5MB)
  backup_count: 5

halt_file: data/STOP              # 이 파일이 존재하면 신규 진입 중지 (기존 포지션/보호주문 유지)
```

### 3.2 `.env.example` (verbatim)

```dotenv
# ------------------------------------------------------------
# API 키 파일 예시. 이 파일을 .env 로 복사한 뒤 값을 채우세요.
# .env 는 절대 git 에 커밋하지 마세요 (.gitignore 에 포함됨).
# 출금(Withdraw) 권한은 절대 켜지 마세요. 가능하면 IP 제한을 거세요.
# ------------------------------------------------------------

# testnet 모드용: 바이낸스 데모 트레이딩 키 (https://demo.binance.com 에서 발급)
BINANCE_TESTNET_API_KEY=
BINANCE_TESTNET_API_SECRET=

# live 모드용: 메인넷 실거래 키 (충분히 테스트한 뒤에만 사용)
BINANCE_API_KEY=
BINANCE_API_SECRET=

# 주의: CONFIRM_LIVE_TRADING 은 이 파일에서 읽지 않습니다.
# 실거래를 하려면 실행하는 PowerShell 창에서 직접 설정해야 합니다:
#   $env:CONFIRM_LIVE_TRADING = "YES"
```

### 3.3 `.gitignore` (verbatim)
```
.env
.venv/
__pycache__/
*.pyc
data/
*.db
*.db-wal
*.db-shm
logs/
.pytest_cache/
*.tmp
```

### 3.4 `pytest.ini` (verbatim)
```ini
[pytest]
testpaths = tests
addopts = -m "not network" -q
markers =
    network: calls real PUBLIC Binance endpoints (run explicitly with: pytest -m network)
filterwarnings =
    error::DeprecationWarning:bot.*
```

### 3.5 `bot/config.py` — dataclasses (all `@dataclass(frozen=True, slots=True)`)

```python
ABSOLUTE_MAX_LEVERAGE: Final[int] = 20
MAINNET_REST_URL: Final[str] = "https://fapi.binance.com"
TESTNET_REST_URL: Final[str] = "https://demo-fapi.binance.com"
SUPPORTED_INTERVALS: Final[tuple[str, ...]] = ("1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d")
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

class StrategyConfig:   name: str; extra_modules: tuple[str, ...]; params: Mapping[str, Any]   # params stored as MappingProxyType
class StopLossConfig:   mode: Literal["percent", "atr"]; percent: float; atr_period: int; atr_multiple: float
class RiskConfig:       leverage: int; max_leverage: int; risk_per_trade_pct: float; stop_loss: StopLossConfig
                        take_profit_r: float | None; max_position_notional: float; max_margin_fraction: float
                        max_daily_loss_pct: float; kill_switch_flatten: bool; cooldown_bars_after_stop: int
                        min_liq_distance_multiple: float; maint_margin_rate: float; liq_mmr_buffer: float
class FeeConfig:        maker: float; taker: float
class ExecutionConfig:  fees: FeeConfig; slippage_bps: float; working_type: Literal["MARK_PRICE","CONTRACT_PRICE"]
                        price_protect: bool; protective_mode: Literal["close_position","reduce_only"]
                        candle_close_delay_sec: float; kline_limit: int; recv_window_ms: int; heartbeat_sec: int; bot_id: str
class PaperConfig:      initial_balance: float; include_funding: bool
class BacktestConfig:   start: str; end: str | None; initial_balance: float; include_funding: bool; results_dir: str
class DataConfig:       cache_dir: str
class StorageConfig:    db_path: str
class DashboardConfig:  host: str; port: int; refresh_sec: int
class LoggingConfig:    level: str; dir: str; max_bytes: int; backup_count: int
class Credentials:      api_key: str = field(repr=False); api_secret: str = field(repr=False)
                        def __repr__(self) -> str: return "Credentials(api_key=***, api_secret=***)"

class AppConfig:
    mode: Mode                     # bot.models.Mode
    symbol: str
    interval: str
    strategy: StrategyConfig
    risk: RiskConfig
    execution: ExecutionConfig
    paper: PaperConfig
    backtest: BacktestConfig
    data: DataConfig
    storage: StorageConfig
    dashboard: DashboardConfig
    logging: LoggingConfig
    halt_file: str
    base_dir: Path                 # absolute; directory of the config file (or override)

    def resolve_path(self, p: str | Path) -> Path      # absolute p unchanged; relative -> base_dir / p
    @property db_path -> Path                          # resolve_path(storage.db_path)
    @property cache_dir -> Path                        # resolve_path(data.cache_dir)
    @property halt_path -> Path
    def rest_base_url(self) -> str                     # paper/live -> MAINNET_REST_URL; testnet -> TESTNET_REST_URL
    def to_dict(self) -> dict[str, Any]                # plain JSON-able dict of everything except base_dir (as str) ; never secrets
                                                       # implemented with models.to_jsonable (manual field walk); NEVER dataclasses.asdict/deepcopy
                                                       # (MappingProxyType cannot be pickled -> TypeError)
```

### 3.6 `bot/config.py` — functions

```python
def load_config(path: str | Path = "config.yaml", *, base_dir: str | Path | None = None) -> AppConfig
```
- Reads YAML with `encoding="utf-8"` via `yaml.safe_load`. Missing file -> `ConfigError("config file not found: <path>; copy config.example.yaml to config.yaml")`.
- Starts from `DEFAULTS` (a module-level nested dict **identical in values to config.example.yaml**), deep-merges the file over it. Unknown keys anywhere (except inside `strategy.params`) -> `ConfigError("unknown config key: risk.levrage")`.
- `base_dir` default = parent directory of the config file (absolute).
- Type rules: `int` fields reject `bool` and non-integral floats; `float` fields accept int/float (not bool); `bool` fields accept only bool; strings are stripped. `mode` and `symbol` are upper/lower normalized: `mode` lower-case, `symbol` upper-case, `ma_type` is not touched here (strategy validates).
- Validation (each failure -> `ConfigError` with the key path in the message):

| Key | Rule |
|---|---|
| mode | in `{paper, testnet, live}` |
| symbol | matches `^[A-Z0-9]{2,20}USDT$` |
| interval | in `SUPPORTED_INTERVALS` |
| strategy.name | non-empty string (existence checked later by the registry) |
| strategy.extra_modules | list of str |
| strategy.params | mapping (may be empty) |
| risk.max_leverage | int, 1 ≤ x ≤ 20 (`ABSOLUTE_MAX_LEVERAGE`) else "max_leverage exceeds hard cap 20" |
| risk.leverage | int, 1 ≤ x ≤ risk.max_leverage else "leverage exceeds max_leverage" |
| risk.risk_per_trade_pct | 0 < x ≤ 5 |
| risk.stop_loss.mode | in {percent, atr}; percent 0 < x < 50; atr_period int ≥ 1; atr_multiple > 0 |
| risk.take_profit_r | null or > 0 |
| risk.max_position_notional | > 0 |
| risk.max_margin_fraction | 0 < x ≤ 1 |
| risk.max_daily_loss_pct | 0 ≤ x < 100 |
| risk.cooldown_bars_after_stop | int ≥ 0 |
| risk.min_liq_distance_multiple | ≥ 1 |
| risk.maint_margin_rate, liq_mmr_buffer | 0 ≤ x < 0.5 |
| execution.fees.maker/taker | 0 ≤ x ≤ 0.01 |
| execution.slippage_bps | 0 ≤ x ≤ 500 |
| execution.working_type | MARK_PRICE / CONTRACT_PRICE |
| execution.protective_mode | close_position / reduce_only |
| execution.candle_close_delay_sec | 0 ≤ x ≤ 60 |
| execution.kline_limit | int 50 ≤ x ≤ 1500 |
| execution.recv_window_ms | int 1 ≤ x ≤ 60000 |
| execution.heartbeat_sec | int 5 ≤ x ≤ 600 |
| execution.bot_id | `^[A-Za-z0-9]{1,8}$`. Must be unique per running bot on one Binance account (documented in README); ids also carry a symbol tag (§4.1 `make_client_id`), so two bots on different symbols never collide even with the same bot_id. Two bots on the **same** symbol and account are unsupported. |
| paper.initial_balance, backtest.initial_balance | > 0 |
| backtest.start | parseable by `timeutil.parse_date_ms`; backtest.end null or parseable and > start |
| dashboard.host | in `LOOPBACK_HOSTS` else "dashboard must bind to localhost only" |
| dashboard.port | int 1..65535; refresh_sec int 2..3600 |
| logging.level | in DEBUG/INFO/WARNING/ERROR/CRITICAL (case-insensitive, stored upper) |
| logging.max_bytes | int ≥ 10000; backup_count int 0..50 |

```python
def with_overrides(cfg: AppConfig, *, mode: str | None = None, symbol: str | None = None, interval: str | None = None,
                   strategy_name: str | None = None, strategy_params: Mapping[str, Any] | None = None,
                   initial_balance: float | None = None) -> AppConfig
```
Returns a re-validated copy (`dataclasses.replace`). `mode="live"` -> `ConfigError("live mode can only be enabled in the config file")`. `strategy_params` are merged over existing params (not replaced): `params=MappingProxyType(dict(old.params) | dict(strategy_params))`.

```python
def load_credentials(cfg: AppConfig, *, environ: Mapping[str, str] | None = None) -> Credentials | None
```
- `environ` default `os.environ`. Also reads `cfg.base_dir / ".env"` with `dotenv.dotenv_values` (never `load_dotenv`; do not mutate `os.environ`). Process environment wins over `.env`.
- paper -> returns `None` (never reads keys).
- testnet -> `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`; live -> `BINANCE_API_KEY` / `BINANCE_API_SECRET`. Missing/empty -> `ConfigError("testnet mode requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET in .env")`.

```python
def assert_live_allowed(cfg: AppConfig, *, environ: Mapping[str, str] | None = None) -> None
```
- No-op unless `cfg.mode == Mode.LIVE`. For live: requires `environ["CONFIRM_LIVE_TRADING"] == "YES"` exactly (case-sensitive), where `environ` defaults to `os.environ` — **the .env file is NOT consulted**. Otherwise raise `LiveTradingNotConfirmed` with a Korean+English message explaining both opt-ins.

---

## 4. Shared foundation: models, errors, time utilities, file helpers

### 4.1 `bot/models.py`

Constants:
```python
KLINE_COLUMNS: Final = ("open_time","open","high","low","close","volume","close_time",
                        "quote_volume","trades","taker_buy_base","taker_buy_quote")
KLINE_DTYPES: Final = {"open_time":"int64","open":"float64","high":"float64","low":"float64","close":"float64",
                       "volume":"float64","close_time":"int64","quote_volume":"float64","trades":"int64",
                       "taker_buy_base":"float64","taker_buy_quote":"float64"}
TRADE_COLUMNS: Final = ("trade_id","source","run_id","symbol","direction","qty","entry_time","entry_price",
                        "exit_time","exit_price","exit_reason","gross_pnl","fees","funding","net_pnl",
                        "r_multiple","initial_stop","take_profit","leverage")
CLIENT_ID_RE: Final = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")

# Korean metric labels: defined HERE (single source), re-exported by bot/backtest/report.py and served by the
# dashboard in /api/meta (so app.js never hand-copies them).
METRIC_LABELS_KO: Final[dict[str, str]] = {
  "total_return": "총 수익률", "cagr": "연환산 수익률(CAGR)", "max_drawdown": "최대 낙폭(MDD)",
  "max_drawdown_duration_bars": "최대 낙폭 지속(봉)", "sharpe": "샤프 지수", "sharpe_daily": "샤프 지수(일간)",
  "sortino": "소르티노 지수", "n_trades": "거래 수", "win_rate": "승률", "profit_factor": "손익비(Profit Factor)",
  "expectancy": "기대값(USDT/거래)", "expectancy_r": "기대값(R)", "avg_win": "평균 수익", "avg_loss": "평균 손실",
  "best_trade": "최고 거래", "worst_trade": "최악 거래", "avg_holding_hours": "평균 보유 시간(h)", "exposure": "시장 노출 비율",
  "total_fees": "총 수수료", "total_funding": "총 펀딩비", "n_liquidations": "강제청산 횟수", "n_stop_losses": "손절 횟수",
  "n_take_profits": "익절 횟수", "max_consecutive_losses": "최대 연속 손실", "final_equity": "최종 자산",
  "initial_balance": "초기 자산", "bars": "봉 개수", "rejected_entries": "리스크 거부 진입",
  "entries_capped_by_notional": "명목가 상한 적용 진입", "funding_events": "펀딩 적용 횟수"}
PERCENT_METRICS: Final[frozenset[str]] = frozenset({"total_return","cagr","max_drawdown","win_rate","exposure"})
```

**Candle DataFrame convention** (used everywhere a `pd.DataFrame` of candles is passed): columns exactly `KLINE_COLUMNS` with `KLINE_DTYPES`, `RangeIndex`, sorted ascending by `open_time`, unique `open_time`, **closed candles only** (`close_time == open_time + interval_ms - 1`). Extra indicator columns may be appended by strategies.

Enums (all `StrEnum`; value == name unless shown, **written out explicitly** — e.g. `class Side(StrEnum): BUY = "BUY"; SELL = "SELL"`; never `auto()`):
```python
class Mode(StrEnum):        PAPER="paper"; TESTNET="testnet"; LIVE="live"
class Side(StrEnum):        BUY; SELL
class Direction(StrEnum):   LONG; SHORT; FLAT
    @property sign -> int                 # LONG +1, SHORT -1, FLAT 0
    @property opening_side -> Side        # LONG BUY, SHORT SELL; FLAT -> ValueError
    @property closing_side -> Side        # LONG SELL, SHORT BUY; FLAT -> ValueError
    @staticmethod from_qty(qty: float) -> Direction   # >0 LONG, <0 SHORT, 0 FLAT (abs(qty) < 1e-12 is 0)
class SignalAction(StrEnum): LONG; SHORT; CLOSE; NONE
class Action(StrEnum):      NONE; OPEN_LONG; OPEN_SHORT; CLOSE; FLIP_LONG; FLIP_SHORT
class OrderType(StrEnum):   MARKET; STOP_MARKET; TAKE_PROFIT_MARKET
class OrderPurpose(StrEnum): ENTRY; EXIT; STOP_LOSS; TAKE_PROFIT; FLATTEN
class OrderStatus(StrEnum): NEW; PARTIALLY_FILLED; FILLED; CANCELED; REJECTED; EXPIRED; EXPIRED_IN_MATCH; UNKNOWN
class ExitReason(StrEnum):  SIGNAL; FLIP; STOP_LOSS; TAKE_PROFIT; LIQUIDATION; KILL_SWITCH; END_OF_DATA;
                            PROTECTION_FAILED; MANUAL; UNKNOWN
class BotState(StrEnum):    STARTING; RUNNING; HALTED; KILL_SWITCH; ERROR; STOPPED
```

Dataclasses (frozen unless noted; `slots=True`):
```python
@dataclass(frozen=True, slots=True)
class Candle:
    open_time: int; open: float; high: float; low: float; close: float; volume: float; close_time: int
    def __post_init__(self) -> None   # coerces: object.__setattr__(self, "open_time", int(self.open_time)), times -> int, prices -> float
    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Candle         # works with a DataFrame row / dict; int()/float() every field
def candles_from_df(df: pd.DataFrame) -> list[Candle]           # iterate with df.itertuples(index=False) (keeps int64 times), coerce

@dataclass(frozen=True, slots=True)
class Signal:
    action: SignalAction
    bar_open_time: int          # open_time of the CLOSED bar that produced the signal
    price: float                # close of that bar
    reason: str = ""            # e.g. "golden_cross", "dead_cross", "warmup"
    meta: Mapping[str, float] = field(default_factory=dict)   # indicator values, e.g. {"ma_fast":..,"ma_slow":..}
    def __post_init__(self) -> None   # coerces bar_open_time -> int, price -> float, meta -> {str: float} (NaN kept as float)
    def to_dict(self) -> dict; @classmethod from_dict(cls, d) -> Signal

@dataclass(frozen=True, slots=True)
class SymbolFilters:
    symbol: str; status: str; contract_type: str
    tick_size: Decimal; min_price: Decimal; max_price: Decimal
    step_size: Decimal; min_qty: Decimal; max_qty: Decimal               # LOT_SIZE
    market_step_size: Decimal; market_min_qty: Decimal; market_max_qty: Decimal   # MARKET_LOT_SIZE
    min_notional: Decimal                                                 # MIN_NOTIONAL.notional
    multiplier_up: Decimal; multiplier_down: Decimal                      # PERCENT_PRICE
    trigger_protect: Decimal; market_take_bound: Decimal
    def to_dict(self) -> dict[str, str]     # Decimals as str
    @classmethod from_dict(cls, d) -> SymbolFilters

@dataclass(frozen=True, slots=True)
class TradePlan:
    symbol: str; direction: Direction
    ref_price: float                   # reference entry price used for sizing (next-bar open / forming candle open)
    qty: Decimal                       # already floored to market step
    stop_price: Decimal                # tick-rounded toward entry
    take_profit_price: Decimal | None  # tick-rounded toward entry
    notional: float                    # float(qty) * ref_price
    risk_amount: float                 # float(qty) * per_unit_loss (USDT lost if SL fills incl. fees+slip)
    leverage: int
    liquidation_price: float           # conservative approximation (risk.approx_liquidation_price)
    sizing_cap: str = "risk"           # which bound set the size: "risk" | "margin" | "notional" (§8.5 step 8)

@dataclass(frozen=True, slots=True)
class RiskDecision:
    plan: TradePlan | None
    reason: str                        # "ok" or a rejection code (§8.6)
    @property ok -> bool

@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str; side: Side; order_type: OrderType; purpose: OrderPurpose; client_id: str
    quantity: Decimal | None = None; reduce_only: bool = False; close_position: bool = False
    trigger_price: Decimal | None = None

@dataclass(frozen=True, slots=True)
class OrderResult:
    client_id: str; exchange_id: str | None; symbol: str; side: Side; order_type: OrderType; purpose: OrderPurpose
    status: OrderStatus; requested_qty: float | None; executed_qty: float; avg_price: float | None
    trigger_price: float | None; fee: float; ts: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

@dataclass(frozen=True, slots=True)
class ProtectiveOrder:
    kind: OrderPurpose                 # STOP_LOSS or TAKE_PROFIT
    client_id: str; exchange_id: str | None; side: Side; trigger_price: float
    status: str                        # algoStatus string ("NEW", ...); paper uses "NEW"
    close_position: bool; quantity: float | None

@dataclass(frozen=True, slots=True)
class Position:
    symbol: str; qty: float            # signed: >0 long, <0 short (never 0: flat is represented by None)
    entry_price: float; mark_price: float | None; unrealized_pnl: float
    liquidation_price: float | None; isolated_margin: float | None; leverage: int | None; updated_at: int
    @property direction -> Direction

@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    ts: int
    wallet_balance: float              # USDT wallet balance (paper: cash)
    equity: float                      # wallet + unrealized (exchange: USDT marginBalance)
    available_balance: float
    unrealized_pnl: float
    position: Position | None
    protective_orders: tuple[ProtectiveOrder, ...] = ()
    open_orders_count: int = 0         # regular (non-algo) open orders on the symbol

@dataclass(slots=True)                 # mutable
class ActiveTrade:
    trade_id: str; symbol: str; direction: Direction
    qty: float                         # absolute
    entry_price: float                 # actual average fill
    entry_time: int                    # ms of the fill (paper/backtest: forming/next bar open_time)
    entry_bar_open_time: int           # open_time of the bar in which the entry filled
    stop_price: float; take_profit_price: float | None; liquidation_price: float | None
    leverage: int; risk_amount: float; entry_fee: float; entry_client_id: str
    protect_seq: int = 0               # incremented each time protective orders are (re)placed
    entry_order_id: str | None = None  # exchange orderId of the entry (OpenOutcome.entry_order.exchange_id); None for
                                       # adopted positions, paper and backtest
    def to_dict(self) -> dict; @classmethod from_dict(cls, d) -> ActiveTrade   # from_dict tolerates a missing entry_order_id
    # Protective ids of the CURRENT generation (used by U5 sync/ensure_protection and U6 alike):
    #   sl id = make_client_id(bot_id, symbol, "SL", entry_bar_open_time, protect_seq)
    #   tp id = make_client_id(bot_id, symbol, "TP", entry_bar_open_time, protect_seq)   (only if take_profit_price is not None)
    #   protect_seq == 0 means "no bot protective order placed yet" (adopted position).

@dataclass(frozen=True, slots=True)
class OpenOutcome:
    filled: bool
    qty: float = 0.0; avg_price: float = 0.0; entry_fee: float = 0.0; entry_time: int = 0
    entry_order: OrderResult | None = None; protective: tuple[ProtectiveOrder, ...] = (); message: str = ""
    # Callers MUST check `filled` before reading any other field (defaults are placeholders when filled is False).

@dataclass(frozen=True, slots=True)
class PositionClosure:
    exit_time: int; exit_price: float; qty: float; reason: ExitReason
    exit_fee: float                    # USDT, >= 0
    funding: float                     # total funding over the trade's life; + = paid, - = received
    gross_pnl: float | None            # exchange realized PnL if known; None -> computed from prices
    order: OrderResult | None = None

@dataclass(frozen=True, slots=True)
class SyncResult:
    account: AccountSnapshot
    closure: PositionClosure | None
    issues: tuple[str, ...] = ()       # codes: UNTRACKED_POSITION, QTY_MISMATCH, ORPHAN_PROTECTIVE_CANCELED,
                                       #        FOREIGN_OPEN_ORDERS, SL_MISSING, PROTECTION_QTY_MISMATCH,
                                       #        CLOSURE_DETAILS_UNKNOWN

@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str; source: str         # "backtest" | "paper" | "testnet" | "live"
    run_id: str | None; symbol: str; direction: Direction; qty: float
    entry_time: int; entry_price: float; exit_time: int; exit_price: float; exit_reason: ExitReason
    gross_pnl: float; fees: float; funding: float; net_pnl: float
    r_multiple: float | None; initial_stop: float | None; take_profit: float | None; leverage: int
    @classmethod
    def from_closure(cls, active: ActiveTrade, closure: PositionClosure, *, source: str, run_id: str | None = None) -> Trade
    def to_dict(self) -> dict          # keys == TRADE_COLUMNS, enums as str
    @classmethod from_dict(cls, d) -> Trade

@dataclass(slots=True)
class BacktestResult:
    run_id: str; created_at: int; symbol: str; interval: str; strategy: str
    params: dict[str, Any]; config: dict[str, Any]
    start_time: int; end_time: int     # first equity bar open_time; last bar close_time
    initial_balance: float
    metrics: dict[str, float | int | None]
    equity: pd.DataFrame               # columns: time(int64 ms = bar open_time), equity(float64), in_position(bool), position_qty(float64)
    trades: list[Trade]
    result_dir: str | None = None

@dataclass(slots=True)
class BotStatus:
    updated_at: int; started_at: int; mode: Mode; symbol: str; interval: str; strategy: str
    state: BotState; message: str; account: AccountSnapshot | None; last_signal: Signal | None
    last_bar_open_time: int | None; entries_blocked_reason: str | None; pid: int
```

`Trade.from_closure` math (exact):
```
sign        = active.direction.sign
gross       = closure.gross_pnl if closure.gross_pnl is not None else sign * active.qty * (closure.exit_price - active.entry_price)
fees        = active.entry_fee + closure.exit_fee
funding     = closure.funding
net         = gross - fees - funding
r_multiple  = net / active.risk_amount if active.risk_amount > 0 else None
entry/exit times & prices from active/closure; initial_stop = active.stop_price; take_profit = active.take_profit_price
qty = active.qty; exit_reason = closure.reason; leverage = active.leverage; trade_id = active.trade_id
```

Functions:
```python
def to_jsonable(obj: Any) -> Any
    # dataclass -> dict (recursive, via dataclasses.fields; never dataclasses.asdict), StrEnum -> value, Decimal -> str,
    # numpy.generic -> .item() (then the float rule), tuple/list -> list, pd.DataFrame -> list of records (values converted
    # recursively), float NaN/inf -> None, Mapping (incl. MappingProxyType) -> dict, Path -> str. Everything else unchanged.
def symbol_tag(symbol: str) -> str
    # hashlib.sha1(symbol.encode()).hexdigest()[:4]  ("BTCUSDT" -> "4314")
def client_id_prefix(bot_id: str, symbol: str) -> str
    # f"{bot_id}-{symbol_tag(symbol)}-"  — orders "owned" by this bot on this symbol start with it
def make_client_id(bot_id: str, symbol: str, kind: str, bar_open_time_ms: int, seq: int = 0) -> str
    # f"{bot_id}-{symbol_tag(symbol)}-{kind}-{int(bar_open_time_ms) // 1000}-{int(seq)}"; kind in {"EN","EX","SL","TP","FL","KS"}
    # seq >= 0. Validates the result with CLIENT_ID_RE and len <= 36 else ValueError (max length 8+1+4+1+2+1+10+1+4 = 32).
    # Example: make_client_id("mab1", "BTCUSDT", "EN", 1_790_769_600_000) == "mab1-4314-EN-1790769600-0"
def next_client_id(client_id: str) -> str
    # increments the trailing "-<seq>": "mab1-4314-FL-1790769600-0" -> "mab1-4314-FL-1790769600-1"; ValueError if no trailing int
def validate_candles_df(df: pd.DataFrame, interval_ms: int | None = None) -> None
    # raises DataError if columns/dtypes missing, not sorted, duplicate open_time, or (if interval_ms) close_time != open_time+interval_ms-1
```

### 4.2 `bot/errors.py`

No runtime import from `bot` (see §0.2): `from typing import TYPE_CHECKING` / `if TYPE_CHECKING: from bot.models import OpenOutcome, PositionClosure`.
```python
class BotError(Exception)
class ConfigError(BotError)
class LiveTradingNotConfirmed(ConfigError)
class DataError(BotError)
class StaleDataError(DataError)

class ExchangeError(BotError):
    def __init__(self, msg: str, *, code: int | None = None, http_status: int | None = None,
                 path: str | None = None, retry_after: float | None = None) -> None
    # attributes: msg, code, http_status, path, retry_after ; str(e) = f"[{http_status} {code}] {path}: {msg}"
    # NEVER include query params, keys or signatures in any attribute.
class TransientError(ExchangeError)          # temporary failure. For GET/DELETE/PUT: safe to retry. For POST: only the
                                             # 503 "Service Unavailable" / -1008 cases are known "not executed"
                                             # (attribute not_executed: bool = False); brokers resolve every other POST
                                             # TransientError by lookup, never by blind resend (§9.4)
class RateLimitError(ExchangeError)          # 429, -1003, -1015
class IpBannedError(RateLimitError)          # 418
class TimestampError(ExchangeError)          # -1021, -5028
class AuthError(ExchangeError)               # -1022, -2014, -2015, or no credentials configured
class UnknownOrderStatusError(ExchangeError) # POST only: outcome unknown (timeout, 503 "Unknown error", non-JSON 5xx, -1007, -1000)
class NoChangeNeededError(ExchangeError)     # -4046, -4059, -4171 (treat as success)
class OrderRejectedError(ExchangeError)      # generic order-level 4xx rejection
class InsufficientMarginError(OrderRejectedError)   # -2018, -2019
class ImmediateTriggerError(OrderRejectedError)     # -2021, -4142
class ReduceOnlyRejectedError(OrderRejectedError)   # -2022, -4118
class MinNotionalError(OrderRejectedError)          # -4164
class DuplicateClientIdError(OrderRejectedError)    # -4116
class AlgoLimitError(OrderRejectedError)            # -4045
class ReduceOnlyModeError(OrderRejectedError)       # -4400, -4401
class NoSuchOrderError(ExchangeError)               # -2013, -2011 on cancel of unknown order

class ProtectionFailedError(BotError):
    def __init__(self, msg: str, *, flattened: bool, closure: PositionClosure | None = None,
                 entry: OpenOutcome | None = None) -> None      # attributes: flattened, closure, entry
class EmergencyError(BotError):              # position may be unprotected and could not be closed
    def __init__(self, msg: str, *, entry: OpenOutcome | None = None) -> None   # entry set when raised from open_position
```
Error-code notes: `-4161` (leverage reduction with an open isolated position) and `-2027`/`-2028` (position above the leverage bracket cap) have no dedicated class: -4161 surfaces as `ExchangeError(code=-4161)` and is tolerated by `prepare_symbol` (§9.4); -2027/-2028 surface as `OrderRejectedError` (order path) and are an entry rejection.

### 4.3 `bot/timeutil.py`

```python
INTERVAL_MS: Final[dict[str, int]]   # "1m":60_000 "3m":180_000 "5m":300_000 "15m":900_000 "30m":1_800_000 "1h":3_600_000
                                     # "2h":7_200_000 "4h":14_400_000 "6h":21_600_000 "8h":28_800_000 "12h":43_200_000 "1d":86_400_000
DAY_MS: Final = 86_400_000
def interval_to_ms(interval: str) -> int              # ConfigError if not in INTERVAL_MS
def now_ms(clock: Callable[[], float] = time.time) -> int   # int(round(clock() * 1000))
def floor_time(ts_ms: int, interval_ms: int) -> int   # ts - ts % interval
def next_close_ms(now_ms: int, interval_ms: int) -> int   # floor_time(now, i) + i  (== open_time of next bar)
def expected_last_closed_open(now_ms: int, interval_ms: int) -> int   # floor_time(now, i) - i
def parse_date_ms(s: str) -> int                      # "YYYY-MM-DD" -> 00:00 UTC; ISO 8601 with/without tz (naive = UTC); ConfigError on failure
def ms_to_iso(ts_ms: int) -> str                      # "2024-01-01T00:00:00Z"
def utc_day(ts_ms: int) -> str                        # "2024-01-01"
def bars_per_year(interval: str) -> float             # 365 * DAY_MS / interval_ms  (1h -> 8760.0, 4h -> 2190.0, 1d -> 365.0)
```
`ms_to_iso`/`utc_day` use `datetime.fromtimestamp(ms / 1000, tz=UTC)` (never `utcfromtimestamp`).

### 4.4 `bot/fsutil.py` (U1)

```python
def atomic_replace(tmp: Path, target: Path, *, retries: int = 5, delay_sec: float = 0.2,
                   sleep: Callable[[float], None] = time.sleep) -> None
    # os.replace(tmp, target); on PermissionError retry up to `retries` times with sleep(delay_sec).
    # Still failing -> delete tmp (ignore errors) and raise DataError(
    #   f"file is locked by another program: {target} (엑셀 등에서 파일을 열어두었다면 닫고 다시 실행하세요)")
def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None
    # creates parent dirs; writes f"{path}.tmp" with open(..., "w", encoding=encoding, newline="") then atomic_replace
```
Every unit that writes a cache/result file uses these helpers (pandas: `df.to_csv(tmp, index=False, encoding="utf-8")` then `atomic_replace(tmp, path)`).

---

## 5. Storage — `bot/storage.py`

SQLite via stdlib `sqlite3`. One DB file (`cfg.db_path`) shared by the bot (writer) and dashboard (reader).

Module import side effect (required, numpy boundary §0.2): `sqlite3.register_adapter(np.int64, int)`, `(np.int32, int)`, `(np.float64, float)`, `(np.float32, float)`, `(np.bool_, bool)`. Bind parameters are additionally passed through explicit `int()`/`float()` where the column is INTEGER/REAL.

Connection setup (every `Storage` instance opens exactly one connection):
```python
# writer (read_only=False): parent directory created if missing
sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=False)
PRAGMA journal_mode=WAL;       -- verify result == "wal"
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
conn.row_factory = sqlite3.Row
init_schema()

# reader (read_only=True): NEVER creates the file, NEVER runs DDL or changes journal_mode
if not path.exists(): raise DataError(f"database not found: {path}")
sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=False)
PRAGMA query_only=ON;          -- first statement
PRAGMA busy_timeout=5000;
conn.row_factory = sqlite3.Row
verify schema_version table exists and == SCHEMA_VERSION, else DataError
```
Writes use explicit transactions (`BEGIN IMMEDIATE ... COMMIT`, `ROLLBACK` on exception) via a private context manager `_tx()`. The dashboard uses `read_only=True` (a normal connection with `query_only`, **not** a `mode=ro` URI, because WAL read-only URIs fail when `-shm` is missing).

Lifecycle (required everywhere, because Python 3.14 emits `ResourceWarning: unclosed database` and `pytest -W error` turns it into a failure): always `with Storage(...) as st:` (CLI commands, dashboard dependency, trader wiring, tests). `__exit__` calls `close()`; `close()` is idempotent. `Trader`/CLI close storage in `finally`. Never pass `Storage(...)` inline as an argument.

Single-trader rule: only **one trader process** (any mode) runs at a time, enforced by the global lock file `data/trader.lock` (§10.1). Therefore `bot_status` keeps a single row (`id = 1`); the dashboard shows whichever mode that trader runs.

### 5.1 DDL (verbatim, `SCHEMA_SQL`; `SCHEMA_VERSION = 1`)
```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,              -- JSON
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_status (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at              INTEGER NOT NULL,   -- heartbeat, ms
    started_at              INTEGER NOT NULL,
    mode                    TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    interval                TEXT NOT NULL,
    strategy                TEXT NOT NULL,
    state                   TEXT NOT NULL,
    message                 TEXT NOT NULL DEFAULT '',
    account_json            TEXT,               -- to_jsonable(AccountSnapshot) incl. position + protective_orders
    last_signal_json        TEXT,               -- to_jsonable(Signal)
    last_bar_open_time      INTEGER,
    entries_blocked_reason  TEXT,
    pid                     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    interval        TEXT NOT NULL,
    bar_open_time   INTEGER NOT NULL,
    action          TEXT NOT NULL,              -- SignalAction
    decided_action  TEXT NOT NULL,              -- Action
    price           REAL NOT NULL,
    reason          TEXT NOT NULL DEFAULT '',
    meta_json       TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE (mode, symbol, interval, bar_open_time)
);
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mode            TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    exchange_id     TEXT,
    purpose         TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    status          TEXT NOT NULL,
    requested_qty   REAL,
    executed_qty    REAL NOT NULL DEFAULT 0,
    avg_price       REAL,
    trigger_price   REAL,
    fee             REAL NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    raw_json        TEXT,
    UNIQUE (mode, client_id)
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,              -- backtest | paper | testnet | live
    run_id          TEXT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,
    qty             REAL NOT NULL,
    entry_time      INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    exit_time       INTEGER NOT NULL,
    exit_price      REAL NOT NULL,
    exit_reason     TEXT NOT NULL,
    gross_pnl       REAL NOT NULL,
    fees            REAL NOT NULL,
    funding         REAL NOT NULL,
    net_pnl         REAL NOT NULL,
    r_multiple      REAL,
    initial_stop    REAL,
    take_profit     REAL,
    leverage        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_source_exit ON trades (source, run_id, exit_time);
CREATE TABLE IF NOT EXISTS equity (
    mode        TEXT NOT NULL,
    time        INTEGER NOT NULL,               -- open_time of the last closed bar at the iteration
    equity      REAL NOT NULL,
    wallet      REAL NOT NULL,
    PRIMARY KEY (mode, time)
);
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT NOT NULL,
    interval    TEXT NOT NULL,
    open_time   INTEGER NOT NULL,
    open        REAL NOT NULL,
    high        REAL NOT NULL,
    low         REAL NOT NULL,
    close       REAL NOT NULL,
    volume      REAL NOT NULL,
    PRIMARY KEY (symbol, interval, open_time)
);
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id          TEXT PRIMARY KEY,
    created_at      INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    interval        TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    start_time      INTEGER NOT NULL,
    end_time        INTEGER NOT NULL,
    initial_balance REAL NOT NULL,
    metrics_json    TEXT NOT NULL,
    result_dir      TEXT
);
CREATE TABLE IF NOT EXISTS backtest_equity (
    run_id      TEXT NOT NULL REFERENCES backtest_runs(run_id) ON DELETE CASCADE,
    time        INTEGER NOT NULL,
    equity      REAL NOT NULL,
    PRIMARY KEY (run_id, time)
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    level       TEXT NOT NULL,                  -- INFO | WARNING | ERROR | CRITICAL
    mode        TEXT NOT NULL,
    kind        TEXT NOT NULL,                  -- e.g. KILL_SWITCH, PROTECTION_FAILED, STALE_DATA, ADOPTED_POSITION
    message     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
```
`init_schema()` (writer only; `read_only` -> `DataError`) executes the script, inserts `schema_version(1)` if the table is empty. If a different version is present -> `DataError`.

### 5.2 `Storage` API
```python
class Storage:
    def __init__(self, db_path: str | Path, *, read_only: bool = False) -> None   # opens connection per §5 (writer: + init_schema)
    path: Path
    def init_schema(self) -> None
    def close(self) -> None
    def __enter__(self) -> Storage; def __exit__(self, *exc) -> None

    # key/value state (JSON)
    def get_state(self, key: str) -> Any | None
    def set_state(self, key: str, value: Any) -> None          # value passed through to_jsonable then json.dumps
    def delete_state(self, key: str) -> None

    # status / heartbeat
    def upsert_status(self, status: BotStatus) -> None         # INSERT OR REPLACE id=1 (updated_at/started_at are LOCAL ms)
    def touch_heartbeat(self, ts_ms: int) -> None              # UPDATE bot_status SET updated_at=? WHERE id=1 (LOCAL now_ms)
    def get_status(self) -> dict | None
        # keys: updated_at, started_at, mode, symbol, interval, strategy, state, message,
        #       account (dict|None), last_signal (dict|None), last_bar_open_time, entries_blocked_reason, pid

    # signals
    def record_signal(self, mode: str, symbol: str, interval: str, signal: Signal, decided_action: Action, ts_ms: int) -> None
        # INSERT OR IGNORE on the UNIQUE key
    def recent_signals(self, mode: str, limit: int = 50) -> list[dict]       # newest first

    # orders
    def upsert_order(self, mode: str, result: OrderResult, ts_ms: int) -> None   # upsert on (mode, client_id); created_at kept
    def recent_orders(self, mode: str, limit: int = 100) -> list[dict]

    # trades
    def insert_trade(self, trade: Trade) -> None               # INSERT OR REPLACE by trade_id
    def insert_trades(self, trades: Iterable[Trade]) -> None   # one transaction
    def list_trades(self, *, source: str | None = None, run_id: str | None = None, limit: int = 500) -> list[dict]
        # newest exit_time first; dict keys == TRADE_COLUMNS

    # live/paper equity
    def append_equity(self, mode: str, time_ms: int, equity: float, wallet: float) -> None   # INSERT OR REPLACE
    def equity_curve(self, mode: str, limit: int = 5000) -> list[dict]   # ascending time; keys time, equity, wallet (last `limit` points)

    # candles (for dashboard chart)
    def upsert_candles(self, symbol: str, interval: str, df: pd.DataFrame) -> int   # returns rows written
    def get_candles(self, symbol: str, interval: str, limit: int = 500) -> list[dict]  # ascending; keys open_time, open, high, low, close, volume

    # backtests
    def save_backtest(self, result: BacktestResult) -> None
        # one transaction: backtest_runs row, trades rows (source="backtest", run_id), backtest_equity rows (time, equity)
    def list_backtests(self, limit: int = 100) -> list[dict]
        # newest first; keys run_id, created_at, symbol, interval, strategy, params(dict), start_time, end_time,
        #                   initial_balance, metrics(dict), result_dir
    def get_backtest(self, run_id: str) -> dict | None        # list_backtests keys + config(dict)
    def backtest_equity(self, run_id: str) -> list[dict]      # ascending; keys time, equity

    # events
    def log_event(self, level: str, mode: str, kind: str, message: str, ts_ms: int | None = None) -> None
    def recent_events(self, limit: int = 50, mode: str | None = None) -> list[dict]   # newest first
```
State keys used by other units (exact strings):
- `active_trade:{mode}:{symbol}` — `ActiveTrade.to_dict()`
- `last_bar:{mode}:{symbol}:{interval}` — int (last processed closed bar open_time)
- `kill_switch:{mode}:{symbol}` — `DailyLossKillSwitch.to_dict()`
- `cooldown:{mode}:{symbol}` — `Cooldown.to_dict()`
- `paper_state:{symbol}` — PaperBroker state (§9.3)
- `halted:{mode}:{symbol}` — `{"reason": str, "ts": int}` when entries are halted until restart (§10.3 step 9, §10.5)
- `emergency:{mode}:{symbol}` — `{"attempt": int, "bar": int, "since": int}` while an emergency flatten is in progress (§10.5); deleted once flat

---

## 6. Exchange layer (U2)

### 6.1 `bot/exchange/rest.py`

```python
USER_AGENT: Final = "binance-futures-bot/0.1"
BACKOFF_SECONDS: Final = (0.2, 0.4, 0.8)
WEIGHT_GUARD_FRACTION: Final = 0.8

def hmac_sha256_signature(secret: str, payload: str) -> str
    # hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()  (lowercase hex)

def encode_params(params: Mapping[str, Any] | None) -> str
    # Drops None values, keeps insertion order. bool -> "true"/"false"; Decimal -> filters.format_decimal;
    # float -> repr-free formatting via format_decimal(Decimal(str(v))); int/str unchanged. urllib.parse.urlencode(..., quote_via=quote)
    # (quote_via=urllib.parse.quote so spaces are %20; safe="" is fine).

def parse_rate_limit_headers(headers: Mapping[str, str]) -> dict[str, int]
    # case-insensitive regex r"x-mbx-(used-weight|order-count)-(\d+)([smhd])" -> {"used-weight-1m": 51, "order-count-10s": 3, ...}

def map_error(http_status: int, payload: Any, *, path: str, method: str, headers: Mapping[str, str]) -> ExchangeError
    # payload: parsed JSON (dict) or raw text. See mapping table below.

class BinanceRestClient:
    def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None, *,
                 recv_window_ms: int = 5000, timeout: tuple[float, float] = (5.0, 15.0), max_retries: int = 3,
                 weight_limit: int = 2400, resync_interval_sec: float = 300.0,
                 session: requests.Session | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None
    base_url: str                         # rstrip("/")
    has_credentials: bool                 # both key and secret non-empty
    weight_limit: int                     # updated by MarketData from exchangeInfo rateLimits
    used_weight_1m: int | None            # last seen header value
    order_count_10s: int | None; order_count_1m: int | None

    def sync_time(self) -> int
        # t0=clock(); GET /fapi/v1/time; t1=clock(); offset = serverTime - round((t0+t1)/2*1000). Stores offset + sync instant.
        # |offset| > 1000 -> logger.warning("local clock differs from Binance by %d ms; sync Windows time") (once per 10 min).
        # Returns offset ms.
    def set_time_offset(self, offset_ms: int) -> None    # for tests; marks as synced now
    @property time_offset_ms -> int | None
    def server_time_ms(self) -> int       # now_ms(clock) + (offset or 0); no HTTP
    def timestamp_ms(self) -> int         # server_time_ms(), but first sync_time() if never synced or older than resync_interval_sec

    def public_get(self, path: str, params: Mapping[str, Any] | None = None) -> Any
    def signed_request(self, method: Literal["GET","POST","PUT","DELETE"], path: str,
                       params: Mapping[str, Any] | None = None) -> Any
```

**Signed request construction (exact):**
1. If not `has_credentials` -> raise `AuthError("no API credentials configured (paper mode never signs)")` **before** any I/O.
2. `p = dict(params or {})`; drop `None`; then append `p["recvWindow"] = recv_window_ms`, `p["timestamp"] = timestamp_ms()` (in that order, at the end).
3. `query = encode_params(p)`; `sig = hmac_sha256_signature(secret, query)`; `url = f"{base_url}{path}?{query}&signature={sig}"`.
4. Send with header `X-MBX-APIKEY: <key>`; **no body** for any method (all params in the query string).
5. Official test vector: with params `{"symbol":"BTCUSDT","side":"BUY","type":"LIMIT","quantity":"1","price":"9000","timeInForce":"GTC"}`, `recv_window_ms=5000`, offset 0 and clock `1591702613.943`, the query must be exactly `symbol=BTCUSDT&side=BUY&type=LIMIT&quantity=1&price=9000&timeInForce=GTC&recvWindow=5000&timestamp=1591702613943` and signature `3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9` for secret `2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9`.

**Response handling:**
- After every response: parse rate-limit headers, update counters. Log at DEBUG: `"%s %s -> %d (w=%s)" % (method, path, status, used_weight)` — path only, never the query.
- Success = HTTP 2xx and not (JSON dict with integer `code` < 0). Note `{"code":200,"msg":"success"}` is success. Return parsed JSON.
- Failure -> `map_error(...)`, then apply the retry policy.

**Error mapping (`map_error`)** — first matching rule wins:
| Condition | Exception |
|---|---|
| http 418 | `IpBannedError(retry_after=Retry-After header float or 120)` |
| http 429, or code -1003, -1015 | `RateLimitError(retry_after=Retry-After or 60)` |
| code -1021, -5028 | `TimestampError` |
| code -1022, -2014, -2015 | `AuthError` |
| method == "POST" and (code -1008, or (http 503 and "Service Unavailable" in text)) — checked **before** the non-JSON rule | `TransientError(not_executed=True)` |
| method == "POST" and (code -1007 or -1000, or (http ≥ 500 and ("Unknown error" in text or payload not JSON))) | `UnknownOrderStatusError` |
| http ≥ 500 (any other case, any method), or code -1000, -1001, -1007, -1008 (non-POST), or code -1001 (POST) | `TransientError` (`not_executed=False`) |
| code -4046, -4059, -4171 | `NoChangeNeededError` |
| code -2018, -2019 | `InsufficientMarginError` |
| code -2021, -4142 | `ImmediateTriggerError` |
| code -2022, -4118 | `ReduceOnlyRejectedError` |
| code -2013, -2011 | `NoSuchOrderError` |
| code -4116 | `DuplicateClientIdError` |
| code -4045 | `AlgoLimitError` |
| code -4164 | `MinNotionalError` |
| code -4400, -4401 | `ReduceOnlyModeError` |
| other http 4xx with a code on an order path (`/fapi/v1/order`, `/fapi/v1/algoOrder`) | `OrderRejectedError` |
| anything else | `ExchangeError` |

**Retry policy** (attempt counter shared per call, max `max_retries` retries):
| Situation | GET / DELETE / PUT | POST |
|---|---|---|
| `requests.ConnectTimeout` (never connected) | retry with BACKOFF | retry with BACKOFF |
| `requests.ReadTimeout`, other `requests.ConnectionError` | retry with BACKOFF | raise `UnknownOrderStatusError(...) from None` (no retry) |
| `TransientError` | retry with BACKOFF (cancels are idempotent) | retry **only** if `not_executed` (503 "Service Unavailable" / -1008); otherwise raise |
| `UnknownOrderStatusError` | (never produced for non-POST) | raise immediately |
| `TimestampError` | `sync_time()` then retry once | `sync_time()` then retry once (new timestamp + signature) |
| `RateLimitError` (429) | sleep `min(retry_after, 60)` then retry | raise |
| `IpBannedError` | raise | raise |
| everything else | raise | raise |
After retries are exhausted, raise the last exception.
Every `requests` exception is converted inside the client and raised with `from None` (`TransientError(...) from None` / `UnknownOrderStatusError(...) from None`), so no URL-bearing `requests` exception (its text contains the full signed URL) is ever chained into a traceback. Messages built by the client contain only `method` and `path`.

**Weight guard:** before any request, if `used_weight_1m is not None and used_weight_1m >= weight_limit * 0.8` and the last header arrived in the current UTC minute, sleep until the next minute boundary + 0.5 s (using injected `sleep`), then reset `used_weight_1m = None`.

### 6.2 `bot/exchange/filters.py`

```python
def to_decimal(x: float | int | str | Decimal) -> Decimal
    # Decimal passes through; float -> Decimal(repr(x)) i.e. Decimal(str(x)); rejects NaN/inf with ValueError
def floor_to_step(value, step: Decimal) -> Decimal
    # v = to_decimal(value); if v < 0: ValueError; (v / step).to_integral_value(ROUND_FLOOR) * step, quantized to step's exponent
def ceil_to_step(value, step: Decimal) -> Decimal
def round_price(value, tick: Decimal, mode: Literal["down","up","nearest"] = "nearest") -> Decimal
    # nearest = ROUND_HALF_UP ; result quantized to tick exponent
def round_protective_price(price, tick: Decimal, *, entry: float) -> Decimal
    # rounds TOWARD entry (tighter): price < entry -> "up"; price > entry -> "down"; equal -> "nearest"
def normalize_market_qty(qty, filters: SymbolFilters) -> Decimal
    # floor_to_step(min(to_decimal(qty), filters.market_max_qty), filters.market_step_size); returns Decimal("0") if result < market_min_qty
def meets_min_notional(qty: Decimal, price, filters: SymbolFilters) -> bool   # qty * to_decimal(price) >= min_notional
def format_decimal(d: Decimal) -> str
    # plain notation, no exponent, trailing zeros stripped, no trailing ".": 0.001->"0.001", 84000.10->"84000.1", 100->"100", 1.000->"1"
def parse_symbol_filters(symbol_info: dict) -> SymbolFilters
    # f = {x["filterType"]: x for x in symbol_info["filters"]}
    # PRICE_FILTER tickSize/minPrice/maxPrice; LOT_SIZE stepSize/minQty/maxQty;
    # MARKET_LOT_SIZE (fallback to LOT_SIZE values if absent); MIN_NOTIONAL key "notional" (fallback "minNotional", default "0");
    # PERCENT_PRICE multiplierUp/multiplierDown (default "1.05"/"0.95"); triggerProtect default "0.05"; marketTakeBound default "0.05".
    # Missing PRICE_FILTER or LOT_SIZE -> DataError.
```

### 6.3 `bot/exchange/market.py`

```python
def klines_to_df(rows: list[list[Any]]) -> pd.DataFrame
    # 12-field rows -> DataFrame with KLINE_COLUMNS/KLINE_DTYPES (drops field 11 "ignore"); pd.to_numeric for strings;
    # sorted by open_time, duplicates dropped (keep last). Empty input -> empty frame with correct columns/dtypes.
def split_closed(df: pd.DataFrame, server_time_ms: int) -> tuple[pd.DataFrame, Candle | None]
    # closed = rows with close_time < server_time_ms (reset_index(drop=True));
    # forming = last row if its open_time <= server_time_ms <= close_time else None

class MarketData:
    def __init__(self, client: BinanceRestClient, *, filters_ttl_sec: float = 3600.0) -> None
    client: BinanceRestClient
    def server_time(self) -> int                           # client.sync_time(); return client.server_time_ms()
    def exchange_info(self) -> dict                        # GET /fapi/v1/exchangeInfo; updates client.weight_limit from rateLimits
                                                           # (REQUEST_WEIGHT, interval MINUTE, intervalNum 1)
    def symbol_filters(self, symbol: str) -> SymbolFilters # cached per symbol for filters_ttl_sec; DataError if symbol absent
    def klines(self, symbol: str, interval: str, *, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> pd.DataFrame           # GET /fapi/v1/klines; limit clamped 1..1500; INCLUDES the forming candle
    def recent_klines(self, symbol: str, interval: str, limit: int) -> tuple[pd.DataFrame, Candle | None, int]
        # server_now = self.server_time(); df = klines(limit=limit+1); closed, forming = split_closed(df, server_now)
        # returns (closed, forming, server_now)
    def funding_rates(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame
        # GET /fapi/v1/fundingRate paginated: limit=1000, startTime=start, endTime=end; next startTime = last fundingTime + 1;
        # stop when < 1000 rows or next start > end. Columns: funding_time int64, funding_rate float64, mark_price float64
        # (mark_price may be "" in old rows -> NaN). Sorted, deduped.
    def mark_price(self, symbol: str) -> float             # GET /fapi/v1/premiumIndex?symbol= -> float(markPrice)
    def premium_index(self, symbol: str) -> dict[str, float | int]
        # GET /fapi/v1/premiumIndex?symbol= -> {"mark_price": float, "last_funding_rate": float, "next_funding_time": int, "time": int}
```

---

## 7. Historical data — `bot/data/downloader.py` (U2)

Cache layout under `cfg.cache_dir`:
- `klines/{SYMBOL}_{interval}.csv` — header row, columns `KLINE_COLUMNS`, closed candles only.
- `funding/{SYMBOL}.csv` — columns `funding_time,funding_rate,mark_price`.
- `exchange_info/{SYMBOL}.json` — `SymbolFilters.to_dict()` + `{"fetched_at": ms, "host": base_url}`.

```python
def klines_cache_path(cache_dir: Path, symbol: str, interval: str) -> Path
def funding_cache_path(cache_dir: Path, symbol: str) -> Path
def filters_cache_path(cache_dir: Path, symbol: str) -> Path

def load_klines(cache_dir: Path, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame
    # reads CSV with dtype=KLINE_DTYPES; filters open_time >= start_ms and open_time <= end_ms; missing file -> empty frame.

def download_klines(market: MarketData, cache_dir: Path, symbol: str, interval: str, start_ms: int,
                    end_ms: int | None = None, *, progress: Callable[[int, int], None] | None = None) -> pd.DataFrame
```
Algorithm:
1. `server_now = market.server_time()`; `end = min(end_ms or server_now, server_now)`; `i = interval_to_ms(interval)`; align `start = floor_time(start_ms, i)`.
2. Load existing cache. Ranges to fetch: `[start, cached_min - i]` if `start < cached_min`, and `[cached_max + i, end]` if `cached_max + i <= end` (or `[start, end]` if the cache is empty).
3. For each range: loop `market.klines(symbol, interval, start_ms=cur, end_ms=range_end, limit=1500)`; drop rows with `close_time >= server_now` (open candle); append; `cur = last_open_time + i`; stop when fewer than 1500 rows are returned or `cur > range_end`. Call `progress(rows_so_far, expected_rows)` if given.
4. Merge with cache, drop duplicate `open_time` (keep new), sort, `validate_candles_df(df, i)`, write atomically (`df.to_csv(<path>.tmp, index=False, encoding="utf-8")` + `fsutil.atomic_replace`).
5. `gaps = find_gaps(df, i)`; if any, `logger.warning("%d gaps in %s %s (exchange maintenance?)", ...)`. Never forward-fill.
6. Return `load_klines(cache_dir, symbol, interval, start_ms, end_ms)`.

```python
def find_gaps(df: pd.DataFrame, interval_ms: int) -> list[tuple[int, int]]   # (prev_open_time, next_open_time) where diff != interval_ms
def load_funding(cache_dir: Path, symbol: str, start_ms: int | None = None, end_ms: int | None = None) -> pd.DataFrame
def download_funding(market: MarketData, cache_dir: Path, symbol: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame
    # same incremental logic via market.funding_rates (next start = last funding_time + 1)
def load_or_fetch_filters(market: MarketData | None, cache_dir: Path, symbol: str, *, max_age_sec: float = 86400.0) -> SymbolFilters
    # cache fresh -> return it; else if market: fetch symbol_filters (MAINNET host) and write cache; else if stale cache exists
    # -> return it with a warning; else DataError("no cached exchange filters; run: python -m bot download ...")
```
Backtests always use **mainnet** public data (never demo klines).

---

## 8. Strategy, indicators, risk (U3)

### 8.1 `bot/strategy/indicators.py`
```python
def sma(s: pd.Series, period: int) -> pd.Series            # s.rolling(period, min_periods=period).mean()
def ema(s: pd.Series, period: int) -> pd.Series            # s.ewm(span=period, adjust=False, min_periods=period).mean()
def moving_average(s: pd.Series, period: int, kind: str) -> pd.Series   # kind "SMA"/"EMA" (upper-case), else ConfigError
def true_range(df: pd.DataFrame) -> pd.Series              # max(h-l, |h-prev_c|, |l-prev_c|); row 0 = h-l
def atr(df: pd.DataFrame, period: int) -> pd.Series        # true_range(df).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
```
All functions are causal (value at row i depends only on rows ≤ i), return float64 Series aligned to the input index, and raise `ValueError` for `period < 1`.
Reference values (tests): `sma([1,2,3,4,5],3) = [nan,nan,2,3,4]`; `ema([1,2,3,4,5],3) = [nan,nan,2.25,3.125,4.0625]`.

### 8.2 `bot/strategy/base.py`
```python
class Strategy(ABC):
    name: ClassVar[str]                                   # registry key, lower_snake_case
    required_columns: ClassVar[tuple[str, ...]] = ("open_time","open","high","low","close")

    def __init__(self, params: Mapping[str, Any] | None = None) -> None
        # self.params = {**self.default_params(), **(params or {})}; unknown keys -> ConfigError; then self.validate_params()
    @classmethod
    @abstractmethod
    def default_params(cls) -> dict[str, Any]
    def validate_params(self) -> None                     # override; raise ConfigError on bad params
    @property
    @abstractmethod
    def warmup_bars(self) -> int                          # min number of closed bars before signal_at may return non-NONE
    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame
        # returns a NEW DataFrame (df.copy()) with indicator columns appended; must not mutate df; must be causal.
    @abstractmethod
    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal
        # signal produced at the CLOSE of row i, using only rows 0..i of `prepared`. Must return NONE with reason "warmup"
        # when i < warmup_bars - 1 or any needed value is NaN.
    def generate(self, df: pd.DataFrame) -> Signal         # prepared = self.prepare(df); return self.signal_at(prepared, len(prepared)-1)
    def describe(self) -> str                              # f"{name}({k=v, ...})"
```
Contract: **the same class instance code is used in backtest (prepare once, `signal_at(i)` for each i) and live (`generate(closed_df)`)**. Strategies never see the forming candle and never know the position (the trader/engine maps signals to actions with `risk.decide_action`).

### 8.3 `bot/strategy/registry.py`
```python
def register(cls: type[Strategy]) -> type[Strategy]        # decorator; duplicate name -> ValueError; empty name -> ValueError
def get_strategy_class(name: str) -> type[Strategy]        # ConfigError(f"unknown strategy '{name}'. available: ...")
def create_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy
def available_strategies() -> list[str]                    # sorted
def load_strategy_modules(modules: Iterable[str]) -> None  # importlib.import_module each; ImportError -> ConfigError
```
`bot/strategy/__init__.py`: `from . import ma_cross  # noqa: F401` and re-exports `Strategy, register, create_strategy, available_strategies, load_strategy_modules`.

User extension (documented in README): create `user_strategies/my_rsi.py` next to `config.yaml` with `@register class MyRsi(Strategy): name = "my_rsi" ...`, add `"user_strategies.my_rsi"` to `strategy.extra_modules`. The CLI inserts `cfg.base_dir` at `sys.path[0]` before `load_strategy_modules`.

### 8.4 `bot/strategy/ma_cross.py`
```python
@register
class MACrossStrategy(Strategy):
    name = "ma_cross"
    default_params -> {"fast_period": 20, "slow_period": 50, "ma_type": "EMA", "allow_short": True}
    validate_params: fast_period int ≥ 1; slow_period int > fast_period; ma_type upper-cased in {"SMA","EMA"}; allow_short bool
    warmup_bars: EMA -> 3 * slow_period + 1 ; SMA -> slow_period + 1
    prepare: adds "ma_fast", "ma_slow" (moving_average of close)
    signal_at(prepared, i):
        if i < warmup_bars - 1 or i < 1 -> NONE("warmup")
        f0, s0 = ma_fast[i-1], ma_slow[i-1]; f1, s1 = ma_fast[i], ma_slow[i]; any NaN -> NONE("warmup")
        d0 = f0 - s0; d1 = f1 - s1
        if d0 <= 0 and d1 > 0  -> LONG  (reason "golden_cross")
        if d0 >= 0 and d1 < 0  -> SHORT (reason "dead_cross") if allow_short else CLOSE (reason "dead_cross_close_long")
        else NONE("no_cross")
        meta = {"ma_fast": f1, "ma_slow": s1}; price = close[i]; bar_open_time = open_time[i]
```

### 8.5 `bot/risk.py`
```python
def compute_stop_price(entry: float, direction: Direction, cfg: StopLossConfig, atr_value: float | None) -> float | None
    # percent: LONG entry*(1-p/100), SHORT entry*(1+p/100)
    # atr: None if atr_value is None/NaN/<=0; LONG entry - m*atr, SHORT entry + m*atr
    # returns None if result <= 0
def compute_take_profit(entry: float, stop: float, direction: Direction, r_multiple: float | None) -> float | None
    # None if r_multiple is None; entry + sign * r * |entry - stop|
def approx_liquidation_price(entry: float, direction: Direction, leverage: int, mmr: float) -> float
    # LONG  entry*(1 - 1/L)/(1 - mmr) ; SHORT entry*(1 + 1/L)/(1 + mmr)   (cum = 0 => conservative)
    # e.g. (84000, LONG, 10, 0.004) -> 75903.6145 ; (84000, SHORT, 10, 0.004) -> 92031.8725

def plan_entry(*, direction: Direction, ref_price: float, equity: float, atr_value: float | None,
               filters: SymbolFilters, risk: RiskConfig, fees: FeeConfig, slippage_bps: float) -> RiskDecision
```
`plan_entry` algorithm (exact order; first failure returns `RiskDecision(None, code)`):
1. `direction` must be LONG/SHORT else ValueError. `equity <= 0` -> `"no_equity"`. `ref_price <= 0` -> `"bad_price"`.
2. `stop_raw = compute_stop_price(...)`; None -> `"stop_unavailable"` (e.g. ATR NaN).
3. `stop = round_protective_price(stop_raw, filters.tick_size, entry=ref_price)`; LONG requires `stop < ref_price`, SHORT `stop > ref_price`, else `"invalid_stop"`.
4. `tp = compute_take_profit(ref_price, float(stop), direction, risk.take_profit_r)` then `round_protective_price(tp, tick, entry=ref_price)` if not None; if TP ends on the wrong side or equals ref -> `"invalid_take_profit"`.
5. `slip = slippage_bps / 10_000`; `s = float(stop)`; per-unit loss of a clean stop-out **exactly as the fill model books it** (entry and SL exit both slipped adversely, taker fee on the slipped prices, §9.1):
   - LONG:  `per_unit_loss = (ref - s) + ref*slip + s*slip + ref*(1+slip)*fees.taker + s*(1-slip)*fees.taker`
   - SHORT: `per_unit_loss = (s - ref) + ref*slip + s*slip + ref*(1-slip)*fees.taker + s*(1+slip)*fees.taker`
6. `risk_amount_target = equity * risk.risk_per_trade_pct / 100`; `qty_risk = risk_amount_target / per_unit_loss`.
7. `qty_margin = equity * risk.max_margin_fraction * risk.leverage / ref`; `qty_notional = risk.max_position_notional / ref` (the trader passes a `RiskConfig` whose `max_position_notional` is already `min(config value, exchange bracket maxNotionalValue)`, §10.3).
8. `raw = min(qty_risk, qty_margin, qty_notional)`; `sizing_cap` = the name of the smallest (`"risk"`, `"margin"`, `"notional"`; ties resolved in that order). `qty = normalize_market_qty(raw, filters)`; `qty == 0` -> `"below_min_qty"`; `not meets_min_notional(qty, ref, filters)` -> `"below_min_notional"` (never size up).
9. `liq = approx_liquidation_price(ref, direction, risk.leverage, risk.maint_margin_rate + risk.liq_mmr_buffer)`; if `|ref - liq| < risk.min_liq_distance_multiple * |ref - stop|` -> `"liquidation_too_close"`.
10. Return `RiskDecision(TradePlan(symbol=filters.symbol, direction, ref_price, qty, stop, tp, notional=float(qty)*ref, risk_amount=float(qty)*per_unit_loss, leverage=risk.leverage, liquidation_price=liq, sizing_cap=sizing_cap), "ok")`.

Worked example (test `test_plan_entry_reference_example`): equity 10000, risk 1 %, LONG, ref 50000, stop mode percent 2 %, taker 0.0005, slip 5 bps, leverage 3, max_margin_fraction 0.9, max notional 5000 (explicit in the test), TP 2R, BTC filters. -> stop 49000.0, per_unit_loss = 1000 + 25 + 24.5 + 25.0125 + 24.48775 = 1099.00025, qty_risk 0.0909918.., qty_margin 0.54, qty_notional 0.1 -> sizing_cap "risk", qty `Decimal("0.090")`, TP 52000.0, risk_amount 98.9100225, liq ≈ 33636.06 (distance 16363.9 ≥ 2000) -> ok.
Consequence (tested in U4 `test_clean_stop_out_is_minus_one_r`): a trade that enters at `ref` and is stopped exactly at `stop` (no gap, no funding) books `r_multiple == -1.0 ± 1e-9`.

```python
def decide_action(signal: SignalAction, position: Direction, entries_allowed: bool) -> Action
```
| signal \ position | FLAT | LONG | SHORT |
|---|---|---|---|
| NONE | NONE | NONE | NONE |
| LONG | OPEN_LONG if allowed else NONE | NONE | FLIP_LONG if allowed else CLOSE |
| SHORT | OPEN_SHORT if allowed else NONE | FLIP_SHORT if allowed else CLOSE | NONE |
| CLOSE | NONE | CLOSE | NONE |

`SignalAction.CLOSE` means "exit a long" (it is produced by a dead cross when shorts are disabled); it never closes a short (a short can only exist here by adoption or because `allow_short` was switched off while short; it keeps its exchange SL/TP and is closed by the next golden cross via FLIP/CLOSE).

```python
class DailyLossKillSwitch:
    def __init__(self, max_daily_loss_pct: float) -> None
    day: str | None; day_start_equity: float | None; last_equity: float | None
    tripped: bool; tripped_at: int | None; reason: str | None
    def seed(self, equity: float) -> None
        # if last_equity is None: last_equity = equity   (backtest: initial_balance; trader: equity at startup)
    def update(self, t_ms: int, equity: float) -> bool
        # t_ms = CLOSE time of the bar whose closing equity this is (backtest t_close; trader bar_for_ids + interval_ms - 1)
        # d = utc_day(t_ms)
        # if d != day:                                   # first call, or a new UTC day
        #     day = d; day_start_equity = last_equity if last_equity is not None else equity
        #     tripped = False; tripped_at = None; reason = None
        # last_equity = equity
        # if pct > 0 and not tripped and equity <= day_start_equity * (1 - pct/100):
        #     tripped=True, tripped_at=t_ms, reason=f"daily loss {loss_pct:.2f}% >= {pct}%" ; return True (newly tripped)
        # return False
        # => the baseline of day D is the last equity seen before D (the close of the previous day's last bar), so the
        #    first bar of every day counts; on 1d candles every bar can trip.
    @property entries_allowed -> bool                       # not tripped
    def to_dict(self) -> dict                               # includes last_equity
    @classmethod from_dict(cls, d: dict, max_daily_loss_pct: float) -> DailyLossKillSwitch   # tolerates missing last_equity

class Cooldown:
    def __init__(self, bars: int, interval_ms: int) -> None
    until_ms: int | None
    def trigger(self, stop_bar_open_time_ms: int) -> None   # until_ms = stop_bar_open_time + bars * interval_ms
    def active(self, decision_bar_open_time_ms: int) -> bool   # until_ms is not None and decision_bar_open_time < until_ms (strict)
    def to_dict(self) -> dict; @classmethod from_dict(cls, d, bars, interval_ms) -> Cooldown
```
Cooldown semantics example: bars=3, stop-out in bar with open_time T. Entry decisions at the close of bars T, T+i, T+2i are blocked (3 decisions); the close of T+3i may open (fill at T+4i). bars=0 blocks nothing. Exits are never blocked. The UTC day reset means the kill switch re-arms at 00:00 UTC (09:00 KST).

---

## 9. Fill model, brokers (U4, U5)

### 9.1 `bot/fillmodel.py` (U4) — shared by backtest and paper broker
```python
@dataclass(frozen=True, slots=True)
class FillModel:
    maker_fee: float; taker_fee: float; slippage_bps: float
    @classmethod from_config(cls, execution: ExecutionConfig) -> FillModel
    @property slip -> float                                        # slippage_bps / 10_000
    def market_fill_price(self, ref_price: float, side: Side) -> float   # BUY ref*(1+slip); SELL ref*(1-slip)
    def exit_fill_price(self, base_price: float, closing_side: Side) -> float   # same adverse rule as market_fill_price
    def fee(self, qty: float, price: float, *, taker: bool = True) -> float     # abs(qty)*price*(taker_fee or maker_fee)

def resolve_intrabar_exit(direction: Direction, o: float, h: float, l: float, stop: float,
                          tp: float | None, liq: float | None) -> tuple[ExitReason, float] | None
    # Returns (reason, base_price before slippage) or None. Gap rules on the OPEN first (the open is the first price of
    # the bar, so an open beyond a level is not ambiguous), then the conservative SL-first rules inside the bar:
    # LONG : if liq is not None and o <= liq:   (LIQUIDATION, liq)       # gap through liquidation
    #        elif o <= stop:                    (STOP_LOSS, o)           # gap through stop fills at open
    #        elif tp is not None and o >= tp:   (TAKE_PROFIT, o)         # gap beyond TP fills TP at open
    #        elif l <= stop:                    (STOP_LOSS, stop)
    #        elif liq is not None and l <= liq: (LIQUIDATION, liq)
    #        elif tp is not None and h >= tp:   (TAKE_PROFIT, tp)
    # SHORT: mirror: o >= liq -> (LIQ, liq); o >= stop -> (STOP_LOSS, o); tp and o <= tp -> (TAKE_PROFIT, o);
    #        h >= stop -> (STOP_LOSS, stop); liq and h >= liq -> (LIQ, liq); tp and l <= tp -> (TAKE_PROFIT, tp)
    # If both SL and TP are touched inside the same bar (neither gapped at the open), STOP_LOSS wins (tested).

def funding_payment(qty_signed: float, mark_price: float, rate: float) -> float   # qty_signed*mark*rate ; + = paid

def liquidation_loss(qty: float, entry_price: float, leverage: int, funding_paid: float = 0.0) -> float
    # gross_pnl booked at liquidation = -(abs(qty)*entry_price/leverage - funding_paid)
    # Under isolated margin, funding is paid from / received into the position's isolated margin, so the total loss at
    # liquidation is exactly the initial margin: with net = gross - fees - funding, net = -IM - fees (funding_paid is the
    # position's accumulated funding, + = paid, - = received).
```
Exit accounting rule (backtest and paper): STOP_LOSS/TAKE_PROFIT fills at `exit_fill_price(base, closing_side)` with taker fee; LIQUIDATION: `exit_price = liq`, `gross_pnl = liquidation_loss(qty, entry, leverage, funding_paid=<position funding so far>)`, `exit_fee = 0`. Not modelled (documented in README): the shift of the liquidation price caused by funding and the exchange's liquidation clearance fee (the fee is counted for exchange-detected liquidations, §9.4 sync).

### 9.2 `bot/broker/base.py` (U5)
```python
class Broker(ABC):
    mode: Mode
    @abstractmethod
    def prepare_symbol(self, symbol: str, leverage: int) -> SymbolFilters
    @abstractmethod
    def sync(self, symbol: str, active: ActiveTrade | None, closed_candles: Sequence[Candle]) -> SyncResult
        # Bring broker state up to date and report it. Paper: simulate protective exits / funding / liquidation over candles
        # not yet processed. Exchange: read account; detect a closure of `active`; clean orphans. Never opens positions.
        # closed_candles may be EMPTY (trader re-reads after executing, and the between-bar protection check): then no
        # simulation happens and no cursor/state changes — only the account and issues are built (read-only).
    @abstractmethod
    def open_position(self, plan: TradePlan, *, entry_client_id: str, sl_client_id: str, tp_client_id: str | None,
                      ref_price: float, bar_time: int) -> OpenOutcome
        # Market entry, then protective SL (+TP if plan.take_profit_price). If SL cannot be placed: flatten and raise
        # ProtectionFailedError(flattened=True, closure=..., entry=...). If flatten also fails: raise EmergencyError(entry=...).
        # filled=False is returned ONLY when the broker has confirmed that no position resulted.
    @abstractmethod
    def close_position(self, symbol: str, active: ActiveTrade | None, *, reason: ExitReason, client_id: str,
                       ref_price: float | None, bar_time: int) -> PositionClosure | None
        # Market reduce-only close of the whole position, then cancel THIS BOT's protective + regular orders on the symbol
        # (client id starts with client_id_prefix(bot_id, symbol)); foreign orders are never cancelled.
        # Returns None if already flat (after still cancelling the bot's orphan orders).
    @abstractmethod
    def ensure_protection(self, active: ActiveTrade, account: AccountSnapshot, *, sl_client_id: str,
                          tp_client_id: str | None) -> tuple[ProtectiveOrder, ...]
        # Guarantees a VALID bot SL for the current position (and TP if active.take_profit_price and missing/invalid).
        # Valid SL = kind STOP_LOSS, client id starts with the bot prefix, status in {"NEW","TRIGGERING"}, and (reduce_only
        # mode) quantity >= abs(position qty) - 1e-12. If none: PLACE the new generation first (sl_client_id/tp_client_id,
        # which the trader builds with protect_seq + 1), THEN cancel the stale bot protective orders by clientAlgoId
        # (place-before-cancel, so there is never a gap). Returns the resulting protective orders.
        # SL failure -> flatten + ProtectionFailedError as in open_position.
    def max_notional(self, symbol: str) -> float | None
        # NOT abstract; default None. ExchangeBroker returns the cached maxNotionalValue of the leverage bracket (§9.4).
```

### 9.3 `bot/broker/paper.py` (U5) — local simulation on real public data
```python
class PaperBroker(Broker):
    mode = Mode.PAPER
    def __init__(self, *, market: MarketData, fill_model: FillModel, storage: Storage, initial_balance: float,
                 include_funding: bool = True, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None
```
- `market.client` must have no credentials (assert `not market.client.has_credentials`, else `ConfigError`).
- State (persisted after every mutation to `storage.set_state(f"paper_state:{symbol}", ...)`):
  `{"cash": float, "position": null | {"direction","qty","entry_price","entry_time","entry_bar_open_time","stop","tp","liq","leverage","sl_client_id","tp_client_id","funding"}, "last_bar_open_time": int|null, "last_close": float|null, "funding_cursor": int|null}`. Missing state -> `cash = initial_balance`.
- `funding_cursor` = `funding_time` of the last funding event **actually applied** (never the end of a queried range), so a record that Binance publishes a few seconds late is picked up by the next fetch. Queries always start at `funding_cursor + 1`.
- `prepare_symbol` -> `market.symbol_filters(symbol)` (mainnet filters). Leverage is only recorded.
- `sync(symbol, active, closed_candles)`:
  0. If `closed_candles` is empty: skip steps 1–3 entirely (no simulation, no funding fetch, `last_bar_open_time`/`last_close`/`funding_cursor` unchanged) and only run steps 4–5.
  1. If `last_bar_open_time is None`: set it to the last candle's open_time (do not simulate history); persist.
  2. For each candle with `open_time > last_bar_open_time` (ascending) and while a position exists and `candle.open_time >= position.entry_bar_open_time`:
     a. if include_funding: apply funding events with `entry_time < ft <= candle.open_time` and `ft > funding_cursor`, using `market.funding_rates(symbol, funding_cursor+1, candle.close_time)` fetched **once per sync** for the whole range; `payment = funding_payment(sign*qty, mark_price or candle.open, rate)`; `cash -= payment`; `position.funding += payment`; `funding_cursor = ft`.
     b. `hit = resolve_intrabar_exit(dir, o, h, l, stop, tp, liq)`. If hit: first apply funding events `<= candle.close_time`; then exit at `exit_time = candle.close_time`: price/fee/gross per §9.1 (LIQUIDATION: `gross = liquidation_loss(qty, entry, leverage, funding_paid=position.funding)`); `cash += gross - exit_fee`; build `PositionClosure(exit_time, exit_price, qty, reason, exit_fee, funding=position.funding, gross_pnl=gross)`; clear position.
  3. Update `last_bar_open_time`, `last_close` to the last candle. Persist.
  4. Account: mark = `last_close`; `upnl = sign*qty*(mark - entry)`; `equity = cash + upnl`; `available = cash - margin` where `margin = qty*entry/leverage`; position -> `Position(qty signed, entry, mark, upnl, liq, margin, leverage, updated_at=now)`; protective orders synthesized (STOP_LOSS always; TAKE_PROFIT if tp) with status "NEW", `exchange_id=None`, `close_position=True`.
  5. Issues: if `active` is None and a paper position exists -> "UNTRACKED_POSITION"; if `active` is not None and no paper position and no closure -> "CLOSURE_DETAILS_UNKNOWN" plus a closure with reason UNKNOWN at `last_close` (keeps trader state consistent).
- `open_position`: requires flat (else `BotError`). `side = plan.direction.opening_side`; `fill = fill_model.market_fill_price(ref_price, side)`; `qty = float(plan.qty)`; `fee = fill_model.fee(qty, fill)`; `cash -= fee`; position stored with `stop=float(plan.stop_price)`, `tp`, `liq=plan.liquidation_price`, `entry_time=bar_time`, `entry_bar_open_time=bar_time`, `funding=0`, `funding_cursor=bar_time`. Returns `OpenOutcome(filled=True, qty, fill, fee, bar_time, entry_order=OrderResult(... status FILLED ...), protective=(SL, TP?))`. Never raises ProtectionFailedError.
- `close_position`: `ref_price` required (ValueError if None). Flat -> None. If include_funding: apply pending funding events with `funding_cursor < ft <= bar_time` (fetch `funding_rates(symbol, funding_cursor+1, bar_time)`). **Late-record rule** (parity with the backtest, which charges funding at `ft == exit_time`): let `last_known_ft` = the latest funding time known (fetched rows or `funding_cursor`) and `funding_interval` = the difference of the last two known funding times (default 8 h = 28_800_000). If the fetched rows contain no event at `bar_time` but `bar_time > last_known_ft`, `(bar_time - last_known_ft) % funding_interval == 0` and `bar_time > position.entry_time`, the settlement record is probably not published yet: re-fetch up to 3 times with `sleep(1.0)`; if still missing, charge an **estimated** payment `funding_payment(sign*qty, p["mark_price"], p["last_funding_rate"])` with `p = market.premium_index(symbol)`, set `funding_cursor = bar_time`, log WARNING `"funding at %s estimated from premiumIndex"` and `storage.log_event("WARNING", "paper", "FUNDING_ESTIMATED", ...)`. Then `exit = exit_fill_price(ref_price, closing_side)`; `exit_fee = fee(qty, exit)`; `gross = sign*qty*(exit-entry)`; `cash += gross - exit_fee`; closure with `exit_time=bar_time`, `funding=position.funding`, given `reason`.
- `ensure_protection`: returns the synthesized protective orders (always present; never places anything).

### 9.4 `bot/broker/exchange_broker.py` (U5) — testnet (Demo Trading) and live

```python
class ExchangeBroker(Broker):
    def __init__(self, *, mode: Mode, client: BinanceRestClient, market: MarketData, execution: ExecutionConfig,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None
```
Constructor guards: `mode` must be TESTNET or LIVE (else ConfigError); `client.has_credentials` must be true; `client.base_url` must equal `TESTNET_REST_URL` for testnet and `MAINNET_REST_URL` for live (else ConfigError "host/mode mismatch").

All endpoints below are called through `client.signed_request` except exchangeInfo/klines (public). Params are listed **in the exact insertion order** to send. Booleans are sent as `"true"`/`"false"` (encode_params does this). Prices/quantities via `format_decimal`.

Ownership: `prefix = client_id_prefix(execution.bot_id, symbol)`. An order (regular `clientOrderId` or algo `clientAlgoId`) is **own** iff its id starts with `prefix`. The broker only ever cancels own orders.

Private helpers (names are normative so tests can target them; all use the injected `sleep`):
- `_lookup_order(symbol, client_id) -> dict | None` — `GET /fapi/v1/order symbol, origClientOrderId`; `NoSuchOrderError` -> None.
- `_await_terminal(symbol, order) -> dict` — while `order["status"]` not in {FILLED, CANCELED, EXPIRED, EXPIRED_IN_MATCH, REJECTED}: up to 5 polls `GET /fapi/v1/order symbol, orderId` with `sleep(1.0)` between; returns the last seen order.
- `_position(symbol) -> dict | None` — `GET /fapi/v3/positionRisk symbol`, row with `positionSide == "BOTH"` and `float(positionAmt) != 0`, else None.
- `_lookup_algo(symbol, client_algo_id) -> dict | None` — `GET /fapi/v1/algoOrder clientAlgoId`; up to 3 attempts `sleep(0.5)` apart while not found (`NoSuchOrderError` or any other 4xx `ExchangeError`); a found order whose `symbol` differs is treated as not found (logged ERROR).
- `_funding_income(symbol, start_ms, end_ms) -> float` — `GET /fapi/v1/income symbol, incomeType=FUNDING_FEE, startTime, endTime, limit=1000`, paginated: next `startTime = last row time + 1` until fewer than 1000 rows or start > end; returns `-sum(float(income))` (+ = paid).
- `_income_sum(symbol, income_type, start_ms, end_ms) -> float` — same pagination for any `incomeType`, returns `sum(float(income))` (signed as Binance reports it).
- `_cancel_own_orders(symbol) -> int` — `GET /fapi/v1/openAlgoOrders symbol` -> for each own: `DELETE /fapi/v1/algoOrder clientAlgoId`; `GET /fapi/v1/openOrders symbol` -> for each own: `DELETE /fapi/v1/order symbol, origClientOrderId`. `NoSuchOrderError` ignored; transient errors are retried by the client (DELETE is idempotent). Returns the number cancelled.
- `_fresh_client_id(symbol, client_id) -> str` — while `_lookup_order(symbol, client_id)` finds an existing order: `client_id = next_client_id(client_id)` (max 5, then `BotError`). Guarantees every order we send has a never-used id, so lookups by `origClientOrderId` are unambiguous.

**prepare_symbol(symbol, leverage)**
1. `client.sync_time()`; `filters = market.symbol_filters(symbol)` (same host). `filters.status != "TRADING"` or `contract_type != "PERPETUAL"` -> ConfigError.
2. `GET /fapi/v1/accountConfig` -> `dualSidePosition`, `multiAssetsMargin`.
3. If `dualSidePosition` is true:
   - **live**: never change it (it is account-wide and flips UM and CM together) -> `ConfigError("account is in Hedge mode. 바이낸스 선물 설정에서 포지션 모드를 단방향(One-way)으로 직접 바꾼 뒤 다시 실행하세요 (UM/CM 모두 적용됨)")`.
   - **testnet**: `POST /fapi/v1/positionSide/dual` `dualSidePosition=false`. `NoChangeNeededError` = ok. Code -4067/-4068/-4531 -> ConfigError("account is in Hedge mode; close all UM/CM positions/orders and switch to One-way manually").
4. If `multiAssetsMargin` is true:
   - **live**: never change it -> `ConfigError("multi-assets mode is on. 바이낸스 선물 설정에서 멀티에셋 모드를 끄고(단일 자산 모드) 다시 실행하세요")`.
   - **testnet**: `POST /fapi/v1/multiAssetsMargin` `multiAssetsMargin=false` (`NoChangeNeededError` ok; other failure -> ConfigError: isolated margin requires single-asset mode).
5. `sc = GET /fapi/v1/symbolConfig symbol` (array; take the row for `symbol`) -> `marginType`, `leverage`, `maxNotionalValue`; `pos = _position(symbol)`.
6. If `sc.marginType != "ISOLATED"`: if `pos` -> ConfigError("cannot switch to ISOLATED while a position exists; close it manually"); else `POST /fapi/v1/marginType symbol, marginType=ISOLATED` (`NoChangeNeededError` -4046 ok; -4047/-4048 -> ConfigError("cannot switch to ISOLATED while orders/positions exist")).
7. Leverage (never reduce under an open position):
   - `pos` exists and `int(sc.leverage) != leverage` -> do **not** call `/leverage`; log WARNING `"keeping exchange leverage %d for the open position; configured %d applies when flat"`; cache `self._leverage[symbol] = int(sc.leverage)`, `self._max_notional[symbol] = float(sc.maxNotionalValue)`.
   - otherwise `POST /fapi/v1/leverage symbol, leverage` -> response `leverage` must equal requested; cache it and `maxNotionalValue`. `-4028` -> ConfigError. `ExchangeError(code=-4161)` (race: position appeared) -> tolerate exactly like the previous bullet.
   - Always record `self._desired_leverage[symbol] = leverage`.
8. `GET /fapi/v1/symbolConfig symbol` -> verify ISOLATED and leverage == cached (ConfigError otherwise).
9. Return filters. (The broker does not see `RiskConfig`; the trader compares `max_notional()` with `risk.max_position_notional`, logs a WARNING once when the bracket is lower, and caps sizing, §10.3.)

`max_notional(symbol)` returns `self._max_notional.get(symbol)`; `Position.leverage` is `self._leverage.get(symbol)`.

**account snapshot** (private `_account(symbol) -> AccountSnapshot`):
- `GET /fapi/v3/account` -> asset `USDT`: `walletBalance`, `marginBalance` (= equity), `availableBalance`, `unrealizedProfit`.
- `GET /fapi/v3/positionRisk` `symbol` -> row with `positionSide == "BOTH"`; `positionAmt == 0` or absent -> no position. Fields `entryPrice, markPrice, unRealizedProfit, liquidationPrice, isolatedMargin, updateTime`; leverage from cache.
- `GET /fapi/v1/openAlgoOrders` `symbol` (response may be a list or `{"orders":[...]}`; accept both) -> `ProtectiveOrder` for **all** algo orders on the symbol (own and foreign; the dashboard shows both): `orderType STOP_MARKET -> STOP_LOSS`, `TAKE_PROFIT_MARKET -> TAKE_PROFIT` (other types skipped), `client_id=clientAlgoId`, `exchange_id=str(algoId)`, `trigger_price=float(triggerPrice)`, `status=algoStatus`, `close_position=closePosition in (True,"true")`, `quantity=float(quantity) if quantity not in (None,"","0") else None`.
- `GET /fapi/v1/openOrders` `symbol` -> `open_orders_count` (all regular orders); own/foreign split kept privately for sync step 7.

**Placing an order whose outcome is not definitive.** "Non-definitive" = `UnknownOrderStatusError`, `TransientError` (any, incl. -1001), `RateLimitError` (after `sleep(min(e.retry_after or 1, 10))`), `DuplicateClientIdError`. The broker never blindly re-sends after a non-definitive outcome; it resolves by lookup first (entry/close: `_lookup_order`; protective: `_lookup_algo`).

**open_position(plan, entry_client_id, sl_client_id, tp_client_id, ref_price, bar_time)**
0. Leverage catch-up: if `self._leverage[symbol] != self._desired_leverage[symbol]` (it was kept for an earlier position, §prepare_symbol 7), `POST /fapi/v1/leverage` now (the trader only opens when flat) and update the caches; failure -> return `OpenOutcome(filled=False, message="leverage update failed: ...")`.
1. Pre-flight idempotency: `o = _lookup_order(symbol, entry_client_id)`. If found: `o = _await_terminal(o)` if not terminal; if `float(o.executedQty) > 0` (**whatever the status**, incl. EXPIRED with a partial fill) -> reuse it and skip step 2; if it ended with 0 executed -> `entry_client_id = _fresh_client_id(symbol, entry_client_id)` and continue.
2. Entry: `POST /fapi/v1/order` params `symbol, side=plan.direction.opening_side, type=MARKET, quantity=format_decimal(plan.qty), newClientOrderId=entry_client_id, newOrderRespType=RESULT`.
   - Success -> `o = response`.
   - Non-definitive -> up to 3 `_lookup_order` calls spaced `sleep(1.0)`; found -> `o`; still not found -> `o = None`.
   - Definitive rejection: `InsufficientMarginError`, `MinNotionalError`, `ReduceOnlyModeError`, other `OrderRejectedError` (incl. -2027/-2028 bracket cap) -> `o = None`, `message = "<code> <msg>"` (no raise).
3. If `o` is not terminal (a MARKET RESULT can come back NEW/PARTIALLY_FILLED): `o = _await_terminal(o)`.
4. **Position truth check (mandatory before any `filled=False`):** `pos = _position(symbol)`.
   - `pos` is None and (`o` is None or executedQty == 0) -> return `OpenOutcome(filled=False, message=message or "entry not filled (status <s>)")`. This is the ONLY way to return `filled=False` after step 2.
   - `pos` exists with a direction opposite to `plan.direction` -> do not touch it; return `OpenOutcome(filled=False, message="unexpected opposite position")` (the next sync reports UNTRACKED_POSITION and the trader adopts and protects it).
   - `pos` exists (same direction) -> the entry is filled even if the order lookup failed ("recovered"): `qty = float(o.executedQty) if o and executedQty > 0 else abs(float(pos.positionAmt))`; `pos_qty = abs(float(pos.positionAmt))` (used as the reduce_only SL quantity).
   - `o` has executedQty > 0 but `pos` is None -> the position was already closed again (e.g. instant liquidation / manual): still return `filled=True` with the fill data and `protective=()`; the next sync records the closure.
5. Average price: `avgPrice` from `o` if present and > 0, else `GET /fapi/v1/order symbol, orderId` (`avgPrice`); recovered without `o` -> `float(pos.entryPrice)`. `entry_time = o.updateTime` (recovered: `pos.updateTime`).
6. Entry fee: if the orderId is known: `GET /fapi/v1/userTrades symbol, orderId` -> sum `commission` where `commissionAsset == "USDT"`; if unavailable, non-USDT or orderId unknown -> estimate `qty*avg*taker_fee`.
7. SL via the placement procedure of §9.5 (`kind=STOP_LOSS`, trigger `plan.stop_price`, quantity `pos_qty` in reduce_only mode). SL is mandatory.
8. SL failure (procedure returned "failed"): `closure = close_position(symbol, None, reason=PROTECTION_FAILED, client_id=make_client_id(bot_id, symbol, "FL", bar_time, 0), ref_price=None, bar_time=bar_time)`; raise `ProtectionFailedError(flattened=True, closure=closure, entry=outcome)` where `outcome` is the filled `OpenOutcome` built so far. If that close raises -> raise `EmergencyError("entry filled but unprotected and flatten failed", entry=outcome)`.
9. TP (only if `plan.take_profit_price`): same procedure with `kind=TAKE_PROFIT`; failure -> log WARNING and keep the position (the SL exists).
10. Return `OpenOutcome(filled=True, qty, avg, fee, entry_time, entry_order=<OrderResult from o, or None when recovered>, protective=(sl, tp?))`.

**close_position(symbol, active, reason, client_id, ref_price, bar_time)**
1. `pos = _position(symbol)`. Flat -> `_cancel_own_orders(symbol)` and return None.
2. `client_id = _fresh_client_id(symbol, client_id)`.
3. Loop (at most 3 orders in total, each with a fresh id obtained by `next_client_id` + `_fresh_client_id`): `POST /fapi/v1/order` `symbol, side=closing side, type=MARKET, quantity=format_decimal(abs(positionAmt)), reduceOnly=true, newClientOrderId=client_id, newOrderRespType=RESULT`.
   - Success -> `_await_terminal`; remember the order.
   - Non-definitive -> up to 3 `_lookup_order` (1 s apart); found -> `_await_terminal`, remember it.
   - `DuplicateClientIdError` / not found after lookups / `ReduceOnlyRejectedError` -> fall through to the re-read below.
   - Then re-read `pos = _position(symbol)`: flat -> leave the loop; still open (partial fill, EXPIRED by `marketTakeBound`, unknown outcome) -> next iteration with the remaining `abs(positionAmt)` and the next id.
   - After 3 orders still not flat -> raise `EmergencyError("position not flat after close")`.
4. `_cancel_own_orders(symbol)` (own algo + own regular orders only; foreign orders are left alone and reported by sync).
5. Re-read position; if still non-zero -> raise `EmergencyError("position not flat after close")`.
6. Closure from `userTrades(symbol, orderId)` of every remembered close order (fallback when no order was identified: `userTrades symbol, startTime=bar_time - 60_000` filtered to the closing side): `exit_price` = qty-weighted average fill price, `exit_fee` = USDT commission sum, `gross_pnl` = realizedPnl sum, `exit_time` = max fill time; `funding = _funding_income(symbol, active.entry_time, now)` if `active` else 0.0. `order` = the last close order as `OrderResult`.

**sync(symbol, active, closed_candles)** (candles ignored; an empty list is normal)
1. `account = _account(symbol)`.
2. `active` not None and exchange flat -> the position was closed by SL/TP/liquidation/manual:
   - `GET /fapi/v1/userTrades symbol, startTime=max(active.entry_time, now-7d+60s), limit=1000`; closing fills = `side == active.direction.closing_side` and `time >= active.entry_time` and (`orderId != active.entry_order_id` when `active.entry_order_id` is not None; when it is None — adopted/paper-origin — every closing-side fill after `entry_time` counts).
   - Reason: `GET /fapi/v1/allAlgoOrders symbol, startTime=max(active.entry_time, now-7d+60s)` -> own orders whose `actualOrderId` is among the closing orderIds: `STOP_MARKET` -> STOP_LOSS, `TAKE_PROFIT_MARKET` -> TAKE_PROFIT. Else look up the last closing order (`GET /fapi/v1/order symbol, orderId`): `clientOrderId` starts with `"autoclose-"` -> LIQUIDATION; else MANUAL. Lookup errors -> UNKNOWN.
   - `exit_price` = qty-weighted average of closing fills; `exit_time` = max fill time; `exit_fee` = USDT commission sum; `gross_pnl` = realizedPnl sum; `funding = _funding_income(symbol, active.entry_time, now)`.
   - LIQUIDATION only: add the liquidation clearance fee if Binance books it separately: `exit_fee += abs(min(0.0, _income_sum(symbol, "INSURANCE_CLEAR", active.entry_time, now)))`. `# SPEC-GAP: verify on Demo whether the clearance fee is already inside realizedPnl/commission; if it is, INSURANCE_CLEAR rows will simply be absent.`
   - No closing fills found -> closure with reason UNKNOWN, `exit_price = active.entry_price`, `gross_pnl=None`, issue "CLOSURE_DETAILS_UNKNOWN".
   - Then `_cancel_own_orders(symbol)` (issue "ORPHAN_PROTECTIVE_CANCELED" if it cancelled anything).
3. `active` None and exchange position exists -> issue "UNTRACKED_POSITION" (trader adopts it).
4. `active` not None, position exists, `abs(qty) != active.qty` (beyond 1e-12) -> issue "QTY_MISMATCH".
5. Position exists and no **own** STOP_LOSS with status in {"NEW","TRIGGERING"} in `account.protective_orders` -> issue "SL_MISSING". (Foreign stops do not count.)
6. `protective_mode == reduce_only`, position exists, an own live SL exists, and (own SL `quantity < abs(qty) - 1e-12` or an own live TP has `quantity != abs(qty)` beyond 1e-12) -> issue "PROTECTION_QTY_MISMATCH".
7. Flat and own protective orders exist -> `_cancel_own_orders`, issue "ORPHAN_PROTECTIVE_CANCELED".
8. Foreign regular or algo open orders on the symbol -> issue "FOREIGN_OPEN_ORDERS" (never cancelled).
9. Return `SyncResult(account (re-read if anything was cancelled), closure, issues)`.

**ensure_protection(active, account, sl_client_id, tp_client_id)** — semantics of §9.2: if a valid own SL (and, when `active.take_profit_price`, a valid own TP) exists, return without any request. Otherwise place the missing/invalid ones with the §9.5 procedure (SL at `active.stop_price`, TP at `active.take_profit_price`; reduce_only quantity = `abs(position qty)` from `account.position`), then cancel the stale own protective orders of the replaced kind by `DELETE /fapi/v1/algoOrder clientAlgoId` (place-before-cancel). SL failure -> open_position step 8 policy (flatten with `make_client_id(bot_id, symbol, "FL", active.entry_bar_open_time, active.protect_seq + 1)`, `ProtectionFailedError(flattened=True, closure=..., entry=None)`; flatten failure -> `EmergencyError`). TP failure -> WARNING only.

### 9.5 Protective orders — exact request (CURRENT API: Algo Order service)
Stop and take-profit orders MUST NOT be sent to `/fapi/v1/order` (it returns `-4120 STOP_ORDER_SWITCH_ALGO` since 2025-12-09). Use:

`POST /fapi/v1/algoOrder` with params in this order:
```
algoType=CONDITIONAL
symbol=<SYMBOL>
side=<SELL for a long position | BUY for a short position>
type=<STOP_MARKET | TAKE_PROFIT_MARKET>
triggerPrice=<format_decimal(round_protective_price(price, tick, entry=entry_price))>
workingType=<execution.working_type>          # default MARK_PRICE
priceProtect=<"true"|"false" from execution.price_protect>   # default "false" (see below)
# protective_mode == close_position (default):
closePosition=true
# protective_mode == reduce_only (fallback if demo testing shows closePosition is ignored):
quantity=<format_decimal(abs(position qty))>
reduceOnly=true
clientAlgoId=<sl_client_id | tp_client_id>
```
Never send `closePosition` together with `quantity` or `reduceOnly` (-4137). Never send `stopPrice` (old name). Response: `algoId, clientAlgoId, algoStatus, triggerPrice, ...` -> `ProtectiveOrder(exchange_id=str(algoId), status=algoStatus)`.

`priceProtect`: default **false**. With `true`, Binance blocks the trigger while |mark − last| / mark exceeds the symbol's `triggerProtect` (BTCUSDT 5 %) — exactly during the dislocations in which the stop is needed. `workingType=MARK_PRICE` already protects against last-price wicks. The option is kept; README explains the risk of `true`.

**Placement procedure** (`_place_protective(symbol, kind, trigger, quantity, client_algo_id) -> ProtectiveOrder | None`; None = "failed"). OK statuses = {"NEW", "TRIGGERING", "TRIGGERED", "FINISHED"}.
1. `POST /fapi/v1/algoOrder` as above.
   - Response with `clientAlgoId == client_algo_id` and `algoStatus` in OK statuses -> **success** (no list read; a `GET openAlgoOrders` immediately after placement can lag the separate Algo Service and must NOT be used to declare failure). Response without `algoStatus` -> resolve (step 2).
   - `ImmediateTriggerError` (-2021/-4142: price already beyond the level) -> failed, no retry.
   - `AlgoLimitError` (-4045) -> `_cancel_own_orders`-style cleanup of own **algo orders not belonging to the live position** (for the current position keep the newest own SL/TP) -> retry the POST once.
   - Non-definitive (`UnknownOrderStatusError`, `TransientError`, `DuplicateClientIdError`, `RateLimitError` after `sleep(min(retry_after or 1, 10))`) -> resolve (step 2).
   - Any other `OrderRejectedError`/`ExchangeError` -> failed.
2. Resolve: `a = _lookup_algo(symbol, client_algo_id)` (3 attempts, 0.5 s apart).
   - found with `algoStatus` in OK statuses -> success (TRIGGERED/FINISHED mean the stop already fired; return it — the next sync/protection check records the closure).
   - not found (or CANCELED/REJECTED/EXPIRED) -> **retry the POST once** with the same `client_algo_id`; a retry that is itself non-definitive is resolved once more with `_lookup_algo`; still not confirmed -> failed.
3. Every failure is logged at ERROR with the code; SL failure leads to flatten (§9.4 open_position step 8).

Related endpoints: query `GET /fapi/v1/algoOrder (algoId|clientAlgoId)`, history `GET /fapi/v1/allAlgoOrders symbol, startTime`, cancel `DELETE /fapi/v1/algoOrder (algoId|clientAlgoId)`, list `GET /fapi/v1/openAlgoOrders symbol`. `DELETE /fapi/v1/allOpenOrders` does NOT cancel algo orders and `DELETE /fapi/v1/algoOpenOrders` cancels foreign ones too — the bot uses neither; it cancels own orders one by one (`_cancel_own_orders`). Account-wide limit is 200 open conditional orders.

Trigger semantics: STOP_MARKET SELL triggers when price ≤ trigger (long SL); STOP_MARKET BUY when ≥ (short SL); TAKE_PROFIT_MARKET SELL when ≥ (long TP); BUY when ≤ (short TP).

---

## 10. Trader loop — `bot/trader.py` (U6)

### 10.1 Public API
```python
@dataclass(slots=True)
class IterationReport:
    server_time: int
    bar_open_time: int | None           # last closed bar processed (None if data stale)
    signal: SignalAction | None
    action: Action
    executed: bool
    skipped_reason: str | None          # "already_processed", "stale_data", "entries_blocked:<why>", "risk:<code>", ...
    closure: PositionClosure | None
    errors: list[str]

class SingleInstanceLock:               # context manager; ONE global lock file cfg.resolve_path("data/trader.lock")
    def __init__(self, path: Path) -> None
    def __enter__(self) -> SingleInstanceLock
    def __exit__(self, *exc) -> None
```
`SingleInstanceLock` implementation (exact; only one trader process of **any** mode may run, §5):
```python
# module level: conditional imports only
if os.name == "nt": import msvcrt
else: import fcntl
__enter__: path.parent.mkdir(parents=True, exist_ok=True)
           fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)      # never "w" (truncates / PermissionError), never append/text mode
           os.lseek(fd, 0, os.SEEK_SET)                           # msvcrt locks start at the CURRENT position
           nt:    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   except OSError -> os.close(fd); raise BotError("another trader instance is running (<path>)")
           posix: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   except OSError -> same BotError
           never write anything to the lock file
__exit__:  nt: os.lseek(fd, 0, os.SEEK_SET); msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)   posix: fcntl.flock(fd, fcntl.LOCK_UN)
           os.close(fd)   (do not delete the file). NEVER use os.kill(pid, 0) on Windows (it terminates the process).
```

```python
class Trader:
    def __init__(self, cfg: AppConfig, *, broker: Broker, market: MarketData, strategy: Strategy, storage: Storage,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None
    stopped_before_start: bool                             # True if a stop was requested before startup() finished
    def startup(self) -> None
    def run_once(self) -> IterationReport
    def protection_check(self) -> None                     # between-bar check, testnet/live only (§10.4)
    def run_forever(self, *, max_iterations: int | None = None) -> None
    def request_stop(self) -> None                         # sets a flag; checked between iterations and every <= 1 s while waiting

def build_trader(cfg: AppConfig, storage: Storage, *, environ: Mapping[str, str] | None = None) -> Trader
    # Wires mode-specific components:
    #  paper:   client = BinanceRestClient(MAINNET_REST_URL)  (NO credentials) ; broker = PaperBroker(...)
    #  testnet: creds = load_credentials(cfg, environ=environ); client = BinanceRestClient(TESTNET_REST_URL, key, secret, recv_window_ms=...)
    #           broker = ExchangeBroker(mode=TESTNET, ...)
    #  live:    assert_live_allowed(cfg, environ=environ); creds; client on MAINNET_REST_URL; ExchangeBroker(mode=LIVE, ...)
    # market = MarketData(client). strategy: load_strategy_modules(cfg.strategy.extra_modules) then create_strategy.
    # The caller owns `storage` (opened with `with Storage(cfg.db_path) as st:`) and closes it.
```

Trader-internal names used below (normative): `self.active: ActiveTrade | None`, `self.kill`, `self.cooldown`, `self.last_bar`, `self.halted: dict | None`, `self.emergency: dict | None`, `self.effective_limit`, `self.filters`.
`_effective_risk() -> RiskConfig`: `mn = broker.max_notional(symbol)`; if `mn` and `mn < cfg.risk.max_position_notional`: `dataclasses.replace(cfg.risk, max_position_notional=mn)` (WARNING logged once per distinct value) else `cfg.risk`. Called before every `plan_entry`.
`_ref_price(forming, entry_bar_time) -> float`: `forming.open` if `forming is not None and forming.open_time == entry_bar_time` else `market.mark_price(symbol)`; computed lazily, at most once per iteration.
`_active_from_entry(plan, outcome, *, entry_bar_time, entry_client_id) -> ActiveTrade`: `trade_id=f"{mode}-{symbol}-{entry_bar_time}-{'L' if LONG else 'S'}"`, `qty=outcome.qty`, `entry_price=outcome.avg_price`, `entry_time=outcome.entry_time`, `entry_bar_open_time=entry_bar_time`, `stop_price=float(plan.stop_price)`, `take_profit_price=float(plan.take_profit_price) if not None`, `liquidation_price=plan.liquidation_price`, `leverage=plan.leverage`, `risk_amount=plan.risk_amount * outcome.qty / float(plan.qty)`, `entry_fee=outcome.entry_fee`, `entry_client_id`, `protect_seq=1`, `entry_order_id=outcome.entry_order.exchange_id if outcome.entry_order else None`.
`_record_closure(closure)`: `trade = Trade.from_closure(self.active, closure, source=mode)`; `storage.insert_trade`; event (INFO; WARNING for LIQUIDATION/UNKNOWN); `self.active = None`; `storage.delete_state(active_trade key)`; if `closure.reason in {STOP_LOSS, LIQUIDATION}`: `cooldown.trigger(floor_time(closure.exit_time, interval_ms))`; persist cooldown.

**`_handle_sync(result, atr_value)`** — the ONE handler for every `SyncResult` (startup reconcile, step 5, the FLIP re-read, the post-execution re-read of step 14, and `protection_check`); a closure returned by any of these syncs is always recorded, never ignored:
1. `result.closure` and `self.active` -> `_record_closure(result.closure)`.
2. "UNTRACKED_POSITION" -> adopt: `pos = result.account.position`; `stop = compute_stop_price(pos.entry_price, dir, cfg.risk.stop_loss, atr_value)` (fallback: percent mode with `cfg.risk.stop_loss.percent` if that returns None, e.g. no candles), rounded with `round_protective_price(..., entry=pos.entry_price)`; TP via `compute_take_profit`; `ActiveTrade(trade_id=f"{mode}-{symbol}-adopted-{server_now}", qty=abs(pos.qty), entry_price=pos.entry_price, entry_time=pos.updated_at, entry_bar_open_time=floor_time(pos.updated_at, interval_ms), stop_price, take_profit_price, liquidation_price=pos.liquidation_price, leverage=pos.leverage or cfg.risk.leverage, risk_amount=abs(pos.qty)*abs(entry-stop), entry_fee=0.0, entry_client_id="adopted", protect_seq=0, entry_order_id=None)`; save; event `ADOPTED_POSITION` (WARNING).
3. "QTY_MISMATCH" -> `self.active.qty = abs(pos.qty)`; save; event WARNING.
4. A position exists, `self.active` exists and ("SL_MISSING" or "PROTECTION_QTY_MISMATCH" in issues) -> `seq = self.active.protect_seq + 1`; `sl_id = make_client_id(bot_id, symbol, "SL", active.entry_bar_open_time, seq)`; `tp_id = make_client_id(bot_id, symbol, "TP", active.entry_bar_open_time, seq) if active.take_profit_price is not None else None`; `placed = broker.ensure_protection(active, result.account, sl_client_id=sl_id, tp_client_id=tp_id)`; if any returned order has `client_id in {sl_id, tp_id}`: `active.protect_seq = seq`; save. `ProtectionFailedError e` -> if `e.closure`: `_record_closure(e.closure)` (reason PROTECTION_FAILED); set `halted = {"reason": "protection_failed", "ts": now}`; event CRITICAL; state HALTED. `EmergencyError` propagates (§10.5).

### 10.2 Startup (`startup()`)
1. Write status `STARTING`. If mode is LIVE: log the banner below at CRITICAL (English + Korean), then count down 10 s as 10 × `sleep(1)`, checking the stop flag after each second; stop requested -> `stopped_before_start = True`, return immediately (nothing was sent to the exchange).
   ```
   ###############################################################
   #  LIVE TRADING ON BINANCE MAINNET - REAL MONEY AT RISK        #
   #  실거래 모드: 실제 자금이 사용됩니다                          #
   #  symbol=<S> interval=<I> leverage=<L> risk=<R>%/trade         #
   #  Press Ctrl+C within 10 seconds to abort / 10초 안에 Ctrl+C   #
   ###############################################################
   ```
2. `effective_limit = max(cfg.execution.kline_limit, 2 * strategy.warmup_bars, strategy.warmup_bars + 2, 3 * cfg.risk.stop_loss.atr_period if stop mode is atr else 0)`; if > 1500 -> ConfigError("strategy warmup too long for 1500 klines"). If it exceeds `kline_limit`, log INFO. (Live re-seeds EMA/ATR at the start of every window; 2× warmup keeps the seed residual below e^-12 so live and backtest agree on the same bar.)
3. `self.filters = broker.prepare_symbol(symbol, cfg.risk.leverage)`.
4. Load persisted state: active trade, last_bar, kill switch, cooldown, emergency (keys §5.2). Delete `halted:*` for this mode/symbol (a restart clears a halt).
5. Reconcile: `closed, forming, server_now = market.recent_klines(symbol, interval, effective_limit)`; `result = broker.sync(symbol, self.active, candles_from_df(closed))`; `_handle_sync(result, atr_last)` (`atr_last` computed exactly as in §10.3 step 4).
6. `self.kill.seed(result.account.equity)` (no-op if a persisted `last_equity` exists); persist.
7. Write status `RUNNING` (or `KILL_SWITCH` if tripped, `ERROR` if an emergency state was loaded — `run_forever` then starts with the emergency flatten, §10.5).

### 10.3 One iteration (`run_once()`), exact order
1. `closed, forming, server_now = market.recent_klines(symbol, interval, effective_limit)`. (This also re-syncs the clock.)
2. Staleness: `expected = expected_last_closed_open(server_now, interval_ms)`. If `closed` is empty or `int(closed.open_time.iloc[-1]) < expected`: retry up to 3 times with `sleep(2)`. Still stale -> log event `STALE_DATA` (WARNING); steps 3–6 still run with the data we have, steps 8–12 are **skipped** (`skipped_reason="stale_data"`). Also stale if `len(closed) < strategy.warmup_bars + 1`.
3. If `closed` is non-empty: `storage.upsert_candles(symbol, interval, closed.tail(500))`.
4. Iteration constants (defined BEFORE any order can be sent):
   - `bar_for_ids = int(closed.open_time.iloc[-1]) if not closed.empty else expected` (native int, §0.2); `bar = bar_for_ids`.
   - `entry_bar_time = bar_for_ids + interval_ms`.
   - `ref = _ref_price(forming, entry_bar_time)` (lazy).
   - `atr_last = float(indicators.atr(closed, cfg.risk.stop_loss.atr_period).iloc[-1])` when stop mode is atr and `closed` is non-empty; NaN or unavailable -> None.
5. `result = broker.sync(symbol, self.active, candles_from_df(closed))`; `_handle_sync(result, atr_last)`; `account = result.account` (re-read with `broker.sync(symbol, self.active, [])` if `_handle_sync` placed or closed anything).
6. Kill switch: `newly = kill.update(bar_for_ids + interval_ms - 1, account.equity)` (the processed bar's CLOSE time — identical to the engine's `t_close`); persist. If `newly`: event `KILL_SWITCH` (CRITICAL). Then, on **every** iteration (not only when newly tripped): if `kill.tripped and cfg.risk.kill_switch_flatten and account.position is not None`: `closure = broker.close_position(symbol, self.active, reason=KILL_SWITCH, client_id=make_client_id(bot_id, symbol, "KS", bar_for_ids), ref_price=ref, bar_time=entry_bar_time)` -> `_record_closure(closure)` if closure. (Idempotent: the id is per bar and `close_position` returns None when flat; a failed close is retried next iteration; a restart on a tripped day still flattens — matching the engine, which flattens whenever tripped.)
7. If stale -> go to step 13.
8. If `bar == self.last_bar` -> `skipped_reason="already_processed"`; go to step 13.
9. `signal = strategy.generate(closed)`; entries gate: `entries_allowed = kill.entries_allowed and not cooldown.active(bar) and not cfg.halt_path.exists() and not self.halted and not self.emergency`; blocked reason string for status (`"kill_switch" | "cooldown" | "halt_file" | "halted:<reason>"`).
10. `action = decide_action(signal.action, position_direction, entries_allowed)`; `storage.record_signal(...)`.
11. Execute:
    - CLOSE / FLIP_*: `closure = broker.close_position(symbol, self.active, reason=SIGNAL (CLOSE) or FLIP (FLIP_*), client_id=make_client_id(bot_id, symbol, "EX", bar), ref_price=ref, bar_time=entry_bar_time)` -> `_record_closure(closure)` if closure. For FLIP: `post = broker.sync(symbol, None, [])`; `_handle_sync(post, atr_last)`; **require flat** (`post.account.position is None`), else abort the open leg with event `FLIP_ABORTED`; `account = post.account` — the open leg is sized from THIS post-close account (paper: cash after the exit's slippage and fee), exactly like the engine (§11.1 step 2).
    - OPEN_* / FLIP_* (open leg): `decision = plan_entry(direction, ref_price=ref, equity=account.equity, atr_value=atr_last, filters=self.filters, risk=_effective_risk(), fees=cfg.execution.fees, slippage_bps=cfg.execution.slippage_bps)`. Rejected -> `skipped_reason=f"risk:{code}"`, event INFO. Else `en_id = make_client_id(bot_id, symbol, "EN", bar)`; `outcome = broker.open_position(plan, entry_client_id=en_id, sl_client_id=make_client_id(bot_id, symbol, "SL", entry_bar_time, 1), tp_client_id=(make_client_id(bot_id, symbol, "TP", entry_bar_time, 1) if plan.take_profit_price else None), ref_price=ref, bar_time=entry_bar_time)`. If `outcome.filled`: `self.active = _active_from_entry(plan, outcome, entry_bar_time=entry_bar_time, entry_client_id=en_id)`; save immediately; `storage.upsert_order` for the entry order (if any). Not filled -> event INFO with `outcome.message`.
    - `ProtectionFailedError e` (from open_position): if `e.closure` and `e.entry` and `e.entry.filled`: `self.active = _active_from_entry(plan, e.entry, ...)`, then `_record_closure(e.closure)` (reason PROTECTION_FAILED); if `e.closure` is None: nothing to record (position already flat), log WARNING. Then `halted = {"reason": "protection_failed", "ts": now}` persisted, event CRITICAL, state HALTED.
    - `EmergencyError e`: if `e.entry` and `e.entry.filled`: `self.active = _active_from_entry(plan, e.entry, ...)` and save (so the emergency flatten records a proper trade); `self.emergency = {"attempt": 0, "bar": entry_bar_time, "since": now_ms(clock)}` persisted; event CRITICAL; state ERROR; re-raise (handled by `run_forever`, §10.5).
12. `self.last_bar = bar`; persist (**even if execution failed** — a missed signal is safer than repeated attempts).
13. Build the `IterationReport`.
14. If anything executed or any closure was recorded this iteration: `post = broker.sync(symbol, self.active, [])`; `_handle_sync(post, atr_last)`; `account = post.account`. Then `storage.append_equity(mode, bar_for_ids, account.equity, account.wallet_balance)` and `storage.upsert_status(BotStatus(updated_at=now_ms(clock), ... state, last_signal, entries_blocked_reason ...))` (status timestamps are LOCAL, §0.2).

### 10.4 Timing (`run_forever`) and stopping
```
install handlers (only if threading.current_thread() is threading.main_thread()):
    old_int = signal.signal(SIGINT, lambda *_: self.request_stop())
    if hasattr(signal, "SIGBREAK"): old_brk = signal.signal(SIGBREAK, lambda *_: self.request_stop())
try:
    startup(); if stopped_before_start: return
    iterations = 0
    while not stop:
        if self.emergency: _emergency_step()          # §10.5; waits 10 s (in 1 s chunks) between attempts
        else: report = run_once()                      # exceptions handled per §10.5
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations: break
        now = market.client.server_time_ms()
        wake = next_close_ms(now, interval_ms) + int(candle_close_delay_sec * 1000)
        while now < wake and not stop:
            sleep(min(1.0, (wake - now) / 1000))       # <= 1 s chunks through the injected sleep: a stop is honoured within 1 s
            every heartbeat_sec (local clock): storage.touch_heartbeat(now_ms(clock))
            testnet/live only, every min(heartbeat_sec, 60) s: protection_check()   (§10.5 policy; never fatal except the fatal row)
            now = market.client.server_time_ms()
finally:
    status STOPPED with message "stopped by user (positions and exchange stop orders are kept)" (user stop) or
           "single run finished (--once)" (max_iterations reached); restore the old signal handlers
```
- The stop flag is only ever checked **between** iterations, between 1 s sleep chunks and in the live countdown; it never interrupts an order sequence (a Ctrl+C between the entry fill and the SL POST would otherwise leave an unprotected position). Test: `request_stop()` called from the fake sleep during an iteration still lets `run_once` finish `open_position` including the SL.
- `KeyboardInterrupt` can still arrive when handlers could not be installed (non-main thread); it is caught in `run_forever` and treated as a user stop.
- `protection_check()`: return immediately in paper mode (paper stops are simulated per candle). Otherwise `result = broker.sync(symbol, self.active, [])` (≈12 request weight: account 5, positionRisk 5, openAlgoOrders 1, openOrders 1) and `_handle_sync(result, atr_value=None)`: a position without a live own SL (NEW/TRIGGERING) is re-protected, a position that went flat is recorded as closed, all within one check interval instead of one candle. Upsert the status if anything changed.
- `trade --once` = `run_forever(max_iterations=1)`: startup, one `run_once()`, final status, no waiting. CLI exit code 0; 130 if `stopped_before_start` (live countdown aborted).

### 10.5 Exception policy inside the loop
| Exception | Behaviour |
|---|---|
| `ConfigError`, `AuthError`, `IpBannedError` | status ERROR, event CRITICAL, stop the loop, re-raise (CLI exit code 2 for ConfigError, 1 otherwise) |
| `RateLimitError` | event WARNING, sleep `retry_after` (in ≤1 s chunks, honouring stop), continue |
| `TransientError`, `TimestampError`, `StaleDataError`, `requests` network errors | event WARNING, skip iteration, continue |
| `ProtectionFailedError` | handled in step 11 / `_handle_sync` (halt entries, keep looping) |
| `EmergencyError` | CRITICAL; `self.emergency` persisted (§10.3 step 11; if it reaches `run_forever` from any other path — `_handle_sync`/`ensure_protection`, `close_position` — and `self.emergency` is None, set `{"attempt": 0, "bar": <bar_for_ids of that iteration>, "since": now_ms(clock)}`; `close_position` additionally guarantees unused ids via `_fresh_client_id`). `_emergency_step()`: `attempt += 1` (persisted in `emergency:{mode}:{symbol}`); `closure = broker.close_position(symbol, self.active, reason=PROTECTION_FAILED, client_id=make_client_id(bot_id, symbol, "FL", emergency["bar"], attempt), ref_price=<paper: _ref_price(...) of a fresh recent_klines, else None>, bar_time=<paper: current forming open_time>)`; success (closure or None) -> `_record_closure` if closure, delete the emergency state, `halted = {"reason": "emergency_flatten"}`, state HALTED; failure -> CRITICAL, retry after 10 s. Every attempt uses a new id (attempt counter), so lookups never hit an earlier FL order. |
| any other `Exception` | log with traceback, `consecutive_errors += 1`; at 5 -> set `halted` ("repeated_errors"), state ERROR but keep looping (sync + protection continue); reset counter after a clean iteration |

### 10.6 Safety invariants (tests assert these)
- Never act on a forming candle; never act twice on the same bar (`last_bar`).
- Never open when not flat; flips are close -> verify flat -> open.
- Never hold a position without an exchange SL (testnet/live): open without SL -> immediate flatten; `filled=False` only after the broker confirmed the position is flat; between candles the SL is re-checked at least every `min(heartbeat_sec, 60)` s.
- Kill switch, cooldown, halt file, and `halted` state block entries only; exits and protection always run. A tripped kill switch flattens (if configured) on every iteration while a position exists.
- Stale data -> no new signals/orders.
- Paper mode can never sign (no credentials in the client).
- A stop request never interrupts an order sequence.

---

## 11. Backtest (U4)

### 11.1 `bot/backtest/engine.py`
```python
def new_run_id(clock: Callable[[], float] = time.time) -> str      # "bt-YYYYMMDD-HHMMSS-<6 hex>" (UTC, secrets.token_hex(3))

def run_backtest(candles: pd.DataFrame, strategy: Strategy, *, symbol: str, interval: str, filters: SymbolFilters,
                 risk: RiskConfig, execution: ExecutionConfig, initial_balance: float,
                 funding: pd.DataFrame | None = None, trade_start_ms: int | None = None,
                 run_id: str | None = None, config_snapshot: dict | None = None) -> BacktestResult
```
Setup:
- `validate_candles_df(candles, interval_ms)`; `prepared = strategy.prepare(candles)`; `atr = indicators.atr(candles, risk.stop_loss.atr_period)` if stop mode is atr.
- `fm = FillModel.from_config(execution)`; `kill = DailyLossKillSwitch(risk.max_daily_loss_pct)`; `kill.seed(initial_balance)`; `cool = Cooldown(risk.cooldown_bars_after_stop, interval_ms)`.
- `funding` None or empty -> no funding is charged (the CLI decides whether that is acceptable, §12.1); `metrics["funding_events"]` counts the events actually charged.
- `s = max(strategy.warmup_bars - 1, (risk.stop_loss.atr_period if atr mode else 0), first index with open_time >= trade_start_ms (0 if None))`. If `s >= n - 1` -> `DataError("not enough candles")`.
- `cash = initial_balance`; `pos = None` (an `ActiveTrade`); `pending = None` (`Action` + reason); funding events sorted, pointer `fp` skips events with `funding_time <= open_time[s]`.

Per bar `i` from `s` to `n-1` (`o,h,l,c = row i`, `t_open`, `t_close`):
1. **Funding to bar open**: `apply_funding_until(t_open)` — for each unprocessed event `ft <= t_open`: if `pos` and `pos.entry_time < ft`: `pay = funding_payment(sign*qty, mark_price if not NaN else o, rate)`; `cash -= pay`; `pos_funding += pay`. Advance pointer regardless.
2. **Pending at open** (decided at close of bar i-1):
   - CLOSE/FLIP_*/KILL: exit at `fm.market_fill_price(o, closing_side)`, fee taker, reason SIGNAL/FLIP/KILL_SWITCH, `exit_time = t_open`; `cash += gross - fee`; append `Trade.from_closure`.
   - OPEN_*/FLIP_* open leg: `decision = plan_entry(direction, ref_price=o, equity=cash, atr_value=atr[i-1], ...)` (for a FLIP, `cash` is already the cash AFTER the close leg at this same open — the trader sizes from the post-close account the same way). If ok: `fill = fm.market_fill_price(o, opening_side)`, `qty=float(plan.qty)`, `fee = fm.fee(qty, fill)`, `cash -= fee`, `pos = ActiveTrade(trade_id=f"{run_id}-{k:05d}", entry_price=fill, entry_time=t_open, entry_bar_open_time=t_open, stop/tp/liq from plan, risk_amount=plan.risk_amount, entry_fee=fee, entry_client_id="bt", leverage=risk.leverage)`; if `plan.sizing_cap == "notional"`: `metrics["entries_capped_by_notional"] += 1`. Rejected -> count in `metrics["rejected_entries"]`.
3. **Intrabar exits** (if `pos`): `hit = resolve_intrabar_exit(dir, o, h, l, stop, tp, liq)`. If hit: `apply_funding_until(t_close)`; exit per §9.1 with `exit_time = t_close` (LIQUIDATION: `gross = liquidation_loss(qty, entry_price, leverage, funding_paid=pos_funding)`); append trade; if reason in {STOP_LOSS, LIQUIDATION}: `cool.trigger(t_open)`; `pos = None`.
4. **Last bar**: if `i == n-1` and `pos`: exit at `fm.exit_fill_price(c, closing_side)` with taker fee, reason END_OF_DATA, `exit_time = t_close`.
5. **Mark-to-market**: `equity_i = cash + (sign*qty*(c - entry_price) if pos else 0)`; record `(t_open, equity_i, in_position_i, position_qty_i)` where `in_position_i` = a position existed at any time during bar i.
6. **Kill switch**: `newly = kill.update(t_close, equity_i)`; if `kill.tripped` (newly or earlier the same day) and `risk.kill_switch_flatten` and `pos`: `pending = CLOSE (reason KILL_SWITCH)` and skip step 7.
7. **Signal** (if `i < n-1`): `sig = strategy.signal_at(prepared, i)`; `entries_allowed = kill.entries_allowed and not cool.active(t_open)`; `pending = decide_action(sig.action, pos_direction, entries_allowed)` (NONE -> None).
Result: `metrics = compute_metrics(equity_df, trades, initial_balance=..., interval=...)` plus `metrics["rejected_entries"]`, `metrics["entries_capped_by_notional"]`, `metrics["funding_events"]` (all int); `start_time = open_time[s]`, `end_time = close_time[n-1]`.

Look-ahead guarantees (tested): decisions at bar i only use rows ≤ i; fills use row i+1's open; intrabar exits use the bar's own OHLC after entry at its open.

### 11.2 `bot/backtest/metrics.py`
```python
def compute_metrics(equity: pd.DataFrame, trades: Sequence[Trade], *, initial_balance: float, interval: str) -> dict[str, float | int | None]
```
Let `E0 = initial_balance`, `E = equity["equity"]` (length N), `r_t = E_t / E_{t-1} - 1` with `E_{-1} = E0` (N returns, flat bars included), `P = bars_per_year(interval)`.
| Key | Formula |
|---|---|
| initial_balance | E0 |
| final_equity | E[-1] |
| total_return | E[-1]/E0 - 1 |
| cagr | days = (end_time - start_time)/DAY_MS where start_time = equity.time[0], end_time = equity.time[-1] + interval_ms; `(E[-1]/E0)^(365.25/days) - 1` if days > 0 and E[-1] > 0; `-1.0` if E[-1] ≤ 0; None if days ≤ 0 |
| max_drawdown | `max(1 - E_t / running_max)` with running_max including E0; ≥ 0 |
| max_drawdown_duration_bars | longest run of consecutive bars with E_t < running_max |
| sharpe | `mean(r)/std(r, ddof=1) * sqrt(P)`; None if N < 2 or std == 0 |
| sharpe_daily | resample to UTC days (last equity per day; prepend E0), daily returns, `mean/std(ddof=1)*sqrt(365)`; None if < 2 days or std 0 |
| sortino | `mean(r) / sqrt(mean(min(r,0)^2)) * sqrt(P)`; None if downside dev 0 |
| n_trades, n_wins, n_losses | counts; win = net_pnl > 0; loss = net_pnl ≤ 0 |
| win_rate | n_wins/n_trades; None if 0 trades |
| profit_factor | sum(net>0)/abs(sum(net≤0 and net<0)); None if no losing trades |
| expectancy | mean(net_pnl); None if 0 trades |
| expectancy_r | mean(r_multiple of trades with r not None); None if none |
| avg_win / avg_loss | mean net of wins / of losses (None if none) |
| best_trade / worst_trade | max/min net_pnl (None if none) |
| avg_holding_hours | mean((exit_time-entry_time)/3.6e6) |
| exposure | sum(in_position)/N |
| total_fees / total_funding | sums over trades |
| n_liquidations / n_stop_losses / n_take_profits | counts by exit_reason |
| max_consecutive_losses | longest run of losses in exit_time order |
| bars | N |
All values plain Python `float`/`int`/`None` (no numpy scalars, no NaN/inf).

### 11.3 `bot/backtest/report.py`
```python
from bot.models import METRIC_LABELS_KO, PERCENT_METRICS   # re-exported (single source in models, §4.1)

def save_backtest_result(result: BacktestResult, results_dir: Path, storage: Storage | None = None) -> Path
    # dir = results_dir / run_id (created). Files (utf-8):
    #   result.json : {run_id, created_at, symbol, interval, strategy, params, config, start_time, end_time,
    #                  initial_balance, metrics} (json.dumps(to_jsonable(...), ensure_ascii=False, indent=2, allow_nan=False))
    #   trades.csv  : header TRADE_COLUMNS
    #   equity.csv  : time,time_iso,equity,in_position,position_qty
    # every file is written with bot.fsutil (tmp + atomic_replace).
    # sets result.result_dir = str(dir); if storage: storage.save_backtest(result). Returns dir.
def format_metrics_table(metrics: Mapping[str, Any]) -> str
    # one line per METRIC_LABELS_KO key present: "<label>: <value>" ; percent metrics as "12.34%"; floats 2-4 decimals; None -> "-"
```

---

## 12. CLI, logging, dashboard

### 12.1 `bot/cli.py` and `bot/__main__.py` (U6)
`bot/__main__.py`: `from bot.cli import main; raise SystemExit(main())`.

```python
def build_parser() -> argparse.ArgumentParser
def main(argv: Sequence[str] | None = None) -> int
```
Global options: `-c/--config PATH` (default `config.yaml`), `--log-level {DEBUG,INFO,WARNING,ERROR}` (overrides config).

| Command | Flags | Behaviour |
|---|---|---|
| `download` | `--symbol`, `--interval`, `--start DATE` (default cfg.backtest.start), `--end DATE`, `--no-funding` | mainnet public client; `download_klines`, `download_funding` (unless `--no-funding`), `load_or_fetch_filters`. Prints row counts + cache paths (Korean). |
| `backtest` | `--symbol`, `--interval`, `--start`, `--end`, `--strategy NAME`, `--param KEY=VALUE` (repeatable; value parsed with `yaml.safe_load`), `--initial-balance`, `--no-funding`, `--offline`, `--no-save` | See flow below. Prints `format_metrics_table` and the result directory. |
| `trade` | `--once`, `--mode {paper,testnet}`, `--reset-paper` | `with_overrides(mode=...)`; `with SingleInstanceLock(cfg.resolve_path("data/trader.lock")):` (global: one trader of any mode) `with Storage(cfg.db_path) as st:` `build_trader(cfg, st)`; `--reset-paper` (paper only) deletes `paper_state:<symbol>`, `active_trade:paper:<symbol>`, `last_bar:paper:...`, kill/cooldown/halted/emergency keys for paper, then continues. `--once` -> `trader.run_forever(max_iterations=1)`; else `trader.run_forever()`. Exit 0 (also after a user stop), 130 if `trader.stopped_before_start`. |
| `dashboard` | `--host` (default cfg.dashboard.host), `--port` | host must be in `LOOPBACK_HOSTS` (else exit 2). `run_dashboard(cfg, host, port)`. |
| `strategies` | – | prints `available_strategies()` with default params. |

Backtest flow:
1. Load cfg + overrides -> `sys.path.insert(0, str(cfg.base_dir))`, `load_strategy_modules`, `create_strategy` -> `start_ms`, `end_ms` (default `server/local now`) -> `warmup_ms = (strategy.warmup_bars + atr_period + 5) * interval_ms`; `data_start = start_ms - warmup_ms`.
2. `use_funding = cfg.backtest.include_funding and not args.no_funding`.
3. Unless `--offline`: mainnet `MarketData`, `download_klines(data_start, end)`, `download_funding(data_start, end)` if `use_funding`, `load_or_fetch_filters(market, ...)`. `--offline`: `load_or_fetch_filters(None, ...)`.
4. `df = load_klines(cache, symbol, interval, data_start, end_ms)` (empty -> DataError).
5. Funding (online AND `--offline`): if `use_funding`: `funding = load_funding(cache, symbol, data_start, end_ms)`. Coverage check against the candles: `funding_interval` = median diff of `funding_time` (default 8 h); problem if the frame is empty, or `funding.funding_time.iloc[0] > start_ms + funding_interval`, or `funding.funding_time.iloc[-1] < df.close_time.iloc[-1] - funding_interval`. Problem + `--offline` + empty frame -> `DataError("no cached funding for <symbol>; run: python -m bot download ... or use --no-funding")`; any other problem -> WARNING (Korean + English: "펀딩비 데이터가 기간 전체를 덮지 않습니다") and continue. `use_funding` false -> `funding = None`.
6. `config_snapshot = cfg.to_dict() | {"funding_coverage": {"included": use_funding, "rows": len(funding) or 0, "first": first funding_time or None, "last": last funding_time or None}}`.
7. `result = run_backtest(df, strategy, ..., funding=funding, trade_start_ms=start_ms, config_snapshot=config_snapshot)`.
8. Unless `--no-save`: `with Storage(cfg.db_path) as st: save_backtest_result(result, cfg.resolve_path(cfg.backtest.results_dir), st)`.
9. Print `format_metrics_table(result.metrics)` (includes `funding_events` and `entries_capped_by_notional`) and the result directory.

Exit codes: 0 success (and a user stop of `trade`); 1 runtime error (`BotError`, unexpected); 2 `ConfigError` / usage; 3 `LiveTradingNotConfirmed`; 130 KeyboardInterrupt outside the trader, or `trade` aborted before startup finished. Errors print a one-line Korean+English message to stderr (no traceback unless `--log-level DEBUG`).

Console encoding: first thing in `main()`, before any output: `for s in (sys.stdout, sys.stderr): try: s.reconfigure(errors="replace") except Exception: pass` (redirected stdout on this machine is cp949; the Korean metrics table must never raise `UnicodeEncodeError`).

Logging setup per command: `setup_logging(cfg.logging, base_dir=cfg.base_dir, log_name=<command>, secrets=<creds values if any>)` -> file `logs/<command>.log` (separate files so the trader and dashboard processes never rotate the same file on Windows; only one trader runs at a time, §10.1). `main()` calls `logging_setup.shutdown_logging()` in `finally`.

### 12.2 `bot/logging_setup.py` (U1)
```python
def setup_logging(cfg: LoggingConfig, *, base_dir: Path, log_name: str, secrets: Iterable[str] = (),
                  console: bool = True) -> logging.Logger
    # configures the "bot" logger (propagate False): level from cfg; formatter
    #   "%(asctime)s %(levelname)-8s %(name)s: %(message)s" with UTC time (formatter.converter = time.gmtime, datefmt "%Y-%m-%dT%H:%M:%SZ")
    # handlers: StreamHandler(sys.stderr) (if console) and RotatingFileHandler(base_dir/cfg.dir/f"{log_name}.log",
    #   maxBytes, backupCount, encoding="utf-8", delay=True). Both use RedactingFormatter and RedactingFilter.
    #   Idempotent (removes AND closes old handlers).
    # also attaches the same handlers to the "uvicorn", "uvicorn.error", "uvicorn.access" loggers (propagate False) when
    #   log_name == "dashboard" (uvicorn must then be started with log_config=None, §12.3).
def shutdown_logging() -> None
    # removes and closes every handler on "bot", "uvicorn", "uvicorn.error", "uvicorn.access" (idempotent). Used by the CLI
    # in finally and by the autouse test fixture (open handlers block tmp_path cleanup on Windows).
def add_secrets(secrets: Iterable[str]) -> None   # registers values (len >= 6) to redact globally
def clear_secrets() -> None                       # empties the registry (tests)
def redact(text: str) -> str
    # replaces every registered secret with "***"; regex r"(signature=)[0-9a-fA-F]+" -> r"\1***";
    # r"(X-MBX-APIKEY['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9]+" -> r"\1***"
class RedactingFormatter(logging.Formatter):
    def format(self, record) -> str            # return redact(super().format(record))  — covers msg, args AND the traceback
    def formatException(self, ei) -> str       # redact(super().formatException(ei))
    def formatStack(self, stack_info) -> str   # redact(super().formatStack(stack_info))
class RedactingFilter(logging.Filter):
    def filter(self, record) -> bool   # record.msg = redact(record.getMessage()); record.args = None; returns True
                                       # (exc_text is filled later by Formatter.format, so traceback redaction is the formatter's job)
```
The console stream must not crash on cp949: wrap with `errors="replace"` (`sys.stderr.reconfigure(errors="replace")` inside try/except; the CLI also does this for stdout, §12.1).

### 12.3 Dashboard — `bot/dashboard/app.py` (U7)
```python
CDN_LIGHTWEIGHT_CHARTS: Final = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
def create_app(cfg: AppConfig) -> FastAPI
def run_dashboard(cfg: AppConfig, host: str, port: int) -> None
    # host not in LOOPBACK_HOSTS -> ConfigError. Called AFTER setup_logging(..., log_name="dashboard") (done by the CLI).
    # uvicorn.run(create_app(cfg), host=host, port=port, log_config=None, log_level="info", access_log=False)
    # log_config=None is required: the default LOGGING_CONFIG would dictConfig() away our redacting uvicorn handlers.
```
- **Read-only**: only `GET` routes (plus the StaticFiles mount). No trading controls, no POST/PUT/DELETE/PATCH. Each request uses a dependency with `yield`: if `cfg.db_path` does not exist -> yields `None` and every route returns its empty payload (`{"status": null}`, `{"trades": []}`, `{"points": []}`, `{"candles": [], "markers": []}`, `{"events": []}`, `{"runs": []}`, 404 for a backtest detail) — the dashboard never creates the DB or its folder; else `with Storage(cfg.db_path, read_only=True) as st: yield st` (no DDL, no journal_mode change, `query_only`, §5). All `/api/*` responses carry header `Cache-Control: no-store`.
- DNS-rebinding guard: `create_app` adds `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost", "[::1]", "::1", "testserver"])` (`testserver` = Starlette TestClient). A request with `Host: evil.com` gets 400.
- Static: `app.mount("/static", StaticFiles(directory=<package>/dashboard/static), name="static")`; `GET /` -> `FileResponse(index.html)`.
- JSON conventions: chart points use `time` in **UTC seconds** (int) for lightweight-charts; table rows keep ms fields as stored. Floats as JSON numbers; missing -> `null`.

Routes and response shapes:
| Route | Response |
|---|---|
| `GET /api/health` | `{"ok": true, "time": <ms>}` |
| `GET /api/meta` | `{"mode": cfg.mode, "symbol": cfg.symbol, "interval": cfg.interval, "refresh_sec": cfg.dashboard.refresh_sec, "heartbeat_sec": cfg.execution.heartbeat_sec, "version": "0.1.0", "read_only": true, "metric_labels": models.METRIC_LABELS_KO, "percent_metrics": sorted(models.PERCENT_METRICS)}` (app.js uses these; it never hard-codes metric labels) |
| `GET /api/status` | `{"status": null}` when no row, else `{"status": {<get_status() keys>, "heartbeat_age_sec": float, "stale": bool, "position": account.position or null, "protective_orders": account.protective_orders or []}}`; `heartbeat_age_sec = (now_ms() - updated_at) / 1000` with the LOCAL clock (updated_at is local too, §0.2); `stale = heartbeat_age_sec > max(3*heartbeat_sec, 90)` |
| `GET /api/trades?source=&run_id=&limit=200` | `{"trades": [<TRADE_COLUMNS dict>, ...]}`; `source` default = status.mode or cfg.mode; limit 1..2000 |
| `GET /api/equity?mode=&limit=5000` | `{"mode": m, "points": [{"time": <sec>, "equity": float, "wallet": float}, ...]}` ascending |
| `GET /api/candles?limit=300` | `{"symbol", "interval", "candles": [{"time": <sec>, "open","high","low","close"}], "markers": [...]}` — symbol/interval from status (fallback cfg); markers from trades of source = mode whose times fall within the candle range: entry marker `{"time": floor(entry_time to interval)/1000, "position": "belowBar"|"aboveBar", "shape": "arrowUp"|"arrowDown", "color": "#26a69a"|"#ef5350", "text": "롱 진입"|"숏 진입"}`; exit marker `{"time": floor(exit_time)/1000, "position": "aboveBar" (long) / "belowBar" (short), "shape": "circle", "color": "#607d8b", "text": EXIT_REASON_KO[reason]}`. Markers sorted ascending by time (required by lightweight-charts). |
| `GET /api/events?limit=50` | `{"events": [{ts, level, mode, kind, message}, ...]}` newest first |
| `GET /api/backtests?limit=50` | `{"runs": [{run_id, created_at, symbol, interval, strategy, params, start_time, end_time, initial_balance, metrics}, ...]}` |
| `GET /api/backtests/{run_id}` | 404 `{"detail": "backtest not found"}` or `{"run": {... get_backtest keys ...}, "equity": [{"time": <sec>, "equity": float}], "trades": [...]}`. Equity downsampled when > 5000 points: keep every `ceil(n/5000)`-th point plus the last. |

Korean label maps (defined in `app.js`; `EXIT_REASON_KO` also in `app.py` for markers; metric labels come from `/api/meta`):
```
EXIT_REASON_KO = {SIGNAL:"신호 청산", FLIP:"포지션 전환", STOP_LOSS:"손절", TAKE_PROFIT:"익절", LIQUIDATION:"강제청산",
                  KILL_SWITCH:"킬스위치", END_OF_DATA:"백테스트 종료", PROTECTION_FAILED:"보호주문 실패", MANUAL:"수동 청산", UNKNOWN:"알 수 없음"}
STATE_KO = {STARTING:"시작 중", RUNNING:"실행 중", HALTED:"신규 진입 중지", KILL_SWITCH:"킬스위치 발동", ERROR:"오류", STOPPED:"정지됨"}
MODE_KO = {paper:"페이퍼(모의)", testnet:"테스트넷(데모)", live:"실거래"}
SIGNAL_KO = {LONG:"롱", SHORT:"숏", CLOSE:"청산", NONE:"없음"}
DIRECTION_KO = {LONG:"롱", SHORT:"숏"}
```
Static page (`index.html`, `lang="ko"`, `<title>바이낸스 선물 자동매매 대시보드</title>`, loads `CDN_LIGHTWEIGHT_CHARTS`, `/static/style.css`, `/static/app.js`). Vanilla JS, no build step. Sections in order:
1. 헤더: 제목, 모드 배지 (live = red), "읽기 전용" 배지, 마지막 새로고침 시각 (KST).
2. **봇 상태**: 모드, 심볼, 간격, 전략, 상태, 하트비트("N초 전", stale -> red "응답 없음"), 지갑 잔고, 평가 자산, 사용 가능 잔고, 미실현 손익, 마지막 신호(액션·봉 시각·가격), 진입 차단 사유.
3. **포지션**: 방향, 수량, 진입가, 마크가, 미실현 손익, 청산가 — or "포지션 없음".
4. **보호 주문** table: 종류(손절/익절), 주문 방향, 트리거 가격, 상태, 주문 ID.
5. **캔들 차트** (candlestick + markers, height 420).
6. **자산 곡선** (line series).
7. **거래 내역** table: 진입 시각, 청산 시각, 방향, 수량, 진입가, 청산가, 청산 사유, 수수료, 펀딩비, 순손익(+ green / − red), R.
8. **최근 이벤트** list.
9. **백테스트 결과**: runs table (실행 ID, 생성 시각, 심볼, 간격, 전략, 총 수익률, 최대 낙폭, 샤프 지수, 거래 수, 승률); clicking a row loads detail (metrics grid with the METRIC_LABELS_KO labels, equity chart, trades table).
10. 푸터: "본 소프트웨어는 투자 조언이 아니며, 모든 거래의 책임은 사용자에게 있습니다."
Behaviour: fetch `/api/meta` once; every `refresh_sec` refresh status, trades, equity, candles, events; refresh the backtest list every 60 s. Times formatted with `toLocaleString('ko-KR', {timeZone: 'Asia/Seoul'})`; chart time labels via `localization.timeFormatter` in KST. Numbers: prices 2 decimals (or tick-appropriate), percents 2 decimals. If the CDN fails to load, charts show "차트 라이브러리를 불러오지 못했습니다" and tables still work. Handle `fetch` errors by showing "서버 연결 실패" without stopping the refresh timer.

---

## 13. Error-handling and safety policy (summary)

| Failure | Where handled | Behaviour |
|---|---|---|
| Missing/invalid config | `load_config` | ConfigError, exit 2, nothing started |
| Live without `CONFIRM_LIVE_TRADING=YES` (process env) | `assert_live_allowed` | exit 3, no network call |
| Clock skew | REST client | offset from `/fapi/v1/time` applied to every timestamp; resync every 5 min and on -1021/-5028 (retry once); warn if > 1 s |
| Rate limit 429 | REST client / trader | honour Retry-After; POST not retried |
| IP ban 418 | trader | stop loop, exit 1 |
| Transient 5xx / network on GET/DELETE/PUT | REST client | 3 retries 0.2/0.4/0.8 s |
| POST outcome unknown (timeout, 5xx, -1000/-1001/-1007, 429, duplicate id) | ExchangeBroker | look up by client id (orders) / clientAlgoId (algo) before any retry; never blind-resend; `filled=False` only after positionRisk confirms flat |
| Entry filled late / status not final | ExchangeBroker | poll order to a terminal status (5 × 1 s), then position truth check; any position gets an SL immediately |
| Stale / missing candles | trader | no signals/orders this iteration; sync + protection still run |
| Entry rejected (margin, min notional, reduce-only mode) | ExchangeBroker | no position; event logged |
| SL placement fails / would trigger immediately | ExchangeBroker | immediate reduce-only market close; trader halts new entries until restart |
| Close fails and position may be unprotected | trader | EmergencyError: retry flatten every 10 s, CRITICAL logs |
| Position without a live own SL (cancelled/rejected/expired algo, qty too small in reduce_only mode) | trader | detected on each candle AND by the between-bar `protection_check` (≤ `min(heartbeat_sec, 60)` s); place new SL first, then cancel stale ones; failure -> flatten |
| Leverage change refused with an open isolated position (-4161) | ExchangeBroker | keep the exchange leverage for that position; apply the configured leverage when flat |
| Account in Hedge / multi-assets mode | ExchangeBroker | testnet: switch automatically; live: ConfigError with Korean instructions (never change account-wide settings on mainnet) |
| Unknown position (crash/manual) | trader | adopt, attach SL/TP, WARNING event |
| Daily loss limit | trader / engine | kill switch: block entries until next UTC day; flatten if configured |
| Stop-out | trader / engine | cooldown N bars |
| Halt file `data/STOP` exists | trader | block new entries; existing positions keep exchange SL/TP |
| Auth failure | trader | stop loop, exit 1; secrets never logged |
| Ctrl+C / Ctrl+Break | trader | SIGINT/SIGBREAK handler only sets a stop flag; the current order sequence completes; stop honoured within 1 s while waiting; status STOPPED; positions and exchange SL/TP are left in place (documented); exit 0 |

---

## 14. Test plan

### 14.1 `tests/conftest.py` (U1) — shared fixtures (exact names)
- `_block_network` (autouse): unless the test has the `network` marker, monkeypatch `socket.socket.connect`, `socket.socket.connect_ex` and `socket.create_connection` with wrappers that **pass through loopback** and raise `RuntimeError("network disabled in unit tests")` for everything else. Loopback = `address` is a tuple whose `address[0]` is in `{"127.0.0.1", "::1", "localhost"}` (AF_UNIX/other address shapes also pass through). Reason (verified on this machine, CPython 3.14.6 win_amd64): `_socket` has no `socketpair`, so `socket.socketpair` is `_fallback_socketpair`, which connects to 127.0.0.1; asyncio's ProactorEventLoop uses it for its self-pipe and Starlette's `TestClient` starts that loop — a blanket block breaks every FastAPI test.
- `_isolate_env` (autouse): `for k in ("CONFIRM_LIVE_TRADING", "BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"): monkeypatch.delenv(k, raising=False)`. Unit tests that need values pass `environ={...}` explicitly.
- `_reset_logging` (autouse, teardown): `logging_setup.shutdown_logging()` and `logging_setup.clear_secrets()` (open RotatingFileHandlers block tmp_path cleanup on Windows; registry/handler state must not leak between tests).
- `candle_factory` -> function `make(closes: Sequence[float], *, start_ms: int = 1_704_067_200_000, interval: str = "1h", wick: float = 0.001, opens: Sequence[float] | None = None) -> pd.DataFrame`: open = previous close (first open = first close) unless `opens` given; `high = max(o,c)*(1+wick)`, `low = min(o,c)*(1-wick)`, volume 1.0, `close_time = open_time + ms - 1`, quote_volume = close, trades 1, taker_buy_base 0.5, taker_buy_quote 0.5*close. Output passes `validate_candles_df`.
- `ohlc_factory` -> function `make(rows: Sequence[tuple[float,float,float,float]], *, start_ms=..., interval="1h") -> pd.DataFrame` for explicit OHLC bars.
- `btc_filters` -> `SymbolFilters` for mainnet BTCUSDT: tick 0.10, min_price 556.80, max_price 4529764, step 0.001, min_qty 0.001, max_qty 1000, market step 0.001, market min 0.001, market max 120, min_notional 50, up 1.0500, down 0.9500, trigger_protect 0.0500, market_take_bound 0.05, status TRADING, contract PERPETUAL.
- `exchange_info_btc` -> dict loaded from `tests/data/exchange_info_btcusdt.json` (U1 writes: `{"timezone":"UTC","serverTime":1790770782215,"rateLimits":[{"rateLimitType":"REQUEST_WEIGHT","interval":"MINUTE","intervalNum":1,"limit":2400},{"rateLimitType":"ORDERS","interval":"MINUTE","intervalNum":1,"limit":1200},{"rateLimitType":"ORDERS","interval":"SECOND","intervalNum":10,"limit":300}],"exchangeFilters":[],"assets":[],"symbols":[{"symbol":"BTCUSDT","pair":"BTCUSDT","contractType":"PERPETUAL","status":"TRADING","baseAsset":"BTC","quoteAsset":"USDT","marginAsset":"USDT","pricePrecision":2,"quantityPrecision":3,"triggerProtect":"0.0500","liquidationFee":"0.012500","marketTakeBound":"0.05","filters":[<the 7 mainnet filters from research §4.1>],"orderTypes":["LIMIT","MARKET","STOP","STOP_MARKET","TAKE_PROFIT","TAKE_PROFIT_MARKET","TRAILING_STOP_MARKET"],"timeInForce":["GTC","IOC","FOK","GTX","GTD"]}]}`).
- `app_config` -> `load_config(<repo>/config.example.yaml, base_dir=tmp_path)` (so db/cache/logs go to tmp). `config.yaml` is never read by tests.
- `storage` -> `with Storage(tmp_path / "test.db") as st: yield st`.
- `fixed_clock` -> factory `make(start_s: float = 1_790_769_600.0) -> FakeClock` where
  ```python
  class FakeClock:
      now_s: float
      sleeps: list[float]                  # every requested sleep, in order
      def __call__(self) -> float          # returns now_s  (use as clock=c)
      def advance(self, s: float) -> None  # now_s += s
      def sleep(self, s: float) -> None    # sleeps.append(s); advance(s)   (use as sleep=c.sleep)
  ```
  There is no separate `fake_sleep` fixture: tests pass `clock=c, sleep=c.sleep`. `FakeClock` is importable from `tests/conftest.py` for type hints only; units construct it through the fixture.

### 14.2 Per-module cases (names are required; assertions described)
**U1**
- `test_config.py`: `test_example_config_loads_defaults` (mode paper, leverage 3, interval 1h, price_protect False, max_position_notional 20000); `test_defaults_match_example` (`yaml.safe_load(config.example.yaml) == config.DEFAULTS`); `test_unknown_key_rejected`; `test_leverage_above_max_rejected`; `test_max_leverage_above_hard_cap_rejected` (21); `test_bool_not_accepted_as_int`; `test_bad_interval_rejected` (`1w`, `1s`); `test_bad_symbol_rejected` (`BTCUSD`); `test_dashboard_non_loopback_rejected` (`0.0.0.0`); `test_live_requires_env_confirmation` (missing/`yes`/`NO` raise, `YES` passes); `test_confirm_live_not_read_from_dotenv`; `test_credentials_paper_returns_none`; `test_credentials_testnet_missing_raises`; `test_credentials_repr_hides_secret`; `test_with_overrides_rejects_live`; `test_with_overrides_merges_params`; `test_paths_resolved_relative_to_base_dir`; `test_to_dict_has_no_secrets`; `test_to_dict_is_json_serializable` (params MappingProxyType -> dict).
- `test_models.py`: `test_make_client_id_format` (`make_client_id("mab1","BTCUSDT","EN",1_790_769_600_000) == "mab1-4314-EN-1790769600-0"`, ≤36, regex; np.int64 time accepted and rendered without ".0"); `test_make_client_id_rejects_bad_bot_id`; `test_client_ids_differ_per_symbol`; `test_next_client_id_increments_seq`; `test_client_id_prefix`; `test_enum_values_are_explicit_upper` (`Side.BUY == "BUY"`, `Mode.PAPER == "paper"`); `test_direction_helpers`; `test_trade_from_closure_long`; `test_trade_from_closure_short`; `test_trade_from_closure_prefers_exchange_gross`; `test_to_jsonable_handles_enum_decimal_nan_dataclass`; `test_to_jsonable_converts_numpy_scalars` (`np.int64`, `np.float64(nan)` -> None, `np.bool_`); `test_candle_from_row_coerces_numpy` (`df.iloc[i]` of a mixed-dtype frame -> `int` times); `test_signal_coerces_numpy`; `test_active_trade_roundtrip` (incl. `entry_order_id`, and from_dict without it); `test_open_outcome_defaults`; `test_signal_roundtrip`; `test_symbol_filters_roundtrip`; `test_validate_candles_df_rejects_unsorted_and_dupes`; `test_errors_module_has_no_runtime_bot_imports` (parse `bot/errors.py` with `ast`: every `import bot...` / `from bot... import` sits inside an `if TYPE_CHECKING:` block).
- `test_timeutil.py`: interval ms table; `1M` rejected; `test_next_close_and_expected_last_closed`; `test_parse_date_ms_utc`; `test_bars_per_year` (1h 8760, 4h 2190, 1d 365); `test_ms_to_iso_utc`.
- `test_fsutil.py`: `test_atomic_write_text_utf8`; `test_atomic_replace_retries_then_data_error` (monkeypatched `os.replace` raising PermissionError; 5 sleeps of 0.2 recorded; DataError message contains the path).
- `test_storage.py`: `test_schema_and_wal_mode`; `test_status_roundtrip_and_heartbeat`; `test_kv_state_roundtrip`; `test_set_state_accepts_numpy_int`; `test_record_signal_idempotent`; `test_numpy_bar_time_stored_as_integer` (`Signal` with `np.int64` bar time -> `SELECT typeof(bar_open_time)` == 'integer'; a second insert with a native int is ignored by the UNIQUE key); `test_trades_insert_and_filter`; `test_equity_append_and_curve`; `test_candles_upsert_get`; `test_save_and_get_backtest`; `test_read_only_storage_cannot_write` (sqlite3.OperationalError on write); `test_read_only_does_not_create_db` (DataError, file and folder not created); `test_read_only_runs_no_ddl` (`set_trace_callback` records no CREATE/INSERT/journal_mode statements); `test_reader_sees_writer_commits` (two instances); `test_close_is_idempotent_and_no_resource_warning`.
- `test_logging.py`: `test_secret_redacted_in_message_and_args`; `test_signature_redacted`; `test_traceback_redacted` (`logger.exception` of an error whose message contains `signature=abc123` and a registered secret; neither appears in the file); `test_file_handler_utf8_korean` (writes "킬스위치" and reads it back); `test_setup_logging_idempotent`; `test_shutdown_logging_closes_handlers`.
- `test_conftest_network.py`: `test_loopback_allowed_and_testclient_works` (FastAPI `TestClient(app).get("/x")` answers under `_block_network`); `test_external_connect_blocked`.

**U2**
- `test_rest.py`: `test_hmac_official_vector` (payload -> `3c6612...af9`); `test_hmac_wrong_order_differs` (`ec11dc...519`); `test_signed_request_url_matches_official_vector` (responses; URL query + signature exact; header `X-MBX-APIKEY`); `test_encode_params_bool_decimal_none`; `test_no_credentials_raises_before_http` (0 calls); `test_error_mapping` (parametrized over every row of the §6.1 table, including method-dependent rows); `test_get_retries_on_503_service_unavailable` (3 retries, backoff 0.2/0.4/0.8 recorded by `FakeClock.sleeps`); `test_delete_5xx_non_json_is_transient_and_retried`; `test_post_503_service_unavailable_retried` (not_executed); `test_post_unknown_error_not_retried` (1 call, UnknownOrderStatusError); `test_post_read_timeout_is_unknown_status` (no chained `__cause__`/`__context__` shown: `e.__suppress_context__` is True); `test_connect_timeout_retried`; `test_timestamp_error_resyncs_and_retries_once`; `test_429_get_sleeps_retry_after`; `test_418_raises_ip_banned_no_retry`; `test_rate_limit_headers_parsed_case_insensitive`; `test_weight_guard_sleeps_at_80_percent`; `test_sync_time_offset_midpoint`; `test_exception_str_has_no_secret_or_query`.
- `test_filters.py`: `test_parse_symbol_filters_from_exchange_info`; `test_parse_min_notional_key_is_notional`; `test_floor_to_step_edge_cases` (0.0019999->0.001; 0.1+0.2 with step 0.1 -> 0.3; 1.0 -> "1"; 0.0009 -> 0; demo step 0.0001: 0.12345 -> 0.1234); `test_floor_negative_raises`; `test_round_price_modes`; `test_round_protective_toward_entry` (long SL 49000.07 -> 49000.1; short SL 51000.07 -> 51000.0; long TP 52000.07 -> 52000.0); `test_normalize_market_qty_clamps_to_market_max` (200 -> 120); `test_normalize_below_min_returns_zero`; `test_meets_min_notional`; `test_format_decimal_no_exponent` (Decimal("1E+2") -> "100", "0.00010" -> "0.0001").
- `test_market.py`: `test_klines_to_df_dtypes_and_order`; `test_split_closed_drops_forming_candle`; `test_recent_klines_returns_forming`; `test_funding_rates_paginates` (startTime = last+1); `test_exchange_info_updates_weight_limit`; `test_symbol_filters_cached`; `test_mark_price`; `test_premium_index`.
- `test_downloader.py`: `test_download_paginates_with_start_plus_interval`; `test_download_writes_csv_and_reloads_same_dtypes`; `test_incremental_download_fetches_only_new`; `test_open_candle_never_cached`; `test_dedupe_on_merge`; `test_find_gaps`; `test_load_or_fetch_filters_uses_cache_offline`; `test_load_or_fetch_filters_without_cache_raises`; `test_download_funding_incremental`.
- `test_network.py` (all `@pytest.mark.network`, public GET only): server time within 60 s of local; 3 klines BTCUSDT 1h closed filter; exchangeInfo BTCUSDT parses; fundingRate latest rows parse; premiumIndex parses.

**U3**
- `test_indicators.py`: `test_sma_reference`; `test_ema_reference`; `test_atr_reference` (hand-computed 5-bar example); `test_indicators_are_causal` (prefix equality); `test_period_zero_raises`.
- `test_strategy.py`: `test_registry_has_ma_cross`; `test_unknown_strategy_raises_config_error`; `test_bad_params_rejected` (fast ≥ slow, ma_type "WMA", unknown key); `test_golden_cross_long_on_cross_bar_only`; `test_dead_cross_short`; `test_dead_cross_close_when_short_disabled`; `test_warmup_returns_none`; `test_no_lookahead_signal_prefix_equality` (for every i: `signal_at(prepare(df), i).action == signal_at(prepare(df.iloc[:i+1]), i).action`); `test_rolling_window_matches_full_history` (long synthetic series, 3000 bars, both SMA and EMA: for every i ≥ W with `W = max(500, 2*warmup_bars)`: `generate(df.iloc[i-W+1:i+1].reset_index(drop=True)).action == signal_at(prepare(df), i).action`); `test_signal_bar_time_is_native_int`; `test_prepare_does_not_mutate_input`; `test_sma_and_ema_modes`; `test_custom_strategy_registration_and_load_modules` (tmp module on sys.path).
- `test_risk.py`: `test_stop_percent_long_short`; `test_stop_atr`; `test_stop_atr_nan_rejected`; `test_take_profit_r`; `test_plan_entry_reference_example` (§8.5 numbers: per_unit_loss 1099.00025, qty 0.090, risk_amount 98.9100225, sizing_cap "risk"); `test_per_unit_loss_short_mirror`; `test_qty_floored_to_step`; `test_notional_cap_binds` (sizing_cap "notional"); `test_margin_cap_binds` (sizing_cap "margin"); `test_below_min_notional_rejected_not_sized_up`; `test_liquidation_too_close_rejected` (leverage 20, percent stop 5 %); `test_liquidation_price_research_example` (75903.61 / 92031.87); `test_decide_action_table` (parametrized, all 12 cells × allowed/blocked; CLOSE+SHORT -> NONE); `test_kill_switch_trips_at_threshold` (4.99 % no, 5 % yes); `test_kill_switch_baseline_is_previous_day_last_equity` (first bar of a day that loses 6 % trips); `test_kill_switch_trips_on_1d_single_bar` (1d series, seed 10000, a bar closing at 9400 trips on that bar); `test_kill_switch_resets_next_utc_day`; `test_kill_switch_roundtrip` (incl. last_equity); `test_cooldown_semantics` (bars=3: decisions at T, T+i, T+2i blocked; T+3i allowed); `test_cooldown_zero_blocks_nothing`.

**U4**
- `test_fillmodel.py`: `test_market_fill_slippage_direction`; `test_fee_taker_maker`; `test_long_stop_gap_fills_at_open`; `test_gap_open_beyond_tp_fills_tp` (LONG o ≥ tp and l ≤ stop -> (TAKE_PROFIT, o); SHORT mirror); `test_sl_first_when_both_touched`; `test_tp_only`; `test_short_mirror`; `test_liquidation_on_gap_open`; `test_funding_payment_sign` (long + rate pays; short + rate receives); `test_liquidation_loss_nets_out_funding` (funding paid 5: gross = -(IM - 5); net = -IM - fees).
- `test_backtest_engine.py`: `test_signal_on_close_fill_on_next_open` (entry_price == open[i+1]*(1+slip)); `test_no_trade_before_warmup`; `test_fees_charged_both_legs`; `test_clean_stop_out_is_minus_one_r` (entry at ref, stopped exactly at stop, no gap, no funding -> r_multiple == -1.0 ± 1e-9, long and short); `test_sl_first_rule_in_engine`; `test_flip_closes_then_opens` (two trades, first exit_reason FLIP, second entry same timestamp, open leg sized from post-close cash); `test_allow_short_false_only_closes`; `test_kill_switch_blocks_entries_and_flattens`; `test_kill_switch_trips_on_1d_interval`; `test_cooldown_after_stop_blocks_reentry` (first new fill at T+4i for bars=3); `test_funding_charged_by_timestamp_rule` (entry at ft not charged; exit at ft charged; mid-trade charged with correct sign); `test_funding_events_metric`; `test_liquidation_loses_isolated_margin` (with funding paid before liquidation: net == -IM - fees); `test_entries_capped_by_notional_counted`; `test_end_of_data_closes_position`; `test_equity_curve_length_and_start`; `test_trade_start_ms_respected`; `test_deterministic`; `test_future_bars_do_not_change_past_trades` (mutate bars after k: trades with exit_time < open_time[k] identical).
- `test_metrics.py`: `test_total_return_and_final_equity`; `test_max_drawdown_known_series` ([100,120,90,130,65] from E0 100 -> 0.5); `test_sharpe_annualization_1h` (manual formula with sqrt(8760)); `test_sharpe_none_when_flat`; `test_profit_factor_none_without_losses`; `test_win_rate_expectancy`; `test_exposure`; `test_cagr`; `test_no_numpy_scalars_or_nan`.
- `test_report.py`: `test_save_writes_json_csv_and_db`; `test_result_json_has_no_nan`; `test_format_metrics_table_korean_labels_and_percent`; `test_report_reexports_labels_from_models` (`report.METRIC_LABELS_KO is models.METRIC_LABELS_KO`).

**U5**
- `test_paper_broker.py` (fake MarketData object, real Storage, `FakeClock`): `test_paper_requires_no_credentials`; `test_open_fills_at_ref_with_slippage_and_fee`; `test_sl_hit_closes_with_stop_loss`; `test_sl_first_when_both_touched`; `test_tp_hit`; `test_gap_through_stop_fills_at_open`; `test_gap_beyond_tp_fills_tp_at_open`; `test_candles_before_entry_ignored`; `test_processed_candles_not_reprocessed`; `test_sync_with_empty_candles_is_readonly` (no simulation, state and cursors unchanged, account built); `test_state_persists_across_instances`; `test_funding_applied_with_timestamp_rule`; `test_funding_cursor_is_last_applied_event` (a record published late — returned only by the second fetch — is still applied); `test_close_at_funding_time_waits_for_late_record` (record appears on the 2nd re-fetch; sleeps recorded); `test_close_at_funding_time_estimates_when_missing` (premium_index used; FUNDING_ESTIMATED event); `test_close_position_at_ref_price`; `test_account_equity_mark_to_market`; `test_liquidation_loss` (funding netted: net == -IM - fees); `test_first_sync_does_not_simulate_history`.
- `test_exchange_broker.py` (`responses` mocks on `https://demo-fapi.binance.com`, fake keys `"k"*64`/`"s"*64`, `FakeClock` clock/sleep; ActiveTrade objects are built exactly as U6 builds them: `_active_from_entry` fields with `protect_seq=1`, `entry_order_id` set, and an adopted variant with `protect_seq=0`, `entry_order_id=None`): `test_refuses_paper_mode_and_host_mismatch`; `test_prepare_symbol_sequence_and_no_change_codes` (-4059, -4046 tolerated; leverage verified); `test_prepare_symbol_hedge_mode_blocked_raises`; `test_prepare_symbol_live_never_changes_account_modes` (live + dual/multi-assets true -> ConfigError, no POST sent); `test_prepare_symbol_keeps_leverage_with_open_position` (no `/leverage` POST; WARNING; `-4161` tolerated); `test_leverage_applied_when_flat_before_entry`; `test_max_notional_cached_from_leverage_response`; `test_open_sends_market_then_algo_stop` (asserts exact params of both requests; `algoType=CONDITIONAL`, `type=STOP_MARKET`, `triggerPrice`, `closePosition=true`, `workingType=MARK_PRICE`, `priceProtect=false`, `clientAlgoId`; no `GET openAlgoOrders` needed when the POST returns `algoStatus=NEW`); `test_stop_orders_never_sent_to_order_endpoint` (no `/fapi/v1/order` call with STOP/TAKE_PROFIT types, no `stopPrice` param anywhere); `test_take_profit_placed_when_planned`; `test_reduce_only_protective_mode_params` (quantity = abs(positionAmt)); `test_sl_failure_flattens_and_raises`; `test_sl_immediate_trigger_flattens`; `test_sl_post_unknown_status_found_by_client_algo_id_no_flatten`; `test_sl_post_transient_then_absent_retries_once`; `test_sl_duplicate_id_existing_order_counts_as_placed`; `test_sl_rate_limit_sleeps_before_retry` (sleep min(retry_after, 10)); `test_sl_lookup_triggered_is_success`; `test_entry_unknown_status_queries_by_client_id`; `test_entry_unknown_then_position_exists_is_protected` (lookups find nothing, positionRisk shows the position -> SL placed, filled=True); `test_entry_not_final_is_polled_to_terminal`; `test_filled_false_only_after_flat_confirmed`; `test_preflight_existing_fill_not_resent` (EXPIRED with executedQty > 0 is reused); `test_avg_price_missing_falls_back_to_get_order`; `test_close_position_reduce_only_and_cancels_only_own_orders` (foreign algo/regular orders untouched; own cancelled one by one; neither `algoOpenOrders` nor `allOpenOrders` DELETE used); `test_close_partial_fill_retries_with_new_id` (first FL order EXPIRED with partial fill; second order uses `next_client_id`); `test_close_duplicate_client_id_uses_next_id`; `test_funding_income_paginates` (limit=1000, startTime = last+1); `test_sync_detects_stop_loss_closure` (via allAlgoOrders actualOrderId); `test_sync_closure_without_entry_order_id_uses_time_filter`; `test_sync_liquidation_adds_insurance_clear_fee`; `test_sync_reports_untracked_position`; `test_sync_ignores_foreign_stop_for_sl_missing`; `test_sync_reduce_only_small_sl_qty_reports_mismatch`; `test_sync_cancels_orphan_own_algo_orders_when_flat`; `test_ensure_protection_replaces_missing_sl`; `test_ensure_protection_places_before_cancel` (call order: POST new SL, then DELETE old by clientAlgoId).

**U6**
- `test_trader.py` (FakeBroker implementing `Broker` with scripted outcomes, FakeMarket returning candle_factory data, real Storage, `FakeClock`): `test_once_processes_last_closed_bar`; `test_same_bar_not_processed_twice`; `test_stale_data_skips_orders`; `test_forming_candle_open_used_as_ref_price`; `test_open_long_on_golden_cross_saves_active_trade` (entry_order_id stored, protect_seq 1, ids carry the symbol tag); `test_flip_closes_verifies_flat_then_opens` (call order; open leg sized from the post-close account); `test_flip_blocked_becomes_close_only`; `test_closure_recorded_and_cooldown_after_stop`; `test_closure_from_post_execution_sync_is_recorded`; `test_kill_switch_flattens_and_blocks`; `test_kill_switch_flatten_retried_after_transient_error` (trip; first close raises TransientError; next iteration closes); `test_kill_switch_flattens_after_restart_on_tripped_day`; `test_kill_switch_paper_mode_flatten_uses_ref_price` (real PaperBroker; no ValueError); `test_kill_switch_uses_bar_close_time` (same tripping bar as the engine on an identical series); `test_halt_file_blocks_entries_not_exits`; `test_protection_failure_halts_entries` (trade recorded from plan + e.entry with reason PROTECTION_FAILED); `test_emergency_flatten_uses_new_id_each_attempt` (attempt counter persisted in `emergency:*`); `test_untracked_position_adopted_and_protected` (leverage from position); `test_sl_missing_triggers_ensure_protection`; `test_protection_qty_mismatch_triggers_ensure_protection`; `test_protection_check_between_bars` (testnet FakeBroker: SL disappears while waiting -> ensure_protection called before the next candle; paper: no-op); `test_max_notional_caps_sizing`; `test_status_and_heartbeat_written` (local timestamps); `test_next_wake_time` (close + delay; sleeps are ≤ 1 s chunks); `test_stop_request_mid_iteration_finishes_order_sequence` (`request_stop()` called from the fake sleep inside `open_position` -> SL still placed, loop exits afterwards); `test_live_countdown_abort_sets_stopped_before_start`; `test_auth_error_stops_loop`; `test_build_trader_paper_has_no_credentials`; `test_build_trader_live_requires_confirmation`; `test_single_instance_lock` (second acquire in the same process on a separate fd -> BotError; release then re-acquire works; file content never written).
- `test_cli.py`: `test_parser_commands`; `test_param_parsing_types`; `test_trade_mode_live_rejected_exit_2`; `test_live_config_without_env_exit_3` (tmp config with mode live, no network); `test_dashboard_non_loopback_exit_2`; `test_backtest_offline_end_to_end` (synthetic kline CSV + synthetic funding CSV in tmp cache + cached filters JSON; asserts result dir + DB row, `total_funding != 0`, `config.funding_coverage.included` true, exit 0); `test_backtest_offline_without_funding_cache_raises` (exit 1, message mentions `--no-funding`); `test_no_resource_warnings` (offline backtest under `warnings.simplefilter("error", ResourceWarning)` + `gc.collect()`); `test_stdout_reconfigured_errors_replace`; `test_strategies_lists_ma_cross`.

**U7**
- `test_dashboard.py` (FastAPI `TestClient`, tmp Storage with seeded rows): `test_health`; `test_meta` (includes `metric_labels` == `models.METRIC_LABELS_KO`); `test_missing_db_returns_empty_payloads_and_creates_nothing`; `test_status_empty_returns_null`; `test_status_populated_with_heartbeat_age_and_stale`; `test_trades_filter_by_source`; `test_equity_points_seconds`; `test_candles_and_markers_sorted`; `test_events`; `test_backtests_list_and_detail`; `test_backtest_404`; `test_equity_downsampled_over_5000`; `test_index_html_korean_and_cdn`; `test_only_get_routes` (inspect `app.routes`: no POST/PUT/PATCH/DELETE methods); `test_post_returns_405`; `test_cache_control_no_store`; `test_foreign_host_header_rejected` (`Host: evil.com` -> 400); `test_run_dashboard_rejects_non_loopback`; `test_run_dashboard_passes_log_config_none` (monkeypatched `uvicorn.run` receives `log_config=None`).

---

## 15. E2E acceptance checklist (integrator, PowerShell, from the project folder)

Use `$py = ".\.venv\Scripts\python.exe"` and `$env:PYTHONUTF8 = "1"`. Paper mode only. No keys.
1. `& $py -m pip install --only-binary ":all:" -r requirements-dev.txt; & $py -m pip check` -> clean.
2. `& $py -m pytest` -> all pass, 0 failures, 0 errors (network tests deselected). `& $py -m pytest -W error -q` also passes.
3. `& $py -m pytest -m network` -> passes (public GET only).
4. `& $py -m bot strategies` -> lists `ma_cross` with defaults.
5. `& $py -m bot download --symbol BTCUSDT --interval 1h --start 2024-01-01` -> `data/klines/BTCUSDT_1h.csv` (~24k+ rows, header, no duplicate open_time, last `close_time` < now), `data/funding/BTCUSDT.csv`, `data/exchange_info/BTCUSDT.json`. Re-run completes in a few seconds and fetches only new rows.
6. `& $py -m bot backtest --start 2024-01-01` -> prints the Korean metrics table; `data/backtests/<run_id>/result.json|trades.csv|equity.csv` exist; `result.json` parses and has no NaN; a `backtest_runs` row exists.
7. `& $py -m bot backtest --offline --param fast_period=10 --param slow_period=30 --param ma_type=SMA --no-funding` -> succeeds without network.
8. `& $py -m bot trade --once` -> exit 0; `logs/trade.log` shows server time sync, last closed bar, signal, decision; `bot_status` row with mode `paper`; `candles` rows present; grep the log: no `/fapi/v1/order`, `/fapi/v1/algoOrder`, `signature=`.
9. Run `trade --once` again within the same candle -> log/report says `already_processed`.
10. Create `data\STOP`, run `trade --once` -> `entries_blocked_reason = halt_file`; delete the file.
11. Copy config to `tmp_live.yaml` with `mode: live`; `& $py -m bot -c tmp_live.yaml trade --once` (without setting the env var) -> exit code 3, no network. Delete the file.
12. `& $py -m bot dashboard --host 0.0.0.0` -> exit 2.
13. Start `& $py -m bot dashboard` in the background; `Invoke-RestMethod http://127.0.0.1:8000/api/health`, `/api/meta`, `/api/status`, `/api/trades?source=paper`, `/api/equity?mode=paper`, `/api/candles`, `/api/events`, `/api/backtests`, `/api/backtests/<run_id>` -> 200 with the shapes in §12.3; `Invoke-WebRequest -Method Post http://127.0.0.1:8000/api/status` -> 405; `/` returns HTML containing "바이낸스 선물 자동매매 대시보드". Open in a browser: charts render (CDN reachable), labels Korean. Stop the server.
14. `git check-ignore -v .env data/bot.db logs/trade.log .venv` (if git is initialised) -> all ignored.
15. README: Korean; sections present per §16.

Manual (user, later, with their own demo keys — NOT done by the integrator): set `mode: testnet`, fill `.env`, run `trade --once`, confirm in the Demo UI that the entry has a STOP_MARKET algo order with close-position; if the demo ignores `closePosition`, switch `execution.protective_mode: reduce_only`.

---

## 16. README.md outline (U7, Korean)
1. 소개 (기능: 백테스트, 페이퍼, 테스트넷, 실거래, 대시보드) + 굵은 경고: **투자 조언 아님 / 원금 손실 위험 / 실거래 전 충분한 테스트**.
2. 요구 사항 (Windows 11, Python 3.14), 설치 (venv, `--only-binary`), `Activate.ps1` 없이 `.venv\Scripts\python.exe` 직접 호출, `$env:PYTHONUTF8="1"`, PowerShell 따옴표 주의.
3. 시계 동기화 안내 (설정 > 시간 및 언어 > 날짜 및 시간 > 지금 동기화), 봇의 서버 시간 보정 설명.
4. 설정 (`config.yaml` 항목 표, `.env`).
5. 명령어: download / backtest / trade (--once, --mode, --reset-paper) / dashboard / strategies, 예시 포함.
6. 모드 설명: paper(기본, 키 불필요) / testnet(데모 트레이딩) / live(이중 확인 `mode: live` + `$env:CONFIRM_LIVE_TRADING="YES"`).
7. 테스트넷(데모) API 키 발급 방법 (demo.binance.com 로그인 -> API 관리 -> API 생성 -> 키/시크릿 저장, 출금 권한 금지, 데모 키는 메인넷에서 동작 안 함).
8. 리스크 관리 설명 (격리 마진, 단방향, 레버리지 상한, 위험 % 사이징, 손절/익절이 거래소 알고 주문으로 등록됨, 봉 사이에도 `heartbeat_sec`(최대 60초)마다 손절 주문 존재를 점검, 일일 손실 킬스위치(전날 마지막 자산 대비, UTC 00:00 = 한국 09:00 초기화), 쿨다운, `data/STOP` 파일로 신규 진입 중지, Ctrl+C 시 진행 중인 주문 절차는 마친 뒤 종료하고 포지션·보호주문은 유지). 추가로:
   - `price_protect` 기본값 false 인 이유: true 면 마크가/최종가 괴리(BTC 5%) 시 손절이 발동하지 않을 수 있음.
   - `max_position_notional` 은 절대 상한이라 자산이 커져도 포지션이 그 이상 커지지 않음(복리 효과 제한). 백테스트 결과의 "명목가 상한 적용 진입" 수로 확인.
   - live 모드에서는 계정 전체 설정(헤지 모드, 멀티에셋 모드)을 봇이 바꾸지 않음 — 직접 변경 안내.
   - 봇은 자기 주문(ID 접두사 `bot_id-심볼태그-`)만 취소함. 같은 계정에서 봇을 여러 개 돌리면 `bot_id` 를 봇마다 다르게. 같은 심볼에 봇 2개는 지원하지 않음. 트레이더 프로세스는 한 번에 하나만 실행 가능(`data/trader.lock`).
9. 전략 추가 방법 (Strategy 상속 + @register + extra_modules).
10. 백테스트 가정 (종가 신호/다음 봉 시가 체결, 수수료, 슬리피지 — 손절 체결에도 적용, 펀딩(캐시가 기간을 덮지 않으면 경고), 시가 갭 규칙 후 손절 우선 규칙, 청산 근사: 펀딩에 따른 청산가 이동과 청산 수수료는 백테스트에 미반영) 및 한계 (과최적화 주의).
11. 대시보드 사용법 (127.0.0.1 전용, 읽기 전용, 다른 Host 헤더 거부).
12. 테스트 실행 (`pytest`, `pytest -m network`).
13. 문제 해결 (-1021 시간 오류, -4120, 418 IP 차단, cp949 인코딩, "file is locked by another program" = 엑셀 등에서 CSV 를 열어둔 경우, "another trader instance is running").
14. 면책 조항.

---

## 17. Work breakdown (FINAL — parallel units, exclusive file ownership)

All units code against §0 and §3–§14 exactly. Paths are relative to the project root `binance-futures-bot/`. Where a unit's tests need another unit's code that may not exist yet, it uses fakes/stubs local to its own test files (or `tests/_helpers_u<N>.py`); at integration everything is real. Each unit must run `pytest tests/<its files>` green (also with `-W error`) against the finished tree. No unit edits a file it does not own; `docs/**` and the three `requirements*.txt` files are final and read-only for everyone (U1 owns the requirements files but must not change them).

| Unit | Owns (exclusively) | Provides | Consumes | Must follow |
|---|---|---|---|---|
| **U1 Foundation, storage, logging, test fixtures** | `bot/__init__.py`, `bot/config.py`, `bot/errors.py`, `bot/models.py`, `bot/timeutil.py`, `bot/fsutil.py`, `bot/logging_setup.py`, `bot/storage.py`, `config.example.yaml`, `config.yaml`, `.env.example`, `.gitignore`, `pytest.ini`, `requirements.txt`, `requirements-dev.txt`, `requirements-lock.txt` (keep as is), `tests/__init__.py`, `tests/conftest.py`, `tests/data/exchange_info_btcusdt.json`, `tests/test_config.py`, `tests/test_models.py`, `tests/test_timeutil.py`, `tests/test_fsutil.py`, `tests/test_storage.py`, `tests/test_logging.py`, `tests/test_conftest_network.py` | every dataclass/enum/constant of §4.1 (incl. `METRIC_LABELS_KO`, `PERCENT_METRICS`, `make_client_id`, `next_client_id`, `client_id_prefix`, `symbol_tag`, `to_jsonable`), all exceptions (§4.2), time utils (§4.3), `atomic_replace`/`atomic_write_text` (§4.4), `AppConfig` + `load_config`/`with_overrides`/`load_credentials`/`assert_live_allowed`/`DEFAULTS` (§3), `Storage` (§5), `setup_logging`/`shutdown_logging`/`add_secrets`/`clear_secrets`/`redact`/`RedactingFormatter`/`RedactingFilter` (§12.2), fixtures `_block_network`, `_isolate_env`, `_reset_logging`, `candle_factory`, `ohlc_factory`, `btc_filters`, `exchange_info_btc`, `app_config`, `storage`, `fixed_clock`/`FakeClock` (§14.1) | nothing | §0, §1, §2, §3, §4, §5, §12.2, §14.1, §14.2 U1 |
| **U2 Exchange client, market data, downloader** | `bot/exchange/__init__.py`, `bot/exchange/rest.py`, `bot/exchange/filters.py`, `bot/exchange/market.py`, `bot/data/__init__.py`, `bot/data/downloader.py`, `tests/test_rest.py`, `tests/test_filters.py`, `tests/test_market.py`, `tests/test_downloader.py`, `tests/test_network.py` | `BinanceRestClient` (signing, method-aware `map_error`, retry policy, weight guard, time sync, `from None` exception wrapping), filters/rounding (`to_decimal`, `floor_to_step`, `round_price`, `round_protective_price`, `normalize_market_qty`, `meets_min_notional`, `format_decimal`, `parse_symbol_filters`), `klines_to_df`, `split_closed`, `MarketData` (incl. `premium_index`), downloader (`download_klines`, `download_funding`, `load_klines`, `load_funding`, `find_gaps`, `load_or_fetch_filters`, cache paths) | U1 | §0, §4, §6, §7, §13, §14.2 U2 |
| **U3 Strategy, indicators, risk** | `bot/strategy/__init__.py`, `bot/strategy/base.py`, `bot/strategy/registry.py`, `bot/strategy/indicators.py`, `bot/strategy/ma_cross.py`, `bot/risk.py`, `tests/test_indicators.py`, `tests/test_strategy.py`, `tests/test_risk.py` | `Strategy` ABC, registry (`register`, `create_strategy`, `available_strategies`, `load_strategy_modules`), `MACrossStrategy`, indicators (`sma`, `ema`, `moving_average`, `true_range`, `atr`), `compute_stop_price`, `compute_take_profit`, `approx_liquidation_price`, `plan_entry` (slippage-exact `per_unit_loss`, `sizing_cap`), `decide_action` (CLOSE+SHORT -> NONE), `DailyLossKillSwitch` (`seed`, previous-day baseline), `Cooldown` (strict) | U1; `bot.exchange.filters` (U2) | §0, §4.1, §8, §14.2 U3 |
| **U4 Fill model & backtest** | `bot/fillmodel.py`, `bot/backtest/__init__.py`, `bot/backtest/engine.py`, `bot/backtest/metrics.py`, `bot/backtest/report.py`, `tests/test_fillmodel.py`, `tests/test_backtest_engine.py`, `tests/test_metrics.py`, `tests/test_report.py` | `FillModel`, `resolve_intrabar_exit` (gap rules then SL-first), `funding_payment`, `liquidation_loss(..., funding_paid)`, `new_run_id`, `run_backtest` (metrics `rejected_entries`, `entries_capped_by_notional`, `funding_events`), `compute_metrics`, `save_backtest_result`, `format_metrics_table`, re-export of `METRIC_LABELS_KO`/`PERCENT_METRICS` | U1; U3 (strategy, indicators, risk); U2 (`exchange.filters`) | §0, §4, §8.5, §9.1, §11, §14.2 U4 |
| **U5 Brokers** | `bot/broker/__init__.py`, `bot/broker/base.py`, `bot/broker/paper.py`, `bot/broker/exchange_broker.py`, `tests/test_paper_broker.py`, `tests/test_exchange_broker.py` | `Broker` ABC (incl. non-abstract `max_notional`), `PaperBroker` (empty-candle read-only sync, applied-event funding cursor, late-record rule), `ExchangeBroker` (prepare_symbol with live/testnet account-mode rules and leverage-keeping, position-truth-checked `open_position`, id-fresh multi-order `close_position`, own-orders-only cancellation, `sync` issues incl. `PROTECTION_QTY_MISMATCH`, place-before-cancel `ensure_protection`, §9.5 placement procedure, paginated income) | U1; U2 (`BinanceRestClient`, `MarketData`, filters); U4 (`fillmodel`) | §0, §4, §6 (error classes/retry semantics), §9.2–§9.5, §13, §14.2 U5 |
| **U6 Trader & CLI** | `bot/trader.py`, `bot/cli.py`, `bot/__main__.py`, `tests/test_trader.py`, `tests/test_cli.py` | `IterationReport`, `SingleInstanceLock` (global `data/trader.lock`, exact msvcrt/fcntl recipe), `Trader` (`startup`, `run_once`, `protection_check`, `run_forever`, `request_stop`, `stopped_before_start`, `_handle_sync`), `build_trader`, `build_parser`, `main` (exit codes, stdout/stderr reconfigure, funding-aware backtest flow) | all units (U1–U5, U7's `run_dashboard`) | §0, §3, §5 (state keys, lifecycle), §8.5, §9.2, §10, §12.1, §13, §14.2 U6, §15 |
| **U7 Dashboard & README** | `bot/dashboard/__init__.py`, `bot/dashboard/app.py`, `bot/dashboard/static/index.html`, `bot/dashboard/static/app.js`, `bot/dashboard/static/style.css`, `README.md`, `tests/test_dashboard.py` | `create_app` (GET-only, TrustedHostMiddleware, missing-DB empty payloads, read-only storage dependency), `run_dashboard` (`log_config=None`), `CDN_LIGHTWEIGHT_CHARTS`, `EXIT_REASON_KO`, static page, Korean README | U1 (`config`, `storage`, `models`, `timeutil` only) | §0, §5 (read-only), §12.3, §16, §14.2 U7 |

Integration order for the integrator (after all units deliver): U1 -> U2 -> U3 -> U4 -> U5 -> U7 -> U6; run the whole suite (`pytest` and `pytest -W error`), then §15.

Cross-unit contracts most likely to be misread (double-check):
1. Time units: ms everywhere internally; seconds only in dashboard chart JSON. Bar/order times = server time; status/heartbeat/event times = local `now_ms`.
2. `Signal.bar_open_time` is the closed bar; entries fill in the **next** bar (`bar + interval_ms`), which is also `ActiveTrade.entry_bar_open_time`.
3. Funding sign: positive = paid (cost). `net = gross - fees - funding`. At liquidation `gross = -(IM - funding_paid)` so `net = -IM - fees`.
4. `Position.qty` is signed; `ActiveTrade.qty` and `Trade.qty` are absolute.
5. Protective orders go ONLY to `/fapi/v1/algoOrder` with `triggerPrice` (`priceProtect=false` by default). The bot cancels only its OWN orders one by one (`DELETE /fapi/v1/algoOrder clientAlgoId`, `DELETE /fapi/v1/order origClientOrderId`); it never calls `DELETE /fapi/v1/algoOpenOrders` or `DELETE /fapi/v1/allOpenOrders`.
6. Client ids: `make_client_id(bot_id, symbol, kind, bar_ms, seq)` -> `"<bot_id>-<4 hex symbol tag>-<kind>-<bar_s>-<seq>"`; current protective generation = `protect_seq`; FL/KS/EX/EN retries always use a never-used id (`next_client_id`, `_fresh_client_id`, emergency attempt counter).
7. `OpenOutcome(filled=False)` means "confirmed flat"; callers check `filled` before reading anything else.
8. Every `SyncResult` (including from `sync(..., [])`) goes through `Trader._handle_sync`; `sync` with an empty candle list never mutates paper simulation state.
9. Kill switch: `update(bar close time, equity)`, baseline = last equity before the UTC day changed; `seed()` with initial balance (engine) / startup equity (trader). Cooldown `active` is strict (`<`).
10. Paper mode client has no credentials; `signed_request` must fail locally.
11. Gap rules and the SL-first rule are implemented once, in `fillmodel.resolve_intrabar_exit`, and used by both the backtest engine and the paper broker.
12. numpy scalars never cross into models/Storage/JSON/client ids (§0.2).
