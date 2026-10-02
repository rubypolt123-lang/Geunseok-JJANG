from __future__ import annotations

import io
import os

from PIL import Image

from chart_analyzer import imaging
from chart_analyzer.imaging import (
    ClipboardSource,
    FolderSource,
    fingerprint,
    prepare_image,
)

from .conftest import make_chart_image


def test_prepare_keeps_small_image_as_png(chart_image):
    prepared = prepare_image(chart_image)
    assert prepared.media_type == "image/png"
    assert (prepared.width, prepared.height) == chart_image.size
    assert Image.open(io.BytesIO(prepared.data)).size == chart_image.size


def test_prepare_shrinks_long_edge_to_limit():
    big = make_chart_image(3840, 2160)
    prepared = prepare_image(big)
    assert max(prepared.width, prepared.height) == imaging.MAX_LONG_EDGE
    assert prepared.height == round(2160 * imaging.MAX_LONG_EDGE / 3840)


def test_prepare_flattens_transparency():
    rgba = Image.new("RGBA", (100, 50), (255, 0, 0, 0))
    prepared = prepare_image(rgba)
    decoded = Image.open(io.BytesIO(prepared.data))
    assert decoded.mode == "RGB"
    assert decoded.getpixel((10, 10)) == (255, 255, 255)


def test_prepare_falls_back_to_jpeg_when_png_too_large(monkeypatch):
    noisy = Image.frombytes("RGB", (300, 300), os.urandom(300 * 300 * 3))  # PNG 로 압축이 거의 안 됨
    monkeypatch.setattr(imaging, "MAX_IMAGE_BYTES", 200_000)
    prepared = prepare_image(noisy)
    assert prepared.media_type == "image/jpeg"
    assert len(prepared.data) <= 200_000


def test_clipboard_source_ignores_existing_and_detects_new(chart_image):
    other = make_chart_image(400, 300)
    sequence = iter([chart_image, chart_image, None, other, other])
    source = ClipboardSource(grab=lambda: next(sequence))
    source.prime()  # 시작 시점에 있던 이미지
    assert source.poll() == []  # 같은 이미지 → 무시
    assert source.poll() == []  # 클립보드 비어 있음
    captures = source.poll()  # 새 캡처
    assert len(captures) == 1
    assert fingerprint(captures[0].image) == fingerprint(other)
    assert source.poll() == []  # 같은 캡처를 두 번 분석하지 않음


def test_clipboard_source_handles_empty_clipboard():
    source = ClipboardSource(grab=lambda: None)
    source.prime()
    assert source.poll() == []


def test_grab_clipboard_reads_copied_image_file(monkeypatch, tmp_path, chart_image):
    path = tmp_path / "chart.png"
    chart_image.save(path)
    monkeypatch.setattr(imaging.ImageGrab, "grabclipboard", lambda: [str(tmp_path / "note.txt"), str(path)])
    grabbed = imaging.grab_clipboard_image()
    assert grabbed is not None and grabbed.size == chart_image.size


def test_grab_clipboard_returns_none_when_unavailable(monkeypatch):
    def boom():
        raise NotImplementedError("no clipboard")

    monkeypatch.setattr(imaging.ImageGrab, "grabclipboard", boom)
    assert imaging.grab_clipboard_image() is None


def test_folder_source_waits_for_stable_new_file(tmp_path, chart_image):
    chart_image.save(tmp_path / "old.png")
    source = FolderSource(tmp_path)
    source.prime()

    new = tmp_path / "Screenshot 2026-10-02.png"
    chart_image.save(new)
    (tmp_path / "memo.txt").write_text("not an image", encoding="utf-8")
    assert source.poll() == []  # 처음 보면 크기만 기록
    captures = source.poll()  # 크기가 그대로면 분석 대상
    assert [c.source for c in captures] == [str(new)]
    assert source.poll() == []


def test_folder_source_waits_while_file_is_growing(tmp_path, chart_image):
    source = FolderSource(tmp_path)
    source.prime()
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG partial")
    assert source.poll() == []
    chart_image.save(path)  # 크기가 바뀜 → 아직 쓰는 중으로 판단
    os.utime(path)
    assert source.poll() == []
    assert len(source.poll()) == 1


def test_folder_source_missing_folder_is_quiet(tmp_path):
    source = FolderSource(tmp_path / "missing")
    source.prime()
    assert source.poll() == []
