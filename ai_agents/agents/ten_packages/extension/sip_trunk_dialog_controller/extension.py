import asyncio
import json
import time
import uuid

from ten_ai_base.struct import (
    LLMMessage,
    LLMMessageContent,
    LLMRequest,
    LLMResponse,
    LLMResponseMessageDelta,
    LLMResponseMessageDone,
    LLMResponseReasoningDelta,
    LLMResponseReasoningDone,
    parse_llm_response,
)
from ten_runtime import AsyncExtension, AsyncTenEnv, Cmd, Data, Loc, StatusCode

from .config import SipTrunkDialogControllerConfig


def _is_punctuation(char: str) -> bool:
    return char in {
        ".",
        "?",
        "!",
        "\n",
        "\u3002",
        "\uff1f",
        "\uff01",
    }


def _parse_sentences(fragment: str, content: str) -> tuple[list[str], str]:
    sentences: list[str] = []
    current_sentence = fragment
    for char in content:
        current_sentence += char
        if _is_punctuation(char):
            if any(c.isalnum() for c in current_sentence):
                sentences.append(current_sentence.strip())
            current_sentence = ""
    return sentences, current_sentence


def _has_speech_content(text: str) -> bool:
    return any(c.isalnum() for c in text)


def _speech_char_count(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def _normalize_control_text(text: str) -> str:
    return "".join(char.lower() for char in text if char.isalnum())


def _parse_control_commands(commands: str) -> set[str]:
    normalized = commands
    for separator in {"，", "、", ";", "；", "\n", "\t"}:
        normalized = normalized.replace(separator, ",")
    return {
        command
        for command in (_normalize_control_text(part) for part in normalized.split(","))
        if command
    }


def _trim_for_phone_budget(text: str, max_chars: int) -> str:
    clipped = text.strip()[:max_chars].rstrip(" \t\r\n,，、;；:：")
    if not clipped:
        return ""
    if clipped[-1] not in {".", "?", "!", "。", "？", "！"}:
        clipped += "。"
    return clipped


class SipTrunkDialogControllerExtension(AsyncExtension):
    def __init__(self, name: str):
        super().__init__(name)
        self.config = SipTrunkDialogControllerConfig()
        self.ten_env: AsyncTenEnv | None = None
        self.contexts: list[LLMMessage] = []
        self.turn_id = 0
        self.session_id = "phone"
        self.sentence_fragment = ""
        self.current_llm_task: asyncio.Task | None = None
        self.interrupt_flush_sent = False
        self.tts_playing = False
        self.pending_tts_output = False
        self.tts_audio_started_at: float | None = None
        self.tts_output_active_until = 0.0
        self.spoken_chars_this_turn = 0
        self.spoken_output_closed = False
        self.call_active = True
        self.stopped = False
        self.recent_interrupt_command_text: str | None = None
        self.recent_interrupt_command_expires_at = 0.0
        self.recent_interrupt_expires_at = 0.0
        self.barge_in_candidate_expires_at = 0.0

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        self.ten_env = ten_env
        self.config = await self._load_config(ten_env)
        ten_env.log_info(
            "sip_trunk_dialog_controller on_init: "
            f"llm={self.config.llm_extension}, "
            f"tts={self.config.tts_extension}, "
            f"media_bridge={self.config.media_bridge_extension}, "
            f"language={self.config.response_language}"
        )

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        await super().on_start(ten_env)
        ten_env.log_info("sip_trunk_dialog_controller started")

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        self.stopped = True
        if self.current_llm_task and not self.current_llm_task.done():
            self.current_llm_task.cancel()
            try:
                await self.current_llm_task
            except asyncio.CancelledError:
                pass
        ten_env.log_info("sip_trunk_dialog_controller stopping")
        await super().on_stop(ten_env)

    async def on_data(self, ten_env: AsyncTenEnv, data: Data) -> None:
        if self.ten_env is None:
            self.ten_env = ten_env

        data_name = data.get_name()
        if data_name in {"sip_peer_connected", "sip_peer_disconnected"}:
            await self._handle_peer_event(ten_env, data_name, data)
            return

        if data_name == "sip_user_speech_start":
            await self._handle_user_speech_start_event(ten_env, data)
            return

        if data_name in {
            "tts_audio_start",
            "tts_audio_end",
            "tts_flush_end",
        }:
            await self._handle_tts_event(ten_env, data_name, data)
            return

        if data_name != "asr_result":
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored data: " f"name={data_name}"
            )
            return

        if not self.call_active:
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored asr_result after hangup"
            )
            return

        payload, err = data.get_property_to_json(None)
        if err is not None:
            ten_env.log_warn("sip_trunk_dialog_controller invalid asr_result payload")
            return

        try:
            asr_result = json.loads(payload or "{}")
        except json.JSONDecodeError:
            ten_env.log_warn("sip_trunk_dialog_controller failed to decode asr_result")
            return

        text = str(asr_result.get("text", "")).strip()
        final = bool(asr_result.get("final", False))
        duration_ms = int(asr_result.get("duration_ms") or 0)
        metadata = asr_result.get("metadata", {}) or {}
        self.session_id = str(metadata.get("session_id", self.session_id))

        if not text:
            return

        ten_env.log_info(
            "sip_trunk_dialog_controller asr_result: " f"final={final}, text={text}"
        )

        if not final:
            if self._should_interrupt_on_urgent_command(text):
                await self._interrupt_ai_output_from_asr(
                    text,
                    "urgent_partial_asr",
                    remember_command=True,
                )
                return
            if self._should_interrupt_on_partial(text, duration_ms):
                await self._interrupt_ai_output_from_asr(text, "partial_asr")
            return

        if self._should_ignore_final_after_interrupt_command(text):
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored final_asr after "
                f"urgent interrupt: text={text}, duration_ms={duration_ms}"
            )
            return

        if self._should_interrupt_on_final_urgent_command(text):
            await self._interrupt_ai_output_from_asr(
                text,
                "urgent_final_asr",
                remember_command=True,
            )
            return

        if self._should_ignore_ambiguous_final_asr(text, duration_ms):
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored ambiguous final_asr: "
                f"text={text}, duration_ms={duration_ms}"
            )
            return

        if self.current_llm_task and not self.current_llm_task.done():
            self.current_llm_task.cancel()

        if self._should_flush_on_final_asr():
            self._discard_interrupted_assistant_context()
            await self._send_interrupt_flush(text, "final_asr")

        self.turn_id += 1
        self.sentence_fragment = ""
        self.interrupt_flush_sent = False
        self.pending_tts_output = False
        self.tts_playing = False
        self.tts_output_active_until = 0.0
        self.recent_interrupt_command_text = None
        self.recent_interrupt_command_expires_at = 0.0
        self.recent_interrupt_expires_at = 0.0
        self.barge_in_candidate_expires_at = 0.0
        self._reset_spoken_output_gate()
        self.current_llm_task = asyncio.create_task(self._run_llm_and_tts(text))

    async def _handle_peer_event(
        self,
        ten_env: AsyncTenEnv,
        data_name: str,
        data: Data,
    ) -> None:
        payload, _ = data.get_property_to_json(None)
        try:
            event = json.loads(payload or "{}")
        except json.JSONDecodeError:
            event = {}

        peer_role = str(event.get("peer_role", ""))
        if peer_role and peer_role != "fs":
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored peer event: "
                f"name={data_name}, peer_role={peer_role}"
            )
            return

        if data_name == "sip_peer_connected":
            await self._reset_for_new_call()
            self.call_active = True
        else:
            self.call_active = False
            await self._cancel_current_llm_task()
            self.sentence_fragment = ""
            self.interrupt_flush_sent = False
            self.pending_tts_output = False
            self.tts_playing = False
            self.tts_audio_started_at = None
            self.tts_output_active_until = 0.0
            self.recent_interrupt_command_text = None
            self.recent_interrupt_command_expires_at = 0.0
            self.recent_interrupt_expires_at = 0.0
            self.barge_in_candidate_expires_at = 0.0
            self._reset_spoken_output_gate()
            await self._send_interrupt_flush("", "peer_disconnected")

        ten_env.log_info(
            "sip_trunk_dialog_controller peer_event: "
            f"name={data_name}, call_active={self.call_active}, payload={payload or '{}'}"
        )

    async def _reset_for_new_call(self) -> None:
        await self._cancel_current_llm_task()
        self.contexts = []
        self.turn_id = 0
        self.session_id = "phone"
        self.sentence_fragment = ""
        self.interrupt_flush_sent = False
        self.pending_tts_output = False
        self.tts_playing = False
        self.tts_audio_started_at = None
        self.tts_output_active_until = 0.0
        self.recent_interrupt_command_text = None
        self.recent_interrupt_command_expires_at = 0.0
        self.recent_interrupt_expires_at = 0.0
        self.barge_in_candidate_expires_at = 0.0
        self._reset_spoken_output_gate()

    async def _handle_user_speech_start_event(
        self,
        ten_env: AsyncTenEnv,
        data: Data,
    ) -> None:
        payload, _ = data.get_property_to_json(None)
        if not self.call_active:
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored user_speech_start after hangup"
            )
            return
        if not self._is_ai_audio_output_active():
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored user_speech_start "
                "without ai audio: "
                f"payload={payload or '{}'}"
            )
            return
        if self.interrupt_flush_sent:
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored duplicate user_speech_start: "
                f"payload={payload or '{}'}"
            )
            return
        if not self._should_accept_user_speech_start_event(payload):
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored low-energy "
                "user_speech_start as likely echo: "
                f"payload={payload or '{}'}"
            )
            return

        self._mark_barge_in_candidate()
        ten_env.log_info(
            "sip_trunk_dialog_controller marked user_speech_start "
            "as barge-in candidate: "
            f"payload={payload or '{}'}"
        )

    async def _cancel_current_llm_task(self) -> None:
        if self.current_llm_task and not self.current_llm_task.done():
            self.current_llm_task.cancel()
            try:
                await self.current_llm_task
            except asyncio.CancelledError:
                pass

    async def _handle_tts_event(
        self,
        ten_env: AsyncTenEnv,
        data_name: str,
        data: Data,
    ) -> None:
        if data_name == "tts_audio_start":
            self.tts_playing = True
            self.pending_tts_output = True
            self.tts_audio_started_at = time.monotonic()
        elif data_name in {"tts_audio_end", "tts_flush_end"}:
            self.tts_playing = False
            self.pending_tts_output = False
            if data_name == "tts_audio_end":
                self._extend_tts_active_until(data)
            else:
                self.tts_audio_started_at = None
                self.tts_output_active_until = 0.0

        payload, _ = data.get_property_to_json(None)
        ten_env.log_info(
            "sip_trunk_dialog_controller tts_event: "
            f"name={data_name}, payload={payload or '{}'}"
        )

    async def _run_llm_and_tts(self, user_text: str) -> None:
        if self.ten_env is None or not self.call_active:
            return

        ten_env = self.ten_env
        request_id = str(uuid.uuid4())
        user_message = LLMMessageContent(role="user", content=user_text)
        messages = self.contexts.copy()
        messages.append(user_message)

        llm_request = LLMRequest(
            request_id=request_id,
            messages=messages,
            streaming=True,
            parameters={"temperature": self.config.temperature},
            tools=[],
        )

        cmd = Cmd.create("chat_completion")
        cmd.set_dests([Loc("", "", self.config.llm_extension)])
        cmd.set_property_from_json(None, json.dumps(llm_request.model_dump()))

        self._append_context(user_message)
        ten_env.log_info(
            "sip_trunk_dialog_controller send_llm_request: "
            f"turn_id={self.turn_id}, request_id={request_id}, text={user_text}"
        )

        try:
            async for cmd_result, cmd_error in ten_env.send_cmd_ex(cmd):
                if cmd_error is not None:
                    ten_env.log_warn("sip_trunk_dialog_controller llm command error")
                    continue
                if cmd_result is None or cmd_result.is_final():
                    continue
                if cmd_result.get_status_code() != StatusCode.OK:
                    ten_env.log_warn(
                        "sip_trunk_dialog_controller llm non-ok result: "
                        f"status={cmd_result.get_status_code()}"
                    )
                    continue
                response_json, _ = cmd_result.get_property_to_json(None)
                llm_response = parse_llm_response(response_json)
                if not self.call_active:
                    return
                await self._handle_llm_response(llm_response)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            ten_env.log_error(
                "sip_trunk_dialog_controller llm request failed: " f"error={err}"
            )

    async def _handle_llm_response(
        self,
        llm_response: LLMResponse | None,
    ) -> None:
        if self.ten_env is None or llm_response is None or not self.call_active:
            return

        match llm_response:
            case LLMResponseMessageDelta():
                delta = llm_response.delta or ""
                text = llm_response.content or ""
                if delta:
                    sentences, self.sentence_fragment = _parse_sentences(
                        self.sentence_fragment,
                        delta,
                    )
                    for sentence in sentences:
                        await self._send_to_tts(sentence, False)
                if text:
                    self._write_context("assistant", text)
            case LLMResponseMessageDone():
                text = llm_response.content or ""
                remaining_text = self.sentence_fragment.strip()
                self.sentence_fragment = ""
                await self._send_to_tts(remaining_text, True)
                if text:
                    self._write_context("assistant", text)
                self.ten_env.log_info(
                    "sip_trunk_dialog_controller llm_done: "
                    f"turn_id={self.turn_id}, text={text}"
                )
            case LLMResponseReasoningDelta():
                return
            case LLMResponseReasoningDone():
                return
            case _:
                self.ten_env.log_info(
                    "sip_trunk_dialog_controller ignored llm response: "
                    f"type={type(llm_response).__name__}"
                )

    async def _send_to_tts(self, text: str, text_input_end: bool) -> None:
        if self.ten_env is None or not self.call_active:
            return

        text = self._take_spoken_text(text)
        if not text and not text_input_end:
            return

        data = Data.create("tts_text_input")
        data.set_dests([Loc("", "", self.config.tts_extension)])
        data.set_property_from_json(
            None,
            json.dumps(
                {
                    "request_id": f"sip-trunk-tts-{self.turn_id}",
                    "text": text,
                    "text_input_end": text_input_end,
                    "metadata": {
                        "session_id": self.session_id,
                        "turn_id": self.turn_id,
                        "language": self.config.response_language,
                    },
                }
            ),
        )
        await self.ten_env.send_data(data)
        if text:
            self.pending_tts_output = True
        self.ten_env.log_info(
            "sip_trunk_dialog_controller sent_tts_text: "
            f"end={text_input_end}, text={text}"
        )

    def _should_interrupt_on_partial(
        self,
        text: str,
        duration_ms: int,
    ) -> bool:
        if not self.config.interrupt_on_partial:
            return False
        if not self._is_ai_output_active():
            return False
        if self.interrupt_flush_sent:
            return False
        if not self._has_barge_in_candidate():
            return False
        if _speech_char_count(text) < max(self.config.interrupt_min_chars, 1):
            return False
        if duration_ms < max(self.config.interrupt_min_duration_ms, 0):
            return False
        return _has_speech_content(text)

    def _should_interrupt_on_urgent_command(self, text: str) -> bool:
        if not self.config.interrupt_on_partial:
            return False
        if not self._is_ai_output_active():
            return False
        if self.interrupt_flush_sent:
            return False
        return self._normalized_urgent_command(text) is not None

    def _should_interrupt_on_final_urgent_command(self, text: str) -> bool:
        if not self._is_ai_output_active():
            return False
        if self.interrupt_flush_sent:
            return False
        return self._normalized_urgent_command(text) is not None

    def _normalized_urgent_command(self, text: str) -> str | None:
        normalized_text = _normalize_control_text(text)
        if not normalized_text:
            return None
        commands = _parse_control_commands(self.config.urgent_interrupt_commands)
        if normalized_text in commands:
            return normalized_text
        return None

    async def _interrupt_ai_output_from_asr(
        self,
        text: str,
        reason: str,
        remember_command: bool = False,
    ) -> None:
        self.interrupt_flush_sent = True
        if self.current_llm_task and not self.current_llm_task.done():
            self.current_llm_task.cancel()
        self._discard_interrupted_assistant_context()
        self.sentence_fragment = ""
        self.pending_tts_output = False
        self.tts_playing = False
        self.tts_audio_started_at = None
        self.tts_output_active_until = 0.0
        self.barge_in_candidate_expires_at = 0.0
        self._remember_recent_interrupt()
        if remember_command:
            self._remember_interrupt_command(text)
        if reason == "partial_asr":
            await self._send_interrupt_flush(text)
            return
        await self._send_interrupt_flush(text, reason)

    def _remember_interrupt_command(self, text: str) -> None:
        normalized_command = self._normalized_urgent_command(text)
        if normalized_command is None:
            return
        self.recent_interrupt_command_text = normalized_command
        self.recent_interrupt_command_expires_at = time.monotonic() + 2

    def _remember_recent_interrupt(self) -> None:
        self.recent_interrupt_expires_at = time.monotonic() + 2

    def _discard_interrupted_assistant_context(self) -> None:
        if not self.contexts:
            return
        last_message = self.contexts[-1]
        if getattr(last_message, "role", None) != "assistant":
            return
        self.contexts.pop()

    def _should_ignore_final_after_interrupt_command(self, text: str) -> bool:
        normalized_text = _normalize_control_text(text)
        now = time.monotonic()
        if self.recent_interrupt_command_text:
            if now > self.recent_interrupt_command_expires_at:
                self.recent_interrupt_command_text = None
            elif normalized_text == self.recent_interrupt_command_text:
                self.recent_interrupt_command_text = None
                self.recent_interrupt_expires_at = 0.0
                return True

        if now > self.recent_interrupt_expires_at:
            return False
        if normalized_text not in _parse_control_commands(
            self.config.urgent_interrupt_commands
        ):
            return False

        self.recent_interrupt_command_text = None
        self.recent_interrupt_expires_at = 0.0
        return True

    def _is_ai_output_active(self) -> bool:
        llm_active = (
            self.current_llm_task is not None and not self.current_llm_task.done()
        )
        playback_active = time.monotonic() < self.tts_output_active_until
        return (
            llm_active or self.pending_tts_output or self.tts_playing or playback_active
        )

    def _is_ai_audio_output_active(self) -> bool:
        playback_active = time.monotonic() < self.tts_output_active_until
        return self.pending_tts_output or self.tts_playing or playback_active

    def _should_accept_user_speech_start_event(self, payload: str | None) -> bool:
        min_rms = max(self.config.speech_start_interrupt_min_rms, 0)
        min_peak = max(self.config.speech_start_interrupt_min_peak, 0)
        if min_rms <= 0 and min_peak <= 0:
            return True

        try:
            event = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return False

        try:
            rms = int(event.get("rms") or 0)
            peak = int(event.get("peak") or 0)
        except (TypeError, ValueError):
            return False

        return rms >= min_rms and peak >= min_peak

    def _should_flush_on_final_asr(self) -> bool:
        if self.interrupt_flush_sent:
            return False
        if not self._is_ai_output_active():
            return False

        min_remaining_ms = max(self.config.final_interrupt_min_remaining_ms, 0)
        if min_remaining_ms <= 0:
            return True

        now = time.monotonic()
        if self.tts_output_active_until > now:
            remaining_ms = (self.tts_output_active_until - now) * 1000
            if remaining_ms <= min_remaining_ms:
                return False

        return True

    def _should_ignore_ambiguous_final_asr(
        self,
        text: str,
        duration_ms: int,
    ) -> bool:
        recent_interrupt_active = time.monotonic() <= self.recent_interrupt_expires_at
        if not self._is_ai_output_active() and not recent_interrupt_active:
            return False
        if self._normalized_urgent_command(text) is not None:
            return False

        speech_chars = _speech_char_count(text)
        min_chars = max(self.config.final_interrupt_min_chars, 1)
        min_duration_ms = max(self.config.final_interrupt_min_duration_ms, 0)
        substantial_chars = max(min_chars + 2, 5)

        if speech_chars >= substantial_chars:
            return False

        if speech_chars >= min_chars and duration_ms >= min_duration_ms:
            return False

        return _has_speech_content(text)

    def _extend_tts_active_until(self, data: Data) -> None:
        payload, err = data.get_property_to_json(None)
        if err is not None:
            return

        try:
            event = json.loads(payload or "{}")
        except json.JSONDecodeError:
            return

        duration_ms = int(event.get("request_total_audio_duration_ms") or 0)
        if duration_ms <= 0 or self.tts_audio_started_at is None:
            return

        guard_seconds = max(self.config.tts_playback_guard_ms, 0) / 1000
        self.tts_output_active_until = max(
            time.monotonic(),
            self.tts_audio_started_at + duration_ms / 1000 + guard_seconds,
        )

    def _reset_spoken_output_gate(self) -> None:
        self.spoken_chars_this_turn = 0
        self.spoken_output_closed = False

    def _mark_barge_in_candidate(self) -> None:
        ttl_seconds = max(self.config.barge_in_candidate_ttl_ms, 0) / 1000
        self.barge_in_candidate_expires_at = time.monotonic() + ttl_seconds

    def _has_barge_in_candidate(self) -> bool:
        return time.monotonic() <= self.barge_in_candidate_expires_at

    def _take_spoken_text(self, text: str) -> str:
        text = text.strip()
        if not text or self.spoken_output_closed:
            return ""

        max_chars = self.config.max_spoken_chars_per_turn
        if max_chars <= 0:
            return text

        remaining = max_chars - self.spoken_chars_this_turn
        if remaining <= 0:
            self.spoken_output_closed = True
            return ""

        if len(text) <= remaining:
            self.spoken_chars_this_turn += len(text)
            return text

        self.spoken_output_closed = True
        self.spoken_chars_this_turn = max_chars
        return _trim_for_phone_budget(text, remaining)

    async def _send_interrupt_flush(
        self,
        text: str,
        reason: str = "partial_asr",
    ) -> None:
        if self.ten_env is None:
            return

        data = Data.create("tts_flush")
        dests = [Loc("", "", self.config.tts_extension)]
        if self.config.media_bridge_extension:
            dests.append(Loc("", "", self.config.media_bridge_extension))
        data.set_dests(dests)
        data.set_property_from_json(
            None,
            json.dumps(
                {
                    "flush_id": f"sip-trunk-interrupt-{self.turn_id + 1}",
                    "metadata": {
                        "session_id": self.session_id,
                        "turn_id": self.turn_id,
                        "reason": reason,
                        "text": text,
                    },
                }
            ),
        )
        await self.ten_env.send_data(data)
        self.ten_env.log_info(
            "sip_trunk_dialog_controller sent_interrupt_flush: "
            f"reason={reason}, text={text}"
        )

    def _append_context(self, message: LLMMessage) -> None:
        self.contexts.append(message)
        max_messages = max(self.config.max_context_messages, 1)
        if len(self.contexts) > max_messages:
            self.contexts = self.contexts[-max_messages:]

    def _write_context(self, role: str, content: str) -> None:
        if self.contexts and self.contexts[-1].role == role:
            self.contexts[-1].content = content
            return
        self._append_context(LLMMessageContent(role=role, content=content))

    async def _load_config(
        self,
        ten_env: AsyncTenEnv,
    ) -> SipTrunkDialogControllerConfig:
        config = SipTrunkDialogControllerConfig()
        config.llm_extension = await self._get_string_property(
            ten_env,
            "llm_extension",
            config.llm_extension,
        )
        config.tts_extension = await self._get_string_property(
            ten_env,
            "tts_extension",
            config.tts_extension,
        )
        config.media_bridge_extension = await self._get_string_property(
            ten_env,
            "media_bridge_extension",
            config.media_bridge_extension,
        )
        config.response_language = await self._get_string_property(
            ten_env,
            "response_language",
            config.response_language,
        )
        config.temperature = await self._get_float_property(
            ten_env,
            "temperature",
            config.temperature,
        )
        config.max_context_messages = await self._get_int_property(
            ten_env,
            "max_context_messages",
            config.max_context_messages,
        )
        config.interrupt_on_partial = await self._get_bool_property(
            ten_env,
            "interrupt_on_partial",
            config.interrupt_on_partial,
        )
        config.interrupt_min_chars = await self._get_int_property(
            ten_env,
            "interrupt_min_chars",
            config.interrupt_min_chars,
        )
        config.interrupt_min_duration_ms = await self._get_int_property(
            ten_env,
            "interrupt_min_duration_ms",
            config.interrupt_min_duration_ms,
        )
        config.urgent_interrupt_commands = await self._get_string_property(
            ten_env,
            "urgent_interrupt_commands",
            config.urgent_interrupt_commands,
        )
        config.final_interrupt_min_chars = await self._get_int_property(
            ten_env,
            "final_interrupt_min_chars",
            config.final_interrupt_min_chars,
        )
        config.final_interrupt_min_duration_ms = await self._get_int_property(
            ten_env,
            "final_interrupt_min_duration_ms",
            config.final_interrupt_min_duration_ms,
        )
        config.final_interrupt_min_remaining_ms = await self._get_int_property(
            ten_env,
            "final_interrupt_min_remaining_ms",
            config.final_interrupt_min_remaining_ms,
        )
        config.speech_start_interrupt_min_rms = await self._get_int_property(
            ten_env,
            "speech_start_interrupt_min_rms",
            config.speech_start_interrupt_min_rms,
        )
        config.speech_start_interrupt_min_peak = await self._get_int_property(
            ten_env,
            "speech_start_interrupt_min_peak",
            config.speech_start_interrupt_min_peak,
        )
        config.barge_in_candidate_ttl_ms = await self._get_int_property(
            ten_env,
            "barge_in_candidate_ttl_ms",
            config.barge_in_candidate_ttl_ms,
        )
        config.max_spoken_chars_per_turn = await self._get_int_property(
            ten_env,
            "max_spoken_chars_per_turn",
            config.max_spoken_chars_per_turn,
        )
        config.tts_playback_guard_ms = await self._get_int_property(
            ten_env,
            "tts_playback_guard_ms",
            config.tts_playback_guard_ms,
        )
        return config

    async def _get_string_property(
        self,
        ten_env: AsyncTenEnv,
        name: str,
        default: str,
    ) -> str:
        value, err = await ten_env.get_property_string(name)
        if err is not None or value is None or value == "":
            return default
        return value

    async def _get_int_property(
        self,
        ten_env: AsyncTenEnv,
        name: str,
        default: int,
    ) -> int:
        value, err = await ten_env.get_property_int(name)
        if err is not None or value is None:
            return default
        return value

    async def _get_float_property(
        self,
        ten_env: AsyncTenEnv,
        name: str,
        default: float,
    ) -> float:
        value, err = await ten_env.get_property_float(name)
        if err is not None or value is None:
            return default
        return value

    async def _get_bool_property(
        self,
        ten_env: AsyncTenEnv,
        name: str,
        default: bool,
    ) -> bool:
        value, err = await ten_env.get_property_bool(name)
        if err is not None or value is None:
            return default
        return value
