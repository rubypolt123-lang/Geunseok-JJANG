"""chart_analyzer 테스트 공용 픽스처. 네트워크(실제 API 호출)는 쓰지 않습니다."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from chart_analyzer.analyzer import AnalysisResult
from chart_analyzer.schema import ChartAnalysis


def make_chart_image(width: int = 640, height: int = 360) -> Image.Image:
    """간단한 캔들 차트 모양의 테스트 이미지."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    price = height // 2
    for i in range(20, width - 20, 24):
        step = ((i * 7) % 23) - 11
        top, bottom = sorted((price, price + step))
        color = "green" if step < 0 else "red"
        draw.line([(i + 6, top - 8), (i + 6, bottom + 8)], fill=color)
        draw.rectangle([i, top, i + 12, bottom + 1], fill=color)
        price = max(40, min(height - 40, price + step))
    return img


def sample_analysis(**overrides) -> ChartAnalysis:
    data = {
        "is_chart": True,
        "title": "BTCUSDT 4시간봉 — 저항 돌파 시도",
        "instrument": "BTCUSDT",
        "timeframe": "4시간",
        "chart_type": "캔들",
        "current_price": "64,150",
        "summary": ["고점과 저점을 높이는 상승 구조", "64,200 저항을 세 번째 시험 중", "RSI 68 로 과열 직전"],
        "overall_bias": "bullish",
        "confidence": "medium",
        "trend": {
            "short_term": "bullish",
            "short_term_comment": "최근 20봉 동안 저점을 높였습니다.",
            "medium_term": "neutral",
            "medium_term_comment": "전체 구간은 60,000~64,200 박스권입니다.",
            "structure": "박스 상단 재시험, 상승 추세선 유지",
        },
        "key_levels": [
            {"kind": "resistance", "price": "64,200", "strength": "high", "basis": "세 번 막힌 박스 상단"},
            {"kind": "support", "price": "62,800", "strength": "medium", "basis": "직전 눌림 저점 | EMA50"},
        ],
        "patterns": [
            {"name": "상승 삼각형", "status": "forming", "signal": "bullish", "description": "수평 저항과 상승 추세선"},
        ],
        "indicators": [{"name": "RSI(14)", "reading": "68, 과매수 직전", "signal": "neutral"}],
        "volume_analysis": "돌파 시도 구간에서 거래량이 늘고 있습니다.",
        "scenarios": [
            {
                "direction": "bullish",
                "likelihood": "medium",
                "trigger": "64,200 위 4시간봉 종가 마감",
                "targets": ["65,500", "67,000"],
                "invalidation": "62,800 아래 마감",
                "description": "박스 상단 돌파 후 측정 목표까지 상승.",
            },
            {
                "direction": "bearish",
                "likelihood": "low",
                "trigger": "62,800 이탈",
                "targets": ["61,500"],
                "invalidation": "64,200 회복",
                "description": "돌파 실패 후 박스 하단으로 회귀.",
            },
        ],
        "risks": ["돌파 실패 시 급락 가능"],
        "watch_points": ["돌파 봉의 거래량"],
        "limitations": ["가격축 눈금 사이 값은 근사치입니다."],
    }
    data.update(overrides)
    return ChartAnalysis.model_validate(data)


def sample_result(**overrides) -> AnalysisResult:
    return AnalysisResult(
        analysis=sample_analysis(**overrides),
        model="claude-opus-5-5",
        input_tokens=3200,
        output_tokens=1800,
        request_id="req_test",
    )


@pytest.fixture
def chart_image() -> Image.Image:
    return make_chart_image()
