# binance-futures-bot

바이낸스 USDT-M 무기한 선물용 자동매매 봇입니다. 같은 전략 코드로 **백테스트 → 페이퍼(모의) → 테스트넷(데모 트레이딩) → 실거래** 순서로 검증하고, 로컬 웹 **대시보드**로 상태를 확인할 수 있습니다.

> **⚠️ 반드시 읽어 주세요**
>
> - **이 소프트웨어는 투자 조언이 아닙니다.** 기본 전략(이동평균 교차)은 예시일 뿐이며 수익을 보장하지 않습니다.
> - **선물 거래는 원금 손실 위험이 매우 큽니다.** 레버리지를 쓰면 짧은 시간에 증거금 전부를 잃을 수 있습니다(강제청산).
> - **실거래 전에 백테스트, 페이퍼, 테스트넷에서 충분히 테스트하세요.** 실거래는 잃어도 되는 금액으로만 하세요.

---

## 목차

0. [가장 쉬운 사용법 — 더블클릭 실행 창](#0-가장-쉬운-사용법--더블클릭-실행-창)
1. [소개](#1-소개)
2. [요구 사항과 설치](#2-요구-사항과-설치)
3. [시계 동기화](#3-시계-동기화)
4. [설정](#4-설정)
5. [명령어](#5-명령어)
6. [실행 모드](#6-실행-모드)
7. [테스트넷(데모) API 키 발급](#7-테스트넷데모-api-키-발급)
8. [리스크 관리](#8-리스크-관리)
9. [전략 추가 방법](#9-전략-추가-방법)
10. [백테스트 가정과 한계](#10-백테스트-가정과-한계)
11. [대시보드 사용법](#11-대시보드-사용법)
12. [테스트 실행](#12-테스트-실행)
13. [문제 해결](#13-문제-해결)
14. [면책 조항](#14-면책-조항)

---

## 0. 가장 쉬운 사용법 — 더블클릭 실행 창

명령어를 몰라도 **창 하나**에서 버튼으로 모두 할 수 있습니다.

1. **Python 3.14 설치** (한 번만): https://www.python.org/downloads/ 에서 설치합니다. 설치 첫 화면에서 **"Add python.exe to PATH"** 를 체크하세요.
2. 프로젝트 폴더의 **`START_BOT.bat`** 을 더블클릭합니다.
   - 처음 한 번은 검은 창에서 필요한 프로그램을 설치합니다(몇 분). 다음부터는 바로 열립니다.
   - "Windows의 PC 보호" 창이 뜨면 **추가 정보 → 실행** 을 누르세요(인터넷에서 받은 파일이라 뜨는 확인 창입니다).
3. 열린 창에서 버튼을 누릅니다.

| 버튼 | 하는 일 |
|---|---|
| **위험도 · 봉 간격 → 적용** | 안정형 / 기본형 / 공격형 / 초공격형 중 하나와 봉 간격을 골라 `config.yaml` 에 저장합니다([위험도 프리셋](#위험도-프리셋)). |
| **백테스트 실행** | 시작일(지난 날짜, 예: `2024-01-01`)부터 지금까지 과거 데이터로 현재 설정을 시험합니다. 결과 표가 아래 "실행 기록"에 나옵니다. |
| **위험도별 비교** | 4가지 위험도 × 1h·4h 봉을 같은 과거 데이터로 한 번에 백테스트해 비교표를 보여 줍니다(`compare` 명령). |
| **▶ 시작 / ■ 중지** | 자동매매를 시작/중지합니다. **모의매매**(가짜 돈, 키 불필요) / **테스트넷**(바이낸스 데모, 데모 키 필요) / **실거래**(진짜 돈, 아래 참고) 중에서 고릅니다. 중지는 진행 중인 주문을 마친 뒤 안전하게 멈춥니다(Ctrl+C 와 같음). |
| **대시보드 열기** | 브라우저에서 차트·포지션·거래 내역·백테스트 결과를 봅니다. |
| **테스트넷 API 키 / 실거래 API 키** | 키를 붙여넣으면 `.env` 파일에 저장합니다([7장](#7-테스트넷데모-api-키-발급), [6장](#live-실거래-이중-확인)). |
| **설정 파일 열기 / 설정 다시 읽기** | 메모장으로 `config.yaml` 을 고친 뒤 다시 읽습니다([4장](#4-설정)). |

- 창을 닫으면 자동매매도 안전하게 멈춥니다. 열린 포지션과 거래소 손절 주문은 그대로 남고, 다시 시작하면 이어서 관리합니다.
- **실거래**를 고르고 시작하면 확인 창이 뜹니다. 심볼·위험도·최대 포지션을 보여 주고, **`실거래` 라고 직접 입력해야** 시작 버튼이 켜집니다. 그때 `config.yaml` 이 `mode: live` 로 바뀌고, 실거래 확인값(`CONFIRM_LIVE_TRADING=YES`)은 **그 자동매매 프로세스에만** 전달됩니다. 시작 후 10초 카운트다운 중에 **중지**를 누르면 주문 없이 취소됩니다. 실거래 전에 [실거래 체크리스트](#실거래-체크리스트)를 꼭 보세요.
- 창은 아래 명령어들을 대신 실행해 줄 뿐입니다. 같은 일을 PowerShell 에서 직접 하려면 2장부터 보세요.

---

## 1. 소개

| 기능 | 설명 |
|---|---|
| 백테스트 | 메인넷 공개 캔들·펀딩비를 내려받아 로컬에서 시뮬레이션합니다. 수수료, 슬리피지, 펀딩비, 손절/익절, 강제청산 근사, 킬스위치, 쿨다운을 반영합니다. 결과는 `data/backtests/<실행 ID>/` 와 DB 에 저장됩니다. |
| 페이퍼(`paper`, 기본) | 실제 메인넷 시세(공개 데이터)로 신호를 만들고 체결은 로컬에서 가상으로 처리합니다. **API 키가 필요 없고 주문을 전혀 보내지 않습니다.** |
| 테스트넷(`testnet`) | 바이낸스 **데모 트레이딩**(`https://demo-fapi.binance.com`)에 실제 주문 흐름(시장가 진입, 거래소 손절/익절 주문)을 보냅니다. 데모 키가 필요합니다. 가짜 돈입니다. |
| 실거래(`live`) | 메인넷 실거래입니다. 설정 파일과 환경변수 두 곳에서 **이중 확인**해야만 실행됩니다. |
| 대시보드 | `127.0.0.1` 에서만 열리는 **읽기 전용** 웹 화면입니다. 봇 상태, 포지션, 보호 주문, 캔들 차트, 자산 곡선, 거래 내역, 이벤트, 백테스트 결과를 보여줍니다. |

동작 방식 요약:

- 전략은 **마감된 캔들**만 봅니다. 봉이 마감되면(캔들 마감 + `candle_close_delay_sec` 초 후) 신호를 계산하고, 진입은 다음 봉 시작 시점에 시장가로 합니다.
- 진입 직후 손절(필수)과 익절(선택) 주문을 **거래소에 조건부(알고) 주문으로** 등록합니다. 봇이 꺼져 있어도 거래소가 손절을 실행합니다.
- 격리 마진, 단방향(One-way) 포지션 모드만 사용합니다. 심볼 하나, 포지션 하나만 관리합니다.
- WebSocket 없이 REST 폴링만 사용합니다(1분봉 이상 전략용).

---

## 2. 요구 사항과 설치

### 요구 사항

- Windows 11, PowerShell
- **Python 3.14** (64비트)
- 인터넷 연결 (바이낸스 공개 API, 대시보드 차트 라이브러리 CDN)

### 설치

프로젝트 폴더에서 PowerShell 을 열고 실행합니다.

```powershell
# 1) 가상환경 만들기 (Python 3.14 를 명시)
py -3.14 -m venv .venv

# 2) pip 업그레이드
.\.venv\Scripts\python.exe -m pip install --upgrade pip

# 3) 패키지 설치 — 휠(바이너리)만 사용하므로 C 컴파일러가 필요 없습니다
.\.venv\Scripts\python.exe -m pip install --only-binary ":all:" -r requirements-dev.txt
#    (실행만 할 거라면 requirements.txt, 검증된 버전 그대로 재현하려면 requirements-lock.txt)

# 4) 확인
.\.venv\Scripts\python.exe -m pip check
```

### PowerShell 사용 팁 (중요)

- **`Activate.ps1` 은 쓰지 않습니다.** Windows 기본 실행 정책(Restricted) 때문에 활성화 스크립트가 막힙니다. 대신 가상환경의 파이썬을 **직접 호출**하세요. 이 문서의 모든 예시는 아래 변수를 씁니다.

  ```powershell
  $py = ".\.venv\Scripts\python.exe"
  & $py -m bot strategies
  ```

  (변수에 담긴 경로를 실행할 때는 앞에 `&` 가 필요합니다. 변수는 그 PowerShell 창에서만 유지됩니다.)

- **UTF-8 모드를 켜세요.** 이 PC 의 기본 인코딩은 cp949 라서 한글 출력/파일이 깨질 수 있습니다. 봇은 파일을 항상 UTF-8 로 쓰지만, 콘솔을 위해 창마다 한 번 실행하세요.

  ```powershell
  $env:PYTHONUTF8 = "1"
  ```

- **따옴표 주의.** PowerShell 5.1 은 외부 프로그램에 넘기는 인수 안의 큰따옴표를 지워버립니다.
  - `python -c` 로 코드를 실행할 때는 코드 안에서 작은따옴표만 쓰세요: `& $py -c "print('ok')"`
  - `--only-binary ":all:"` 처럼 특수문자가 있는 값은 따옴표로 감싸세요.
  - 공백이 있는 경로는 따옴표로 감싸세요: `& $py -m bot -c "D:\my bot\config.yaml" trade --once`
- 가상환경은 다른 폴더로 옮기면 동작하지 않습니다. 프로젝트 폴더를 옮겼다면 `.venv` 를 지우고 다시 만드세요.

---

## 3. 시계 동기화

바이낸스는 서명된 요청의 시각이 서버 시각과 크게 다르면 `-1021` 오류로 거부합니다(허용 범위: 서버 시각보다 1초 이상 빠르면 거부, `recv_window_ms`(기본 5초) 이상 늦어도 거부). 이 PC 는 실제로 바이낸스보다 약 3.8초 늦었고 Windows 시간 동기화가 꺼져 있었습니다.

**먼저 Windows 시계를 동기화하세요:**
`설정 > 시간 및 언어 > 날짜 및 시간 > 지금 동기화` (그리고 "자동으로 시간 설정" 켜기)

봇 자체의 보정:

- 시작할 때 `GET /fapi/v1/time` 으로 서버 시각을 받아 `서버 시각 − 로컬 시각` 차이를 계산하고, 모든 서명 요청의 timestamp 에 이 차이를 더합니다.
- 5분마다 다시 맞추고, `-1021`/`-5028` 오류가 나면 즉시 다시 맞춘 뒤 **한 번만** 재시도합니다.
- 차이가 1초를 넘으면 로그에 경고("local clock differs from Binance ... sync Windows time")를 남깁니다.
- 캔들 시각, 주문 시각은 **서버 시각** 기준입니다. 대시보드는 표시할 때만 한국 시간(KST)으로 바꿉니다.

---

## 4. 설정

### `config.yaml`

설정은 프로젝트 폴더의 `config.yaml` 에 있습니다(처음에는 `config.example.yaml` 과 같습니다. 망가뜨렸다면 `Copy-Item config.example.yaml config.yaml -Force` 로 되돌리세요). 경로는 **설정 파일이 있는 폴더 기준 상대경로**입니다. 모르는 키(오타)나 범위를 벗어난 값은 시작할 때 바로 오류로 알려줍니다(종료 코드 2).

| 키 | 기본값 | 설명 |
|---|---|---|
| `mode` | `paper` | `paper` / `testnet` / `live`. live 는 [이중 확인](#6-실행-모드) 필요 |
| `symbol` | `BTCUSDT` | USDT-M 무기한 선물 심볼 (`...USDT`) |
| `interval` | `1h` | 캔들 간격: `1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d` (3d/1w/1M 미지원) |
| `strategy.name` | `ma_cross` | 등록된 전략 이름 (`strategies` 명령으로 확인) |
| `strategy.extra_modules` | `[]` | 사용자 전략 모듈 import 경로 (예: `["user_strategies.my_rsi"]`) |
| `strategy.params.fast_period` | `20` | 단기 이동평균 기간 |
| `strategy.params.slow_period` | `50` | 장기 이동평균 기간 (`fast_period` 보다 커야 함) |
| `strategy.params.ma_type` | `EMA` | `SMA` 또는 `EMA` |
| `strategy.params.allow_short` | `true` | `false` 면 데드크로스에서 롱 청산만 하고 숏 진입 안 함 |
| `risk.leverage` | `3` | 레버리지 (`max_leverage` 이하) |
| `risk.max_leverage` | `10` | 허용 레버리지 상한 (코드 절대 상한 20) |
| `risk.risk_per_trade_pct` | `1.0` | 1회 거래 위험 = 자산의 %(손절 시 예상 손실액, 0 초과 5 이하) |
| `risk.stop_loss.mode` | `atr` | `percent` 또는 `atr` |
| `risk.stop_loss.percent` | `2.0` | percent 모드: 진입가 대비 손절 거리 % |
| `risk.stop_loss.atr_period` | `14` | atr 모드: ATR 기간 |
| `risk.stop_loss.atr_multiple` | `2.0` | atr 모드: 손절 거리 = ATR × 배수 |
| `risk.take_profit_r` | `2.0` | 익절가 = 손절거리 × R. `null` 이면 익절 주문 없음 |
| `risk.max_position_notional` | `20000` | 포지션 명목가치 절대 상한(USDT) |
| `risk.max_margin_fraction` | `0.9` | 증거금 사용 상한(자산 대비 비율) |
| `risk.max_daily_loss_pct` | `5.0` | 일일(UTC) 손실 한도 %, 도달 시 킬스위치 (`0` = 비활성) |
| `risk.kill_switch_flatten` | `true` | 킬스위치 발동 시 보유 포지션을 시장가로 즉시 청산 |
| `risk.cooldown_bars_after_stop` | `3` | 손절/강제청산 후 신규 진입 금지 봉 수 |
| `risk.min_liq_distance_multiple` | `2.0` | (진입가~청산가 거리) ≥ (손절 거리 × 이 값) 이어야 진입 |
| `risk.maint_margin_rate` | `0.004` | 유지증거금률 (청산가 근사용) |
| `risk.liq_mmr_buffer` | `0.005` | 청산가 근사에 더하는 보수적 버퍼 |
| `execution.fees.maker` / `taker` | `0.0002` / `0.0005` | 수수료율 (시장가·스탑 체결은 taker) |
| `execution.slippage_bps` | `5` | 시장가·스탑 체결의 불리한 슬리피지 (1bp = 0.01%) |
| `execution.working_type` | `MARK_PRICE` | 보호주문 트리거 기준: `MARK_PRICE` / `CONTRACT_PRICE` |
| `execution.price_protect` | `false` | 아래 [리스크 관리](#8-리스크-관리) 참고 (기본 false 권장) |
| `execution.protective_mode` | `close_position` | `close_position` / `reduce_only` (테스트넷 검증용 대체 방식) |
| `execution.candle_close_delay_sec` | `3` | 캔들 마감 후 대기 시간(초) |
| `execution.kline_limit` | `500` | 신호 계산용 캔들 수 (워밍업이 길면 자동으로 늘림, 최대 1500) |
| `execution.recv_window_ms` | `5000` | 서명 요청 허용 시간창(ms) |
| `execution.heartbeat_sec` | `30` | 하트비트 기록 및 보호주문 점검 주기(초) |
| `execution.bot_id` | `mab1` | 주문 ID 접두사 (영문/숫자 1~8자) |
| `paper.initial_balance` | `10000` | 페이퍼 모드 시작 잔고(USDT) |
| `paper.include_funding` | `true` | 페이퍼 모드에서 실제 펀딩비 반영 |
| `backtest.start` / `end` | `"2024-01-01"` / `null` | 백테스트 기간(UTC). `end: null` = 현재 |
| `backtest.initial_balance` | `10000` | 백테스트 시작 자산 |
| `backtest.include_funding` | `true` | 과거 펀딩비 반영 |
| `backtest.results_dir` | `data/backtests` | 결과 폴더 |
| `data.cache_dir` | `data` | `data/klines`, `data/funding`, `data/exchange_info` 캐시 위치 |
| `storage.db_path` | `data/bot.db` | SQLite DB (봇이 쓰고 대시보드가 읽음) |
| `dashboard.host` / `port` / `refresh_sec` | `127.0.0.1` / `8000` / `10` | 대시보드 주소(로컬 전용), 포트, 자동 새로고침 주기(초) |
| `logging.level` / `dir` | `INFO` / `logs` | 로그 레벨, 로그 폴더 |
| `logging.max_bytes` / `backup_count` | `5242880` / `5` | 로그 파일 최대 크기(5MB), 보관 개수 |
| `halt_file` | `data/STOP` | 이 파일이 있으면 신규 진입 중지 |

### `.env` (API 키)

API 키는 **`.env` 파일에만** 둡니다(`config.yaml` 에 넣지 마세요). `.env` 는 `.gitignore` 에 포함되어 있습니다.

```powershell
Copy-Item .env.example .env
notepad .env
```

| 변수 | 용도 |
|---|---|
| `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` | testnet 모드 (데모 트레이딩 키) |
| `BINANCE_API_KEY`, `BINANCE_API_SECRET` | live 모드 (메인넷 실거래 키) |

- paper 모드는 키를 **읽지도 않습니다.**
- 같은 이름의 환경변수가 PowerShell 에 설정되어 있으면 `.env` 보다 우선합니다.
- `CONFIRM_LIVE_TRADING` 은 `.env` 에서 **읽지 않습니다**(실수로 실거래가 켜지는 것을 막기 위해). 실거래 확인은 실행하는 창에서 직접 설정해야 합니다.
- 키, 시크릿, 서명 값은 로그에 기록되지 않습니다(자동 마스킹).

### 생성되는 파일

| 경로 | 내용 |
|---|---|
| `data/klines/<심볼>_<간격>.csv` | 마감된 캔들 캐시 |
| `data/funding/<심볼>.csv` | 펀딩비 캐시 |
| `data/exchange_info/<심볼>.json` | 호가 단위/수량 단위 등 거래 규칙 캐시 |
| `data/backtests/<실행 ID>/` | `result.json`, `trades.csv`, `equity.csv` |
| `data/bot.db` | 상태, 거래, 자산, 이벤트, 백테스트 결과 (SQLite) |
| `data/STOP` | (직접 만드는 파일) 신규 진입 중지 스위치 |
| `data/trader.lock` | 트레이더 중복 실행 방지 잠금 파일 |
| `logs/<명령>.log` | 명령별 로그 (`trade.log`, `backtest.log`, `dashboard.log` ...) UTF-8 |

---

## 5. 명령어

형식: `& $py -m bot [-c 설정파일] [--log-level DEBUG|INFO|WARNING|ERROR] <명령> [옵션]`

- `-c/--config` 기본값은 `config.yaml`
- `--log-level` 은 설정 파일의 로그 레벨을 덮어씁니다. `DEBUG` 이면 오류 시 전체 traceback 도 출력합니다.

### `strategies` — 전략 목록

```powershell
& $py -m bot strategies
```

등록된 전략과 기본 파라미터를 출력합니다 (기본 제공: `ma_cross`).

### `download` — 과거 데이터 내려받기

| 옵션 | 설명 |
|---|---|
| `--symbol`, `--interval` | 기본값은 설정 파일 값 |
| `--start DATE` | 시작일(UTC, `YYYY-MM-DD` 또는 ISO 8601). 기본값 `backtest.start` |
| `--end DATE` | 종료일. 기본값 현재 |
| `--no-funding` | 펀딩비는 받지 않음 |

```powershell
& $py -m bot download --symbol BTCUSDT --interval 1h --start 2024-01-01
```

메인넷 **공개** API(키 불필요)만 사용합니다. 이미 받은 구간은 다시 받지 않고 새 캔들만 추가하므로 두 번째 실행은 몇 초면 끝납니다. 아직 마감되지 않은 캔들은 저장하지 않습니다. 거래소 점검 등으로 빠진 구간이 있으면 경고만 하고 임의로 채우지 않습니다.

### `backtest` — 백테스트

| 옵션 | 설명 |
|---|---|
| `--symbol`, `--interval`, `--start`, `--end` | 기본값은 설정 파일 값 |
| `--strategy NAME` | 전략 이름 |
| `--param KEY=VALUE` | 전략 파라미터 덮어쓰기 (여러 번 사용 가능, 값은 YAML 로 해석: `10` → 정수, `true` → 불리언, `SMA` → 문자열) |
| `--initial-balance` | 시작 자산 |
| `--no-funding` | 펀딩비 미반영 |
| `--offline` | 네트워크 없이 캐시만 사용 |
| `--no-save` | 결과를 파일/DB 에 저장하지 않음 |
| `--profile NAME` | 위험도 프리셋으로 `risk` 값 덮어쓰기: `conservative`(안정형), `standard`(기본형), `aggressive`(공격형), `very_aggressive`(초공격형) |

```powershell
# 필요한 데이터를 자동으로 내려받은 뒤 백테스트
& $py -m bot backtest --start 2024-01-01

# 파라미터를 바꿔서, 네트워크 없이 (캐시 필요), 펀딩 미반영
& $py -m bot backtest --offline --param fast_period=10 --param slow_period=30 --param ma_type=SMA --no-funding
```

- 첫 신호가 시작일에 바로 유효하도록 지표 워밍업에 필요한 만큼 시작일 이전 데이터도 함께 받습니다.
- 한국어 지표 표(총 수익률, 최대 낙폭, 샤프 지수, 승률, 명목가 상한 적용 진입 수 등)와 결과 폴더(`data/backtests/<실행 ID>/`)를 출력합니다. 결과는 대시보드의 "백테스트 결과"에도 나타납니다.
- 펀딩비 캐시가 백테스트 기간 전체를 덮지 않으면 "펀딩비 데이터가 기간 전체를 덮지 않습니다" 경고가 나옵니다. `--offline` 인데 펀딩 캐시가 아예 없으면 오류가 나므로 먼저 `download` 하거나 `--no-funding` 을 쓰세요.

### `compare` — 위험도별 비교표

같은 과거 데이터로 [위험도 프리셋](#위험도-프리셋) 4가지를 봉 간격별로 백테스트하고 비교표를 출력합니다. 결과는 `data/backtests/compare-<시각>.csv` 에도 저장됩니다(DB 의 백테스트 목록에는 넣지 않음).

| 옵션 | 설명 |
|---|---|
| `--intervals LIST` | 쉼표로 구분한 봉 간격 (기본값 `1h,4h`) |
| `--profiles LIST` | 비교할 프리셋 (기본값: 전부) |
| `--symbol`, `--start`, `--end`, `--strategy`, `--param`, `--initial-balance`, `--no-funding`, `--offline` | `backtest` 와 같음 |
| `--no-save` | CSV 저장 안 함 |

```powershell
& $py -m bot compare --start 2023-01-01
& $py -m bot compare --start 2024-01-01 --intervals 15m,1h,4h --profiles standard,aggressive
```

- 표의 "최대 낙폭"은 기간 중 자산이 고점에서 가장 많이 줄었던 비율입니다. 수익률만 보지 말고 이 낙폭을 견딜 수 있는지 함께 보세요.
- 기간이 180일보다 짧으면 연 수익률은 표시하지 않습니다(몇 주를 1년으로 환산하면 숫자가 터무니없어집니다).

### `trade` — 자동매매 실행

| 옵션 | 설명 |
|---|---|
| `--once` | 한 번만(마지막 마감 봉 하나만) 처리하고 종료 |
| `--mode {paper,testnet}` | 설정 파일의 모드를 덮어씀. **`live` 는 명령줄로 켤 수 없습니다** (설정 파일에서만) |
| `--reset-paper` | (paper 전용) 페이퍼 잔고·포지션·킬스위치·쿨다운 상태를 초기화한 뒤 실행 |
| `--stop-on-stdin` | 표준입력으로 `stop` 줄을 받거나 입력이 끝나면(EOF) Ctrl+C 처럼 안전하게 멈춤. 실행 창(`START_BOT.bat`)이 사용합니다 |

```powershell
# 페이퍼 모드로 계속 실행 (Ctrl+C 로 종료)
& $py -m bot trade

# 한 번만 실행해서 동작 확인
& $py -m bot trade --once

# 테스트넷(데모)으로 실행
& $py -m bot trade --mode testnet
```

- 매 봉 마감(+`candle_close_delay_sec` 초)마다 깨어나 **마지막으로 마감된 봉**을 처리합니다. 같은 봉은 두 번 처리하지 않습니다(로그에 `already_processed`).
- 데이터가 늦게 오면(거래소 지연) 그 봉에서는 신규 주문을 내지 않습니다(`stale_data`). 손절 점검은 계속합니다.
- 기다리는 동안 `heartbeat_sec` 마다 하트비트를 기록하고, testnet/live 에서는 손절 주문이 살아있는지 점검합니다.
- 트레이더는 모드와 상관없이 **한 번에 하나만** 실행할 수 있습니다(`data/trader.lock`).

### `dashboard` — 대시보드

| 옵션 | 설명 |
|---|---|
| `--host` | 기본값 `dashboard.host` (`127.0.0.1`). `127.0.0.1`, `localhost`, `::1` 만 허용 (그 외는 종료 코드 2) |
| `--port` | 기본값 `dashboard.port` (`8000`) |

```powershell
& $py -m bot dashboard
# 브라우저에서 http://127.0.0.1:8000 열기
```

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 (사용자가 Ctrl+C 로 트레이더를 멈춘 경우 포함) |
| 1 | 실행 중 오류 (네트워크, 인증, IP 차단 등) |
| 2 | 설정 오류 / 잘못된 명령 |
| 3 | 실거래 확인 없음 (`CONFIRM_LIVE_TRADING`) |
| 130 | Ctrl+C 로 중단 (실거래 카운트다운 중 취소 포함) |

오류가 나면 한국어+영어 한 줄 메시지를 출력합니다. 자세한 내용은 `logs/<명령>.log` 를 보세요.

---

## 6. 실행 모드

### paper (기본, 키 불필요)

- 메인넷 **공개** 시세(캔들, 펀딩비, 마크가격)만 읽고 체결은 로컬에서 시뮬레이션합니다. 백테스트와 같은 체결 모델(슬리피지, 수수료, 손절/익절, 펀딩비)을 씁니다.
- API 클라이언트를 **키 없이** 만들기 때문에 서명 요청(주문)이 구조적으로 불가능합니다.
- 페이퍼 잔고와 포지션은 DB 에 저장되어 재시작해도 이어집니다. 처음부터 다시 하려면 `trade --reset-paper`.

### testnet (바이낸스 데모 트레이딩)

- `https://demo-fapi.binance.com` 에 진짜 주문 흐름을 보냅니다(가짜 돈). 데모 키가 필요합니다([7장](#7-테스트넷데모-api-키-발급)).
- 데모 계정이 헤지 모드/멀티에셋 모드라면 봇이 자동으로 단방향/단일 자산 모드로 바꿉니다(데모 계정에서만).
- 데모 시세는 메인넷과 다릅니다. 전략 연구와 백테스트는 메인넷 데이터로 하고, 데모는 주문 흐름 검증용으로 쓰세요.
- 첫 실행 후 데모 화면에서 진입 포지션에 **STOP_MARKET 조건부(알고) 주문이 "포지션 종료(close position)" 로** 걸려 있는지 확인하세요. 만약 데모가 closePosition 을 무시한다면 `execution.protective_mode: reduce_only` 로 바꾸세요.

### live (실거래, 이중 확인)

실거래는 **두 가지를 모두** 해야 실행됩니다.

1. `config.yaml` 에 `mode: live` (명령줄 `--mode live` 는 거부됩니다)
2. 실행하는 **바로 그 PowerShell 창**에서:

   ```powershell
   $env:CONFIRM_LIVE_TRADING = "YES"
   & $py -m bot trade
   ```

   (정확히 대문자 `YES`. `.env` 에 적어도 인정되지 않습니다. 창을 닫으면 사라집니다.)

- 둘 중 하나라도 없으면 네트워크 요청 없이 종료 코드 3 으로 끝납니다.
- 시작하면 실거래 경고 배너를 보여주고 **10초 카운트다운**을 합니다. 이때 Ctrl+C 를 누르면 아무 주문도 보내지 않고 종료합니다(종료 코드 130).
- 실거래 키는 **출금 권한을 절대 켜지 말고**, 가능하면 IP 제한을 거세요.
- [실행 창](#0-가장-쉬운-사용법--더블클릭-실행-창)에서는 "실거래"를 고르고 확인 창에 `실거래` 를 입력하는 것으로 같은 두 가지를 대신합니다(설정 파일을 `mode: live` 로 바꾸고, 확인값은 그 프로세스에만 전달).

#### 실거래 체크리스트

1. **테스트넷에서 먼저 며칠 돌려 보세요.** 진입 → 거래소 손절/익절 주문 → 청산 흐름이 데모 화면에서 정상인지 확인합니다([테스트넷](#testnet-바이낸스-데모-트레이딩)). 이 봇의 실거래 주문 코드는 테스트넷과 같은 코드입니다.
2. **API 키**: binance.com → API 관리 → API 만들기. "선물 거래 허용(Enable Futures)"만 켜고 **출금 허용은 끄세요.** 가능하면 IP 접근 제한을 거세요. 키는 실행 창의 "실거래 API 키" 버튼(또는 `.env` 의 `BINANCE_API_KEY`/`BINANCE_API_SECRET`)으로 넣습니다.
3. **선물 지갑에는 봇에 맡길 금액만** 넣으세요. 수량 계산의 "자산"은 USDT-M 선물 지갑 전체(마진 잔고)입니다. 예: 1,000 USDT, 위험 2% → 손절 1번에 약 20 USDT 손실.
4. 바이낸스 선물 설정이 **단방향(One-way) 모드**, **단일 자산 모드**인지 확인하세요(아니면 봇이 시작하지 않고 알려 줍니다).
5. 같은 심볼에 **직접 잡아 둔 포지션/주문이 없게** 하세요. 봇이 모르는 포지션을 발견하면 넘겨받아 손절을 겁니다.
6. 처음에는 **안정형/기본형 + 작은 금액**으로 시작하고, 며칠간 대시보드와 바이낸스 화면의 주문/체결이 일치하는지 확인한 뒤 위험도를 올리세요.

---

## 7. 테스트넷(데모) API 키 발급

1. 일반 바이낸스 계정으로 **https://demo.binance.com** 에 로그인합니다(데모 계정 활성화를 요청하면 진행). 예전 주소 `testnet.binancefuture.com` 은 여기로 이동합니다.
2. 오른쪽 위 계정 아이콘 → **API 관리(API Management)** 로 갑니다. 바로가기: `https://demo.binance.com/en/my/settings/api-management`
3. **API 생성(Create API)** 을 누르고 이름을 붙여 생성합니다(시스템 생성 HMAC 키 권장).
4. **API 키와 시크릿을 바로 저장**하세요. 시크릿은 한 번만 보여줍니다.
5. **출금(Withdraw) 권한은 켜지 마세요.** 가능하면 IP 제한을 거세요.
6. `.env` 에 넣습니다:

   ```dotenv
   BINANCE_TESTNET_API_KEY=발급받은_키
   BINANCE_TESTNET_API_SECRET=발급받은_시크릿
   ```

7. 실행: `& $py -m bot trade --mode testnet --once`

> **데모 키는 메인넷에서 동작하지 않고, 메인넷 키는 데모에서 동작하지 않습니다.** 둘은 완전히 별개입니다. 화면의 정확한 메뉴 이름은 바이낸스 개편에 따라 조금 다를 수 있습니다.

---

## 8. 리스크 관리

### 위험도 프리셋

실행 창의 "위험도"(또는 `backtest --profile`, `compare`)로 고르는 4가지 묶음입니다. 바뀌는 값은 아래 네 가지뿐이고 나머지 `risk` 값은 그대로입니다.

| 프리셋 | 레버리지 | 손절 1회 손실 (`risk_per_trade_pct`) | 하루 손실 한도 | 익절 | 손절 10번 연속이면 |
|---|---|---|---|---|---|
| 안정형 `conservative` | 2배 | 자산의 0.5% | 3% | 2R | 약 -5% |
| 기본형 `standard` (기본값) | 3배 | 1% | 5% | 2R | 약 -10% |
| 공격형 `aggressive` | 5배 | 2% | 8% | 3R | 약 -18% |
| 초공격형 `very_aggressive` | 10배 | 3% | 12% | 없음(반대 신호까지 보유) | 약 -26% |

- **수익과 손실의 크기를 정하는 것은 레버리지가 아니라 "손절 1회 손실(%)"입니다.** 이 봇은 손절이 나면 정확히 자산의 그 %만 잃도록 수량을 계산하므로, 같은 거래에서 2%는 1%보다 이익도 손실도 약 2배가 됩니다.
- 레버리지는 같은 포지션에 묶이는 증거금과 강제청산까지의 거리만 바꿉니다. 레버리지가 높을수록 급락/급등 때 손절 전에 강제청산될 위험이 커집니다(봇은 청산가가 손절가보다 충분히 멀 때만 진입합니다).
- 전략에 실제 우위가 없으면 위험도를 올려도 손실만 빨라집니다. 고르기 전에 `compare` 로 과거 결과(특히 최대 낙폭)를 확인하세요.

### 기본 구조

- **격리 마진(ISOLATED)**: 포지션마다 증거금이 분리되어 강제청산되어도 그 포지션의 증거금만 잃습니다. 봇이 심볼을 격리 마진으로 설정합니다.
- **단방향(One-way) 모드**: 롱/숏을 동시에 갖지 않습니다. 포지션 전환은 "청산 → 실제로 포지션이 없는지 확인 → 반대 방향 진입" 순서입니다.
- **레버리지 상한**: `risk.leverage ≤ risk.max_leverage ≤ 20`(코드 절대 상한). 포지션이 열린 상태에서는 레버리지를 바꾸지 않고, 포지션이 없을 때 설정값을 적용합니다.
- **위험 % 기반 수량 계산**: 한 번 손절될 때의 손실(진입·손절 양쪽 수수료와 슬리피지 포함)이 자산의 `risk_per_trade_pct`% 가 되도록 수량을 정합니다. 예: 자산 10,000 USDT, 1% → 손절 시 약 100 USDT 손실. 다만 다음 상한 중 가장 작은 값으로 제한됩니다.
  - 증거금 상한: 명목가 ≤ 자산 × `max_margin_fraction` × 레버리지
  - 명목가 상한: `max_position_notional` (testnet/live 에서는 거래소의 레버리지 구간 최대 명목가가 더 작으면 그 값)
  - 최소 주문 금액(BTCUSDT 50 USDT)에 못 미치면 **수량을 키우지 않고 진입을 포기**합니다.
- **청산가 거리 점검**: 보수적으로 근사한 청산가가 손절가보다 충분히 멀지 않으면(`min_liq_distance_multiple`) 진입하지 않습니다.

### 손절/익절 주문

- 진입 직후 손절(필수)과 익절(`take_profit_r` 설정 시)을 **거래소 알고(조건부) 주문**(`STOP_MARKET` / `TAKE_PROFIT_MARKET`, 마크가격 기준, 포지션 전체 종료)으로 등록합니다. 2025-12 이후 바이낸스는 일반 주문 API 로 스탑 주문을 받지 않으므로(`-4120`) 알고 주문 API 만 사용합니다.
- **손절 주문을 걸지 못하면 즉시 시장가로 청산**하고, 재시작할 때까지 신규 진입을 멈춥니다. 청산마저 실패하면 10초마다 계속 청산을 재시도합니다.
- **봉 사이에도 점검**: testnet/live 에서는 `heartbeat_sec`(최대 60초)마다 내 손절 주문이 살아있는지 확인하고, 없으면 새 손절을 먼저 건 뒤 옛 주문을 취소합니다(보호 공백 없음).
- 봇이 모르는 포지션(수동 진입, 봇 재시작 중 체결 등)을 발견하면 그 포지션을 넘겨받아 손절/익절을 붙이고 경고 이벤트를 남깁니다.

### 진입 차단 장치

- **일일 손실 킬스위치**: 자산이 **전날 마지막 자산** 대비 `max_daily_loss_pct`% 이상 줄면 그날은 신규 진입을 막고, `kill_switch_flatten: true` 면 보유 포지션을 시장가로 청산합니다. 매일 **UTC 00:00 = 한국 시간 오전 9:00** 에 초기화됩니다.
- **쿨다운**: 손절/강제청산 후 `cooldown_bars_after_stop` 개 봉 동안 신규 진입을 하지 않습니다.
- **STOP 파일**: `data/STOP` 파일을 만들면 신규 진입만 멈춥니다. 기존 포지션과 거래소 손절/익절은 그대로 유지됩니다. 파일을 지우면 다시 진입합니다.

  ```powershell
  New-Item -ItemType File data\STOP     # 신규 진입 중지
  Remove-Item data\STOP                 # 재개
  ```

- 위 장치들은 **신규 진입만** 막습니다. 청산과 손절 점검은 항상 동작합니다.

### Ctrl+C (종료)

- Ctrl+C(또는 Ctrl+Break)를 누르면 **진행 중인 주문 절차(진입 → 손절 등록 등)는 끝까지 마친 뒤** 종료합니다(대기 중이면 1초 안에 종료).
- **포지션과 거래소의 손절/익절 주문은 그대로 남습니다.** 봇이 꺼져 있어도 거래소가 손절을 실행합니다. 다시 실행하면 상태를 맞춰 이어서 관리합니다. 포지션을 정리하고 싶다면 바이낸스 화면에서 직접 청산하세요.

### 꼭 알아둘 점

- **`price_protect` 기본값이 false 인 이유**: true 로 하면 마크가격과 최종 체결가격이 크게 벌어질 때(BTCUSDT 기준 5%) 바이낸스가 손절 발동을 막습니다. 그런 급변 상황이 바로 손절이 가장 필요한 때입니다. 트리거 기준이 이미 마크가격(`MARK_PRICE`)이라 순간적인 꼬리에는 반응하지 않으므로 false 를 권장합니다.
- **`max_position_notional` 은 절대 상한**입니다. 자산이 아무리 커져도 포지션이 이 값 이상 커지지 않으므로 복리 효과가 제한됩니다. 백테스트 결과의 "**명목가 상한 적용 진입**" 수로 얼마나 자주 이 상한에 걸렸는지 확인하세요.
- **live 모드에서는 계정 전체 설정을 봇이 바꾸지 않습니다.** 계정이 헤지 모드이거나 멀티에셋 모드이면 오류 메시지를 보여주고 멈춥니다. 바이낸스 선물 화면의 설정에서 포지션 모드를 **단방향(One-way)** 으로, 멀티에셋 모드를 **끔(단일 자산 모드)** 으로 직접 바꾼 뒤 다시 실행하세요(포지션 모드는 UM/CM 모두에 적용됩니다).
- **봇은 자기 주문만 취소합니다.** 주문 ID 가 `bot_id-심볼태그-` (예: `mab1-4314-`)로 시작하는 주문만 자기 것으로 봅니다. 수동 주문이나 다른 프로그램의 주문은 건드리지 않고 이벤트로만 알립니다.
  - 같은 계정에서 봇을 여러 개 돌린다면 `execution.bot_id` 를 봇마다 다르게 하세요.
  - **같은 계정, 같은 심볼에 봇 2개는 지원하지 않습니다.**
  - 한 PC 에서 트레이더 프로세스는 한 번에 하나만 실행됩니다(`data/trader.lock`).

---

## 9. 전략 추가 방법

1. `config.yaml` 이 있는 폴더에 `user_strategies` 폴더를 만들고 `user_strategies\my_rsi.py` 를 작성합니다.

```python
from __future__ import annotations

from typing import Any

import pandas as pd

from bot.errors import ConfigError
from bot.models import Signal, SignalAction
from bot.strategy import Strategy, register


@register
class MyRsi(Strategy):
    name = "my_rsi"  # config 의 strategy.name 에 쓸 이름

    @classmethod
    def default_params(cls) -> dict[str, Any]:
        return {"period": 14, "lower": 30, "upper": 70}

    def validate_params(self) -> None:
        p = self.params
        if not isinstance(p["period"], int) or p["period"] < 2:
            raise ConfigError("my_rsi.period must be an integer >= 2")
        if not 0 < p["lower"] < p["upper"] < 100:
            raise ConfigError("my_rsi requires 0 < lower < upper < 100")

    @property
    def warmup_bars(self) -> int:
        return 3 * self.params["period"] + 1

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()  # 입력을 수정하지 말 것
        n = self.params["period"]
        delta = out["close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        out["rsi"] = 100 - 100 / (1 + gain / loss)  # 과거 값만 사용 (미래 참조 금지)
        return out

    def signal_at(self, prepared: pd.DataFrame, i: int) -> Signal:
        t = int(prepared["open_time"].iloc[i])
        price = float(prepared["close"].iloc[i])
        if i < max(self.warmup_bars - 1, 1):
            return Signal(SignalAction.NONE, t, price, "warmup")
        prev, cur = prepared["rsi"].iloc[i - 1], prepared["rsi"].iloc[i]
        if pd.isna(prev) or pd.isna(cur):
            return Signal(SignalAction.NONE, t, price, "warmup")
        meta = {"rsi": float(cur)}
        if prev < self.params["lower"] <= cur:
            return Signal(SignalAction.LONG, t, price, "rsi_cross_up", meta)
        if prev > self.params["upper"] >= cur:
            return Signal(SignalAction.SHORT, t, price, "rsi_cross_down", meta)
        return Signal(SignalAction.NONE, t, price, "no_signal", meta)
```

2. `config.yaml` 에 모듈을 등록하고 전략을 선택합니다. **`params` 에는 그 전략이 아는 키만** 둘 수 있으므로 전략을 바꿀 때는 `params` 도 통째로 바꾸세요(모르는 키는 오류).

```yaml
strategy:
  name: my_rsi
  extra_modules: ["user_strategies.my_rsi"]
  params:
    period: 14
    lower: 30
    upper: 70
```

3. 백테스트로 확인합니다: `& $py -m bot backtest --param period=21`

규칙:

- 전략은 **마감된 캔들만** 받고 포지션을 모릅니다. `LONG`/`SHORT`/`CLOSE`/`NONE` 신호만 내면 봇이 포지션에 맞게 진입/청산/전환을 결정합니다(`CLOSE` 는 롱 청산 의미).
- `prepare` 는 **인과적**이어야 합니다: 행 i 의 값은 행 0..i 만으로 계산하세요(`shift(-1)`, 가운데 정렬 rolling 등 미래 참조 금지).
- `signal_at(prepared, i)` 는 행 i 마감 시점의 신호이며, 워밍업 전이나 값이 NaN 이면 `NONE`("warmup")을 돌려줘야 합니다.
- **같은 코드가 백테스트와 실거래에서 그대로 쓰입니다**(백테스트: `prepare` 한 번 후 `signal_at(i)` 반복, 실거래: 최근 캔들로 `generate`). 지수이동평균처럼 초기값에 영향을 받는 지표는 `warmup_bars` 를 넉넉히 잡으세요(봇은 워밍업의 2배 이상 캔들을 받아 계산합니다).

---

## 10. 백테스트 가정과 한계

가정:

- **체결 시점**: 봉 마감 종가에서 신호 → **다음 봉 시가**에 시장가 체결. 진입·청산 모두 불리한 방향으로 `slippage_bps` 슬리피지를 적용하고 taker 수수료를 냅니다.
- **손절/익절**: 봉 안에서 다음 순서로 판정합니다.
  1. 시가 갭 규칙: 시가가 이미 손절가를 넘어 열리면 **시가에** 손절 체결, 시가가 익절가를 넘어 열리면 시가에 익절 체결.
  2. 그다음 같은 봉 안에서 손절과 익절이 모두 닿았다면 **손절이 먼저**라고 가정합니다(보수적).
  3. 손절/익절 체결에도 슬리피지와 taker 수수료를 적용합니다.
- **펀딩비**: 포지션 보유 중 지난 펀딩 시각마다 `수량 × 마크가격 × 펀딩비율` 을 반영합니다(+ = 지불, − = 수령). 진입 시각과 같은 펀딩은 내지 않고, 청산 시각과 같은 펀딩은 냅니다. 펀딩 캐시가 기간 전체를 덮지 않으면 경고합니다(`--no-funding` 으로 끌 수 있음).
- **강제청산 근사**: 보수적으로 근사한 청산가에 닿으면 강제청산으로 처리하고 **격리 증거금 전부 + 수수료**를 잃은 것으로 계산합니다. 펀딩비로 인한 청산가 이동과 거래소 청산 수수료는 백테스트에 **반영하지 않습니다.**
- 킬스위치, 쿨다운, 수량 계산(위험 %, 증거금/명목가 상한, 최소 주문 금액)은 실거래와 같은 코드로 동작합니다.
- 데이터 마지막 봉에서 포지션이 남아 있으면 종가로 청산합니다("백테스트 종료").
- 항상 **메인넷** 공개 데이터를 사용합니다(데모 시세는 실제 시장과 다름).

한계:

- **과최적화 주의**: 파라미터를 과거 데이터에 맞출수록 미래 성과는 나빠지기 쉽습니다. 기간을 나눠 검증하고, 페이퍼와 테스트넷에서 충분히 확인하세요.
- 호가창 깊이, 부분 체결, 주문 지연, 거래소 장애, 레버리지 구간(최대 명목가)은 모델링하지 않습니다.
- 거래소 점검 등으로 빠진 캔들은 채우지 않습니다(경고만 함).
- 과거 성과는 미래 수익을 보장하지 않습니다.

---

## 11. 대시보드 사용법

```powershell
& $py -m bot dashboard            # 기본: http://127.0.0.1:8000
& $py -m bot dashboard --port 8080
```

브라우저에서 `http://127.0.0.1:8000` 을 엽니다. 종료는 Ctrl+C.

- **읽기 전용**입니다. 주문, 설정 변경 같은 조작 기능이 없고 GET 요청만 받습니다. DB 도 읽기 전용으로 엽니다.
- **127.0.0.1(로컬) 전용**입니다. `0.0.0.0` 같은 외부 주소로는 실행되지 않으며(종료 코드 2), 다른 Host 헤더로 들어온 요청은 거부합니다(400, DNS 리바인딩 방지).
- 트레이더(`trade`)와 별도 프로세스입니다. 다른 PowerShell 창에서 실행하세요. 트레이더가 같은 DB(`data/bot.db`)에 쓰는 내용을 `refresh_sec` 초마다 자동으로 읽어옵니다.
- DB 가 아직 없으면 빈 화면으로 표시되고, DB 나 폴더를 새로 만들지 않습니다. `trade` 나 `backtest` 를 한 번 실행하면 내용이 나타납니다.
- 화면 구성: 봇 상태(모드, 상태, 하트비트, 잔고, 마지막 신호, 진입 차단 사유) · 포지션 · 보호 주문 · 캔들 차트(▲ 롱 진입 / ▼ 숏 진입 / ● 청산 사유) · 자산 곡선 · 거래 내역 · 최근 이벤트 · 백테스트 결과(행을 클릭하면 지표, 자산 곡선, 거래 상세).
- 하트비트가 `max(3 × heartbeat_sec, 90)` 초보다 오래되면 빨간 "**응답 없음**" 으로 표시됩니다(트레이더가 꺼졌거나 멈춤).
- 시간은 모두 **한국 시간(KST)** 으로 표시합니다. 거래 내역의 펀딩비는 + 가 지불, − 가 수령입니다.
- 차트는 CDN(jsdelivr)의 lightweight-charts 라이브러리를 씁니다. 인터넷이 안 되면 "차트 라이브러리를 불러오지 못했습니다" 가 표시되지만 표와 상태는 정상 동작합니다. 서버가 꺼지면 "서버 연결 실패" 배지가 뜨고, 서버가 다시 켜지면 자동으로 이어집니다.

---

## 12. 테스트 실행

```powershell
# 단위 테스트 (네트워크를 쓰지 않음, 네트워크 테스트는 자동 제외)
& $py -m pytest

# 경고를 오류로 취급해 더 엄격하게
& $py -m pytest -W error -q

# 바이낸스 공개 API 연결 테스트 (키 불필요, 공개 GET 만 호출)
& $py -m pytest -m network

# 특정 파일만
& $py -m pytest tests\test_dashboard.py -q
```

단위 테스트는 외부 네트워크 접속이 차단된 상태로 실행되며, API 키나 실제 주문을 쓰지 않습니다.

---

## 13. 문제 해결

| 증상 / 메시지 | 원인과 해결 |
|---|---|
| `-1021` "Timestamp for this request is outside of the recvWindow" | PC 시계가 바이낸스와 어긋났습니다. [3장](#3-시계-동기화)대로 Windows 시계를 동기화하세요. 봇은 자동으로 다시 맞추고 한 번 재시도합니다. 계속되면 시계 동기화가 켜져 있는지 확인하세요. |
| `-4120` STOP_ORDER_SWITCH_ALGO | 스탑/익절 주문을 일반 주문 API 로 보냈다는 뜻입니다. 이 봇은 알고 주문 API(`/fapi/v1/algoOrder`)만 쓰므로 정상 버전에서는 나오지 않습니다. 코드를 수정했거나 다른 프로그램이 보낸 주문인지 확인하세요. |
| HTTP `418` (IP 차단) | 요청 한도 초과(429)가 반복되어 바이낸스가 IP 를 일시 차단했습니다(2분~최대 3일). 봇은 즉시 멈춥니다(종료 코드 1). 같은 IP 에서 바이낸스 API 를 쓰는 다른 프로그램을 끄고, 차단이 풀릴 때까지 기다린 뒤 실행하세요. 반복 재시작은 차단을 길게 만듭니다. |
| HTTP `429` (요청 한도) | 봇이 `Retry-After` 만큼 기다렸다가 계속합니다. 자주 보이면 같은 IP 의 다른 프로그램을 확인하세요. |
| `-2015` / `-2014` / `-1022` (인증 오류) | 키가 틀렸거나, IP 제한에 걸렸거나, 데모 키를 메인넷에(또는 반대로) 쓰고 있습니다. testnet 은 `BINANCE_TESTNET_*`, live 는 `BINANCE_*` 키를 씁니다. |
| 한글이 깨지거나 `UnicodeEncodeError` | 콘솔 기본 인코딩이 cp949 입니다. `$env:PYTHONUTF8 = "1"` 을 설정하고 실행하세요. 로그 파일은 UTF-8 이므로 메모장/VS Code 에서 UTF-8 로 여세요. |
| `file is locked by another program: ... (엑셀 등에서 파일을 열어두었다면 닫고 다시 실행하세요)` | 엑셀 등에서 CSV(`data/klines/...csv`, `trades.csv` 등)를 열어 두어 파일을 바꿀 수 없습니다. 그 프로그램을 닫고 다시 실행하세요. |
| `another trader instance is running (...trader.lock)` | 다른 `trade` 프로세스가 이미 실행 중입니다(모드 무관, 하나만 허용). 그 창에서 Ctrl+C 로 멈추거나 작업 관리자에서 `python.exe` 를 확인하세요. 프로세스가 끝나면 잠금은 자동으로 풀립니다(파일은 남아 있어도 됩니다). |
| `account is in Hedge mode ...` / `multi-assets mode is on ...` (live) | live 모드는 계정 설정을 바꾸지 않습니다. 바이낸스 선물 설정에서 단방향 모드 / 단일 자산 모드로 직접 바꾸세요. |
| `no cached exchange filters; run: python -m bot download ...` | `--offline` 백테스트에 필요한 거래 규칙 캐시가 없습니다. 먼저 `download` 를 한 번 실행하세요. |
| `no cached funding ... use --no-funding` | `--offline` 인데 펀딩비 캐시가 없습니다. `download` 를 실행하거나 `--no-funding` 을 쓰세요. |
| 종료 코드 3, "실거래(live) 모드는 이중 확인이 필요합니다 ... / Live trading requires two opt-ins" | 실거래 이중 확인이 안 되었습니다(`mode: live` + 같은 창의 `$env:CONFIRM_LIVE_TRADING = "YES"`). [6장](#live-실거래-이중-확인) 참고. |
| `dashboard must bind to localhost only` | 대시보드는 `127.0.0.1` / `localhost` / `::1` 에서만 실행됩니다. |
| 대시보드가 비어 있음 | 아직 `trade`/`backtest` 를 실행하지 않았거나, 대시보드가 다른 설정 파일(다른 `db_path`)을 보고 있습니다. 같은 `-c` 설정으로 실행하세요. |
| `Activate.ps1` 실행 오류 (실행 정책) | 활성화하지 말고 `.\.venv\Scripts\python.exe` 를 직접 쓰세요([2장](#powershell-사용-팁-중요)). |
| `unknown config key: ...` | `config.yaml` 에 오타가 있습니다. 메시지의 키 경로를 확인하세요. |

자세한 원인은 `logs\<명령>.log` 에 있습니다. 더 자세히 보려면 `--log-level DEBUG` 로 실행하세요.

---

## 14. 면책 조항

- 이 소프트웨어는 **교육 및 연구 목적**으로 "있는 그대로(AS IS)" 제공되며, 어떠한 명시적·묵시적 보증도 하지 않습니다.
- **본 소프트웨어는 투자 조언이 아니며, 모든 거래의 책임은 사용자에게 있습니다.** 이 소프트웨어의 사용, 오류, 지연, 거래소 장애, 설정 실수로 인해 발생한 어떠한 손실에 대해서도 개발자는 책임지지 않습니다.
- 암호화폐 선물 거래는 원금 전액 손실 위험이 있습니다. 거주 국가의 법규와 바이낸스 이용 약관을 확인하고 준수하는 것은 사용자의 책임입니다.
- 백테스트와 페이퍼 결과는 실제 결과와 다를 수 있습니다. 실거래는 충분한 테스트 후, 잃어도 되는 금액으로만 하세요.
