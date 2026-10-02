"""차트 분석기 창(GUI). 더블클릭으로 켜고 버튼으로 조작합니다."""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, font, messagebox, scrolledtext, ttk

from . import analyzer, settings
from .imaging import Capture, ClipboardSource, grab_clipboard_image, load_image
from .report import BIAS_LABEL, list_reports, write_index
from .worker import AnalysisWorker, Failed, Finished, Job, Started

EFFORT_OPTIONS = (("빠르게 (저렴)", "medium"), ("보통 (추천)", "high"), ("꼼꼼하게 (느림)", "xhigh"))
CLIPBOARD_POLL_MS = 1000
EVENT_POLL_MS = 200
KOREAN_FONTS = ("Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR", "NanumGothic", "WenQuanYi Zen Hei")

STATUS_COLORS = {
    "ready": ("#0f8a5f", "#e3f5ec"),
    "busy": ("#2f5bd3", "#e6ecfb"),
    "paused": ("#5b6573", "#eceff2"),
    "warn": ("#b35c00", "#fdf0e1"),
}

HELP_TEXT = """\
[사용 방법]
1. 이 창을 켜 두면 '자동 분석'이 켜져 있습니다.
2. 차트 화면에서 Win + Shift + S 를 누르고, 분석할 부분을 마우스로 드래그하세요.
3. 30초~2분 뒤 분석 보고서가 인터넷 창(브라우저)으로 열립니다.

[잘 분석되게 캡처하는 요령]
- 오른쪽 가격 숫자(가격축)가 보이게 캡처하세요.
- 종목 이름과 봉 간격(예: BTCUSDT 4h)도 같이 캡처하면 보고서에 들어갑니다.
- RSI, MACD 같은 보조지표는 화면에 켜 둔 것만 분석합니다.
- '메모' 칸에 상황을 적으면 참고해서 분석합니다. 예) 롱 진입 고민 중

[그 밖에]
- 이미 저장된 사진은 '이미지 파일 선택' 버튼으로 분석할 수 있습니다.
- 지난 보고서는 아래 목록을 더블클릭하거나 '보고서 폴더 열기'로 볼 수 있습니다.
- 분석 1장마다 Claude API 요금(대략 0.1~0.2달러)이 듭니다. '빠르게'를 고르면 더 저렴합니다.
- 보고서는 참고 자료이며 투자 조언이 아닙니다.
"""


def open_path(path: Path) -> None:
    """파일이나 폴더를 윈도우 기본 프로그램으로 엽니다."""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - 사용자가 연 보고서 파일
    else:
        webbrowser.open(path.resolve().as_uri())


def _pick_font_family(root: tk.Misc) -> str | None:
    available = set(font.families(root))
    return next((name for name in KOREAN_FONTS if name in available), None)


