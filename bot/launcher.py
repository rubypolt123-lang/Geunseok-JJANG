"""Beginner launcher: one Korean window for backtests, risk presets, paper/testnet/live trading and the dashboard.

Start it by double-clicking ``START_BOT.bat`` (or ``python -m bot.launcher``).

# SPEC-GAP: not part of SPEC v1. Added so the bot can be used without typing PowerShell commands. The launcher holds
# no trading logic: every action runs the regular CLI (``python -m bot ...``) as a child process and shows its
# output. Live trading keeps two deliberate opt-ins: ``mode: live`` is written to config.yaml only after the user
# types the confirmation word in the live dialog, and CONFIRM_LIVE_TRADING=YES is set for that one child process
# only (never in this process or any other child).
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO, Any, Final

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from dotenv import dotenv_values

from bot.config import (
    ENV_CONFIRM_LIVE,
    ENV_LIVE_KEY,
    ENV_LIVE_SECRET,
    ENV_TESTNET_KEY,
    ENV_TESTNET_SECRET,
    AppConfig,
    load_config,
)
from bot.errors import BotError, ConfigError
from bot.fsutil import atomic_write_text
from bot.models import Mode
from bot.profiles import PROFILES, RiskProfile, get_profile, matching_profile, profile_updates, update_config_file
from bot.timeutil import parse_date_ms

logger = logging.getLogger(__name__)

PROJECT_DIR: Final = Path(__file__).resolve().parent.parent
CONFIG_NAME: Final = "config.yaml"
EXAMPLE_CONFIG_NAME: Final = "config.example.yaml"
ENV_NAME: Final = ".env"
ENV_EXAMPLE_NAME: Final = ".env.example"

STOP_COMMAND: Final = "stop"  # bot.cli.STDIN_STOP_COMMAND (not imported: the CLI module pulls in pandas)
LIVE_CONFIRM_WORD: Final = "실거래"
POLL_MS: Final = 100
MAX_LINES_PER_POLL: Final = 500
MAX_LOG_LINES: Final = 3000
DASHBOARD_WAIT_SEC: Final = 20.0
CLOSE_WAIT_SEC: Final = 30.0
OPEN_BROWSER: Final = "__open_browser__"
DASHBOARD_FAILED: Final = "__dashboard_failed__"
INTERVAL_CHOICES: Final[tuple[str, ...]] = ("15m", "30m", "1h", "2h", "4h", "1d")

PROCESS_LABELS: Final[dict[str, str]] = {
    "backtest": "백테스트",
    "compare": "위험도 비교",
    "trade": "자동매매",
    "dashboard": "대시보드",
}
MODE_LABELS: Final[dict[Mode, str]] = {Mode.PAPER: "모의매매", Mode.TESTNET: "테스트넷", Mode.LIVE: "실거래"}
KEY_VARS: Final[dict[Mode, tuple[str, str]]] = {
    Mode.TESTNET: (ENV_TESTNET_KEY, ENV_TESTNET_SECRET),
    Mode.LIVE: (ENV_LIVE_KEY, ENV_LIVE_SECRET),
}

_API_VALUE_RE: Final = re.compile(r"^[A-Za-z0-9+/=_-]{16,256}$")
_ENV_ASSIGNMENT_RE: Final = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------


def python_for_children(executable: str | None = None) -> str:
    """The console interpreter: under ``pythonw.exe`` (no console) children run with ``python.exe`` next to it."""
    exe = Path(executable or sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(exe)


def bot_command(config_path: Path, *args: str, python: str | None = None) -> list[str]:
    return [python_for_children(python), "-m", "bot", "-c", str(config_path), *args]


def _checked_start(start: str) -> str:
    text = str(start).strip()
    try:
        parse_date_ms(text)
    except ConfigError:
        raise ConfigError(f"시작일 '{text}' 을(를) 읽을 수 없습니다. 2024-01-01 처럼 입력하세요.") from None
    return text


def backtest_args(start: str) -> list[str]:
    return ["backtest", "--start", _checked_start(start)]


def compare_args(start: str, intervals: str = "1h,4h") -> list[str]:
    return ["compare", "--start", _checked_start(start), "--intervals", intervals]


def trade_args(mode: Mode) -> list[str]:
    """Paper/testnet name their mode on the command line; live never does (the CLI only takes it from config.yaml)."""
    mode = Mode(mode)
    if mode is Mode.LIVE:
        return ["trade", "--stop-on-stdin"]
    return ["trade", "--mode", mode.value, "--stop-on-stdin"]


def child_env(extra: Mapping[str, str] | None = None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment of a child: UTF-8 I/O; the live confirmation only when explicitly passed in ``extra``."""
    env = dict(os.environ if base is None else base)
    env.pop(ENV_CONFIRM_LIVE, None)
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if extra:
        env.update(extra)
    return env


def dashboard_url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{int(port)}/"


def losing_streak_pct(risk_per_trade_pct: float, losses: int = 10) -> float:
    """Equity lost (in %) after ``losses`` stop-losses in a row (each one costs ``risk_per_trade_pct`` of equity)."""
    return (1.0 - (1.0 - float(risk_per_trade_pct) / 100.0) ** losses) * 100.0


