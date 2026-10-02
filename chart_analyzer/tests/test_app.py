"""창(GUI) 동작 테스트. tkinter 나 화면이 없는 환경에서는 건너뜁니다."""

from __future__ import annotations

import queue

import pytest

tk = pytest.importorskip("tkinter")

from chart_analyzer import app as appmod  # noqa: E402
from chart_analyzer import settings  # noqa: E402
from chart_analyzer.imaging import Capture, ClipboardSource  # noqa: E402
from chart_analyzer.report import save_report  # noqa: E402
from chart_analyzer.worker import Failed, Finished, Started  # noqa: E402

from .conftest import make_chart_image, sample_result  # noqa: E402


class FakeWorker:
    def __init__(self) -> None:
        self.events: queue.Queue = queue.Queue()
        self.jobs: list = []

    def submit(self, job) -> None:
        self.jobs.append(job)


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError:
        pytest.skip("화면(display)이 없는 환경")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def opened(monkeypatch):
    paths = []
    monkeypatch.setattr(appmod, "open_path", paths.append)
    return paths


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-testtesttesttest")


def make_app(root, tmp_path, frames):
    worker = FakeWorker()
    clipboard = ClipboardSource(grab=lambda: next(frames, None), sequence=lambda: None)
    app = appmod.ChartAnalyzerApp(
        root, worker=worker, clipboard=clipboard, out_dir=tmp_path, check_key=lambda key: None, ask_key_on_start=False
    )
    return app, worker


def log_text(app) -> str:
    return app.log_box.get("1.0", "end")


def test_new_capture_is_submitted_with_settings(root, tmp_path, with_key, opened):
    frames = iter([None, make_chart_image(500, 300)])
    app, worker = make_app(root, tmp_path, frames)
    assert "자동 분석 켜짐" in app.status.cget("text")

    app.note.set("롱 고민")
    app.effort.set("xhigh")
    app._poll_clipboard()

    assert len(worker.jobs) == 1
    job = worker.jobs[0]
    assert (job.note, job.effort, job.model) == ("롱 고민", "xhigh", "claude-opus-5-5")
    assert "분석 중입니다" in app.status.cget("text")


def test_paused_watch_ignores_captures(root, tmp_path, with_key, opened):
    frames = iter([None, make_chart_image(500, 300)])
    app, worker = make_app(root, tmp_path, frames)
    app.toggle_watch()
    assert "자동 분석 꺼짐" in app.status.cget("text")
    app._poll_clipboard()
    assert worker.jobs == []


def test_finished_event_updates_list_log_and_opens_report(root, tmp_path, with_key, opened, chart_image):
    app, worker = make_app(root, tmp_path, iter([]))
    app.submit(Capture(chart_image, "클립보드"))
    job = worker.jobs[0]
    result = sample_result()
    from chart_analyzer.imaging import prepare_image

    saved = save_report(result, prepare_image(chart_image), tmp_path, source="클립보드")

    worker.events.put(Started(job))
    worker.events.put(Finished(job, result, saved))
    app._poll_events()

    assert "완료: BTCUSDT 4시간봉" in log_text(app)
    assert len(app.tree.get_children()) == 1
    assert opened == [saved.html]
    assert "자동 분석 켜짐" in app.status.cget("text")
    assert not app.progress.winfo_manager()  # 진행 막대도 사라짐


def test_failed_event_is_logged(root, tmp_path, with_key, opened, chart_image):
    app, worker = make_app(root, tmp_path, iter([]))
    app.submit(Capture(chart_image, "클립보드"))
    worker.events.put(Failed(worker.jobs[0], "요청 한도를 넘었습니다."))
    app._poll_events()
    assert "분석 실패: 요청 한도를 넘었습니다." in log_text(app)
    assert app.pending == 0


def test_without_key_asks_for_key_instead_of_submitting(root, tmp_path, monkeypatch, opened, chart_image):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    env = tmp_path / ".env"
    monkeypatch.setattr(settings, "ENV_PATH", env)
    app, worker = make_app(root, tmp_path, iter([]))
    assert "API 키를 먼저" in app.status.cget("text")

    app.submit(Capture(chart_image, "클립보드"))
    assert worker.jobs == []
    dialog = app.key_dialog
    assert dialog is not None

    dialog.key.set("sk-ant-api03-testtesttesttest")
    dialog.save()
    for _ in range(50):
        root.update()
        if not dialog.winfo_exists():
            break
        root.after(20)
    assert not dialog.winfo_exists()
    assert env.read_text(encoding="utf-8") == "ANTHROPIC_API_KEY=sk-ant-api03-testtesttesttest\n"
    assert "자동 분석 켜짐" in app.status.cget("text")


def test_key_dialog_shows_check_problem(root, tmp_path, monkeypatch, opened):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(settings, "ENV_PATH", tmp_path / ".env")
    dialog = appmod.ApiKeyDialog(root, on_saved=lambda: None, check=lambda key: "API 키가 올바르지 않습니다.")
    dialog.key.set("sk-ant-api03-wrongwrongwrong")
    dialog.save()
    for _ in range(50):
        root.update()
        if "올바르지" in dialog.message.cget("text"):
            break
        root.after(20)
    assert "올바르지" in dialog.message.cget("text")
    assert dialog.winfo_exists()
    assert not (tmp_path / ".env").exists()
    dialog.destroy()
