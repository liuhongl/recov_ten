from __future__ import annotations

import pytest

from app.billing_comparison_report import build_billing_report


def test_billing_report_includes_all_buckets_and_marks_missing_usage():
    report = build_billing_report(
        sample_id="qwen-billing-001",
        started_at="2026-06-14T00:00:00Z",
        provider="qwen",
        scenario="full_call",
        model="qwen3-omni-flash-realtime",
        usage={
            "realtime.input_text_tokens": 100,
            "realtime.output_text_tokens": 50,
            "realtime.output_audio_tokens": 300,
            "file_asr.audio_seconds": 8,
        },
        evidence={
            "local_summary_path": "artifacts/qwen/summary.json",
            "provider_request_id": "req-1",
            "console_bill_checked": False,
        },
        price_catalog={
            "realtime.input_text": 2.2,
            "realtime.output_audio": 75.1,
            "file_asr.audio_seconds": 0.01,
        },
    )

    assert report["sample_id"] == "qwen-billing-001"
    assert report["provider"] == "qwen"
    assert report["scenario"] == "full_call"
    assert report["model"] == "qwen3-omni-flash-realtime"
    assert report["usage"]["realtime.output_audio_tokens"] == 300
    assert report["evidence"]["console_bill_checked"] is False

    line_items = {
        item["bucket"]: item for item in report["estimate"]["line_items"]
    }
    assert list(line_items) == [
        "realtime.input_text",
        "realtime.input_audio",
        "realtime.output_text",
        "realtime.output_audio",
        "opening.input_text",
        "opening.output_audio",
        "file_asr.audio_seconds",
    ]
    assert line_items["realtime.input_text"]["status"] == "estimated"
    assert line_items["realtime.input_audio"]["status"] == "unavailable"
    assert line_items["opening.input_text"]["status"] == "unavailable"
    assert line_items["opening.output_audio"]["status"] == "unavailable"
    assert line_items["realtime.output_text"]["status"] == "not_billed"
    assert line_items["realtime.output_text"]["billed"] is False
    assert line_items["realtime.output_text"]["rmb"] == 0
    assert line_items["file_asr.audio_seconds"]["unit"] == "seconds"
    assert report["estimate"]["total"] == pytest.approx(
        (100 * 2.2 + 300 * 75.1) / 1_000_000 + 8 * 0.01
    )
