from __future__ import annotations

import queue

from chart_analyzer.analyzer import AnalysisError
from chart_analyzer.imaging import Capture
from chart_analyzer.worker import AnalysisWorker, Failed, Finished, Job, Started

from .conftest import sample_result


def collect(worker: AnalysisWorker, count: int) -> list:
    return [worker.events.get(timeout=10) for _ in range(count)]


def job(chart_image, source="클립보드") -> Job:
    return Job(capture=Capture(chart_image, source), note="메모", model="claude-opus-5-5", effort="high")


def test_worker_analyzes_in_order_and_saves(tmp_path, chart_image):
    seen = []

    def analyze(image, **kwargs):
        seen.append(kwargs)
        return sample_result()

    worker = AnalysisWorker(tmp_path, analyze=analyze)
    worker.submit(job(chart_image, "첫번째"))
    worker.submit(job(chart_image, "두번째"))

    events = collect(worker, 4)
    assert [type(e) for e in events] == [Started, Finished, Started, Finished]
    assert [e.job.capture.source for e in events[::2]] == ["첫번째", "두번째"]
    assert events[1].saved.html.is_file()
    assert seen[0] == {"note": "메모", "model": "claude-opus-5-5", "effort": "high"}


def test_worker_reports_failures_and_keeps_running(tmp_path, chart_image):
    calls = iter([AnalysisError("모델이 거절했습니다"), ZeroDivisionError("boom"), None])

    def analyze(image, **kwargs):
        exc = next(calls)
        if exc is not None:
            raise exc
        return sample_result()

    worker = AnalysisWorker(tmp_path, analyze=analyze)
    for _ in range(3):
        worker.submit(job(chart_image))

    events = [e for e in collect(worker, 6) if not isinstance(e, Started)]
    assert isinstance(events[0], Failed) and events[0].message == "모델이 거절했습니다"
    assert isinstance(events[1], Failed) and "예상하지 못한 오류" in events[1].message
    assert isinstance(events[2], Finished)
    try:
        worker.events.get_nowait()
    except queue.Empty:
        pass
    else:  # pragma: no cover
        raise AssertionError("이벤트가 더 있으면 안 됩니다")
