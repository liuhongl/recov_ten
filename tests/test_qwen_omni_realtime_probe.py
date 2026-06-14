from __future__ import annotations

from app.qwen_omni_realtime_client import (
    QwenOmniRealtimeProbeResult,
    QwenOmniRealtimeUsage,
)
from app.qwen_omni_realtime_probe import build_realtime_billing_report_from_result


def test_build_realtime_billing_report_from_probe_result():
    result = QwenOmniRealtimeProbeResult(
        session_id="sess-1",
        model="qwen3-omni-flash-realtime",
        voice="Cherry",
        input_audio_bytes=640,
        output_audio_bytes=1280,
        input_transcript="你好。",
        output_transcript="您好。",
        usage=QwenOmniRealtimeUsage(
            input_text_tokens=10,
            input_audio_tokens=40,
            output_text_tokens=5,
            output_audio_tokens=35,
        ),
        cost_estimate_rmb=None,
        cost_line_items={},
        event_counts={},
        sanitized_events=[],
        first_audio_delta_ms=12,
        response_done_ms=34,
    )

    report = build_realtime_billing_report_from_result(
        result,
        sample_id="qwen-realtime-001",
        started_at="2026-06-14T00:00:00Z",
        local_summary_path="artifacts/qwen/summary.json",
    )

    assert report["scenario"] == "realtime"
    assert report["usage"]["realtime.input_text_tokens"] == 10
    assert report["usage"]["realtime.output_audio_tokens"] == 35
    assert report["evidence"]["local_summary_path"] == "artifacts/qwen/summary.json"
    output_text = {
        item["bucket"]: item for item in report["estimate"]["line_items"]
    }["realtime.output_text"]
    assert output_text["status"] == "not_billed"
    assert output_text["billed"] is False
