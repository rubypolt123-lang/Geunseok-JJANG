"""분석을 백그라운드 스레드에서 차례대로 처리합니다 (창이 멈추지 않도록)."""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import analyzer
from .analyzer import AnalysisResult, describe_error
from .imaging import Capture, prepare_image
from .report import SavedReport, save_report


@dataclass(frozen=True)
class Job:
    capture: Capture
    note: str | None
    model: str
    effort: str


@dataclass(frozen=True)
class Started:
    job: Job


@dataclass(frozen=True)
class Finished:
    job: Job
    result: AnalysisResult
    saved: SavedReport


@dataclass(frozen=True)
class Failed:
    job: Job
    message: str


Event = Started | Finished | Failed


class AnalysisWorker:
    """작업을 받은 순서대로 하나씩 분석하고, 결과를 events 큐에 넣습니다."""

    def __init__(self, out_dir: Path, analyze: Callable[..., AnalysisResult] = analyzer.analyze_chart) -> None:
        self.out_dir = out_dir
        self._analyze = analyze
        self._jobs: queue.Queue[Job] = queue.Queue()
        self.events: queue.Queue[Event] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="chart-analysis", daemon=True)
        self._thread.start()

    def submit(self, job: Job) -> None:
        self._jobs.put(job)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            self.events.put(Started(job))
            self.events.put(self._process(job))

    def _process(self, job: Job) -> Event:
        try:
            image = prepare_image(job.capture.image)
            result = self._analyze(image, note=job.note, model=job.model, effort=job.effort)
            saved = save_report(result, image, self.out_dir, source=job.capture.source)
        except OSError as exc:
            return Failed(job, f"이미지를 처리하거나 보고서를 저장하지 못했습니다: {exc}")
        except Exception as exc:  # noqa: BLE001 - 어떤 오류든 창에 보여 주고 다음 작업을 계속합니다
            try:
                return Failed(job, describe_error(exc))
            except Exception:  # noqa: BLE001
                return Failed(job, "예상하지 못한 오류가 났습니다.\n" + traceback.format_exc(limit=5))
        return Finished(job, result, saved)