def exit_message(kind: str, code: int) -> str:
    if kind == "backtest":
        if code == 0:
            return "백테스트 완료! 결과 표는 위 기록에 있고, 대시보드의 '백테스트 결과'에서도 볼 수 있습니다."
        if code == 2:
            return "설정 오류로 백테스트를 시작하지 못했습니다. 위 기록의 오류 메시지를 확인하세요."
        return "백테스트가 오류로 멈췄습니다. 위 기록의 오류 메시지를 확인하세요 (인터넷 연결 확인)."
    if kind == "compare":
        if code == 0:
            return "위험도 비교 완료! 위 기록의 비교표에서 최종 자산과 최대 낙폭을 함께 보세요."
        return "위험도 비교가 오류로 멈췄습니다. 위 기록의 오류 메시지를 확인하세요 (인터넷 연결 확인)."
    if kind == "trade":
        if code == 0:
            return "자동매매가 멈췄습니다. 열린 포지션은 다음에 시작하면 이어서 관리합니다."
        if code == 130:
            return "자동매매가 시작 전에 중단되었습니다 (주문은 보내지 않았습니다)."
        if code == 2:
            return "설정 오류로 자동매매를 시작하지 못했습니다 (config.yaml, API 키, 바이낸스 계정 설정을 확인하세요)."
        if code == 3:
            return "실거래 확인이 없어 시작하지 않았습니다."
        if code == 1:
            return "자동매매가 오류로 멈췄습니다. 위 기록이나 logs 폴더의 trade.log 를 확인하세요."
        return f"자동매매가 비정상 종료되었습니다 (종료 코드 {code})."
    if code == 0:
        return "대시보드를 껐습니다."
    return f"대시보드가 종료되었습니다 (종료 코드 {code}). 같은 포트를 다른 프로그램이 쓰고 있을 수 있습니다."


# ---------------------------------------------------------------------------------------------
# Files: config.yaml, .env
# ---------------------------------------------------------------------------------------------


def ensure_config(project_dir: Path) -> Path:
    """``config.yaml`` of the project; copied from ``config.example.yaml`` when missing."""
    config = Path(project_dir) / CONFIG_NAME
    if not config.is_file():
        example = Path(project_dir) / EXAMPLE_CONFIG_NAME
        if not example.is_file():
            raise ConfigError(f"{CONFIG_NAME} 와 {EXAMPLE_CONFIG_NAME} 가 모두 없습니다: {project_dir}")
        shutil.copyfile(example, config)
    return config


def read_env_file(path: Path) -> dict[str, str]:
    if not Path(path).is_file():
        return {}
    values = dotenv_values(path, interpolate=False, encoding="utf-8-sig")
    return {k: v for k, v in values.items() if isinstance(k, str) and isinstance(v, str)}


def has_api_keys(env_path: Path, mode: Mode, environ: Mapping[str, str] | None = None) -> bool:
    """Both variables of ``mode`` set (process environment or ``.env``), as ``load_credentials`` would find them."""
    env = os.environ if environ is None else environ
    values = read_env_file(env_path)

    def present(name: str) -> bool:
        return bool((env.get(name) or "").strip() or values.get(name, "").strip())

    key_name, secret_name = KEY_VARS[Mode(mode)]
    return present(key_name) and present(secret_name)


def validate_api_value(value: str, what: str) -> str:
    text = str(value).strip()
    if not _API_VALUE_RE.fullmatch(text):
        raise ConfigError(f"{what}: 바이낸스에서 복사한 값을 공백·따옴표 없이 그대로 붙여넣으세요.")
    return text


def set_env_values(text: str, values: Mapping[str, str]) -> str:
    """``text`` with each ``NAME=...`` of ``values`` replaced in place (or appended); other lines unchanged."""
    remaining = dict(values)
    out: list[str] = []
    for line in text.splitlines():
        match = _ENV_ASSIGNMENT_RE.match(line)
        if match is not None and match.group(1) in values:
            name = match.group(1)
            if name in remaining:
                out.append(f"{name}={remaining.pop(name)}")
            continue  # a later duplicate would override the new value in python-dotenv: drop it
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.extend(f"{name}={value}" for name, value in remaining.items())
    return "\n".join(out) + "\n"


def save_api_keys(project_dir: Path, mode: Mode, api_key: str, api_secret: str) -> Path:
    """Write the key pair of ``mode`` into ``<project>/.env`` (created from ``.env.example`` when missing)."""
    key_name, secret_name = KEY_VARS[Mode(mode)]
    key = validate_api_value(api_key, "API Key")
    secret = validate_api_value(api_secret, "Secret Key")
    env_path = Path(project_dir) / ENV_NAME
    example = Path(project_dir) / ENV_EXAMPLE_NAME
    if env_path.is_file():
        base = env_path.read_text(encoding="utf-8-sig")
    elif example.is_file():
        base = example.read_text(encoding="utf-8-sig")
    else:
        base = ""
    atomic_write_text(env_path, set_env_values(base, {key_name: key, secret_name: secret}))
    return env_path


def risk_text(cfg: AppConfig) -> str:
    stop = cfg.risk.stop_loss
    stop_text = f"ATR×{stop.atr_multiple:g}" if stop.mode == "atr" else f"{stop.percent:g}%"
    take_profit = f"{cfg.risk.take_profit_r:g}R" if cfg.risk.take_profit_r is not None else "없음(추세 끝까지)"
    return (
        f"레버리지 {cfg.risk.leverage}배 · 손절 1회에 자산의 {cfg.risk.risk_per_trade_pct:g}% · 손절 거리 {stop_text} · "
        f"익절 {take_profit} · 하루 -{cfg.risk.max_daily_loss_pct:g}%면 그날 진입 중지"
    )


