"""Beginner launcher: one Korean window for backtests, paper/testnet trading and the dashboard.

Start it by double-clicking ``START_BOT.bat`` (or ``python -m bot.launcher``).

# SPEC-GAP: not part of SPEC v1. Added so the bot can be used without typing PowerShell commands. The launcher holds
# no trading logic: every action runs the regular CLI (``python -m bot ...``) as a child process and shows its
# output. Live trading is deliberately NOT offered here; it keeps the two opt-ins of §6 (config + env variable).
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

from bot.config import ENV_TESTNET_KEY, ENV_TESTNET_SECRET, AppConfig, load_config
from bot.errors import BotError, ConfigError
from bot.fsutil import atomic_write_text
from bot.models import Mode
from bot.timeutil import parse_date_ms

logger = logging.getLogger(__name__)

PROJECT_DIR: Final = Path(__file__).resolve().parent.parent
CONFIG_NAME: Final = "config.yaml"
EXAMPLE_CONFIG_NAME: Final = "config.example.yaml"
ENV_NAME: Final = ".env"
ENV_EXAMPLE_NAME: Final = ".env.example"

STOP_COMMAND: Final = "stop"  # bot.cli.STDIN_STOP_COMMAND (not imported: the CLI module pulls in pandas)
POLL_MS: Final = 100
MAX_LINES_PER_POLL: Final = 500
MAX_LOG_LINES: Final = 3000
DASHBOARD_WAIT_SEC: Final = 20.0
CLOSE_WAIT_SEC: Final = 30.0
OPEN_BROWSER: Final = "__open_browser__"
DASHBOARD_FAILED: Final = "__dashboard_failed__"

PROCESS_LABELS: Final[dict[str, str]] = {"backtest": "백테스트", "trade": "자동매매", "dashboard": "대시보드"}
MODE_LABELS: Final[dict[Mode, str]] = {Mode.PAPER: "모의매매", Mode.TESTNET: "테스트넷"}

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


def backtest_args(start: str) -> list[str]:
    text = str(start).strip()
    try:
        parse_date_ms(text)
    except ConfigError:
        raise ConfigError(f"시작일 '{text}' 을(를) 읽을 수 없습니다. 2024-01-01 처럼 입력하세요.") from None
    return ["backtest", "--start", text]


def trade_args(mode: Mode) -> list[str]:
    mode = Mode(mode)
    if mode not in (Mode.PAPER, Mode.TESTNET):
        raise ConfigError("이 화면에서는 모의매매와 테스트넷만 실행할 수 있습니다 (실거래는 README 6장 참고)")
    return ["trade", "--mode", mode.value, "--stop-on-stdin"]


def dashboard_url(host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{int(port)}/"


def exit_message(kind: str, code: int) -> str:
    if kind == "backtest":
        if code == 0:
            return "백테스트 완료! 결과 표는 위 기록에 있고, 대시보드의 '백테스트 결과'에서도 볼 수 있습니다."
        if code == 2:
            return "설정 오류로 백테스트를 시작하지 못했습니다. 위 기록의 오류 메시지를 확인하세요."
        return "백테스트가 오류로 멈췄습니다. 위 기록의 오류 메시지를 확인하세요 (인터넷 연결 확인)."
    if kind == "trade":
        if code == 0:
            return "자동매매가 멈췄습니다. 열린 포지션은 다음에 시작하면 이어서 관리합니다."
        if code == 130:
            return "자동매매가 시작 전에 중단되었습니다."
        if code == 2:
            return "설정 오류로 자동매매를 시작하지 못했습니다 (config.yaml 또는 API 키를 확인하세요)."
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


def has_testnet_keys(env_path: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Both testnet variables set (process environment or ``.env``), as ``load_credentials`` would find them."""
    env = os.environ if environ is None else environ
    values = read_env_file(env_path)

    def present(name: str) -> bool:
        return bool((env.get(name) or "").strip() or values.get(name, "").strip())

    return present(ENV_TESTNET_KEY) and present(ENV_TESTNET_SECRET)


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


