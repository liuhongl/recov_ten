from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol

from .business_dialog_style import (
    BUSINESS_AMOUNT_DISPUTE_RULES,
    BUSINESS_COMMUNICATION_NORMS_RULES,
    BUSINESS_CRITICAL_RUNTIME_RULES,
    BUSINESS_DIALOG_SPEAKING_STYLE,
    BUSINESS_DIALOG_STYLE_RULES,
    BUSINESS_FACT_BOUNDARY_RULES,
    BUSINESS_PRIVACY_DISCLOSURE_RULES,
    BUSINESS_PROPERTY_FEE_SCENE_RULES,
    BUSINESS_RULE_PRIORITY_RULES,
)
from .config import GatewayConfig
from .opening import (
    OpeningAudioGenerator,
    OpeningAudioStore,
    OpeningGenerationFailed,
    OpeningRequest,
    build_prepared_opening_audio,
)
from .postgres import (
    BusinessPromptPreparation,
    PromptSnapshot,
    _render_business_prompt,
    _sanitize_business_strategy_text,
)

BROWSER_PROMPT_SCENE = "browser-realtime-test"
BROWSER_PROMPT_VERSION = "browser-test"
BROWSER_CALL_ID_PREFIX = "browser-"
DEFAULT_BROWSER_PROMPT_TTL_SECONDS = 1800

SAFE_BROWSER_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
PROMPT_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*元")

SECTION_TITLES = {
    "rule_priority": "规则优先级",
    "critical_runtime": "高优先级运行红线",
    "dialog_style": "对话风格",
    "fact_boundary": "事实边界",
    "privacy_disclosure": "身份核实与隐私边界",
    "amount_dispute": "金额与争议处理",
    "property_fee_scene": "物业费场景约束",
    "communication_norms": "沟通规范",
    "extra": "补充测试规则",
}
REPLACEABLE_SECTION_TITLES = {
    key: title for key, title in SECTION_TITLES.items() if key != "extra"
}

PUBLIC_CONSTRAINT_DEFAULTS = {
    "rule_priority": BUSINESS_RULE_PRIORITY_RULES,
    "critical_runtime": BUSINESS_CRITICAL_RUNTIME_RULES,
    "dialog_style": BUSINESS_DIALOG_STYLE_RULES,
    "fact_boundary": BUSINESS_FACT_BOUNDARY_RULES,
    "privacy_disclosure": BUSINESS_PRIVACY_DISCLOSURE_RULES,
    "amount_dispute": BUSINESS_AMOUNT_DISPUTE_RULES,
    "property_fee_scene": BUSINESS_PROPERTY_FEE_SCENE_RULES,
    "communication_norms": BUSINESS_COMMUNICATION_NORMS_RULES,
    "extra": (),
}


class BusinessPromptPreparerProtocol(Protocol):
    def prepare(self, context: Mapping[str, Any]) -> BusinessPromptPreparation | None:
        ...


@dataclass(frozen=True)
class BrowserPromptRegistration:
    call_id: str
    mode: str
    prompt_snapshot: PromptSnapshot
    expires_in_seconds: int
    sensitive_summary: dict[str, bool]
    opening: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BrowserPromptDatabasePreview:
    context: dict[str, Any]
    prompt_snapshot: PromptSnapshot
    sensitive_summary: dict[str, bool]
    opening: dict[str, Any]


@dataclass(frozen=True)
class StoredBrowserPrompt:
    registration: BrowserPromptRegistration
    expires_at: float


class BrowserPromptTestStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_BROWSER_PROMPT_TTL_SECONDS,
        now=None,
        business_prompt_preparer: BusinessPromptPreparerProtocol | None = None,
        opening_generator: OpeningAudioGenerator | None = None,
        opening_store: OpeningAudioStore | None = None,
        config: GatewayConfig | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = ttl_seconds
        self._now = now or time.time
        self._items: dict[str, StoredBrowserPrompt] = {}
        self.business_prompt_preparer = business_prompt_preparer
        self.opening_generator = opening_generator
        self.opening_store = opening_store
        self.config = config or GatewayConfig()

    def public_constraint_defaults(self) -> dict[str, Any]:
        return browser_public_constraint_defaults()

    def preview_database(self, payload: Mapping[str, Any]) -> BrowserPromptDatabasePreview:
        if self.business_prompt_preparer is None:
            raise ValueError("database prompt preparer unavailable")

        context = _context_payload(payload)
        preparation = self.business_prompt_preparer.prepare(context)
        if preparation is None:
            raise ValueError("database business prompt unavailable")

        opening = getattr(preparation, "opening", None)
        opening_payload = {"status": "unavailable"}
        if opening is not None:
            opening_payload = {
                "status": "available",
                "text_hash": opening.opening_text_hash,
                "voice": opening.voice,
                "speaker": opening.speaker,
            }

        return BrowserPromptDatabasePreview(
            context=context,
            prompt_snapshot=preparation.prompt_snapshot,
            sensitive_summary=_sensitive_summary(
                preparation.prompt_snapshot.instructions
            ),
            opening=opening_payload,
        )

    def register(self, payload: Mapping[str, Any]) -> BrowserPromptRegistration:
        call_id = _required_text(payload.get("call_id"), "call_id")
        _validate_browser_call_id(call_id)
        mode = _mode(payload)
        if mode == "database":
            registration = self._register_database(call_id, payload)
        elif mode == "manual":
            snapshot = build_browser_prompt_snapshot(payload)
            registration = BrowserPromptRegistration(
                call_id=call_id,
                mode=mode,
                prompt_snapshot=snapshot,
                expires_in_seconds=self.ttl_seconds,
                sensitive_summary=_sensitive_summary(snapshot.instructions),
                opening={"status": "disabled"},
            )
        else:
            raise ValueError("mode must be manual or database")

        self._items[call_id] = StoredBrowserPrompt(
            registration=registration,
            expires_at=self._now() + self.ttl_seconds,
        )
        self._purge_expired()
        return registration

    def get(self, call_id: str) -> PromptSnapshot | None:
        registration = self.get_registration(call_id)
        if registration is None:
            return None
        return registration.prompt_snapshot

    def get_registration(self, call_id: str) -> BrowserPromptRegistration | None:
        stored = self._items.get(call_id)
        if stored is None:
            return None
        if stored.expires_at <= self._now():
            self._items.pop(call_id, None)
            return None
        return stored.registration

    def _register_database(
        self,
        call_id: str,
        payload: Mapping[str, Any],
    ) -> BrowserPromptRegistration:
        if self.business_prompt_preparer is None:
            raise ValueError("database prompt preparer unavailable")

        context = _context_payload(payload)
        preparation = self.business_prompt_preparer.prepare(context)
        if preparation is None:
            raise ValueError("database business prompt unavailable")

        snapshot, warnings = _browser_snapshot_from_base(
            call_id=call_id,
            mode="database",
            base_snapshot=preparation.prompt_snapshot,
            payload=payload,
            context=context,
        )
        opening = self._prepare_opening_if_requested(
            call_id,
            payload,
            getattr(preparation, "opening", None),
            speaking_style=_optional_text(snapshot.metadata.get("speaking_style")),
        )
        return BrowserPromptRegistration(
            call_id=call_id,
            mode="database",
            prompt_snapshot=snapshot,
            expires_in_seconds=self.ttl_seconds,
            sensitive_summary=_sensitive_summary(snapshot.instructions),
            opening=opening,
            warnings=warnings,
        )

    def _prepare_opening_if_requested(
        self,
        call_id: str,
        payload: Mapping[str, Any],
        opening: OpeningRequest | None,
        *,
        speaking_style: str | None = None,
    ) -> dict[str, Any]:
        opening_payload = payload.get("opening")
        if not isinstance(opening_payload, Mapping) or not opening_payload.get(
            "enabled"
        ):
            return {"status": "disabled"}
        if opening is None:
            return {"status": "unavailable", "reason": "opening unavailable"}
        if self.opening_generator is None or self.opening_store is None:
            return {
                "status": "unavailable",
                "reason": "opening generator unavailable",
            }
        if speaking_style:
            opening = replace(opening, speaking_style=speaking_style)
        try:
            audio = self.opening_generator.generate(opening)
            prepared = build_prepared_opening_audio(
                call_id=call_id,
                opening=opening,
                audio=audio,
                config=self.config,
            )
        except OpeningGenerationFailed as err:
            return {"status": "failed", "error": str(err)}

        self.opening_store.put(prepared)
        return {
            "status": "ready",
            "text_hash": prepared.opening_text_hash,
            "voice": prepared.voice,
            "speaker": prepared.speaker,
            "audio_sample_rate": prepared.source_sample_rate,
            "phone_frame_count": len(prepared.phone_frames),
        }

    def _purge_expired(self) -> None:
        now = self._now()
        for call_id in [
            key for key, stored in self._items.items() if stored.expires_at <= now
        ]:
            self._items.pop(call_id, None)