def config_summary(cfg: AppConfig) -> str:
    params = ", ".join(f"{key}={value}" for key, value in cfg.strategy.params.items())
    profile = matching_profile(cfg.risk)
    return (
        f"{cfg.symbol} · {cfg.interval} 봉 · 전략 {cfg.strategy.name} ({params})\n"
        f"위험도: {profile.label if profile else '사용자 설정'} — {risk_text(cfg)}"
    )


# ---------------------------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------------------------


class ManagedProcess:
    """One child process; its merged stdout/stderr lines are put on ``sink`` as ``(name, line)``."""

    def __init__(self, name: str, sink: queue.Queue[tuple[str, str]]) -> None:
        self.name = name
        self.sink = sink
        self.purpose = name  # what the current run is (e.g. "compare" in the backtest slot)
        self.proc: subprocess.Popen[str] | None = None
        self.exit_code: int | None = None
        self.stop_sent = False
        self._pump: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.exit_code is None

    def start(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        with_stdin: bool = False,
        extra_env: Mapping[str, str] | None = None,
        purpose: str | None = None,
    ) -> None:
        if self.running:
            raise BotError(f"{self.name} is already running")
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=child_env(extra_env),
            stdin=subprocess.PIPE if with_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **extra,
        )
        self.purpose = purpose or self.name
        self.exit_code = None
        self.stop_sent = False
        self._pump = threading.Thread(
            target=self._pump_output, args=(self.proc.stdout,), name=f"{self.name}-output", daemon=True
        )
        self._pump.start()

    def _pump_output(self, stream: IO[str] | None) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                self.sink.put((self.name, line.rstrip("\r\n")))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def request_stop(self) -> bool:
        """Ask a ``--stop-on-stdin`` child to stop (Ctrl+C semantics). False when it cannot be asked."""
        proc = self.proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(STOP_COMMAND + "\n")
            proc.stdin.flush()
        except (OSError, ValueError):
            return False
        self.stop_sent = True
        return True

    def kill(self) -> None:
        """Terminate at once (only for read-only/restartable work: dashboard, backtest)."""
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def finished(self) -> int | None:
        """The exit code, once: the first call after the process ended and all its output was delivered."""
        proc = self.proc
        if proc is None or self.exit_code is not None:
            return None
        code = proc.poll()
        if code is None or (self._pump is not None and self._pump.is_alive()):
            return None
        self.exit_code = code
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
        return code


