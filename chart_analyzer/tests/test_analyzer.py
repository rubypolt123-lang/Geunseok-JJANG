from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from chart_analyzer import analyzer
from chart_analyzer.analyzer import AnalysisError, analyze_chart
from chart_analyzer.imaging import prepare_image

from .conftest import sample_analysis


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def fake_client(response) -> SimpleNamespace:
    return SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(response)))


def fake_response(*, text: str | None, stop_reason="end_turn", model="claude-opus-5-5", stop_details=None):
    content = [SimpleNamespace(type="thinking", thinking="")]
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        stop_details=stop_details,
        model=model,
        usage=SimpleNamespace(input_tokens=3000, output_tokens=1500),
        _request_id="req_123",
    )


def test_request_shape(chart_image):
    image = prepare_image(chart_image)
    client = fake_client(fake_response(text=sample_analysis().model_dump_json()))

    result = analyze_chart(image, note="롱 진입 고민 중", effort="xhigh", client=client)

    call = client.beta.messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == [analyzer.FALLBACK_BETA]
    assert call["fallbacks"] == "default"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "xhigh"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"]["additionalProperties"] is False
    image_block, text_block = call["messages"][0]["content"]
    assert image_block["source"]["media_type"] == "image/png"
    assert base64.standard_b64decode(image_block["source"]["data"]) == image.data
    assert "롱 진입 고민 중" in text_block["text"]

    assert result.analysis.instrument == "BTCUSDT"
    assert result.model == "claude-opus-5-5"
    assert (result.input_tokens, result.output_tokens, result.request_id) == (3000, 1500, "req_123")


def test_note_is_optional(chart_image):
    content = analyzer.build_user_content(prepare_image(chart_image), "  ")
    assert "사용자 메모" not in content[1]["text"]


def test_fallback_model_is_reported(chart_image):
    client = fake_client(fake_response(text=sample_analysis().model_dump_json(), model="claude-opus-4-8"))
    result = analyze_chart(prepare_image(chart_image), client=client)
    assert result.model == "claude-opus-4-8"


def test_refusal_raises(chart_image):
    details = SimpleNamespace(explanation="policy")
    client = fake_client(fake_response(text="", stop_reason="refusal", stop_details=details))
    with pytest.raises(AnalysisError, match="거절"):
        analyze_chart(prepare_image(chart_image), client=client)


def test_truncated_output_raises(chart_image):
    client = fake_client(fake_response(text='{"is_chart": true, "title": "BTC', stop_reason="max_tokens"))
    with pytest.raises(AnalysisError, match="잘렸습니다"):
        analyze_chart(prepare_image(chart_image), client=client)


def test_invalid_json_raises(chart_image):
    client = fake_client(fake_response(text='{"is_chart": true}'))
    with pytest.raises(AnalysisError, match="형식"):
        analyze_chart(prepare_image(chart_image), client=client)


def test_missing_text_block_raises(chart_image):
    client = fake_client(fake_response(text=None))
    with pytest.raises(AnalysisError, match="분석 결과가 없습니다"):
        analyze_chart(prepare_image(chart_image), client=client)


def test_wire_request_through_real_sdk(chart_image):
    """실제 SDK 를 거쳐 나가는 HTTP 요청 모양을 확인합니다 (네트워크 대신 MockTransport)."""
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "sig"},
                    {"type": "text", "text": sample_analysis().model_dump_json()},
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    client = anthropic.Anthropic(
        api_key="sk-test",
        base_url="https://api.anthropic.com",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    result = analyze_chart(prepare_image(chart_image), client=client)

    assert analyzer.FALLBACK_BETA in captured["headers"]["anthropic-beta"]
    body = captured["body"]
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["schema"]["required"][0] == "is_chart"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert result.analysis.title.startswith("BTCUSDT")
