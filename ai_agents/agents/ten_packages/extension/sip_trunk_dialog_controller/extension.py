import asyncio
import json
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
    return char in {",", ".", "?", "!", "\n", "\u3002", "\uff0c", "\uff1f", "\uff01"}


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
        self.stopped = False

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        self.ten_env = ten_env
        self.config = await self._load_config(ten_env)
        ten_env.log_info(
            "sip_trunk_dialog_controller on_init: "
            f"llm={self.config.llm_extension}, "
            f"tts={self.config.tts_extension}, "
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
        if data.get_name() != "asr_result":
            ten_env.log_info(
                "sip_trunk_dialog_controller ignored data: "
                f"name={data.get_name()}"
            )
            return

        payload, err = data.get_property_to_json(None)
        if err is not None:
            ten_env.log_warn(
                "sip_trunk_dialog_controller invalid asr_result payload"
            )
            return

        try:
            asr_result = json.loads(payload or "{}")
        except json.JSONDecodeError:
            ten_env.log_warn(
                "sip_trunk_dialog_controller failed to decode asr_result"
            )
            return

        text = str(asr_result.get("text", "")).strip()
        final = bool(asr_result.get("final", False))
        metadata = asr_result.get("metadata", {}) or {}
        self.session_id = str(metadata.get("session_id", self.session_id))

        if not text:
            return

        ten_env.log_info(
            "sip_trunk_dialog_controller asr_result: "
            f"final={final}, text={text}"
        )

        if not final:
            return

        self.turn_id += 1
        self.sentence_fragment = ""
        if self.current_llm_task and not self.current_llm_task.done():
            self.current_llm_task.cancel()
        self.current_llm_task = asyncio.create_task(
            self._run_llm_and_tts(text)
        )

    async def _run_llm_and_tts(self, user_text: str) -> None:
        if self.ten_env is None:
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
                    ten_env.log_warn(
                        "sip_trunk_dialog_controller llm command error"
                    )
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
                await self._handle_llm_response(llm_response)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            ten_env.log_error(
                "sip_trunk_dialog_controller llm request failed: "
                f"error={err}"
            )

    async def _handle_llm_response(
        self,
        llm_response: LLMResponse | None,
    ) -> None:
        if self.ten_env is None or llm_response is None:
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
        if self.ten_env is None:
            return
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
        self.ten_env.log_info(
            "sip_trunk_dialog_controller sent_tts_text: "
            f"end={text_input_end}, text={text}"
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