def build_browser_prompt_snapshot(payload: Mapping[str, Any]) -> PromptSnapshot:
    call_id = _required_text(payload.get("call_id"), "call_id")
    _validate_browser_call_id(call_id)
    if _mode(payload) != "manual":
        raise ValueError("build_browser_prompt_snapshot only supports manual mode")

    employee_name = _optional_text(payload.get("employee_name")) or "浏览器测试员工"
    identity_name = _optional_text(payload.get("identityName")) or "项目员工"
    strategy = _sanitize_browser_prompt_text(
        payload.get("strategy_core"),
        append_note=True,
    ) or "浏览器手工测试策略。"
    speaking_style = _speaking_style_override(payload)
    instructions = _render_business_prompt(
        employee_name=employee_name,
        strategy=strategy,
        speaking_style=speaking_style,
        debtor_name=_optional_text(payload.get("debtor_name")) or "测试业主",
        debtor_gender=_optional_text(payload.get("debtor_gender")) or "女",
        debtor_age=payload.get("debtor_age"),
        debt_amount=payload.get("debt_amount"),
        address=payload.get("address"),
    )
    instructions = _with_browser_overrides(
        instructions,
        payload,
    )
    return PromptSnapshot(
        scene=BROWSER_PROMPT_SCENE,
        version=BROWSER_PROMPT_VERSION,
        instructions=instructions,
        content_hash=_hash_text(instructions),
        loaded_at_ms=_now_ms(),
        metadata={
            "source": BROWSER_PROMPT_SCENE,
            "mode": "manual",
            "call_id": call_id,
            "employee_name": employee_name,
            "identityName": identity_name,
            "speaking_style": speaking_style or BUSINESS_DIALOG_SPEAKING_STYLE,
        },
    )


def browser_public_constraint_defaults() -> dict[str, Any]:
    return {
        "speaking_style": BUSINESS_DIALOG_SPEAKING_STYLE,
        "sections": {
            key: "\n".join(values)
            for key, values in PUBLIC_CONSTRAINT_DEFAULTS.items()
        },
    }


def _browser_snapshot_from_base(
    *,
    call_id: str,
    mode: str,
    base_snapshot: PromptSnapshot,
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
) -> tuple[PromptSnapshot, list[str]]:
    speaking_style_override = _speaking_style_override(payload)
    speaking_style = speaking_style_override or _optional_text(
        base_snapshot.metadata.get("speaking_style")
    )
    instructions = base_snapshot.instructions
    if speaking_style_override:
        instructions = _replace_speaking_style_section(
            instructions,
            speaking_style_override,
        )
    instructions = _with_browser_overrides(
        instructions,
        payload,
    )
    metadata = {
        **base_snapshot.metadata,
        "source": BROWSER_PROMPT_SCENE,
        "mode": mode,
        "call_id": call_id,
        "base_scene": base_snapshot.scene,
        "base_version": base_snapshot.version,
        "base_content_hash": base_snapshot.content_hash,
    }
    if speaking_style:
        metadata["speaking_style"] = speaking_style

    warnings: list[str] = []
    persona_input = _optional_text(context.get("personaId"))
    persona_actual = _optional_text(base_snapshot.metadata.get("personaId"))
    if persona_input:
        metadata["personaId_input"] = persona_input
        if persona_actual:
            matches = persona_input == persona_actual
            metadata["personaId_matches"] = matches
            if not matches:
                warnings.append(
                    f"personaId 输入值 {persona_input} 与数据库策略 personaId "
                    f"{persona_actual} 不一致，已使用数据库值。"
                )

    return (
        PromptSnapshot(
            scene=BROWSER_PROMPT_SCENE,
            version=BROWSER_PROMPT_VERSION,
            instructions=instructions,
            content_hash=_hash_text(instructions),
            loaded_at_ms=_now_ms(),
            metadata=metadata,
        ),
        warnings,
    )


def _with_browser_overrides(
    instructions: str,
    payload: Mapping[str, Any],
) -> str:
    sections = _section_overrides(payload)
    for key, values in sections.items():
        title = REPLACEABLE_SECTION_TITLES.get(key)
        if title is None:
            continue
        instructions = _replace_prompt_section(
            instructions,
            title=title,
            values=values,
        )

    lines: list[str] = []
    for key, values in sections.items():
        if key in REPLACEABLE_SECTION_TITLES:
            continue
        title = SECTION_TITLES.get(key, key)
        lines.append(f"## {title}")
        lines.extend(f"{index}. {value}" for index, value in enumerate(values, 1))

    if not lines:
        return instructions
    return "\n".join(
        [
            instructions.rstrip(),
            "",
            "# 浏览器测试补充规则",
            "以下内容只对本次 browser call_id 生效，用于测试配置效果；不得突破身份核实、隐私保护、支付安全、事实边界和法律红线。",
            *lines,
        ]
    )