class ApiKeyDialog(tk.Toplevel):
    """API 키를 붙여 넣고, 실제로 쓸 수 있는 키인지 확인한 뒤 .env 에 저장합니다."""

    def __init__(
        self,
        master: tk.Misc,
        on_saved: Callable[[], None],
        check: Callable[[str], str | None] = analyzer.check_api_key,
        first_run: bool = False,
    ) -> None:
        super().__init__(master)
        self.title("API 키 설정")
        self.resizable(False, False)
        self.transient(master)
        self._on_saved = on_saved
        self._check = check
        self._results: queue.Queue[str | None] = queue.Queue()
        self.key = tk.StringVar()
        self.show = tk.BooleanVar(value=False)

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        intro = (
            "처음 한 번만 하면 됩니다. 차트를 분석하려면 Claude API 키가 필요해요.\n\n"
            if first_run
            else ""
        )
        ttk.Label(
            body,
            text=intro
            + "1. 아래 [Claude 콘솔 열기] 를 눌러 로그인합니다.\n"
            "2. 왼쪽 메뉴의 'API Keys' 에서 'Create Key' 를 누릅니다.\n"
            "3. 만들어진 키(sk-ant- 로 시작)를 복사해서 아래 칸에 붙여 넣습니다 (Ctrl+V).\n"
            "4. [저장] 을 누르면 키가 맞는지 확인하고 저장합니다.\n\n"
            "※ 분석 요금이 나가므로 콘솔의 'Billing' 에서 크레딧을 먼저 충전해 두세요.",
            justify="left",
        ).pack(anchor="w")
        ttk.Button(body, text="Claude 콘솔 열기", command=lambda: webbrowser.open(settings.API_KEY_PAGE)).pack(
            anchor="w", pady=(10, 12)
        )

        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, text="API 키").pack(side="left")
        self.entry = ttk.Entry(row, textvariable=self.key, width=52, show="•")
        self.entry.pack(side="left", padx=8, fill="x", expand=True)
        ttk.Checkbutton(row, text="보이기", variable=self.show, command=self._toggle_show).pack(side="left")

        self.message = ttk.Label(body, text="", foreground="#c8382e", wraplength=520, justify="left")
        self.message.pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        self.cancel_btn = ttk.Button(buttons, text="취소", command=self.destroy)
        self.cancel_btn.pack(side="right")
        self.save_btn = ttk.Button(buttons, text="저장", command=self.save)
        self.save_btn.pack(side="right", padx=8)

        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.entry.focus_set()
        self._center_over(master)
        self.after(50, self._grab_input)

    def _center_over(self, master: tk.Misc) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + max(0, (master.winfo_width() - self.winfo_reqwidth()) // 2)
        y = master.winfo_rooty() + max(0, (master.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")

    def _grab_input(self) -> None:
        """다른 창을 누르지 못하게 이 창에 입력을 모읍니다(창이 아직 안 그려졌으면 생략)."""
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _toggle_show(self) -> None:
        self.entry.configure(show="" if self.show.get() else "•")

    def save(self) -> None:
        key = self.key.get().strip()
        if not key:
            self.message.configure(text="키를 붙여 넣어 주세요.")
            return
        if not settings.looks_like_api_key(key) and not messagebox.askyesno(
            "키 형식 확인",
            "보통 API 키는 'sk-ant-' 로 시작합니다.\n붙여 넣은 내용이 맞는지 확인해 주세요.\n\n그래도 이 값으로 확인해 볼까요?",
            parent=self,
        ):
            return
        self.save_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.message.configure(text="키를 확인하는 중입니다... (요금은 나가지 않습니다)", foreground="#2f5bd3")
        threading.Thread(target=self._run_check, args=(key,), daemon=True).start()
        self.after(100, self._wait_result, key)

    def _run_check(self, key: str) -> None:
        try:
            self._results.put(self._check(key))
        except Exception as exc:  # noqa: BLE001
            self._results.put(f"키를 확인하지 못했습니다: {exc}")

    def _wait_result(self, key: str) -> None:
        try:
            problem = self._results.get_nowait()
        except queue.Empty:
            self.after(100, self._wait_result, key)
            return
        if problem:
            self.message.configure(text=problem, foreground="#c8382e")
            self.save_btn.configure(state="normal")
            self.cancel_btn.configure(state="normal")
            return
        try:
            settings.save_api_key(key, settings.ENV_PATH)
        except OSError as exc:
            self.message.configure(text=f".env 파일에 저장하지 못했습니다: {exc}", foreground="#c8382e")
            self.save_btn.configure(state="normal")
            self.cancel_btn.configure(state="normal")
            return
        self.destroy()
        self._on_saved()


class ChartAnalyzerApp:
    def __init__(
        self,
        root: tk.Tk,
        *,
        worker: AnalysisWorker | None = None,
        clipboard: ClipboardSource | None = None,
        out_dir: Path | None = None,
        check_key: Callable[[str], str | None] = analyzer.check_api_key,
        ask_key_on_start: bool = True,
    ) -> None:
        self.root = root
        self.out_dir = out_dir or settings.REPORTS_DIR
        self.worker = worker or AnalysisWorker(self.out_dir)
        self.clipboard = clipboard or ClipboardSource()
        self.check_key = check_key
        self.pending = 0
        self.key_dialog: ApiKeyDialog | None = None

        self.watching = tk.BooleanVar(value=True)
        self.note = tk.StringVar()
        self.effort = tk.StringVar(value="high")
        self.auto_open = tk.BooleanVar(value=True)

        self._setup_fonts()
        self._build()
        self.clipboard.prime()
        self.refresh_reports()
        self.log("차트 분석기를 시작했습니다. 차트 화면에서 Win + Shift + S 로 캡처해 보세요.")
        self.update_status()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self._on_callback_error
        root.after(CLIPBOARD_POLL_MS, self._poll_clipboard)
        root.after(EVENT_POLL_MS, self._poll_events)
        if ask_key_on_start and not settings.has_api_key():
            root.after(300, lambda: self.open_key_dialog(first_run=True))

    # ------------------------------------------------------------------ 화면 구성

    def _setup_fonts(self) -> None:
        family = _pick_font_family(self.root)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            f = font.nametofont(name, root=self.root)
            if family:
                f.configure(family=family)
            f.configure(size=10)
        self.big_font = font.Font(root=self.root, family=family or "TkDefaultFont", size=12, weight="bold")

    def _build(self) -> None:
        root = self.root
        root.title("차트 분석기")
        root.geometry("780x760")
        root.minsize(640, 600)

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)

        self.status = tk.Label(outer, text="", font=self.big_font, anchor="w", padx=14, pady=12)
        self.status.pack(fill="x")
        self.progress = ttk.Progressbar(outer, mode="indeterminate")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 4))
        self.watch_btn = ttk.Button(actions, command=self.toggle_watch, width=18)
        self.watch_btn.pack(side="left")
        ttk.Button(actions, text="클립보드 이미지 분석", command=self.analyze_clipboard).pack(side="left", padx=8)
        ttk.Button(actions, text="이미지 파일 선택...", command=self.analyze_files).pack(side="left")

        options = ttk.LabelFrame(outer, text=" 분석 설정 ", padding=10)
        options.pack(fill="x", pady=(10, 0))
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="메모 (선택)").grid(row=0, column=0, sticky="w")
        ttk.Entry(options, textvariable=self.note).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        ttk.Label(options, text="예) 롱 진입 고민 중, 4시간봉 기준", foreground="#5b6573").grid(
            row=1, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Label(options, text="분석 깊이").grid(row=2, column=0, sticky="w", pady=(8, 0))
        efforts = ttk.Frame(options)
        efforts.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        for label, value in EFFORT_OPTIONS:
            ttk.Radiobutton(efforts, text=label, value=value, variable=self.effort).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(options, text="분석이 끝나면 보고서를 바로 열기", variable=self.auto_open).grid(
            row=3, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )

        reports = ttk.LabelFrame(outer, text=" 최근 보고서 (더블클릭하면 열립니다) ", padding=10)
        reports.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(reports, columns=("created", "title", "bias"), show="headings", height=7)
        self.tree.heading("created", text="작성 시각")
        self.tree.heading("title", text="제목")
        self.tree.heading("bias", text="판단")
        self.tree.column("created", width=130, stretch=False)
        self.tree.column("title", width=420)
        self.tree.column("bias", width=90, stretch=False)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(reports, orient="vertical", command=self.tree.yview)
        scroll.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self._open_selected_report)
        self.tree.bind("<Return>", self._open_selected_report)
        self._report_paths: dict[str, Path] = {}

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="보고서 폴더 열기", command=self.open_reports_folder).pack(side="left")
        ttk.Button(bottom, text="보고서 전체 목록", command=self.open_index).pack(side="left", padx=8)
        ttk.Button(bottom, text="사용 방법", command=self.show_help).pack(side="right")
        ttk.Button(bottom, text="API 키 설정", command=self.open_key_dialog).pack(side="right", padx=8)

        log_frame = ttk.LabelFrame(outer, text=" 진행 기록 ", padding=6)
        log_frame.pack(fill="both", pady=(10, 0))
        self.log_box = scrolledtext.ScrolledText(log_frame, height=8, wrap="word", state="disabled", relief="flat")
        self.log_box.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ 상태 표시

    def update_status(self) -> None:
        if not settings.has_api_key():
            kind, text = "warn", "API 키를 먼저 설정해 주세요  →  아래 [API 키 설정] 버튼"
        elif self.pending:
            waiting = f"  (기다리는 캡처 {self.pending - 1}장)" if self.pending > 1 else ""
            kind, text = "busy", f"분석 중입니다... 보통 30초~2분 걸려요{waiting}"
        elif self.watching.get():
            kind, text = "ready", "● 자동 분석 켜짐 — Win + Shift + S 로 차트를 캡처하면 바로 분석합니다"
        else:
            kind, text = "paused", "■ 자동 분석 꺼짐 — 아래 버튼으로 직접 분석할 수 있습니다"
        fg, bg = STATUS_COLORS[kind]
        self.status.configure(text=text, fg=fg, bg=bg)
        self.watch_btn.configure(text="■ 자동 분석 끄기" if self.watching.get() else "▶ 자동 분석 켜기")
        if self.pending:
            if not self.progress.winfo_manager():
                self.progress.pack(fill="x", after=self.status)
                self.progress.start(12)
        elif self.progress.winfo_manager():
            self.progress.stop()
            self.progress.pack_forget()

    def log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def refresh_reports(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._report_paths.clear()
        for entry in list_reports(self.out_dir)[:100]:
            item = self.tree.insert("", "end", values=(entry.created, entry.title, BIAS_LABEL.get(entry.bias, "-")))
            self._report_paths[item] = entry.html

    # ------------------------------------------------------------------ 동작

    def toggle_watch(self) -> None:
        self.watching.set(not self.watching.get())
        self.log("자동 분석을 켰습니다." if self.watching.get() else "자동 분석을 껐습니다.")
        self.update_status()

    def submit(self, capture: Capture) -> None:
        if not settings.has_api_key():
            self.log("API 키가 없어서 분석하지 못했습니다. [API 키 설정] 을 눌러 주세요.")
            self.open_key_dialog(first_run=True)
            return
        label = next(name for name, value in EFFORT_OPTIONS if value == self.effort.get())
        self.worker.submit(
            Job(capture=capture, note=self.note.get().strip() or None, model=analyzer.DEFAULT_MODEL, effort=self.effort.get())
        )
        self.pending += 1
        self.log(f"차트를 받았습니다 ({capture.source}) → 분석 대기열에 넣었습니다. [{label}]")
        self.update_status()

    def analyze_clipboard(self) -> None:
        image = grab_clipboard_image()
        if image is None:
            messagebox.showinfo(
                "클립보드에 이미지가 없어요",
                "먼저 차트 화면에서 Win + Shift + S 를 눌러 캡처한 뒤 다시 눌러 주세요.",
                parent=self.root,
            )
            return
        self.submit(Capture(image, "클립보드"))

    def analyze_files(self) -> None:
        names = filedialog.askopenfilenames(
            parent=self.root,
            title="분석할 차트 이미지 선택",
            filetypes=[("이미지", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"), ("모든 파일", "*.*")],
        )
        for name in names:
            path = Path(name)
            try:
                image = load_image(path)
            except OSError:
                self.log(f"이미지로 열 수 없는 파일이라 건너뜁니다: {path.name}")
                continue
            self.submit(Capture(image, path.name))

    def open_key_dialog(self, first_run: bool = False) -> None:
        if self.key_dialog is not None and self.key_dialog.winfo_exists():
            self.key_dialog.lift()
            return
        self.key_dialog = ApiKeyDialog(self.root, self._on_key_saved, check=self.check_key, first_run=first_run)

    def _on_key_saved(self) -> None:
        self.log("API 키를 저장했습니다. 이제 차트를 캡처하면 분석합니다.")
        self.update_status()

    def open_reports_folder(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        open_path(self.out_dir)

    def open_index(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        open_path(write_index(self.out_dir))

    def _open_selected_report(self, _event: object = None) -> None:
        for item in self.tree.selection():
            path = self._report_paths.get(item)
            if path is not None and path.exists():
                open_path(path)

    def show_help(self) -> None:
        messagebox.showinfo("사용 방법", HELP_TEXT, parent=self.root)

    # ------------------------------------------------------------------ 주기적 확인

    def _poll_clipboard(self) -> None:
        try:
            captures = self.clipboard.poll()  # 꺼져 있을 때도 읽어서, 켤 때 지난 캡처를 분석하지 않게 합니다
            if self.watching.get():
                for capture in captures:
                    self.submit(capture)
        finally:
            self.root.after(CLIPBOARD_POLL_MS, self._poll_clipboard)

    def _poll_events(self) -> None:
        try:
            while True:
                try:
                    event = self.worker.events.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(event)
        finally:
            self.root.after(EVENT_POLL_MS, self._poll_events)

    def handle_event(self, event: Started | Finished | Failed) -> None:
        if isinstance(event, Started):
            self.log(f"분석 시작: {event.job.capture.source}")
            return
        self.pending = max(0, self.pending - 1)
        if isinstance(event, Finished):
            a = event.result.analysis
            lines = [f"✔ 완료: {a.title}  [{BIAS_LABEL[a.overall_bias]}]"]
            lines += [f"    · {line}" for line in a.summary]
            if event.result.model != analyzer.DEFAULT_MODEL:
                lines.append(f"    (분석 모델: {event.result.model})")
            self.log("\n".join(lines))
            self.refresh_reports()
            if self.auto_open.get():
                open_path(event.saved.html)
        else:
            self.log(f"✖ 분석 실패: {event.message}")
            self.root.bell()
            if "API 키" in event.message or "크레딧" in event.message:
                messagebox.showwarning("분석하지 못했어요", event.message, parent=self.root)
        self.update_status()

    # ------------------------------------------------------------------ 종료·오류

    def on_close(self) -> None:
        if self.pending and not messagebox.askyesno(
            "분석 중", "아직 분석 중인 차트가 있습니다.\n지금 끄면 그 결과는 저장되지 않아요. 그래도 끌까요?", parent=self.root
        ):
            return
        self.root.destroy()

    def _on_callback_error(self, exc_type, exc, tb) -> None:  # noqa: ANN001 - tkinter 콜백 시그니처
        detail = "".join(traceback.format_exception(exc_type, exc, tb, limit=5))
        self.log("예상하지 못한 오류가 났습니다:\n" + detail)
        messagebox.showerror("오류", f"예상하지 못한 오류가 났습니다.\n\n{exc}", parent=self.root)


def run() -> int:
    if sys.platform == "win32":
        try:  # 고해상도 화면에서 글자가 흐리지 않게
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    settings.load_env()
    root = tk.Tk()
    try:
        ChartAnalyzerApp(root)
    except Exception:
        # 콘솔 없이(pythonw) 실행되므로 오류를 창과 파일로 남깁니다.
        detail = traceback.format_exc()
        log_path = settings.REPORTS_DIR / "오류기록.txt"
        try:
            settings.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            log_path.write_text(detail, encoding="utf-8")
        except OSError:
            pass
        messagebox.showerror("차트 분석기를 열지 못했습니다", f"{detail[-1500:]}\n\n자세한 내용: {log_path}")
        root.destroy()
        return 1
    root.mainloop()
    return 0