def save_testnet_keys(project_dir: Path, api_key: str, api_secret: str) -> Path:
    """Write the demo-trading key pair into ``<project>/.env`` (created from ``.env.example`` when missing)."""
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
    atomic_write_text(env_path, set_env_values(base, {ENV_TESTNET_KEY: key, ENV_TESTNET_SECRET: secret}))
    return env_path


def config_summary(cfg: AppConfig) -> str:
    params = ", ".join(f"{key}={value}" for key, value in cfg.strategy.params.items())
    stop = cfg.risk.stop_loss
    stop_text = f"ATR×{stop.atr_multiple:g}" if stop.mode == "atr" else f"{stop.percent:g}%"
    take_profit = f"{cfg.risk.take_profit_r:g}R" if cfg.risk.take_profit_r is not None else "없음"
    return (
        f"{cfg.symbol} · {cfg.interval} 봉 · 전략 {cfg.strategy.name} ({params})\n"
        f"레버리지 {cfg.risk.leverage}배 · 1회 위험 {cfg.risk.risk_per_trade_pct:g}% · 손절 {stop_text} · "
        f"익절 {take_profit} · 일일 손실 한도 {cfg.risk.max_daily_loss_pct:g}%"
    )


# ---------------------------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------------------------


class ManagedProcess:
    """One child process; its merged stdout/stderr lines are put on ``sink`` as ``(name, line)``."""

    def __init__(self, name: str, sink: queue.Queue[tuple[str, str]]) -> None:
        self.name = name
        self.sink = sink
        self.proc: subprocess.Popen[str] | None = None
        self.exit_code: int | None = None
        self.stop_sent = False
        self._pump: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.exit_code is None

    def start(self, cmd: list[str], *, cwd: Path, with_stdin: bool = False) -> None:
        if self.running:
            raise BotError(f"{self.name} is already running")
        env = dict(os.environ)
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        extra: dict[str, Any] = {}
        if sys.platform == "win32":
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if with_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **extra,
        )
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
    mono = next((f for f in ("Consolas", "D2Coding", "Noto Sans Mono CJK KR", "DejaVu Sans Mono") if f in families), ui)
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
        root.geometry(f"{int(860 * factor)}x{int(800 * factor)}")
        root.minsize(int(720 * factor), int(640 * factor))
        ui_font, mono_font = _setup_fonts(root)
        style = ttk.Style(root)
        style.configure("Title.TLabel", font=(ui_font, 16, "bold"))
        style.configure("Hint.TLabel", foreground="#555555")
        style.configure("Big.TButton", font=(ui_font, 11, "bold"), padding=(14, 6))

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="바이낸스 선물 자동매매 봇", style="Title.TLabel").pack(anchor="w")
        self.summary_var = tk.StringVar(value="설정을 읽는 중...")
        ttk.Label(outer, textvariable=self.summary_var, justify="left").pack(anchor="w", pady=(6, 0))
        self.keys_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.keys_var, style="Hint.TLabel").pack(anchor="w", pady=(2, 0))

        # 1. backtest
        box = ttk.LabelFrame(outer, text=" 1. 백테스트 — 과거 데이터로 전략 시험하기 ", padding=10)
        box.pack(fill="x", pady=(12, 0))
        ttk.Label(box, text="시작일").pack(side="left")
        self.start_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.start_var, width=12).pack(side="left", padx=6)
        ttk.Label(box, text="(지난 날짜, 예: 2024-01-01)", style="Hint.TLabel").pack(side="left")
        self.backtest_btn = ttk.Button(box, text="백테스트 실행", style="Big.TButton", command=self.start_backtest)
        self.backtest_btn.pack(side="right")

        # 2. trading
        box = ttk.LabelFrame(outer, text=" 2. 자동매매 ", padding=10)
        box.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.mode_var = tk.StringVar(value=Mode.PAPER.value)
        self.mode_radios = [
            ttk.Radiobutton(row, text="모의매매 (가짜 돈 · API 키 필요 없음)", value=Mode.PAPER.value, variable=self.mode_var),
            ttk.Radiobutton(row, text="테스트넷 (바이낸스 데모 · 데모 API 키 필요)", value=Mode.TESTNET.value, variable=self.mode_var),
        ]
        for radio in self.mode_radios:
            radio.pack(side="left", padx=(0, 18))
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(10, 0))
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
                "중지해도 열린 포지션과 거래소 손절 주문은 그대로 유지됩니다. 실거래(live)는 이 화면에서 실행할 수 없습니다."
            ),
            style="Hint.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        # 3. dashboard
        box = ttk.LabelFrame(outer, text=" 3. 대시보드 — 차트 · 포지션 · 거래 내역 · 백테스트 결과 ", padding=10)
        box.pack(fill="x", pady=(12, 0))
        self.dashboard_btn = ttk.Button(box, text="대시보드 열기 (브라우저)", command=self.open_dashboard)
        self.dashboard_btn.pack(side="left")
        self.dashboard_stop_btn = ttk.Button(box, text="대시보드 끄기", command=self.stop_dashboard)
        self.dashboard_stop_btn.pack(side="left", padx=8)
        self.dashboard_var = tk.StringVar(value="")
        ttk.Label(box, textvariable=self.dashboard_var, style="Hint.TLabel").pack(side="left", padx=6)

        # settings
        box = ttk.LabelFrame(outer, text=" 설정 ", padding=10)
        box.pack(fill="x", pady=(12, 0))
        ttk.Button(box, text="테스트넷 API 키 입력", command=self.open_key_dialog).pack(side="left")
        ttk.Button(box, text="설정 파일 열기", command=self.edit_config).pack(side="left", padx=8)
        ttk.Button(box, text="설정 다시 읽기", command=self.reload_config).pack(side="left")
        ttk.Button(box, text="로그 폴더 열기", command=self.open_logs).pack(side="left", padx=8)

        # output
        box = ttk.LabelFrame(outer, text=" 실행 기록 ", padding=6)
        box.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(box, height=12, wrap="word", state="disabled", font=(mono_font, 9), relief="flat")
        scroll = ttk.Scrollbar(box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
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
        summary = config_summary(cfg)
        if cfg.mode is Mode.LIVE:
            summary += "\n설정 파일의 mode 는 live 이지만, 이 화면은 모의매매/테스트넷으로만 실행합니다."
        self.summary_var.set(summary)
        if not self.start_var.get().strip():
            self.start_var.set(cfg.backtest.start)
        keys = has_testnet_keys(cfg.base_dir / ENV_NAME)
        self.keys_var.set(
            "테스트넷 API 키: 입력됨" if keys else "테스트넷 API 키: 없음 (모의매매와 백테스트는 키 없이 됩니다)"
        )
        self._refresh_buttons()
        return cfg

    def _refresh_buttons(self) -> None:
        ok = self.cfg is not None and self.closing_since is None
        backtest, trade, dashboard = self.procs["backtest"], self.procs["trade"], self.procs["dashboard"]

        self.backtest_btn.configure(
            state="normal" if ok and not backtest.running else "disabled",
            text="백테스트 실행 중..." if backtest.running else "백테스트 실행",
        )
        self.trade_start_btn.configure(state="normal" if ok and not trade.running else "disabled")
        self.trade_stop_btn.configure(state="normal" if trade.running and not trade.stop_sent else "disabled")
        for radio in self.mode_radios:
            radio.configure(state="disabled" if trade.running else "normal")
        if trade.running and trade.stop_sent:
            self.trade_status_var.set("● 중지하는 중... (진행 중인 주문이 있으면 마친 뒤 멈춥니다)")
            self.trade_status.configure(fg="#b26a00")
        elif trade.running:
            label = MODE_LABELS.get(self.trade_mode or Mode.PAPER, "")
            self.trade_status_var.set(f"● 실행 중 ({label})")
            self.trade_status.configure(fg="#1e7b34")
        else:
            self.trade_status_var.set("● 멈춤")
            self.trade_status.configure(fg="#666666")

        self.dashboard_btn.configure(state="normal" if ok else "disabled")
        self.dashboard_stop_btn.configure(state="normal" if dashboard.running else "disabled")
        if dashboard.running and self.cfg is not None:
            self.dashboard_var.set(dashboard_url(self.cfg.dashboard.host, self.cfg.dashboard.port))
        else:
            self.dashboard_var.set("")

    def _start(self, name: str, args: list[str], *, with_stdin: bool = False) -> bool:
        try:
            config = self._config_path()
            self.procs[name].start(bot_command(config, *args), cwd=self.project_dir, with_stdin=with_stdin)
        except (BotError, OSError) as exc:
            self._note(f"{PROCESS_LABELS[name]}을(를) 시작하지 못했습니다: {exc}", "error")
            messagebox.showerror("시작 실패", f"{PROCESS_LABELS[name]}을(를) 시작하지 못했습니다.\n\n{exc}", parent=self.root)
            return False
        return True

    # -- actions -------------------------------------------------------------------------------------

    def start_backtest(self) -> None:
        if self.reload_config() is None:
            return
        try:
            args = backtest_args(self.start_var.get())
        except ConfigError as exc:
            messagebox.showwarning("시작일 확인", str(exc), parent=self.root)
            return
        if self._start("backtest", args):
            self._note(f"백테스트 시작: {args[-1]} 부터 지금까지 (처음에는 데이터 내려받기로 1~2분 걸릴 수 있어요)")
        self._refresh_buttons()

    def start_trading(self) -> None:
        cfg = self.reload_config()
        if cfg is None:
            return
        mode = Mode(self.mode_var.get())
        if mode is Mode.TESTNET and not has_testnet_keys(cfg.base_dir / ENV_NAME):
            messagebox.showinfo(
                "테스트넷 API 키 필요",
                "테스트넷으로 실행하려면 바이낸스 데모 API 키가 필요합니다.\n다음 창에서 키를 입력해 주세요.",
                parent=self.root,
            )
            self.open_key_dialog()
            return
        if self._start("trade", trade_args(mode), with_stdin=True):
            self.trade_mode = mode
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

    def open_key_dialog(self) -> None:
        KeyDialog(self)

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
            self.log(f"[{PROCESS_LABELS.get(name, name)}] {line}", tag)

    def _check_finished(self) -> None:
        changed = False
        for name, proc in self.procs.items():
            code = proc.finished()
            if code is None:
                continue
            changed = True
            message = exit_message(name, code)
            ok = code == 0
            self._note(message, "ok" if ok else "error")
            if self.closing_since is not None:
                continue
            if name == "trade" and not ok:
                messagebox.showwarning("자동매매 멈춤", message, parent=self.root)
            elif name == "backtest":
                (messagebox.showinfo if ok else messagebox.showwarning)("백테스트", message, parent=self.root)
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


class KeyDialog:
    """Demo-trading (testnet) API key entry; writes ``.env`` next to config.yaml."""

    def __init__(self, app: LauncherApp) -> None:
        self.app = app
        win = self.win = tk.Toplevel(app.root)
        win.title("테스트넷 API 키 입력")
        win.transient(app.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=(
                "https://demo.binance.com 에 로그인 → 계정 아이콘 → API 관리에서 만든\n"
                "데모 트레이딩 API 키를 붙여넣으세요. (실거래 키 아님, 출금 권한은 켜지 마세요)"
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.key_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        ttk.Label(frame, text="API Key").grid(row=1, column=0, sticky="w", pady=(12, 0))
        key_entry = ttk.Entry(frame, textvariable=self.key_var, width=60)
        key_entry.grid(row=1, column=1, sticky="we", pady=(12, 0), padx=(8, 0))
        ttk.Label(frame, text="Secret Key").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.secret_var, width=60, show="•").grid(
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
            path = save_testnet_keys(self.app.project_dir, self.key_var.get(), self.secret_var.get())
        except (ConfigError, OSError, BotError) as exc:
            messagebox.showwarning("저장 실패", str(exc), parent=self.win)
            return
        self.win.destroy()
        self.app.reload_config()
        self.app._note(f"테스트넷 API 키를 저장했습니다 ({path.name}).", "ok")


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