def wait_for_dashboard(
    url: str,
    *,
    timeout_sec: float = DASHBOARD_WAIT_SEC,
    alive: Callable[[], bool] = lambda: True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """True once ``<url>api/health`` answers 200 (direct connection: system proxies are bypassed)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = clock() + timeout_sec
    while clock() < deadline and alive():
        try:
            with opener.open(url + "api/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        sleep(0.5)
    return False


def open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - Explorer / the associated app
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def edit_text_file(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        open_path(path)


# ---------------------------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------------------------


def _setup_fonts(root: tk.Misc) -> tuple[str, str]:
    """Korean-capable UI font (Malgun Gothic on Windows) and a monospace font for the output."""
    families = set(tkfont.families(root))
    ui = next(
        (f for f in ("Malgun Gothic", "맑은 고딕", "Noto Sans CJK KR", "NanumGothic", "Noto Sans KR") if f in families),
        tkfont.nametofont("TkDefaultFont").actual("family"),
    )
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont", "TkCaptionFont"):
        try:
            tkfont.nametofont(name).configure(family=ui, size=10)
        except tk.TclError:
            pass
    mono = next((f for f in ("D2Coding", "Consolas", "Noto Sans Mono CJK KR", "DejaVu Sans Mono") if f in families), ui)
    return ui, mono


class LauncherApp:
    def __init__(self, root: tk.Tk, project_dir: Path = PROJECT_DIR) -> None:
        self.root = root
        self.project_dir = Path(project_dir)
        self.lines: queue.Queue[tuple[str, str]] = queue.Queue()
        self.procs: dict[str, ManagedProcess] = {
            name: ManagedProcess(name, self.lines) for name in ("backtest", "trade", "dashboard")
        }
        self.cfg: AppConfig | None = None
        self.trade_mode: Mode | None = None
        self.closing_since: float | None = None
        self._close_prompted = False
        self._destroyed = False
        self._build()
        self.reload_config()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(POLL_MS, self._poll)

    # -- layout ------------------------------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title("바이낸스 선물 자동매매 봇")
        # sizes are designed at 96 DPI; scale them on high-DPI screens (fonts scale by themselves)
        try:
            factor = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        except (tk.TclError, ValueError):
            factor = 1.0
        # never larger than the screen (laptops at 125-150 % scaling): the output area shrinks instead
        max_w, max_h = root.winfo_screenwidth() - 40, root.winfo_screenheight() - 80
        root.geometry(f"{min(int(900 * factor), max_w)}x{min(int(880 * factor), max_h)}+20+10")
        root.minsize(min(int(760 * factor), max_w), min(int(640 * factor), max_h))
        ui_font, mono_font = _setup_fonts(root)
        self.ui_font = ui_font
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=(ui_font, 16, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Big.TButton", font=(ui_font, 11, "bold"), padding=(14, 5))
        style.configure("Danger.TRadiobutton", foreground="#b00020")

        outer = ttk.Frame(root, padding=(14, 10))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="바이낸스 선물 자동매매 봇", style="Title.TLabel").pack(anchor="w")
        self.summary_var = tk.StringVar(value="설정을 읽는 중...")
        ttk.Label(outer, textvariable=self.summary_var, justify="left").pack(anchor="w", pady=(4, 0))
        self.keys_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.keys_var, style="Hint.TLabel").pack(anchor="w", pady=(2, 0))

        # 1. risk preset + interval
        box = ttk.LabelFrame(outer, text=" 1. 위험도 · 봉 간격 ", padding=(10, 6))
        box.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.profile_var = tk.StringVar(value="standard")
        self.profile_radios = []
        for profile in PROFILES:
            radio = ttk.Radiobutton(
                row, text=profile.label, value=profile.key, variable=self.profile_var, command=self._show_profile
            )
            radio.pack(side="left", padx=(0, 14))
            self.profile_radios.append(radio)
        ttk.Label(row, text="봉 간격").pack(side="left", padx=(10, 4))
        self.interval_var = tk.StringVar(value="1h")
        self.interval_box = ttk.Combobox(
            row, textvariable=self.interval_var, values=INTERVAL_CHOICES, width=5, state="readonly"
        )
        self.interval_box.pack(side="left")
        self.apply_btn = ttk.Button(row, text="적용 (설정 저장)", command=self.apply_profile)
        self.apply_btn.pack(side="right")
        self.profile_desc_var = tk.StringVar()
        ttk.Label(box, textvariable=self.profile_desc_var, style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        # 2. backtest
        box = ttk.LabelFrame(outer, text=" 2. 백테스트 — 과거 데이터로 시험하기 ", padding=(10, 6))
        box.pack(fill="x", pady=(8, 0))
        ttk.Label(box, text="시작일").pack(side="left")
        self.start_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.start_var, width=12).pack(side="left", padx=6)
        ttk.Label(box, text="(지난 날짜, 예: 2024-01-01)", style="Hint.TLabel").pack(side="left")
        self.compare_btn = ttk.Button(box, text="위험도별 비교", command=self.start_compare)
        self.compare_btn.pack(side="right", padx=(8, 0))
        self.backtest_btn = ttk.Button(box, text="백테스트 실행", style="Big.TButton", command=self.start_backtest)
        self.backtest_btn.pack(side="right")

        # 3. trading
        box = ttk.LabelFrame(outer, text=" 3. 자동매매 ", padding=(10, 6))
        box.pack(fill="x", pady=(8, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.mode_var = tk.StringVar(value=Mode.PAPER.value)
        self.mode_radios = [
            ttk.Radiobutton(row, text="모의매매 (가짜 돈)", value=Mode.PAPER.value, variable=self.mode_var),
            ttk.Radiobutton(row, text="테스트넷 (데모 키)", value=Mode.TESTNET.value, variable=self.mode_var),
            ttk.Radiobutton(
                row, text="실거래 (진짜 돈!)", value=Mode.LIVE.value, variable=self.mode_var, style="Danger.TRadiobutton"
            ),
        ]
        for radio in self.mode_radios:
            radio.pack(side="left", padx=(0, 16))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        self.trade_start_btn = ttk.Button(row, text="▶  시작", style="Big.TButton", command=self.start_trading)
        self.trade_start_btn.pack(side="left")
        self.trade_stop_btn = ttk.Button(row, text="■  중지", style="Big.TButton", command=self.stop_trading)
        self.trade_stop_btn.pack(side="left", padx=8)
        self.trade_status_var = tk.StringVar(value="● 멈춤")
        self.trade_status = tk.Label(row, textvariable=self.trade_status_var, font=(ui_font, 11, "bold"), fg="#666666")
        self.trade_status.pack(side="left", padx=10)
        ttk.Label(
            box,
            text=(
                "봉이 마감될 때마다(1h 봉이면 1시간마다) 신호를 확인합니다. 그 사이에는 기록이 조용할 수 있어요.\n"
                "중지해도 열린 포지션과 거래소 손절 주문은 그대로 유지되고, 다시 시작하면 이어서 관리합니다."
            ),
            style="Hint.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        # 4. dashboard
        box = ttk.LabelFrame(outer, text=" 4. 대시보드 — 차트 · 포지션 · 거래 내역 · 백테스트 결과 ", padding=(10, 6))
        box.pack(fill="x", pady=(8, 0))
        self.dashboard_btn = ttk.Button(box, text="대시보드 열기 (브라우저)", command=self.open_dashboard)
        self.dashboard_btn.pack(side="left")
        self.dashboard_stop_btn = ttk.Button(box, text="대시보드 끄기", command=self.stop_dashboard)
        self.dashboard_stop_btn.pack(side="left", padx=8)
        self.dashboard_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.dashboard_var, style="Hint.TLabel").pack(side="left", padx=6)

        # settings
        box = ttk.LabelFrame(outer, text=" 설정 ", padding=(10, 6))
        box.pack(fill="x", pady=(8, 0))
        ttk.Button(box, text="테스트넷 API 키", command=lambda: self.open_key_dialog(Mode.TESTNET)).pack(side="left")
        ttk.Button(box, text="실거래 API 키", command=lambda: self.open_key_dialog(Mode.LIVE)).pack(side="left", padx=8)
        ttk.Button(box, text="설정 파일 열기", command=self.edit_config).pack(side="left")
        ttk.Button(box, text="설정 다시 읽기", command=self.reload_config).pack(side="left", padx=8)
        ttk.Button(box, text="로그 폴더", command=self.open_logs).pack(side="left")

        # output
        box = ttk.LabelFrame(outer, text=" 실행 기록 ", padding=6)
        box.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(box, height=8, wrap="none", state="disabled", font=(mono_font, 9), relief="flat")
        yscroll = ttk.Scrollbar(box, command=self.log_text.yview)
        xscroll = ttk.Scrollbar(box, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        yscroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("info", foreground="#0b5394")
        self.log_text.tag_configure("error", foreground="#b00020")
        self.log_text.tag_configure("ok", foreground="#1e7b34")

    # -- helpers -------------------------------------------------------------------------------------

    def log(self, text: str, tag: str | None = None) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", (tag,) if tag else ())
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _note(self, text: str, tag: str = "info") -> None:
        self.log(f"▶ {text}", tag)

    def _config_path(self) -> Path:
        return ensure_config(self.project_dir)

    def _show_profile(self) -> None:
        try:
            profile = get_profile(self.profile_var.get())
        except ConfigError:
            self.profile_desc_var.set("")
            return
        streak = losing_streak_pct(float(profile.risk["risk_per_trade_pct"]))
        self.profile_desc_var.set(f"{profile.label}: {profile.summary}  (손절 10번 연속이면 자산 약 -{streak:.0f}%)")

    def reload_config(self) -> AppConfig | None:
        try:
            self.cfg = load_config(self._config_path())
        except BotError as exc:
            self.cfg = None
            self.summary_var.set(f"설정 파일 오류: {exc}\n'설정 파일 열기'로 고친 뒤 '설정 다시 읽기'를 누르세요.")
            self.keys_var.set("")
            self._refresh_buttons()
            return None
        cfg = self.cfg
        self.summary_var.set(config_summary(cfg))
        if not self.start_var.get().strip():
            self.start_var.set(cfg.backtest.start)
        profile = matching_profile(cfg.risk)
        if profile is not None:
            self.profile_var.set(profile.key)
        self.interval_var.set(cfg.interval)
        self._show_profile()
        env_path = cfg.base_dir / ENV_NAME
        testnet = "입력됨" if has_api_keys(env_path, Mode.TESTNET) else "없음"
        live = "입력됨" if has_api_keys(env_path, Mode.LIVE) else "없음"
        self.keys_var.set(f"API 키 — 테스트넷: {testnet} · 실거래: {live}   (모의매매와 백테스트는 키 없이 됩니다)")
        self._refresh_buttons()
        return cfg

    def _refresh_buttons(self) -> None:
        ok = self.cfg is not None and self.closing_since is None
        backtest, trade, dashboard = self.procs["backtest"], self.procs["trade"], self.procs["dashboard"]

        running_label = PROCESS_LABELS.get(backtest.purpose, "백테스트")
        self.backtest_btn.configure(
            state="normal" if ok and not backtest.running else "disabled",
            text=f"{running_label} 실행 중..." if backtest.running else "백테스트 실행",
        )
        self.compare_btn.configure(state="normal" if ok and not backtest.running else "disabled")
        self.apply_btn.configure(state="normal" if ok and not trade.running else "disabled")
        for widget in (*self.profile_radios, self.interval_box):
            widget.configure(state="disabled" if trade.running else ("readonly" if widget is self.interval_box else "normal"))
        self.trade_start_btn.configure(state="normal" if ok and not trade.running else "disabled")
        self.trade_stop_btn.configure(state="normal" if trade.running and not trade.stop_sent else "disabled")
        for radio in self.mode_radios:
            radio.configure(state="disabled" if trade.running else "normal")
        if trade.running and trade.stop_sent:
            self.trade_status_var.set("● 중지하는 중... (진행 중인 주문이 있으면 마친 뒤 멈춥니다)")
            self.trade_status.configure(fg="#b26a00")
        elif trade.running:
            mode = self.trade_mode or Mode.PAPER
            self.trade_status_var.set(f"● 실행 중 ({MODE_LABELS[mode]})")
            self.trade_status.configure(fg="#b00020" if mode is Mode.LIVE else "#1e7b34")
        else:
            self.trade_status_var.set("● 멈춤")
            self.trade_status.configure(fg="#666666")

        self.dashboard_btn.configure(state="normal" if ok else "disabled")
        self.dashboard_stop_btn.configure(state="normal" if dashboard.running else "disabled")
        if dashboard.running and self.cfg is not None:
            self.dashboard_var.set(dashboard_url(self.cfg.dashboard.host, self.cfg.dashboard.port))
        else:
            self.dashboard_var.set("")

    def _start(
        self,
        name: str,
        args: list[str],
        *,
        with_stdin: bool = False,
        extra_env: Mapping[str, str] | None = None,
        purpose: str | None = None,
    ) -> bool:
        label = PROCESS_LABELS[purpose or name]
        try:
            config = self._config_path()
            self.procs[name].start(
                bot_command(config, *args), cwd=self.project_dir, with_stdin=with_stdin, extra_env=extra_env, purpose=purpose
            )
        except (BotError, OSError) as exc:
            self._note(f"{label}을(를) 시작하지 못했습니다: {exc}", "error")
            messagebox.showerror("시작 실패", f"{label}을(를) 시작하지 못했습니다.\n\n{exc}", parent=self.root)
            return False
        return True

    def _set_config(self, updates: Mapping[tuple[str, ...], Any]) -> AppConfig | None:
        try:
            cfg = update_config_file(self._config_path(), updates)
        except (BotError, OSError, ValueError) as exc:
            messagebox.showerror("설정 저장 실패", str(exc), parent=self.root)
            return None
        self.reload_config()
        return cfg

    # -- actions -------------------------------------------------------------------------------------

    def apply_profile(self) -> None:
        # read the choice first: reload_config() resets the controls to what config.yaml holds
        profile = get_profile(self.profile_var.get())
        interval = self.interval_var.get()
        cfg = self.reload_config()
        if cfg is None or self.procs["trade"].running:
            return
        self.profile_var.set(profile.key)
        self.interval_var.set(interval)
        self._show_profile()
        risk_pct = float(profile.risk["risk_per_trade_pct"])
        if risk_pct > 1.0 and not messagebox.askyesno(
            f"{profile.label} 적용",
            f"{profile.label}: {profile.summary}\n\n"
            f"손절 1번에 자산의 {risk_pct:g}%를 잃습니다. 손절이 10번 연속 나오면 자산이 약 "
            f"{losing_streak_pct(risk_pct):.0f}% 줄어듭니다 (이 전략에서 연속 손절은 흔한 일입니다).\n"
            "이익도 같은 비율로 커지지만, 먼저 '위험도별 비교'로 과거 결과를 확인하는 것을 권합니다.\n\n적용할까요?",
            parent=self.root,
        ):
            return
        updates = profile_updates(profile, cfg.risk) | {("interval",): interval}
        if self._set_config(updates) is not None:
            self._note(f"적용 완료: 위험도 {profile.label}, 봉 간격 {interval} (config.yaml 에 저장)", "ok")

    def _start_backtest_kind(self, purpose: str) -> None:
        if self.reload_config() is None:
            return
        try:
            args = backtest_args(self.start_var.get()) if purpose == "backtest" else compare_args(self.start_var.get())
        except ConfigError as exc:
            messagebox.showwarning("시작일 확인", str(exc), parent=self.root)
            return
        if self._start("backtest", args, purpose=purpose):
            what = "백테스트" if purpose == "backtest" else "위험도별 비교 (4가지 위험도 × 1h·4h 봉)"
            self._note(f"{what} 시작: {args[2]} 부터 지금까지 (처음에는 데이터 내려받기로 1~2분 걸릴 수 있어요)")
        self._refresh_buttons()

    def start_backtest(self) -> None:
        self._start_backtest_kind("backtest")

    def start_compare(self) -> None:
        self._start_backtest_kind("compare")

    def start_trading(self) -> None:
        cfg = self.reload_config()
        if cfg is None:
            return
        mode = Mode(self.mode_var.get())
        if mode is not Mode.PAPER and not has_api_keys(cfg.base_dir / ENV_NAME, mode):
            messagebox.showinfo(
                f"{MODE_LABELS[mode]} API 키 필요",
                f"{MODE_LABELS[mode]}으로 실행하려면 API 키가 필요합니다.\n다음 창에서 키를 입력해 주세요.",
                parent=self.root,
            )
            self.open_key_dialog(mode)
            return
        extra_env: dict[str, str] | None = None
        if mode is Mode.LIVE:
            dialog = LiveConfirmDialog(self, cfg)
            self.root.wait_window(dialog.win)
            if not dialog.confirmed:
                self._note("실거래 시작을 취소했습니다.")
                return
            extra_env = {ENV_CONFIRM_LIVE: "YES"}  # this child only
        # config.yaml mode follows what runs (the dashboard shows that mode); live is written only after the dialog
        if cfg.mode is not mode and self._set_config({("mode",): mode.value}) is None:
            return
        if self._start("trade", trade_args(mode), with_stdin=True, extra_env=extra_env):
            self.trade_mode = mode
            if mode is Mode.LIVE:
                self._note("실거래 시작: 10초 카운트다운 중에 '중지'를 누르면 주문 없이 취소됩니다.", "error")
            else:
                self._note(f"자동매매 시작 ({MODE_LABELS[mode]}) — 멈추려면 '중지' 버튼을 누르세요")
        self._refresh_buttons()

    def stop_trading(self) -> None:
        if self.procs["trade"].request_stop():
            self._note("중지 요청을 보냈습니다. 진행 중인 주문이 있으면 마친 뒤 멈춥니다.")
        self._refresh_buttons()

    def open_dashboard(self) -> None:
        cfg = self.reload_config()
        if cfg is None:
            return
        url = dashboard_url(cfg.dashboard.host, cfg.dashboard.port)
        dashboard = self.procs["dashboard"]
        if dashboard.running:
            webbrowser.open(url)
            return
        if not self._start("dashboard", ["dashboard"]):
            return
        self._note(f"대시보드를 켜는 중... 준비되면 브라우저가 열립니다 ({url})")
        self._refresh_buttons()

        def wait() -> None:
            ready = wait_for_dashboard(url, alive=lambda: dashboard.proc is not None and dashboard.proc.poll() is None)
            self.lines.put((OPEN_BROWSER if ready else DASHBOARD_FAILED, url))

        threading.Thread(target=wait, name="dashboard-wait", daemon=True).start()

    def stop_dashboard(self) -> None:
        self.procs["dashboard"].kill()

    def open_key_dialog(self, mode: Mode = Mode.TESTNET) -> None:
        KeyDialog(self, mode)

    def edit_config(self) -> None:
        try:
            edit_text_file(self._config_path())
        except (BotError, OSError) as exc:
            messagebox.showerror("열기 실패", str(exc), parent=self.root)
            return
        self._note("설정 파일을 고치고 저장한 뒤 '설정 다시 읽기'를 누르세요. 실행 중인 자동매매는 다시 시작해야 반영됩니다.")

    def open_logs(self) -> None:
        cfg = self.cfg or self.reload_config()
        logs = cfg.resolve_path(cfg.logging.dir) if cfg is not None else self.project_dir / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            open_path(logs)
        except OSError as exc:
            messagebox.showerror("열기 실패", str(exc), parent=self.root)

    # -- event loop ------------------------------------------------------------------------------------

    def _poll(self) -> None:
        if self._destroyed:
            return
        try:
            self._drain_output()
            self._check_finished()
            if self.closing_since is not None:
                self._continue_close()
        except tk.TclError:
            return  # the window is gone
        if not self._destroyed:
            self.root.after(POLL_MS, self._poll)

    def _destroy(self) -> None:
        self._destroyed = True
        self.root.destroy()

    def _drain_output(self) -> None:
        for _ in range(MAX_LINES_PER_POLL):
            try:
                name, line = self.lines.get_nowait()
            except queue.Empty:
                return
            if name == OPEN_BROWSER:
                self._note(f"대시보드 준비 완료: {line}", "ok")
                webbrowser.open(line)
                continue
            if name == DASHBOARD_FAILED:
                self._note("대시보드가 응답하지 않습니다. 위 기록을 확인하세요.", "error")
                continue
            lowered = line.lower()
            tag = "error" if (" error " in lowered or " critical " in lowered or "오류" in line) else None
            label = PROCESS_LABELS.get(self.procs[name].purpose if name in self.procs else name, name)
            self.log(f"[{label}] {line}", tag)

    def _check_finished(self) -> None:
        changed = False
        for name, proc in self.procs.items():
            code = proc.finished()
            if code is None:
                continue
            changed = True
            kind = proc.purpose
            message = exit_message(kind, code)
            ok = code == 0
            self._note(message, "ok" if ok else "error")
            if self.closing_since is not None:
                continue
            if name == "trade" and not ok:
                messagebox.showwarning("자동매매 멈춤", message, parent=self.root)
            elif name == "backtest":
                (messagebox.showinfo if ok else messagebox.showwarning)(PROCESS_LABELS[kind], message, parent=self.root)
        if changed:
            self._refresh_buttons()

    def on_close(self) -> None:
        trade = self.procs["trade"]
        others = [p for name, p in self.procs.items() if name != "trade" and p.running]
        if trade.running:
            if not messagebox.askyesno(
                "자동매매 실행 중",
                "자동매매가 실행 중입니다. 안전하게 멈춘 뒤 창을 닫을까요?\n\n"
                "(열린 포지션과 거래소 손절 주문은 그대로 유지되고, 다음에 시작하면 이어서 관리합니다)",
                parent=self.root,
            ):
                return
            trade.request_stop()
        elif others and not messagebox.askyesno(
            "작업 실행 중", "실행 중인 백테스트/대시보드를 끄고 닫을까요?", parent=self.root
        ):
            return
        for proc in others:
            proc.kill()
        self.closing_since = time.monotonic()
        self._note("종료하는 중...")
        self._refresh_buttons()
        self._continue_close()

    def _continue_close(self) -> None:
        if self._destroyed or self.closing_since is None:
            return
        if not self.procs["trade"].running:
            self._destroy()
            return
        if not self._close_prompted and time.monotonic() - self.closing_since > CLOSE_WAIT_SEC:
            self._close_prompted = True
            if messagebox.askyesno(
                "멈추는 중",
                "자동매매가 아직 진행 중인 작업을 마무리하고 있습니다.\n"
                "창을 지금 닫아도 봇은 그 작업을 마친 뒤 스스로 멈춥니다. 지금 닫을까요?",
                parent=self.root,
            ):
                self._destroy()


KEY_HELP: Final[dict[Mode, str]] = {
    Mode.TESTNET: (
        "https://demo.binance.com 에 로그인 → 계정 아이콘 → API 관리에서 만든\n"
        "데모 트레이딩 API 키를 붙여넣으세요. (가짜 돈 전용 키, 실거래 키와 다릅니다)"
    ),
    Mode.LIVE: (
        "binance.com → 계정 → API 관리 → API 만들기 (시스템 생성)에서 만든 실거래 키를 붙여넣으세요.\n"
        "API 제한 설정: '선물 거래 허용(Enable Futures)'만 켜고, '출금 허용'은 절대 켜지 마세요.\n"
        "가능하면 'IP 접근 제한'에 이 PC의 공인 IP를 넣으세요."
    ),
}


class KeyDialog:
    """API key entry for testnet (demo trading) or live; writes ``.env`` next to config.yaml."""

    def __init__(self, app: LauncherApp, mode: Mode = Mode.TESTNET) -> None:
        self.app = app
        self.mode = Mode(mode)
        win = self.win = tk.Toplevel(app.root)
        win.title(f"{MODE_LABELS[self.mode]} API 키 입력")
        win.transient(app.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=KEY_HELP[self.mode], justify="left").grid(row=0, column=0, columnspan=2, sticky="w")
        self.key_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        ttk.Label(frame, text="API Key").grid(row=1, column=0, sticky="w", pady=(12, 0))
        key_entry = ttk.Entry(frame, textvariable=self.key_var, width=64)
        key_entry.grid(row=1, column=1, sticky="we", pady=(12, 0), padx=(8, 0))
        ttk.Label(frame, text="Secret Key").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.secret_var, width=64, show="•").grid(
            row=2, column=1, sticky="we", pady=(8, 0), padx=(8, 0)
        )
        ttk.Label(frame, text="키는 이 PC의 .env 파일에만 저장되고, 로그에는 기록되지 않습니다.", style="Hint.TLabel").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="저장", command=self.save).pack(side="left")
        ttk.Button(buttons, text="취소", command=win.destroy).pack(side="left", padx=(8, 0))
        win.bind("<Return>", lambda _e: self.save())
        win.bind("<Escape>", lambda _e: win.destroy())
        key_entry.focus_set()
        win.grab_set()

    def save(self) -> None:
        try:
            path = save_api_keys(self.app.project_dir, self.mode, self.key_var.get(), self.secret_var.get())
        except (ConfigError, OSError, BotError) as exc:
            messagebox.showwarning("저장 실패", str(exc), parent=self.win)
            return
        self.win.destroy()
        self.app.reload_config()
        self.app._note(f"{MODE_LABELS[self.mode]} API 키를 저장했습니다 ({path.name}).", "ok")


class LiveConfirmDialog:
    """Shows exactly what will trade with real money; starts only after the confirmation word is typed."""

    def __init__(self, app: LauncherApp, cfg: AppConfig) -> None:
        self.app = app
        self.confirmed = False
        win = self.win = tk.Toplevel(app.root)
        win.title("실거래 시작 확인")
        win.transient(app.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame, text="⚠ 실거래 — 진짜 돈으로 주문합니다", font=(app.ui_font, 14, "bold"), fg="#b00020"
        ).pack(anchor="w")
        profile = matching_profile(cfg.risk)
        details = (
            f"심볼 {cfg.symbol} · {cfg.interval} 봉 · 전략 {cfg.strategy.name}\n"
            f"위험도 {profile.label if profile else '사용자 설정'}: {risk_text(cfg)}\n"
            f"포지션 최대 {cfg.risk.max_position_notional:,.0f} USDT · 격리 마진 · 손절 주문은 바이낸스에 걸어 둡니다"
        )
        ttk.Label(frame, text=details, justify="left").pack(anchor="w", pady=(10, 0))
        notes = (
            "• 바이낸스 선물 지갑의 USDT 전체가 '자산'입니다. 봇에 맡길 금액만 선물 지갑에 넣으세요.\n"
            f"  (예: 선물 지갑 1,000 USDT 이면 손절 1번에 약 {1000 * cfg.risk.risk_per_trade_pct / 100:,.0f} USDT 손실)\n"
            f"• {cfg.symbol} 에 직접 잡아 둔 포지션이 있으면 봇이 넘겨받아 손절을 겁니다. 먼저 정리하세요.\n"
            "• 선물 설정이 단방향(One-way) 모드 · 단일 자산 모드여야 합니다 (아니면 봇이 멈추고 알려 줍니다).\n"
            "• 시작 후 10초 카운트다운 중에 '중지'를 누르면 아무 주문 없이 취소됩니다.\n"
            "• 과거 성과는 미래 수익을 보장하지 않습니다. 잃어도 되는 금액으로만 하세요."
        )
        ttk.Label(frame, text=notes, justify="left", style="Hint.TLabel").pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(frame)
        row.pack(anchor="w", pady=(14, 0))
        ttk.Label(row, text=f"계속하려면 '{LIVE_CONFIRM_WORD}' 를 입력하세요:").pack(side="left")
        self.word_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.word_var, width=12)
        entry.pack(side="left", padx=8)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(14, 0))
        self.start_btn = ttk.Button(buttons, text="실거래 시작", command=self.confirm, state="disabled")
        self.start_btn.pack(side="left")
        ttk.Button(buttons, text="취소", command=win.destroy).pack(side="left", padx=(8, 0))
        self.word_var.trace_add("write", lambda *_: self._update())
        win.bind("<Escape>", lambda _e: win.destroy())
        entry.focus_set()
        win.grab_set()

    def _update(self) -> None:
        self.start_btn.configure(state="normal" if self.word_var.get().strip() == LIVE_CONFIRM_WORD else "disabled")

    def confirm(self) -> None:
        if self.word_var.get().strip() != LIVE_CONFIRM_WORD:
            return
        self.confirmed = True
        self.win.destroy()


def _enable_windows_dpi_awareness() -> None:
    """Sharp (not bitmap-stretched) text on high-DPI Windows screens; must run before ``tk.Tk()``."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def main() -> int:
    _enable_windows_dpi_awareness()
    root = tk.Tk()

    def report(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logger.error("launcher error: %s", text)
        messagebox.showerror("예기치 않은 오류", f"{exc_type.__name__}: {exc}", parent=root)

    root.report_callback_exception = report
    try:
        LauncherApp(root)
    except Exception as exc:  # started with pythonw: there is no console, so say it in a window
        messagebox.showerror("실행 실패", f"{type(exc).__name__}: {exc}", parent=root)
        root.destroy()
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
