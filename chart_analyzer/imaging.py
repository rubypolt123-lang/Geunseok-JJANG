"""이미지 읽기·정리, 클립보드/폴더에서 새 캡처 감지."""

from __future__ import annotations

import hashlib
import io
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageGrab

# Claude 고해상도 비전의 긴 변 최대값. 이보다 크면 줄여서 보냅니다.
MAX_LONG_EDGE = 2576
# API 이미지 1장 크기 제한(5MB, base64 기준)을 넘지 않도록 원본 바이트 기준으로 여유를 둡니다.
MAX_IMAGE_BYTES = 3_600_000

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})


@dataclass(frozen=True)
class PreparedImage:
    """API 로 보낼 준비가 된 이미지."""

    data: bytes
    media_type: str
    width: int
    height: int


def prepare_image(image: Image.Image) -> PreparedImage:
    """크기를 제한 안으로 줄이고 PNG(너무 크면 JPEG)로 인코딩합니다."""
    img = image
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        img = background

    long_edge = max(img.size)
    if long_edge > MAX_LONG_EDGE:
        scale = MAX_LONG_EDGE / long_edge
        new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    if buf.tell() <= MAX_IMAGE_BYTES:
        return PreparedImage(buf.getvalue(), "image/png", img.width, img.height)

    for quality in (92, 85, 75, 60):
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    return PreparedImage(buf.getvalue(), "image/jpeg", img.width, img.height)


def load_image(path: Path) -> Image.Image:
    with Image.open(path) as img:
        img.load()
        return img.copy()


def fingerprint(image: Image.Image) -> str:
    digest = hashlib.sha256()
    digest.update(f"{image.mode}:{image.size}".encode())
    digest.update(image.tobytes())
    return digest.hexdigest()


def grab_clipboard_image() -> Image.Image | None:
    """클립보드의 이미지를 가져옵니다. 이미지가 없거나 클립보드를 읽을 수 없으면 None.

    탐색기에서 이미지 파일을 복사(Ctrl+C)한 경우에는 첫 번째 이미지 파일을 읽습니다.
    """
    try:
        content = ImageGrab.grabclipboard()
    except (OSError, NotImplementedError):
        return None
    if isinstance(content, Image.Image):
        return content
    if isinstance(content, list):
        for name in content:
            path = Path(name)
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                try:
                    return load_image(path)
                except OSError:
                    continue
    return None


def clipboard_sequence() -> int | None:
    """윈도우 클립보드 변경 번호. 내용이 바뀔 때마다 커집니다. 윈도우가 아니면 None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return int(ctypes.windll.user32.GetClipboardSequenceNumber())
    except (OSError, AttributeError):
        return None


@dataclass
class Capture:
    image: Image.Image
    source: str  # 사람이 읽는 출처 설명. 예: "클립보드", 파일 경로


class ClipboardSource:
    """클립보드에 새 이미지가 들어오면 알려줍니다 (Win+Shift+S, PrtSc 등)."""

    def __init__(
        self,
        grab: Callable[[], Image.Image | None] = grab_clipboard_image,
        sequence: Callable[[], int | None] = clipboard_sequence,
    ) -> None:
        self._grab = grab
        self._sequence = sequence
        self._last: str | None = None
        self._last_seq: int | None = None

    def prime(self) -> None:
        """시작 시점에 이미 클립보드에 있던 이미지는 분석하지 않도록 기억해 둡니다."""
        self._last_seq = self._sequence()
        img = self._grab()
        self._last = fingerprint(img) if img is not None else None

    def poll(self) -> list[Capture]:
        # 윈도우에서는 클립보드가 바뀌지 않았으면 이미지를 읽지 않습니다(4K 캡처도 가볍게).
        seq = self._sequence()
        if seq is not None and seq == self._last_seq:
            return []
        img = self._grab()
        if img is None:
            # 다른 프로그램이 클립보드를 잡고 있었을 수도 있으니 변경 번호는 기억하지 않고 다음에 다시 봅니다.
            return []
        self._last_seq = seq
        fp = fingerprint(img)
        if fp == self._last:
            return []
        self._last = fp
        return [Capture(img, "클립보드")]


class FolderSource:
    """폴더에 새로 저장되는 이미지 파일을 감지합니다 (예: 윈도우 '사진\\스크린샷' 폴더).

    파일이 아직 쓰이는 중일 수 있으므로, 크기가 두 번 연속 같을 때 새 캡처로 봅니다.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._seen: set[Path] = set()
        self._pending: dict[Path, int] = {}

    def _image_files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)

    def prime(self) -> None:
        self._seen = set(self._image_files())
        self._pending.clear()

    def poll(self) -> list[Capture]:
        captures: list[Capture] = []
        for path in self._image_files():
            if path in self._seen:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size == 0 or self._pending.get(path) != size:
                self._pending[path] = size
                continue
            self._pending.pop(path, None)
            self._seen.add(path)
            try:
                captures.append(Capture(load_image(path), str(path)))
            except OSError:
                continue
        return captures
