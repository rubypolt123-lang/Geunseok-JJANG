"""Claude API 로 차트 이미지를 분석합니다."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import anthropic
import pydantic

from .imaging import PreparedImage
from .schema import ChartAnalysis

DEFAULT_MODEL = "claude-opus-5-5"
MODEL_CHOICES = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")
EFFORT_CHOICES = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"
MAX_TOKENS = 16000

# 안전 분류기가 요청을 거절하면 서버가 Anthropic 추천 모델로 자동 재시도합니다.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

OUTPUT_FORMAT = {"type": "json_schema", "schema": anthropic.transform_schema(ChartAnalysis.model_json_schema())}

SYSTEM_PROMPT = """\
당신은 경력 15년의 기술적 분석가입니다. 사용자가 보낸 차트 캡처 이미지 한 장을 읽고, 한국어로 차트 분석 보고서를 작성합니다.

작성 원칙:
- 이미지에 실제로 보이는 것만 근거로 삼습니다. 가격, 지표 값, 종목명, 봉 간격은 화면에서 읽은 그대로 적고, 읽을 수 없는 값은 지어내지 말고 null 로 두거나 "판독 불가"라고 적습니다.
- 가격은 차트 가격축 눈금과 마지막 가격 표시를 기준으로 읽습니다. 눈금 사이를 추정했다면 근사값임이 드러나게 "약", "~" 등을 붙입니다.
- 보조지표는 화면에 표시된 것만 해석합니다. 표시되지 않은 지표(RSI, MACD 등)를 추측해서 만들지 않습니다.
- 지지·저항은 여러 번 반응한 가격대, 직전 고점·저점, 이동평균선, 추세선, 거래량이 몰린 구간을 우선합니다.
- 시나리오는 각각 진입 조건(trigger), 목표 가격대, 무효화 조건을 구체적인 가격으로 제시합니다. 확신이 낮으면 confidence 를 low 로 둡니다.
- 상승 근거와 하락 근거를 균형 있게 봅니다. 한쪽 방향으로 단정하지 않습니다.
- 문장은 짧고 명확하게, 투자 판단에 바로 참고할 수 있게 씁니다. 매수·매도 지시가 아니라 분석으로 표현합니다.
- 이미지가 가격 차트가 아니면 is_chart 를 false 로 두고, summary 에 무엇이 보이는지와 차트 캡처를 다시 보내 달라는 안내를 적습니다. 나머지 목록은 빈 배열로 둡니다.
"""


class AnalysisError(Exception):
    """분석 결과를 받지 못한 경우(거절, 출력 잘림 등)."""


@dataclass(frozen=True)
class AnalysisResult:
    analysis: ChartAnalysis
    model: str
    input_tokens: int
    output_tokens: int
    request_id: str | None


def build_user_content(image: PreparedImage, note: str | None) -> list[dict[str, Any]]:
    text = "이 차트를 분석해서 보고서를 작성해 주세요."
    if note and note.strip():
        text += f"\n\n사용자 메모(참고용 맥락): {note.strip()}"
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": base64.standard_b64encode(image.data).decode("ascii"),
            },
        },
        {"type": "text", "text": text},
    ]


def analyze_chart(
    image: PreparedImage,
    *,
    note: str | None = None,
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    client: anthropic.Anthropic | None = None,
) -> AnalysisResult:
    """차트 이미지 한 장을 분석합니다. API 오류는 anthropic 예외 그대로 올라갑니다."""
    client = client or anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": OUTPUT_FORMAT},
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": build_user_content(image, note)}],
    )

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        reason = getattr(details, "explanation", None) or "사유 미제공"
        raise AnalysisError(f"모델이 이 이미지 분석을 거절했습니다 ({reason}).")
    if response.stop_reason == "max_tokens":
        raise AnalysisError("응답이 길이 제한에 걸려 잘렸습니다. --effort 를 낮춰 다시 시도해 보세요.")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise AnalysisError(f"응답에 분석 결과가 없습니다 (stop_reason={response.stop_reason}).")
    try:
        parsed = ChartAnalysis.model_validate_json(text)
    except pydantic.ValidationError as exc:
        raise AnalysisError(f"분석 결과 형식이 올바르지 않습니다: {exc.error_count()}개 항목 오류") from exc

    return AnalysisResult(
        analysis=parsed,
        model=response.model,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        request_id=getattr(response, "_request_id", None),
    )