def _replace_speaking_style_section(instructions: str, speaking_style: str) -> str:
    return _replace_prompt_section(
        instructions,
        title="客服语气配置",
        values=[speaking_style],
        numbered=False,
    )


def _replace_prompt_section(
    instructions: str,
    *,
    title: str,
    values: list[str],
    numbered: bool = True,
) -> str:
    marker = f"# {title}\n"
    replacement = _section_body(values, numbered=numbered)
    if marker not in instructions:
        return "\n".join(
            [
                instructions.rstrip(),
                "",
                marker.rstrip(),
                replacement,
            ]
        )
    before, rest = instructions.split(marker, 1)
    next_section = "\n\n# "
    if next_section not in rest:
        return f"{before}{marker}{replacement}"
    _, after = rest.split(next_section, 1)
    return f"{before}{marker}{replacement}{next_section}{after}"


def _section_body(values: list[str], *, numbered: bool) -> str:
    if not numbered:
        return "\n".join(values)
    return "\n".join(f"{index}. {value}" for index, value in enumerate(values, 1))


def _section_overrides(payload: Mapping[str, Any]) -> dict[str, list[str]]:
    raw_sections: dict[str, Any] = {}
    sections = payload.get("sections")
    if isinstance(sections, Mapping):
        raw_sections.update(sections)
    overrides = payload.get("overrides")
    if isinstance(overrides, Mapping) and isinstance(overrides.get("sections"), Mapping):
        raw_sections.update(overrides["sections"])

    normalized: dict[str, list[str]] = {}
    for key, value in raw_sections.items():
        values = [
            sanitized
            for item in _text_list(value)
            if (
                sanitized := _sanitize_browser_prompt_text(
                    item,
                    append_note=False,
                )
            )
        ]
        if values:
            normalized[str(key)] = values
    return normalized


def _context_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    context = payload.get("context")
    if isinstance(context, Mapping):
        return dict(context)
    keys = ("callId", "debtId", "identityName", "personaId", "tenantId")
    return {key: payload[key] for key in keys if key in payload}


def _mode(payload: Mapping[str, Any]) -> str:
    return (_optional_text(payload.get("mode")) or "manual").lower()


def _speaking_style_override(payload: Mapping[str, Any]) -> str:
    overrides = payload.get("overrides")
    if isinstance(overrides, Mapping):
        value = _optional_text(overrides.get("speaking_style"))
        if value:
            return _sanitize_browser_prompt_text(value, append_note=False)
    return _sanitize_browser_prompt_text(
        payload.get("speaking_style"),
        append_note=False,
    )


def _sanitize_browser_prompt_text(value: object, *, append_note: bool) -> str:
    return _sanitize_business_strategy_text(value, append_note=append_note)


def _sensitive_summary(instructions: str) -> dict[str, bool]:
    amount_line_present = (
        "系统记录待处理金额：" in instructions
        and "系统记录待处理金额：本轮提示词未提供" not in instructions
    ) or PROMPT_AMOUNT_RE.search(instructions) is not None
    address_or_detail_present = re.search(
        r"(地址|房号|费用明细|欠费明细)\s*[:：]\s*\S+",
        instructions,
    ) is not None
    return {
        "amount_in_prompt": amount_line_present,
        "amount_disclosure_forbidden": (
            "不得在通话中说出具体金额" in instructions
            or "不得说出系统记录金额" in instructions
        ),
        "address_room_detail_excluded": not address_or_detail_present,
    }


def _text_list(value: object) -> list[str]:
    if isinstance(value, str):
        cleaned = _clean_text(value)
        return [cleaned] if cleaned else []
    if not isinstance(value, list):
        return []
    cleaned_values = []
    for item in value:
        cleaned = _clean_text(item)
        if cleaned:
            cleaned_values.append(cleaned)
    return cleaned_values


def _required_text(value: object, name: str) -> str:
    text = _optional_text(value)
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _optional_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clean_text(value: object) -> str:
    return _redact_prompt_amounts(" ".join(str(value).split()))


def _redact_prompt_amounts(text: str) -> str:
    return PROMPT_AMOUNT_RE.sub("[金额已隐藏]", text)


def _validate_browser_call_id(call_id: str) -> None:
    if not call_id.startswith(BROWSER_CALL_ID_PREFIX):
        raise ValueError("browser test call_id must start with browser-")
    if not SAFE_BROWSER_CALL_ID_RE.match(call_id):
        raise ValueError("browser test call_id contains unsupported characters")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)
