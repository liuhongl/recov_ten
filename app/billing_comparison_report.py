from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BillingBucket:
    bucket: str
    usage_key: str
    unit: str
    price_unit: str


BILLING_BUCKETS = (
    BillingBucket(
        bucket="realtime.input_text",
        usage_key="realtime.input_text_tokens",
        unit="tokens",
        price_unit="rmb_per_million_tokens",
    ),
    BillingBucket(
        bucket="realtime.input_audio",
        usage_key="realtime.input_audio_tokens",
        unit="tokens",
        price_unit="rmb_per_million_tokens",
    ),
    BillingBucket(
        bucket="realtime.output_text",
        usage_key="realtime.output_text_tokens",
        unit="tokens",
        price_unit="rmb_per_million_tokens",
    ),
    BillingBucket(
        bucket="realtime.output_audio",
        usage_key="realtime.output_audio_tokens",
        unit="tokens",
        price_unit="rmb_per_million_tokens",
    ),
    BillingBucket(
        bucket="opening.input_text",
        usage_key="opening.input_text_tokens",
        unit="tokens",
        price_unit="rmb_per_million_tokens",
    ),
    BillingBucket(
        bucket="opening.output_audio",
        usage_key="opening.output_audio_seconds",
        unit="seconds",
        price_unit="rmb_per_second",
    ),
    BillingBucket(
        bucket="file_asr.audio_seconds",
        usage_key="file_asr.audio_seconds",
        unit="seconds",
        price_unit="rmb_per_second",
    ),
)


def build_billing_report(
    *,
    sample_id: str,
    started_at: str,
    provider: str,
    scenario: str,
    model: str,
    usage: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    price_catalog: dict[str, float] | None = None,
    currency: str = "CNY",
) -> dict[str, Any]:
    prices = price_catalog or {}
    line_items = [
        _build_line_item(
            bucket,
            provider=provider,
            usage=usage,
            price_catalog=prices,
        )
        for bucket in BILLING_BUCKETS
    ]
    total = sum(
        item["rmb"]
        for item in line_items
        if item.get("billed") and isinstance(item.get("rmb"), (int, float))
    )
    return {
        "sample_id": sample_id,
        "started_at": started_at,
        "provider": provider,
        "scenario": scenario,
        "model": model,
        "usage": deepcopy(usage),
        "estimate": {
            "currency": currency,
            "total": total,
            "line_items": line_items,
        },
        "evidence": {
            "local_summary_path": "",
            "provider_request_id": "",
            "call_record_id": "",
            "console_bill_checked": False,
            **(evidence or {}),
        },
    }


def _build_line_item(
    bucket: BillingBucket,
    *,
    provider: str,
    usage: dict[str, Any],
    price_catalog: dict[str, float],
) -> dict[str, Any]:
    quantity = usage.get(bucket.usage_key)
    billed = _is_billed(bucket.bucket, provider=provider, usage=usage)
    base = {
        "bucket": bucket.bucket,
        "usage_key": bucket.usage_key,
        "quantity": quantity if _is_number(quantity) else None,
        "unit": bucket.unit,
        "price_unit": bucket.price_unit,
        "unit_price": price_catalog.get(bucket.bucket),
        "billed": billed,
        "rmb": None,
        "status": "unavailable",
    }
    if not _is_number(quantity):
        return base
    if not billed:
        return {**base, "rmb": 0, "status": "not_billed"}
    unit_price = price_catalog.get(bucket.bucket)
    if unit_price is None:
        return {**base, "status": "usage_only"}
    if bucket.unit == "tokens":
        rmb = float(quantity) * unit_price / 1_000_000
    else:
        rmb = float(quantity) * unit_price
    return {**base, "rmb": rmb, "status": "estimated"}


def _is_billed(bucket: str, *, provider: str, usage: dict[str, Any]) -> bool:
    if (
        provider == "qwen"
        and bucket == "realtime.output_text"
        and _is_number(usage.get("realtime.output_audio_tokens"))
    ):
        return False
    return True


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
