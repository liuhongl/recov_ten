from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServer, WebSocketServerProtocol, serve

from .audio_codec import (
    pcm_s16le_frame_bytes,
    pcm_s16le_rms,
    pcm_s16le_to_samples,
    resample_pcm_s16le_mono,
)
from .business_dialog_style import (
    BUSINESS_CRITICAL_RUNTIME_RULES,
    BUSINESS_DIALOG_SPEAKING_STYLE,
    BUSINESS_DIALOG_STYLE_RULES,
)
from .config import GatewayConfig
from .freeswitch_event_socket import (
    FreeSwitchPlaybackController,
    PlaybackProgressEvent,
)
from .media_contract import PhoneMediaContract
from .opening import OpeningAudioStore, PreparedOpeningAudio
from .playout_controller import (
    PlayoutController,
    PlayoutControllerConfig,
    PlayoutDecision,
    PlayoutPacingMode,
    PlayoutPacingState,
)
from .recording_upload import RecordingUploadError, build_recording_host_path
from .realtime_types import (
    DEFAULT_INPUT_SAMPLE_RATE,
    DEFAULT_OUTPUT_SAMPLE_RATE,
    RealtimeDialogConfig,
    RealtimeDialogContextItem,
    RealtimeTurnResult,
)
from .postgres import PromptSnapshot
from .voice_activity import EnergyVadTurnDetector
from .wav_io import write_pcm16_wav

LOGGER = logging.getLogger(__name__)

HANDOFF_CONNECTING_PROMPT_TEXT = "好的，正在为您转接人工座席，请稍等。"
DEFAULT_PHONE_INSTRUCTIONS = (
    "You are a Chinese phone customer service assistant. "
    "Reply in short, natural spoken Chinese. "
    "Keep each reply within two short sentences. "
    "Only answer the user's latest clearly understood utterance. "
    "If the latest utterance is unclear or too short after an interruption, "
    "ask the user to repeat instead of answering from conversation history. "
    "Do not report the current time unless the latest utterance explicitly asks for it."
)
LATEST_UTTERANCE_GUARD = (
    "以下历史只用于保持上下文，不能当作本轮用户的新问题。"
    "本轮回复必须以用户最新一句清晰语音为准。"
    "如果打断后的最新语音不清楚、太短或只是停顿，请让用户再说一遍。"
    "不要因为历史里问过时间、日期或其他问题，就在本轮继续回答这些旧问题；"
    "除非用户最新一句明确询问时间，否则不要主动报时。"
)
SPOKEN_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*元")
HANDOFF_REQUEST_RE = re.compile(
    r"(?:转接|转|接|找|换|叫|要)(?:一下|个)?人工(?!费|智能)"
    r"|(?:转接|转)(?:一下|个)?工"
    r"|(?:转接|转|找)(?:一下|个)?(?:物业)?客服"
    r"|人工(?:客服|坐席)"
    r"|真人(?:客服|坐席)"
    r"|(?:让|叫|找)(?:你们)?(?:物业)?工作人员(?:跟我|和我)?(?:说|沟通|联系)"
    r"|(?:不想|不愿意|不愿|不|别|不要)(?:再)?(?:跟|和)?"
    r"(?:机器人|机器客服|智能客服|AI|ai)(?:说|聊|沟通)?"
)
HANDOFF_NEGATED_REQUEST_RE = re.compile(
    r"(?:不用|不需要|不要|别|先别|暂时不用|暂时不要|暂时别)"
    r"(?:转|转接|接|找|叫|换)?(?:人工|人工客服|真人客服|物业客服|客服)"
    r"|不是(?:要)?(?:转|转接|找|叫)?(?:人工|人工客服|真人客服|物业客服|客服)"
)
HANDOFF_IDENTITY_QUESTION_RE = re.compile(
    r"(?:你|您|电话那边|这|这个|现在)(?:是|是不是)"
    r"(?:人工客服|真人客服|人工坐席|真人坐席|人工|真人|客服|"
    r"机器人|机器客服|智能客服|AI|ai)(?:吗|嘛|么)$"
)
OPENING_BUSINESS_GUARD = "\n".join(
    [
        "这是待缴费用确认电话，不是闲聊。",
        "开场白后，只有用户明确说自己是业主本人、授权处理人，或明确表示自己可以处理该费用事项，才视为身份已确认。",
        "用户只说“方便”“可以”“好的”“嗯”“对”“是的”“在的”“你说吧”等短句时，不能视为已确认身份。",
        "无论是否确认身份，都不得在通话中说出具体金额，也不得复述用户提到的金额。",
        "未确认身份前不得披露地址、房号或费用明细；只能继续确认本人或授权处理人身份。",
        *BUSINESS_CRITICAL_RUNTIME_RULES,
        *BUSINESS_DIALOG_STYLE_RULES,
        "严禁主动切换到化妆、天气、时间、学习知识、闲聊等无关话题。",
        "如果用户指出你跑题了，先简短道歉，然后立刻回到待缴费用确认。",
    ]
)
DEFAULT_REPLAY_AUDIO_MS = 800
MAX_COMMITTED_HISTORY_EXCHANGES = 6
MAX_COMMITTED_HISTORY_CHARS = 1400
OPENING_TURN_ID = 0
OPENING_BARGE_IN_MIN_SENT_FRAMES = 10
OPENING_BARGE_IN_MIN_PLAYBACK_MS = 300
OPENING_ECHO_CORRELATION_THRESHOLD = 0.4
OPENING_ECHO_MAX_LAST_PLAYBACK_AGE_MS = 120
DEFAULT_DIALOG_MODEL = "1.2.1.1"
MAX_DIALOG_BOT_NAME_CHARS = 20
DIALOG_PROMPT_SOFT_LIMIT_CHARS = 12000
DIALOG_SPEAKING_STYLE = BUSINESS_DIALOG_SPEAKING_STYLE


@dataclass(frozen=True)
class PlaybackFrame:
    turn_id: int
    payload: bytes
    is_underrun_silence: bool = False


@dataclass
class ConversationExchange:
    turn_id: int
    status: str = "completed"
    input_transcript: str = ""
    output_transcript: str = ""
    heard_output_transcript: str = ""
    question_id: str | None = None
    reply_id: str | None = None
    played_audio_ms: int = 0
    playback_completed: bool = False
    source: str = ""
    created_at_ms: int | None = None


class RealtimeSessionProtocol:
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def append_audio(self, input_pcm_16k: bytes) -> None: ...

    async def cancel_response(self) -> None: ...

    async def send_tts_text(self, text: str) -> None: ...

    async def handle_playback_interruption(
        self,
        *,
        interrupted_output_text: str | None = None,
    ) -> None: ...

    async def seed_assistant_context(
        self,
        text: str,
        *,
        source: str = "external",
    ) -> None: ...


class PlaybackControlProtocol:
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def break_playback(self, media_uuid: str) -> bool: ...


class PromptStoreProtocol:
    async def get_prompt_snapshot(
        self,
        scene: str | None = None,
        *,
        fallback_instructions: str | None = None,
    ) -> PromptSnapshot: ...


class CallResultWriterProtocol:
    def enqueue_nowait(self, payload: dict) -> bool: ...


RealtimeSessionFactory = Callable[
    [
        Callable[[int], Awaitable[None]],
        Callable[[int, str], Awaitable[None]],
        Callable[[int, bytes], Awaitable[None]],
        Callable[[RealtimeTurnResult], Awaitable[None]],
        int,
        str,
        str | None,
        RealtimeDialogConfig,
    ],
    RealtimeSessionProtocol,
]
CallAnsweredPredicate = Callable[[str], bool]
PromptSnapshotProvider = Callable[[str], PromptSnapshot | None]
CallContextProvider = Callable[[str], Mapping[str, Any] | None]
CallRecordingPathProvider = Callable[[str], str | None]
HandoffRequester = Callable[[str, dict[str, Any]], Mapping[str, Any]]
AgentTakeoverSuggestionRecorder = Callable[[str, dict[str, Any]], Mapping[str, Any]]


@dataclass
class RealtimePhoneSessionStats:
    call_id: str
    session_id: str
    connected_at: float
    last_seen_at: float
    expected_frame_bytes: int
    recording_path: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    inbound_frames: int = 0
    inbound_bytes: int = 0
    outbound_frames: int = 0
    outbound_bytes: int = 0
    invalid_frame_count: int = 0
    interruptions: int = 0
    dropped_playback_frames: int = 0
    dropped_stale_frames: int = 0
    playback_underruns: int = 0
    max_playback_queue_frames: int = 0
    max_playback_send_gap_ms: int = 0
    playback_send_gap_overruns: int = 0
    playback_fast_send_frames: int = 0
    playback_realtime_send_frames: int = 0
    playback_pacing_switches: int = 0
    flushed_tail_frames: int = 0
    tail_silence_frames: int = 0
    freeswitch_playback_events: int = 0
    freeswitch_queue_completed_events: int = 0
    freeswitch_last_playback_remaining: int | None = None
    freeswitch_break_requests: int = 0
    freeswitch_break_failures: int = 0
    realtime_interrupt_requests: int = 0
    realtime_interrupt_failures: int = 0
    context_repair_requests: int = 0
    local_barge_in_events: int = 0
    turns_started: int = 0
    turns_committed: int = 0
    turns_completed: int = 0
    turns_failed: int = 0
    streamed_input_bytes: int = 0
    inbound_rms_min: int | None = None
    inbound_rms_max: int | None = None
    inbound_rms_last: int | None = None
    inbound_rms_sum: int = 0
    inbound_rms_count: int = 0
    inbound_high_rms_frames: int = 0
    inbound_first_high_rms_frame: int | None = None
    first_audio_at: float | None = None
    first_playback_at: float | None = None
    playback_last_send_at: float | None = None
    playback_last_send_turn_id: int | None = None
    disconnected_at: float | None = None
    failure_reason: str | None = None
    failure_error: str | None = None
    control_messages: list[str] = field(default_factory=list)
    output_transcripts_by_turn: dict[int, str] = field(default_factory=dict)
    opening_text: str | None = field(default=None, repr=False)
    opening_text_hash: str | None = None
    opening_voice: str | None = None
    opening_speaker: str | None = None
    opening_playback_frames: int = 0
    opening_playback_started_at: float | None = None
    opening_playback_completed_at: float | None = None
    opening_playback_interrupted: bool = False
    opening_playback_sent_frames: int = 0
    opening_last_playback_rms: int | None = None
    opening_last_playback_at: float | None = None
    opening_trigger_rms: int | None = None
    opening_trigger_rms_min: int | None = None
    opening_trigger_rms_max: int | None = None
    opening_trigger_rms_avg: int | None = None
    opening_trigger_best_playback_correlation: float | None = None
    opening_trigger_best_playback_frame: int | None = None
    opening_trigger_best_playback_rms: int | None = None
    opening_trigger_last_playback_age_ms: int | None = None
    opening_echo_suppressed_frames: int = 0
    opening_answer_wait_ms: int | None = None
    pending_exchanges: dict[int, ConversationExchange] = field(default_factory=dict)
    committed_exchanges: list[ConversationExchange] = field(default_factory=list)
    closed_output_turn_ids: set[int] = field(default_factory=set, repr=False)
    gateway_history_committed_turns: int = 0
    gateway_history_abandoned_turns: int = 0
    gateway_history_completed_turns: int = 0
    gateway_history_interrupted_turns: int = 0
    gateway_history_missing_output_turns: int = 0
    realtime_session_restarts: int = 0
    replayed_input_frames: int = 0
    replayed_input_bytes: int = 0
    last_realtime_turn_id: int = 0
    current_capture_turn_id: int | None = None
    current_output_turn_id: int | None = None
    turn_speech_started_at: dict[int, float] = field(
        default_factory=dict,
        repr=False,
    )
    turn_first_model_audio_at: dict[int, float] = field(
        default_factory=dict,
        repr=False,
    )
    turn_model_done_at: dict[int, float] = field(default_factory=dict, repr=False)
    turn_first_playback_at: dict[int, float] = field(
        default_factory=dict,
        repr=False,
    )
    turn_last_playback_at: dict[int, float] = field(
        default_factory=dict,
        repr=False,
    )
    turn_max_playback_send_gap_ms: dict[int, int] = field(
        default_factory=dict,
        repr=False,
    )
    turn_model_first_audio_delta_ms: dict[int, int | None] = field(
        default_factory=dict,
        repr=False,
    )
    turn_asr_ended_ms: dict[int, int | None] = field(
        default_factory=dict,
        repr=False,
    )
    playback_buffers: dict[int, "DownsampledPlaybackBuffer"] = field(
        default_factory=dict,
        repr=False,
    )
    playback_queue: asyncio.Queue[PlaybackFrame | None] = field(
        default_factory=asyncio.Queue,
        repr=False,
    )
    model_done_turns: set[int] = field(default_factory=set, repr=False)
    freeswitch_completed_turns: set[int] = field(default_factory=set, repr=False)
    jitter_prefilled_turns: set[int] = field(default_factory=set, repr=False)
    playout_pacing_states: dict[int, PlayoutPacingState] = field(
        default_factory=dict,
        repr=False,
    )
    recent_input_frames_16k: deque[bytes] = field(default_factory=deque, repr=False)
    repair_replay_frames_16k: deque[bytes] = field(default_factory=deque, repr=False)
    opening_inbound_rms_values: deque[int] = field(
        default_factory=lambda: deque(maxlen=120),
        repr=False,
    )
    opening_recent_playback_frames: deque[bytes] = field(
        default_factory=lambda: deque(maxlen=120),
        repr=False,
    )
    opening_recent_playback_frame_numbers: deque[int] = field(
        default_factory=lambda: deque(maxlen=120),
        repr=False,
    )
    realtime_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    opening_barge_in_detector: EnergyVadTurnDetector | None = field(
        default=None,
        repr=False,
    )
    interruption_repair_active: bool = False
    playback_active: bool = False
    handoff_requested: bool = False
    handoff_transitioning: bool = False
    handoff_completed: bool = False
    handoff_trigger_turn_id: int | None = None
    handoff_error: str | None = None
    handoff_result: dict[str, Any] | None = field(default=None, repr=False)
    agent_takeover_suggestion_requested: bool = False
    agent_takeover_suggestion_result: dict[str, Any] | None = field(
        default=None,
        repr=False,
    )
    agent_takeover_suggestion_error: str | None = None
    prompt_scene: str = "default"
    prompt_snapshot: PromptSnapshot | None = None
    background_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)


class FreeSwitchRealtimeGatewayServer:
    """FreeSWITCH media server backed by Doubao S2S Server VAD."""

    def __init__(
        self,
        config: GatewayConfig,
        *,
        api_key: str,
        instructions: str = DEFAULT_PHONE_INSTRUCTIONS,
        frame_duration_ms: int | None = None,
        model_output_sample_rate: int | None = None,
        realtime_session_factory: RealtimeSessionFactory | None = None,
        playback_control: PlaybackControlProtocol | None = None,
        prompt_store: PromptStoreProtocol | None = None,
        call_result_writer: CallResultWriterProtocol | None = None,
        on_media_connected: Callable[[str], None] | None = None,
        on_media_disconnected: Callable[[str], None] | None = None,
        opening_store: OpeningAudioStore | None = None,
        is_call_answered: CallAnsweredPredicate | None = None,
        prompt_snapshot_provider: PromptSnapshotProvider | None = None,
        call_context_provider: CallContextProvider | None = None,
        call_recording_path_provider: CallRecordingPathProvider | None = None,
        handoff_requester: HandoffRequester | None = None,
        agent_takeover_suggestion_recorder: (
            AgentTakeoverSuggestionRecorder | None
        ) = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")

        self.config = config
        self.api_key = api_key
        self.instructions = instructions
        self.frame_duration_ms = (
            frame_duration_ms
            if frame_duration_ms is not None
            else config.freeswitch.frame_duration_ms
        )
        self.model_output_sample_rate = (
            model_output_sample_rate
            if model_output_sample_rate is not None
            else DEFAULT_OUTPUT_SAMPLE_RATE
        )
        self.contract = PhoneMediaContract.from_config(
            config.freeswitch,
            frame_duration_ms=self.frame_duration_ms,
        )
        self.contract.validate_realtime_phone_contract()
        self.expected_frame_bytes = pcm_s16le_frame_bytes(
            self.contract.sample_rate,
            self.frame_duration_ms,
            channels=self.contract.channels,
        )
        if config.playback.tail_silence_ms < 0:
            raise ValueError("playback.tail_silence_ms must be non-negative")
        if config.playback.tail_silence_ms % self.frame_duration_ms != 0:
            raise ValueError(
                "playback.tail_silence_ms must align to frame_duration_ms"
            )
        self.playback_prefill_frames = max(
            1,
            config.playback.jitter_buffer_ms // self.frame_duration_ms,
        )
        self.playout_controller = PlayoutController(
            PlayoutControllerConfig(
                frame_duration_ms=self.frame_duration_ms,
                fast_send_interval_ms=config.playback.send_interval_ms,
                prefill_frames=self.playback_prefill_frames,
            )
        )
        self.playback_send_interval_ms = (
            self.playout_controller.fast_send_interval_ms
        )
        self.replay_frame_limit = max(
            1,
            DEFAULT_REPLAY_AUDIO_MS // self.frame_duration_ms,
        )
        self.repair_replay_frame_limit = max(1, self.replay_frame_limit * 4)
        self._server: WebSocketServer | None = None
        self._address: tuple[str, int] = (
            config.freeswitch.media_host,
            config.freeswitch.media_port,
        )
        self.active_sessions: dict[str, RealtimePhoneSessionStats] = {}
        self.completed_sessions: list[RealtimePhoneSessionStats] = []
        self._realtime_session_factory = realtime_session_factory
        self._realtime_sessions: dict[str, RealtimeSessionProtocol] = {}
        self.playback_control = playback_control or self._create_playback_control()
        self.prompt_store = prompt_store
        self.prompt_snapshot_provider = prompt_snapshot_provider
        self.call_result_writer = call_result_writer
        self._on_media_connected = on_media_connected
        self._on_media_disconnected = on_media_disconnected
        self.opening_store = opening_store
        self._is_call_answered = is_call_answered
        self.call_context_provider = call_context_provider
        self.call_recording_path_provider = call_recording_path_provider
        self._handoff_requester = handoff_requester
        self._agent_takeover_suggestion_recorder = (
            agent_takeover_suggestion_recorder
        )

    @property
    def address(self) -> tuple[str, int]:
        return self._address

    async def start(self) -> None:
        await self._start_playback_control()
        try:
            self._server = await serve(
                self._handle_connection,
                self.config.freeswitch.media_host,
                self.config.freeswitch.media_port,
                ping_interval=None,
            )
        except Exception:
            await self._stop_playback_control()
            raise
        if self._server.sockets:
            sockname = self._server.sockets[0].getsockname()
            self._address = (str(sockname[0]), int(sockname[1]))

        model_name, voice_name = self._realtime_identity_for_log()
        LOGGER.info(
            "freeswitch_realtime_gateway_started mode=server_vad host=%s port=%s "
            "phone_sample_rate=%s model_input_sample_rate=%s "
            "model_output_sample_rate=%s frame_duration_ms=%s channels=%s "
            "phone_codec=%s expected_frame_bytes=%s encoded_payload_bytes=%s "
            "provider=%s model=%s voice=%s server_vad_type=%s "
            "server_vad_threshold=%s server_vad_silence_duration_ms=%s "
            "playback_jitter_buffer_ms=%s playback_tail_silence_ms=%s "
            "playback_send_interval_ms=%s playback_prefill_frames=%s "
            "playback_low_watermark_frames=%s "
            "playback_high_watermark_frames=%s "
            "event_socket_enabled=%s",
            self._address[0],
            self._address[1],
            self.contract.sample_rate,
            DEFAULT_INPUT_SAMPLE_RATE,
            self.model_output_sample_rate,
            self.frame_duration_ms,
            self.contract.channels,
            self.contract.codec,
            self.expected_frame_bytes,
            self.contract.encoded_payload_bytes,
            "doubao_s2s",
            model_name,
            voice_name,
            self.config.server_vad.type,
            self.config.server_vad.threshold,
            self.config.server_vad.silence_duration_ms,
            self.config.playback.jitter_buffer_ms,
            self.config.playback.tail_silence_ms,
            self.playback_send_interval_ms,
            self.playback_prefill_frames,
            self.playout_controller.low_watermark_frames,
            self.playout_controller.high_watermark_frames,
            self.config.event_socket.enabled,
        )

    def _realtime_identity_for_log(self) -> tuple[str, str]:
        return self.config.doubao_s2s.resource_id, self.config.doubao_s2s.speaker

    def _notify_media_connected(self, call_id: str) -> None:
        if self._on_media_connected is None:
            return
        try:
            self._on_media_connected(call_id)
        except Exception:
            LOGGER.warning("media_connected_callback_failed call_id=%s", call_id)

    def _notify_media_disconnected(self, call_id: str) -> None:
        if self._on_media_disconnected is None:
            return
        try:
            self._on_media_disconnected(call_id)
        except Exception:
            LOGGER.warning("media_disconnected_callback_failed call_id=%s", call_id)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        await self._stop_playback_control()
        LOGGER.info("freeswitch_realtime_gateway_stopped")

    async def serve_forever(self) -> None:
        await self.start()
        try:
            await asyncio.Future()
        finally:
            await self.stop()

    async def _handle_connection(
        self,
        websocket: WebSocketServerProtocol,
    ) -> None:
        path = websocket.path
        call_id = _call_id_from_path(path)
        if call_id is None:
            LOGGER.warning("rejecting_unsupported_realtime_media_path path=%s", path)
            await websocket.close(code=1008, reason="unsupported path")
            return

        now = time.time()
        opening_audio = self._pop_opening_audio(call_id)
        if (
            opening_audio is not None
            and _contains_spoken_amount(opening_audio.opening_text)
        ):
            LOGGER.warning(
                "opening_playback_skipped_sensitive_amount call_id=%s text_hash=%s",
                call_id,
                opening_audio.opening_text_hash,
            )
            opening_audio = None
        context = self._load_call_context(call_id)
        recording_path = self._load_call_recording_path(call_id)
        session = RealtimePhoneSessionStats(
            call_id=call_id,
            session_id=uuid.uuid4().hex,
            connected_at=now,
            last_seen_at=now,
            expected_frame_bytes=self.expected_frame_bytes,
            recording_path=recording_path,
            context=context,
            opening_text=(
                None if opening_audio is None else opening_audio.opening_text
            ),
            opening_text_hash=(
                None if opening_audio is None else opening_audio.opening_text_hash
            ),
            opening_voice=None if opening_audio is None else opening_audio.voice,
            opening_speaker=None if opening_audio is None else opening_audio.speaker,
        )
        playback_task = asyncio.create_task(
            self._playback_worker(websocket, session),
            name=f"playback-{session.session_id}",
        )
        self.active_sessions[session.session_id] = session

        LOGGER.info(
            "freeswitch_realtime_media_connected call_id=%s session_id=%s peer=%s "
            "turn_mode=server_vad",
            call_id,
            session.session_id,
            websocket.remote_address,
        )
        self._notify_media_connected(call_id)

        try:
            session.prompt_snapshot = await self._load_prompt_snapshot(session)
            try:
                await self._connect_realtime_session(session)
            except Exception as err:
                session.failure_reason = "realtime_session_connect_failed"
                session.failure_error = str(err)
                LOGGER.warning(
                    "realtime_session_connect_failed call_id=%s session_id=%s "
                    "error=%s",
                    session.call_id,
                    session.session_id,
                    err,
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await websocket.close(
                        code=1011,
                        reason="realtime session connect failed",
                    )
                return
            self._schedule_opening_playback(session, opening_audio)
            async for message in websocket:
                session.last_seen_at = time.time()
                if isinstance(message, bytes):
                    await self._handle_audio_frame(session, message)
                    continue
                await self._handle_control_message(websocket, session, message)
        except ConnectionClosed as err:
            LOGGER.info(
                "freeswitch_realtime_media_disconnected call_id=%s session_id=%s "
                "code=%s",
                call_id,
                session.session_id,
                err.code,
            )
        finally:
            self._notify_media_disconnected(call_id)
            await self._shutdown_session(session, playback_task)

    async def _connect_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> RealtimeSessionProtocol:
        realtime_session = self._create_realtime_session(session)
        self._realtime_sessions[session.session_id] = realtime_session
        await realtime_session.connect()
        LOGGER.info(
            "realtime_session_connected call_id=%s session_id=%s "
            "turn_mode=server_vad committed_history_turns=%s "
            "realtime_session_restarts=%s",
            session.call_id,
            session.session_id,
            len(session.committed_exchanges),
            session.realtime_session_restarts,
        )
        return realtime_session

    async def _load_prompt_snapshot(
        self,
        session: RealtimePhoneSessionStats,
    ) -> PromptSnapshot | None:
        if self.prompt_snapshot_provider is not None:
            try:
                snapshot = self.prompt_snapshot_provider(session.call_id)
            except Exception:
                LOGGER.warning(
                    "prebuilt_prompt_snapshot_load_failed call_id=%s session_id=%s",
                    session.call_id,
                    session.session_id,
                    exc_info=True,
                )
            else:
                if snapshot is not None:
                    LOGGER.info(
                        "prebuilt_prompt_snapshot_loaded call_id=%s session_id=%s "
                        "scene=%s version=%s content_hash=%s",
                        session.call_id,
                        session.session_id,
                        snapshot.scene,
                        snapshot.version,
                        snapshot.content_hash,
                    )
                    return snapshot

        if self.prompt_store is None:
            return None
        try:
            snapshot = await self.prompt_store.get_prompt_snapshot(
                session.prompt_scene,
                fallback_instructions=self.instructions,
            )
        except Exception:
            LOGGER.warning(
                "prompt_snapshot_load_failed call_id=%s session_id=%s scene=%s",
                session.call_id,
                session.session_id,
                session.prompt_scene,
                exc_info=True,
            )
            return None

        LOGGER.info(
            "prompt_snapshot_loaded call_id=%s session_id=%s scene=%s "
            "version=%s content_hash=%s",
            session.call_id,
            session.session_id,
            snapshot.scene,
            snapshot.version,
            snapshot.content_hash,
        )
        return snapshot

    def _create_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> RealtimeSessionProtocol:
        async def on_speech_started(turn_id: int) -> None:
            await self._handle_server_vad_speech_started(session, turn_id)

        async def on_input_transcript(turn_id: int, text: str) -> None:
            await self._handle_input_transcript_available(session, turn_id, text)

        async def on_audio_delta(turn_id: int, audio_delta_24k: bytes) -> None:
            await self._queue_audio_delta(session, turn_id, audio_delta_24k)

        async def on_turn_completed(result: RealtimeTurnResult) -> None:
            await self._finalize_server_vad_turn(session, result)

        if self._realtime_session_factory is not None:
            return self._realtime_session_factory(
                on_speech_started,
                on_input_transcript,
                on_audio_delta,
                on_turn_completed,
                session.last_realtime_turn_id,
                self._instructions_for_realtime_session(session),
                session.opening_speaker,
                self._dialog_config_for_realtime_session(session),
            )

        raise RuntimeError("realtime_session_factory is required for realtime mode")

    def _dialog_config_for_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> RealtimeDialogConfig:
        dialog_context = self._dialog_context_for_realtime_session(session)
        if session.prompt_snapshot is None:
            return RealtimeDialogConfig(dialog_context=dialog_context)

        employee_name = _dialog_text(
            session.prompt_snapshot.metadata.get("employee_name")
        )
        if not employee_name:
            return RealtimeDialogConfig(dialog_context=dialog_context)

        identity_name = _dialog_text(
            session.prompt_snapshot.metadata.get("identityName")
        )
        speaking_style = _dialog_text(
            session.prompt_snapshot.metadata.get("speaking_style")
        )
        system_role = _business_dialog_system_role(
            employee_name,
            identity_name,
            business_instructions=self._instructions_for_realtime_session(session),
        )
        speaking_style = speaking_style or DIALOG_SPEAKING_STYLE
        _log_dialog_prompt_lengths(
            call_id=session.call_id,
            session_id=session.session_id,
            system_role=system_role,
            speaking_style=speaking_style,
        )
        return RealtimeDialogConfig(
            bot_name=_dialog_bot_name(employee_name),
            system_role=system_role,
            speaking_style=speaking_style,
            model=DEFAULT_DIALOG_MODEL,
            dialog_context=dialog_context,
        )

    def _dialog_context_for_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> tuple[RealtimeDialogContextItem, ...]:
        items: list[RealtimeDialogContextItem] = []
        remaining_chars = MAX_COMMITTED_HISTORY_CHARS
        selected = session.committed_exchanges[-MAX_COMMITTED_HISTORY_EXCHANGES:]
        for exchange in selected:
            user_text = exchange.input_transcript.strip()
            assistant_text = exchange.output_transcript.strip()
            if not user_text or not assistant_text:
                continue
            block_len = len(user_text) + len(assistant_text)
            if block_len > remaining_chars:
                continue
            items.append(
                RealtimeDialogContextItem(
                    role="user",
                    text=_redact_spoken_amounts(user_text),
                )
            )
            items.append(
                RealtimeDialogContextItem(
                    role="assistant",
                    text=_redact_spoken_amounts(assistant_text),
                )
            )
            remaining_chars -= block_len
        return tuple(items)

    def _instructions_for_realtime_session(
        self,
        session: RealtimePhoneSessionStats,
    ) -> str:
        instructions = self.instructions
        if session.prompt_snapshot is not None:
            instructions = session.prompt_snapshot.instructions

        opening_lines = []
        if session.opening_text:
            opening_lines = [
                "",
                OPENING_BUSINESS_GUARD,
                "",
                "本通话开始时，系统已经向用户播放了以下开场白。"
                "用户接下来的简短回答可能是在回应这段开场白：",
                f"客服：{_redact_spoken_amounts(session.opening_text)}",
            ]

        return "\n".join([instructions, "", LATEST_UTTERANCE_GUARD, *opening_lines])

    def _pop_opening_audio(self, call_id: str) -> PreparedOpeningAudio | None:
        if self.opening_store is None:
            return None
        return self.opening_store.pop(call_id)

    def _load_call_context(self, call_id: str) -> dict[str, Any]:
        if self.call_context_provider is None:
            return {}
        try:
            context = self.call_context_provider(call_id)
        except Exception:
            LOGGER.warning(
                "call_context_load_failed call_id=%s",
                call_id,
                exc_info=True,
            )
            return {}
        if not isinstance(context, Mapping):
            return {}
        return dict(context)

    def _load_call_recording_path(self, call_id: str) -> str | None:
        if self.call_recording_path_provider is None:
            return None
        try:
            recording_path = self.call_recording_path_provider(call_id)
        except Exception:
            LOGGER.warning(
                "call_recording_path_load_failed call_id=%s",
                call_id,
                exc_info=True,
            )
            return None
        if not isinstance(recording_path, str):
            return None
        recording_path = recording_path.strip()
        return recording_path or None

    def _schedule_opening_playback(
        self,
        session: RealtimePhoneSessionStats,
        opening_audio: PreparedOpeningAudio | None,
    ) -> None:
        if opening_audio is None or not opening_audio.phone_frames:
            return

        async def runner() -> None:
            await self._wait_for_call_answer_before_opening(session)
            await self._start_opening_playback(session, opening_audio)

        task = asyncio.create_task(
            runner(),
            name=f"opening-playback-{session.session_id}",
        )
        session.background_tasks.add(task)
        task.add_done_callback(session.background_tasks.discard)

    async def _wait_for_call_answer_before_opening(
        self,
        session: RealtimePhoneSessionStats,
    ) -> None:
        if self._is_call_answered is None:
            return
        if self._call_is_answered(session.call_id):
            session.opening_answer_wait_ms = 0
            return

        started_at = time.monotonic()
        LOGGER.info(
            "opening_playback_waiting_for_answer call_id=%s session_id=%s "
            "text_hash=%s",
            session.call_id,
            session.session_id,
            session.opening_text_hash,
        )
        while not self._call_is_answered(session.call_id):
            await asyncio.sleep(0.05)

        session.opening_answer_wait_ms = int(
            (time.monotonic() - started_at) * 1000
        )
        LOGGER.info(
            "opening_playback_answer_detected call_id=%s session_id=%s "
            "text_hash=%s wait_ms=%s",
            session.call_id,
            session.session_id,
            session.opening_text_hash,
            session.opening_answer_wait_ms,
        )

    def _call_is_answered(self, call_id: str) -> bool:
        if self._is_call_answered is None:
            return True
        try:
            return self._is_call_answered(call_id)
        except Exception:
            LOGGER.warning("call_answer_state_callback_failed call_id=%s", call_id)
            return True

    async def _start_opening_playback(
        self,
        session: RealtimePhoneSessionStats,
        opening_audio: PreparedOpeningAudio | None,
    ) -> None:
        if opening_audio is None or not opening_audio.phone_frames:
            return

        self._write_opening_source_debug_wav(session, opening_audio)
        now = time.monotonic()
        session.current_output_turn_id = OPENING_TURN_ID
        session.opening_playback_started_at = time.time()
        session.opening_playback_frames = len(opening_audio.phone_frames)
        session.turn_speech_started_at[OPENING_TURN_ID] = now
        session.turn_first_model_audio_at[OPENING_TURN_ID] = now
        session.turn_model_done_at[OPENING_TURN_ID] = now
        session.turn_model_first_audio_delta_ms[OPENING_TURN_ID] = 0
        session.output_transcripts_by_turn[OPENING_TURN_ID] = (
            opening_audio.opening_text
        )
        session.model_done_turns.add(OPENING_TURN_ID)
        session.opening_barge_in_detector = self._create_opening_barge_in_detector()

        for payload in self._opening_recording_warmup_frames(session):
            await self._enqueue_playback_frame(
                session,
                PlaybackFrame(OPENING_TURN_ID, payload),
            )

        for payload in opening_audio.phone_frames:
            await self._enqueue_playback_frame(
                session,
                PlaybackFrame(OPENING_TURN_ID, payload),
            )

        LOGGER.info(
            "opening_playback_queued call_id=%s session_id=%s text_hash=%s "
            "voice=%s frames=%s source_audio_bytes=%s generation_ms=%s "
            "answer_wait_ms=%s",
            session.call_id,
            session.session_id,
            opening_audio.opening_text_hash,
            opening_audio.voice,
            len(opening_audio.phone_frames),
            opening_audio.source_audio_bytes,
            opening_audio.generation_ms,
            session.opening_answer_wait_ms,
        )
        self._schedule_opening_context_seed(session, opening_audio.opening_text)

    def _opening_recording_warmup_frames(
        self,
        session: RealtimePhoneSessionStats,
    ) -> list[bytes]:
        config = self.config.call_recording
        if not config.enabled or not session.recording_path:
            return []
        if config.opening_warmup_ms <= 0:
            return []
        frame_count = math.ceil(config.opening_warmup_ms / self.frame_duration_ms)
        if frame_count <= 0:
            return []
        silence_frame = b"\x00" * self.expected_frame_bytes
        return [silence_frame] * frame_count

    def _write_opening_source_debug_wav(
        self,
        session: RealtimePhoneSessionStats,
        opening_audio: PreparedOpeningAudio,
    ) -> None:
        config = self.config.call_recording
        if (
            not config.enabled
            or not config.opening_source_debug_enabled
            or not session.recording_path
        ):
            return
        try:
            recording_path = build_recording_host_path(config, session.recording_path)
            source_path = recording_path.with_suffix(".opening-source.wav")
            write_pcm16_wav(
                source_path,
                b"".join(opening_audio.phone_frames),
                sample_rate=self.contract.sample_rate,
                channels=self.contract.channels,
            )
        except (OSError, RecordingUploadError, ValueError):
            LOGGER.warning(
                "opening_source_debug_wav_write_failed call_id=%s "
                "session_id=%s recording_path=%s",
                session.call_id,
                session.session_id,
                session.recording_path,
                exc_info=True,
            )

    def _schedule_opening_context_seed(
        self,
        session: RealtimePhoneSessionStats,
        opening_text: str,
    ) -> None:
        realtime_session = self._realtime_sessions.get(session.session_id)
        if realtime_session is None:
            return
        seed_context = getattr(realtime_session, "seed_assistant_context", None)
        if seed_context is None:
            return

        async def runner() -> None:
            started_at = time.monotonic()
            LOGGER.info(
                "opening_context_seed_started call_id=%s session_id=%s "
                "text_hash=%s text_chars=%s",
                session.call_id,
                session.session_id,
                session.opening_text_hash,
                len(opening_text),
            )
            try:
                await seed_context(opening_text, source="opening_context")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.warning(
                    "opening_context_seed_failed call_id=%s session_id=%s "
                    "text_hash=%s elapsed_ms=%s",
                    session.call_id,
                    session.session_id,
                    session.opening_text_hash,
                    int((time.monotonic() - started_at) * 1000),
                    exc_info=True,
                )
                return

            LOGGER.info(
                "opening_context_seeded call_id=%s session_id=%s text_hash=%s "
                "elapsed_ms=%s",
                session.call_id,
                session.session_id,
                session.opening_text_hash,
                int((time.monotonic() - started_at) * 1000),
            )

        task = asyncio.create_task(
            runner(),
            name=f"opening-context-seed-{session.session_id}",
        )
        session.background_tasks.add(task)
        task.add_done_callback(session.background_tasks.discard)

    def _create_opening_barge_in_detector(self) -> EnergyVadTurnDetector | None:
        if not self.config.vad.barge_in_enabled:
            return None
        return EnergyVadTurnDetector(
            self.config.vad,
            frame_bytes=self.expected_frame_bytes,
            frame_duration_ms=self.frame_duration_ms,
        )

    def _create_playback_control(self) -> PlaybackControlProtocol | None:
        if not self.config.event_socket.enabled:
            return None

        password = os.getenv(self.config.event_socket.password_env)
        if not password:
            raise ValueError(
                f"{self.config.event_socket.password_env} is required when "
                "event_socket.enabled=true"
            )

        return FreeSwitchPlaybackController.from_config(
            self.config.event_socket,
            password=password,
            on_playback_event=self._handle_freeswitch_playback_event,
        )

    async def _start_playback_control(self) -> None:
        if self.playback_control is None:
            return
        start = getattr(self.playback_control, "start", None)
        if start is not None:
            await start()

    async def _stop_playback_control(self) -> None:
        if self.playback_control is None:
            return
        stop = getattr(self.playback_control, "stop", None)
        if stop is not None:
            await stop()

    async def _handle_audio_frame(
        self,
        session: RealtimePhoneSessionStats,
        payload: bytes,
    ) -> None:
        if session.first_audio_at is None:
            session.first_audio_at = time.time()
            LOGGER.info(
                "first_freeswitch_realtime_audio call_id=%s session_id=%s bytes=%s",
                session.call_id,
                session.session_id,
                len(payload),
            )

        session.inbound_frames += 1
        session.inbound_bytes += len(payload)

        if len(payload) != self.expected_frame_bytes:
            session.invalid_frame_count += 1
            LOGGER.warning(
                "freeswitch_realtime_frame_size_mismatch call_id=%s "
                "session_id=%s bytes=%s expected=%s frame=%s",
                session.call_id,
                session.session_id,
                len(payload),
                self.expected_frame_bytes,
                session.inbound_frames,
            )
            return

        if self.config.features.inbound_rms_diagnostics_enabled:
            _record_inbound_audio_rms(
                session,
                payload,
                threshold=self.config.vad.speech_rms_threshold,
            )

        frame_16k = resample_pcm_s16le_mono(
            payload,
            self.config.freeswitch.sample_rate,
            DEFAULT_INPUT_SAMPLE_RATE,
        )
        session.recent_input_frames_16k.append(frame_16k)
        while len(session.recent_input_frames_16k) > self.replay_frame_limit:
            session.recent_input_frames_16k.popleft()

        if self._handle_local_opening_barge_in(session, payload):
            return

        async with session.realtime_lock:
            if session.handoff_requested or session.handoff_transitioning:
                return
            if session.interruption_repair_active:
                session.repair_replay_frames_16k.append(frame_16k)
                return

            realtime_session = self._realtime_sessions.get(session.session_id)
            if realtime_session is None:
                return
            await realtime_session.append_audio(frame_16k)
            session.streamed_input_bytes += len(frame_16k)

    def _handle_local_opening_barge_in(
        self,
        session: RealtimePhoneSessionStats,
        payload: bytes,
    ) -> bool:
        detector = session.opening_barge_in_detector
        if detector is None:
            return False
        if session.current_output_turn_id != OPENING_TURN_ID:
            return False
        if session.opening_playback_interrupted:
            return False
        if session.opening_playback_completed_at is not None:
            return False
        if session.interruption_repair_active:
            return False
        if not self._opening_barge_in_is_armed(session):
            return False

        inbound_rms = pcm_s16le_rms(payload)
        session.opening_inbound_rms_values.append(inbound_rms)

        event = detector.process_frame_event(payload)
        if not event.started:
            return False

        playback_elapsed_ms = None
        if session.opening_playback_started_at is not None:
            playback_elapsed_ms = int(
                (time.time() - session.opening_playback_started_at) * 1000
            )
        rms_min, rms_max, rms_avg = _int_window_stats(
            session.opening_inbound_rms_values
        )
        reference_match = _best_playback_reference_match(session, payload)
        last_playback_age_ms = _elapsed_ms(session.opening_last_playback_at)
        if _opening_barge_in_looks_like_playback_echo(
            reference_match,
            last_playback_age_ms,
        ):
            detector.reset()
            session.opening_echo_suppressed_frames += 1
            LOGGER.info(
                "realtime_phone_opening_echo_suppressed call_id=%s "
                "session_id=%s trigger_rms=%s opening_sent_frames=%s "
                "opening_last_playback_rms=%s opening_last_playback_age_ms=%s "
                "opening_best_playback_correlation=%s "
                "opening_best_playback_frame=%s opening_best_playback_rms=%s",
                session.call_id,
                session.session_id,
                inbound_rms,
                session.opening_playback_sent_frames,
                session.opening_last_playback_rms,
                last_playback_age_ms,
                _format_correlation(reference_match.correlation),
                reference_match.frame_number,
                reference_match.rms,
            )
            return True

        session.opening_trigger_rms = inbound_rms
        session.opening_trigger_rms_min = rms_min
        session.opening_trigger_rms_max = rms_max
        session.opening_trigger_rms_avg = rms_avg
        session.opening_trigger_best_playback_correlation = (
            reference_match.correlation
        )
        session.opening_trigger_best_playback_frame = reference_match.frame_number
        session.opening_trigger_best_playback_rms = reference_match.rms
        session.opening_trigger_last_playback_age_ms = last_playback_age_ms
        session.local_barge_in_events += 1
        session.interruption_repair_active = True
        session.repair_replay_frames_16k = deque(
            session.recent_input_frames_16k,
            maxlen=self.repair_replay_frame_limit,
        )
        asyncio.create_task(
            self._run_interruption_repair(
                session,
                reason="local_opening_barge_in",
            ),
            name=f"opening-barge-in-repair-{session.session_id}",
        )
        LOGGER.info(
            "realtime_phone_local_opening_barge_in_started call_id=%s "
            "session_id=%s threshold=%s start_speech_ms=%s trigger_rms=%s "
            "opening_playback_elapsed_ms=%s replay_frames=%s "
            "inbound_rms_min=%s inbound_rms_max=%s inbound_rms_avg=%s "
            "opening_sent_frames=%s opening_last_playback_rms=%s "
            "opening_last_playback_age_ms=%s "
            "opening_best_playback_correlation=%s "
            "opening_best_playback_frame=%s opening_best_playback_rms=%s",
            session.call_id,
            session.session_id,
            self.config.vad.speech_rms_threshold,
            self.config.vad.start_speech_ms,
            inbound_rms,
            playback_elapsed_ms,
            len(session.repair_replay_frames_16k),
            rms_min,
            rms_max,
            rms_avg,
            session.opening_playback_sent_frames,
            session.opening_last_playback_rms,
            last_playback_age_ms,
            _format_correlation(reference_match.correlation),
            reference_match.frame_number,
            reference_match.rms,
        )
        return True

    def _opening_barge_in_is_armed(
        self,
        session: RealtimePhoneSessionStats,
    ) -> bool:
        if session.opening_playback_sent_frames < OPENING_BARGE_IN_MIN_SENT_FRAMES:
            return False

        first_playback_at = session.turn_first_playback_at.get(OPENING_TURN_ID)
        audible_ms = _elapsed_ms(first_playback_at)
        return (
            audible_ms is not None
            and audible_ms >= OPENING_BARGE_IN_MIN_PLAYBACK_MS
        )

    async def _handle_server_vad_speech_started(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
    ) -> None:
        was_busy = self._session_is_busy(session)
        previous_capture_turn_id = session.current_capture_turn_id
        session.last_realtime_turn_id = max(session.last_realtime_turn_id, turn_id)
        if turn_id not in session.turn_speech_started_at:
            session.turns_started += 1
            session.turn_speech_started_at[turn_id] = time.monotonic()
        session.current_capture_turn_id = turn_id
        LOGGER.info(
            "realtime_phone_server_vad_speech_started call_id=%s session_id=%s "
            "turn=%s threshold=%s silence_duration_ms=%s",
            session.call_id,
            session.session_id,
            turn_id,
            self.config.server_vad.threshold,
            self.config.server_vad.silence_duration_ms,
        )

        if was_busy:
            if (
                previous_capture_turn_id is not None
                and previous_capture_turn_id != turn_id
                and previous_capture_turn_id != session.current_output_turn_id
            ):
                self._abandon_pending_turn(
                    session,
                    previous_capture_turn_id,
                    reason="server_vad_speech_started",
                )
            if not session.interruption_repair_active:
                session.interruption_repair_active = True
                session.repair_replay_frames_16k = deque(
                    session.recent_input_frames_16k,
                    maxlen=self.repair_replay_frame_limit,
                )
                asyncio.create_task(
                    self._run_interruption_repair(
                        session,
                        reason="server_vad_speech_started",
                    ),
                    name=f"interruption-repair-{session.session_id}",
                )
            else:
                LOGGER.info(
                    "realtime_phone_interruption_repair_already_active "
                    "call_id=%s session_id=%s provider_turn=%s",
                    session.call_id,
                    session.session_id,
                    turn_id,
                )
            LOGGER.info(
                "realtime_phone_server_vad_speech_started_used_for_interrupt "
                "call_id=%s session_id=%s provider_turn=%s",
                session.call_id,
                session.session_id,
                turn_id,
            )
            return

    async def _run_interruption_repair(
        self,
        session: RealtimePhoneSessionStats,
        *,
        reason: str,
    ) -> None:
        try:
            await self._clear_current_playback(session, reason=reason)
        except Exception:
            LOGGER.warning(
                "realtime_phone_interruption_repair_failed call_id=%s "
                "session_id=%s reason=%s",
                session.call_id,
                session.session_id,
                reason,
                exc_info=True,
            )
        finally:
            if session.interruption_repair_active:
                session.interruption_repair_active = False

    async def _handle_input_transcript_available(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
        transcript: str,
    ) -> None:
        if session.handoff_requested or session.handoff_transitioning:
            return
        normalized = transcript.strip()
        await self._maybe_record_agent_takeover_suggestion(session, normalized)
        if self._handoff_requester is None:
            return
        handoff_reason = _detect_handoff_request(normalized)
        if handoff_reason is None:
            return

        LOGGER.info(
            "realtime_phone_handoff_detected_from_asr call_id=%s session_id=%s "
            "turn=%s reason=%s input_transcript=%s",
            session.call_id,
            session.session_id,
            turn_id,
            handoff_reason,
            normalized,
        )
        self._commit_handoff_request_turn(
            session,
            turn_id,
            input_transcript=normalized,
        )
        await self._trigger_handoff_from_turn(
            session,
            RealtimeTurnResult(
                turn_id=turn_id,
                input_audio_bytes=0,
                output_audio_bytes=0,
                input_transcript=normalized,
                output_transcript="",
                event_counts={},
                first_audio_delta_ms=None,
                response_done_ms=None,
                status="handoff_requested",
            ),
            reason=handoff_reason,
        )

    async def _finalize_server_vad_turn(
        self,
        session: RealtimePhoneSessionStats,
        result: RealtimeTurnResult,
    ) -> None:
        turn_id = result.turn_id
        session.current_capture_turn_id = None
        session.turns_committed += 1
        session.turn_model_done_at[turn_id] = time.monotonic()
        session.turn_model_first_audio_delta_ms[turn_id] = (
            result.first_audio_delta_ms
        )
        session.turn_asr_ended_ms[turn_id] = result.asr_ended_ms
        flushed_frame_count = 0
        tail_silence_frame_count = 0

        handoff_reason = _detect_handoff_request(result.input_transcript)
        if handoff_reason is not None and self._handoff_requester is not None:
            await self._finalize_handoff_request_turn(
                session,
                result,
                reason=handoff_reason,
            )
            return

        await self._maybe_record_agent_takeover_suggestion(
            session,
            result.input_transcript,
        )

        output_buffer = session.playback_buffers.pop(turn_id, None)
        if (
            result.status == "completed"
            and output_buffer is not None
            and session.current_output_turn_id in (None, turn_id)
        ):
            session.current_output_turn_id = turn_id
            tail_frames = output_buffer.flush(pad_last=True)
            flushed_frame_count = len(tail_frames)
            silence_frames = self._tail_silence_frames()
            tail_silence_frame_count = len(silence_frames)
            session.flushed_tail_frames += flushed_frame_count
            session.tail_silence_frames += tail_silence_frame_count
            for frame in [*tail_frames, *silence_frames]:
                await self._enqueue_playback_frame(session, PlaybackFrame(turn_id, frame))
        session.model_done_turns.add(turn_id)

        if result.status == "completed":
            session.turns_completed += 1
        else:
            session.turns_failed += 1

        if result.output_transcript:
            session.output_transcripts_by_turn[turn_id] = result.output_transcript
        if result.input_transcript or result.output_transcript:
            exchange = session.pending_exchanges.get(turn_id)
            if exchange is None:
                exchange = ConversationExchange(turn_id=turn_id)
                session.pending_exchanges[turn_id] = exchange
            if result.input_transcript:
                exchange.input_transcript = result.input_transcript
            if result.output_transcript:
                exchange.output_transcript = result.output_transcript
        if result.status != "completed":
            if turn_id in session.closed_output_turn_ids:
                self._abandon_pending_turn(
                    session,
                    turn_id,
                    reason="realtime_turn_cancelled",
                )
            else:
                session.pending_exchanges.pop(turn_id, None)

        LOGGER.info(
            "realtime_phone_server_vad_turn_done call_id=%s session_id=%s turn=%s "
            "status=%s response_id=%s input_transcript=%s output_transcript=%s "
            "asr_ended_ms=%s first_audio_delta_ms=%s response_done_ms=%s "
            "gateway_first_model_audio_ms=%s gateway_model_done_ms=%s "
            "model_input_bytes=%s model_output_bytes=%s flushed_tail_frames=%s "
            "tail_silence_frames=%s event_counts=%s",
            session.call_id,
            session.session_id,
            turn_id,
            result.status,
            result.response_id,
            result.input_transcript,
            result.output_transcript,
            result.asr_ended_ms,
            result.first_audio_delta_ms,
            result.response_done_ms,
            _elapsed_ms(
                session.turn_speech_started_at.get(turn_id),
                session.turn_first_model_audio_at.get(turn_id),
            ),
            _elapsed_ms(
                session.turn_speech_started_at.get(turn_id),
                session.turn_model_done_at.get(turn_id),
            ),
            result.input_audio_bytes,
            result.output_audio_bytes,
            flushed_frame_count,
            tail_silence_frame_count,
            result.event_counts,
        )

        if session.current_output_turn_id == turn_id and not self._has_playback(session):
            session.current_output_turn_id = None
            session.model_done_turns.discard(turn_id)
            session.jitter_prefilled_turns.discard(turn_id)

    async def _finalize_handoff_request_turn(
        self,
        session: RealtimePhoneSessionStats,
        result: RealtimeTurnResult,
        *,
        reason: str,
    ) -> None:
        turn_id = result.turn_id
        session.model_done_turns.add(turn_id)
        if result.status == "completed":
            session.turns_completed += 1
        else:
            session.turns_failed += 1

        self._commit_handoff_request_turn(
            session,
            turn_id,
            input_transcript=result.input_transcript,
        )
        await self._trigger_handoff_from_turn(session, result, reason=reason)

        LOGGER.info(
            "realtime_phone_handoff_turn_done call_id=%s session_id=%s turn=%s "
            "status=%s reason=%s response_id=%s input_transcript=%s "
            "output_transcript_suppressed=%s model_input_bytes=%s "
            "model_output_bytes=%s handoff_completed=%s handoff_error=%s",
            session.call_id,
            session.session_id,
            turn_id,
            result.status,
            reason,
            result.response_id,
            result.input_transcript,
            bool(result.output_transcript),
            result.input_audio_bytes,
            result.output_audio_bytes,
            session.handoff_completed,
            session.handoff_error,
        )

    async def _trigger_handoff_from_turn(
        self,
        session: RealtimePhoneSessionStats,
        result: RealtimeTurnResult,
        *,
        reason: str,
    ) -> None:
        if session.handoff_requested or session.handoff_transitioning:
            LOGGER.info(
                "realtime_phone_handoff_duplicate_ignored call_id=%s "
                "session_id=%s turn=%s reason=%s",
                session.call_id,
                session.session_id,
                result.turn_id,
                reason,
            )
            return

        session.handoff_transitioning = True
        session.handoff_requested = False
        session.handoff_completed = False
        session.handoff_trigger_turn_id = result.turn_id
        session.handoff_error = None
        try:
            await self._stop_ai_playback_for_handoff(session)
            await self._play_handoff_connecting_prompt(session)
        finally:
            session.handoff_transitioning = False
        session.handoff_requested = True

        requester = self._handoff_requester
        if requester is None:
            return
        payload = {
            "trigger": "customer_requested",
            "reason": reason,
            "last_utterance": result.input_transcript.strip(),
            "wait_timeout_seconds": self.config.handoff.wait_timeout_seconds,
            "ai_turns": self._build_call_result_turns(session),
        }
        try:
            handoff_result = await asyncio.to_thread(
                requester,
                session.call_id,
                payload,
            )
        except Exception as err:
            session.handoff_requested = False
            session.handoff_error = str(err)
            LOGGER.warning(
                "realtime_phone_handoff_request_failed call_id=%s "
                "session_id=%s turn=%s reason=%s error=%s",
                session.call_id,
                session.session_id,
                result.turn_id,
                reason,
                err,
                exc_info=True,
            )
            return

        session.handoff_completed = True
        session.handoff_result = dict(handoff_result)
        await self._close_realtime_session_for_handoff(session)
        LOGGER.info(
            "realtime_phone_handoff_request_succeeded call_id=%s "
            "session_id=%s turn=%s reason=%s",
            session.call_id,
            session.session_id,
            result.turn_id,
            reason,
        )

    async def _maybe_record_agent_takeover_suggestion(
        self,
        session: RealtimePhoneSessionStats,
        transcript: str,
    ) -> None:
        if session.agent_takeover_suggestion_requested:
            return
        recorder = self._agent_takeover_suggestion_recorder
        if recorder is None:
            return
        reason = _detect_agent_takeover_suggestion(transcript)
        if reason is None:
            return
        payload = {
            "reason": reason,
            "last_utterance": transcript.strip(),
        }
        try:
            result = await asyncio.to_thread(recorder, session.call_id, payload)
        except Exception as err:
            session.agent_takeover_suggestion_error = str(err)
            LOGGER.warning(
                "realtime_phone_takeover_suggestion_failed call_id=%s "
                "session_id=%s reason=%s error=%s",
                session.call_id,
                session.session_id,
                reason,
                err,
                exc_info=True,
            )
            return
        session.agent_takeover_suggestion_requested = True
        session.agent_takeover_suggestion_result = dict(result)
        LOGGER.info(
            "realtime_phone_takeover_suggestion_recorded call_id=%s "
            "session_id=%s reason=%s input_transcript=%s",
            session.call_id,
            session.session_id,
            reason,
            transcript,
        )

    async def _play_handoff_connecting_prompt(
        self,
        session: RealtimePhoneSessionStats,
    ) -> None:
        realtime_session = self._realtime_sessions.get(session.session_id)
        if realtime_session is None:
            return
        send_tts_text = getattr(realtime_session, "send_tts_text", None)
        if send_tts_text is None:
            LOGGER.warning(
                "realtime_phone_handoff_prompt_unavailable call_id=%s "
                "session_id=%s",
                session.call_id,
                session.session_id,
            )
            return
        try:
            await asyncio.wait_for(
                send_tts_text(HANDOFF_CONNECTING_PROMPT_TEXT),
                timeout=10,
            )
        except Exception:
            LOGGER.warning(
                "realtime_phone_handoff_prompt_failed call_id=%s session_id=%s",
                session.call_id,
                session.session_id,
                exc_info=True,
            )
            return
        self._commit_handoff_prompt_turn(session, HANDOFF_CONNECTING_PROMPT_TEXT)

    def _commit_handoff_prompt_turn(
        self,
        session: RealtimePhoneSessionStats,
        text: str,
    ) -> None:
        normalized = text.strip()
        if not normalized:
            return
        exchange = ConversationExchange(
            turn_id=-(session.gateway_history_committed_turns + 1),
            status="completed",
            output_transcript=normalized,
            heard_output_transcript=normalized,
            playback_completed=True,
            source="handoff_prompt",
            created_at_ms=int(time.time() * 1000),
        )
        session.committed_exchanges.append(exchange)
        session.gateway_history_committed_turns += 1
        session.gateway_history_completed_turns += 1

    async def _stop_ai_playback_for_handoff(
        self,
        session: RealtimePhoneSessionStats,
    ) -> None:
        session.interruptions += 1
        interrupted_output_turn_id = session.current_output_turn_id
        if interrupted_output_turn_id == OPENING_TURN_ID:
            session.opening_playback_interrupted = True
            session.opening_barge_in_detector = None
        dropped_frames = self._clear_playback_queue(session)
        session.dropped_playback_frames += dropped_frames
        self._abandon_pending_turn(
            session,
            interrupted_output_turn_id,
            reason="handoff_requested",
        )
        session.current_output_turn_id = None
        session.playback_buffers.clear()
        session.model_done_turns.clear()
        session.freeswitch_completed_turns.clear()
        session.jitter_prefilled_turns.clear()
        session.playout_pacing_states.clear()
        session.repair_replay_frames_16k.clear()
        session.interruption_repair_active = False
        session.playback_active = False

        realtime_session = self._realtime_sessions.get(session.session_id)
        if realtime_session is not None:
            session.realtime_interrupt_requests += 1
            try:
                await asyncio.wait_for(realtime_session.cancel_response(), timeout=1)
            except Exception:
                session.realtime_interrupt_failures += 1
                LOGGER.warning(
                    "realtime_phone_handoff_cancel_response_failed call_id=%s "
                    "session_id=%s",
                    session.call_id,
                    session.session_id,
                    exc_info=True,
                )

        await self._break_freeswitch_playback(session, reason="handoff_requested")
        LOGGER.info(
            "realtime_phone_handoff_ai_playback_stopped call_id=%s "
            "session_id=%s dropped_playback_frames=%s "
            "freeswitch_break_requests=%s freeswitch_break_failures=%s "
            "realtime_interrupt_requests=%s realtime_interrupt_failures=%s",
            session.call_id,
            session.session_id,
            dropped_frames,
            session.freeswitch_break_requests,
            session.freeswitch_break_failures,
            session.realtime_interrupt_requests,
            session.realtime_interrupt_failures,
        )

    async def _close_realtime_session_for_handoff(
        self,
        session: RealtimePhoneSessionStats,
    ) -> None:
        realtime_session = self._realtime_sessions.pop(session.session_id, None)
        if realtime_session is None:
            return
        await realtime_session.close()

    def _commit_handoff_request_turn(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
        *,
        input_transcript: str,
    ) -> None:
        if turn_id == OPENING_TURN_ID:
            return
        normalized_input = input_transcript.strip()
        if not normalized_input:
            return

        session.pending_exchanges.pop(turn_id, None)
        existing_exchange = next(
            (
                item
                for item in session.committed_exchanges
                if item.turn_id == turn_id
            ),
            None,
        )
        if existing_exchange is not None:
            if not existing_exchange.input_transcript:
                existing_exchange.input_transcript = normalized_input
            return

        exchange = ConversationExchange(
            turn_id=turn_id,
            status="handoff_requested",
            input_transcript=normalized_input,
            source="handoff_requested",
            created_at_ms=int(time.time() * 1000),
        )
        session.committed_exchanges.append(exchange)
        session.gateway_history_committed_turns += 1

    async def _queue_audio_delta(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
        model_audio_delta: bytes,
    ) -> None:
        if session.handoff_requested:
            session.dropped_stale_frames += 1
            return

        if turn_id in session.closed_output_turn_ids:
            session.dropped_stale_frames += 1
            return

        if session.current_output_turn_id not in (None, turn_id):
            session.dropped_stale_frames += 1
            return

        if turn_id not in session.turn_first_model_audio_at:
            now = time.monotonic()
            session.turn_first_model_audio_at[turn_id] = now
            LOGGER.info(
                "realtime_phone_first_model_audio call_id=%s session_id=%s "
                "turn=%s bytes=%s since_speech_started_ms=%s "
                "playback_queue_frames=%s",
                session.call_id,
                session.session_id,
                turn_id,
                len(model_audio_delta),
                _elapsed_ms(session.turn_speech_started_at.get(turn_id), now),
                session.playback_queue.qsize(),
            )

        session.current_output_turn_id = turn_id
        output_buffer = session.playback_buffers.get(turn_id)
        if output_buffer is None:
            output_buffer = DownsampledPlaybackBuffer(
                source_rate=self.model_output_sample_rate,
                target_rate=self.config.freeswitch.sample_rate,
                frame_bytes=self.expected_frame_bytes,
            )
            session.playback_buffers[turn_id] = output_buffer

        for frame in output_buffer.push(model_audio_delta):
            await self._enqueue_playback_frame(session, PlaybackFrame(turn_id, frame))

    async def _enqueue_playback_frame(
        self,
        session: RealtimePhoneSessionStats,
        frame: PlaybackFrame,
    ) -> None:
        await session.playback_queue.put(frame)
        session.max_playback_queue_frames = max(
            session.max_playback_queue_frames,
            session.playback_queue.qsize(),
        )

    def _tail_silence_frames(self) -> list[bytes]:
        frame_count = self.config.playback.tail_silence_ms // self.frame_duration_ms
        if frame_count <= 0:
            return []
        silence = b"\x00" * self.expected_frame_bytes
        return [silence for _ in range(frame_count)]

    async def _playback_worker(
        self,
        websocket: WebSocketServerProtocol,
        session: RealtimePhoneSessionStats,
    ) -> None:
        while True:
            item = await session.playback_queue.get()
            if item is None:
                return

            if item.turn_id != session.current_output_turn_id:
                if not item.is_underrun_silence:
                    session.dropped_stale_frames += 1
                continue

            frames = await self._prefill_playback_frames(session, item)
            if not frames:
                continue

            for frame in frames:
                if frame.turn_id != session.current_output_turn_id:
                    if not frame.is_underrun_silence:
                        session.dropped_stale_frames += 1
                    continue
                try:
                    await self._send_playback_frame(websocket, session, frame)
                except ConnectionClosed:
                    return

    async def _prefill_playback_frames(
        self,
        session: RealtimePhoneSessionStats,
        first_item: PlaybackFrame,
    ) -> list[PlaybackFrame]:
        if first_item.turn_id in session.jitter_prefilled_turns:
            return [first_item]

        frames = [first_item]
        turn_id = first_item.turn_id
        started_at = time.monotonic()
        deadline = time.monotonic() + (self.config.playback.jitter_buffer_ms / 1000)

        while len(frames) < self.playback_prefill_frames:
            if turn_id in session.model_done_turns:
                if turn_id != OPENING_TURN_ID:
                    break
                try:
                    next_item = session.playback_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            else:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                try:
                    next_item = await asyncio.wait_for(
                        session.playback_queue.get(),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    break

            if next_item is None:
                await session.playback_queue.put(None)
                return []

            if next_item.turn_id != session.current_output_turn_id:
                if not next_item.is_underrun_silence:
                    session.dropped_stale_frames += 1
                continue
            frames.append(next_item)

        session.jitter_prefilled_turns.add(turn_id)
        LOGGER.info(
            "realtime_phone_jitter_prefilled call_id=%s session_id=%s turn=%s "
            "frames=%s target_frames=%s done=%s wait_ms=%s "
            "jitter_buffer_ms=%s queue_remaining_frames=%s",
            session.call_id,
            session.session_id,
            turn_id,
            len(frames),
            self.playback_prefill_frames,
            turn_id in session.model_done_turns,
            _elapsed_ms(started_at),
            self.config.playback.jitter_buffer_ms,
            session.playback_queue.qsize(),
        )
        return frames

    async def _send_playback_frame(
        self,
        websocket: WebSocketServerProtocol,
        session: RealtimePhoneSessionStats,
        item: PlaybackFrame,
    ) -> None:
        send_started_at = time.monotonic()
        if (
            session.playback_last_send_at is not None
            and session.playback_last_send_turn_id == item.turn_id
        ):
            gap_ms = _elapsed_ms(session.playback_last_send_at, send_started_at)
            assert gap_ms is not None
            session.max_playback_send_gap_ms = max(
                session.max_playback_send_gap_ms,
                gap_ms,
            )
            session.turn_max_playback_send_gap_ms[item.turn_id] = max(
                session.turn_max_playback_send_gap_ms.get(item.turn_id, 0),
                gap_ms,
            )
            if gap_ms > self.frame_duration_ms * 2:
                session.playback_send_gap_overruns += 1

        session.playback_last_send_at = send_started_at
        session.playback_last_send_turn_id = item.turn_id

        try:
            session.playback_active = True
            await websocket.send(item.payload)
        except ConnectionClosed:
            session.playback_active = False
            raise

        if item.turn_id == OPENING_TURN_ID:
            _record_opening_playback_frame(session, item.payload, send_started_at)

        if session.first_playback_at is None:
            session.first_playback_at = time.time()
            LOGGER.info(
                "first_realtime_phone_playback call_id=%s session_id=%s",
                session.call_id,
                session.session_id,
            )
        if item.turn_id not in session.turn_first_playback_at:
            session.turn_first_playback_at[item.turn_id] = send_started_at
            LOGGER.info(
                "realtime_phone_turn_first_playback call_id=%s session_id=%s "
                "turn=%s since_speech_started_ms=%s "
                "since_first_model_audio_ms=%s asr_ended_ms=%s "
                "model_first_audio_delta_ms=%s prefill_frames=%s",
                session.call_id,
                session.session_id,
                item.turn_id,
                _elapsed_ms(
                    session.turn_speech_started_at.get(item.turn_id),
                    send_started_at,
                ),
                _elapsed_ms(
                    session.turn_first_model_audio_at.get(item.turn_id),
                    send_started_at,
                ),
                session.turn_asr_ended_ms.get(item.turn_id),
                session.turn_model_first_audio_delta_ms.get(item.turn_id),
                self.playback_prefill_frames,
            )

        session.outbound_frames += 1
        session.outbound_bytes += len(item.payload)
        session.turn_last_playback_at[item.turn_id] = send_started_at
        decision = self._playback_pacing_decision(session, item.turn_id)
        await asyncio.sleep(decision.interval_ms / 1000)
        session.playback_active = False

        if not self._has_playback(session):
            if item.turn_id in session.model_done_turns:
                completed = self._complete_played_turn_if_ready(
                    session,
                    item.turn_id,
                )
                if not completed:
                    LOGGER.debug(
                        "gateway_playback_sent_waiting_for_freeswitch "
                        "call_id=%s session_id=%s turn=%s",
                        session.call_id,
                        session.session_id,
                        item.turn_id,
                    )
            elif session.current_output_turn_id == item.turn_id:
                session.playback_underruns += 1
                LOGGER.debug(
                    "gateway_playback_waiting_for_model_audio "
                    "call_id=%s session_id=%s turn=%s underruns=%s",
                    session.call_id,
                    session.session_id,
                    item.turn_id,
                    session.playback_underruns,
                )

    def _playback_pacing_decision(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
    ) -> PlayoutDecision:
        state = session.playout_pacing_states.setdefault(
            turn_id,
            self.playout_controller.new_state(),
        )
        decision = self.playout_controller.decide(
            state,
            queued_frames=session.playback_queue.qsize(),
        )
        if decision.mode == PlayoutPacingMode.FAST:
            session.playback_fast_send_frames += 1
        else:
            session.playback_realtime_send_frames += 1
        if decision.switched:
            session.playback_pacing_switches += 1
        return decision

    def _commit_played_turn(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
    ) -> None:
        exchange = session.pending_exchanges.pop(turn_id, None)
        if exchange is None:
            return
        if not exchange.input_transcript or not exchange.output_transcript:
            return

        exchange.status = "completed"
        exchange.playback_completed = True
        exchange.played_audio_ms = self._played_audio_ms_for_turn(session, turn_id)
        exchange.heard_output_transcript = exchange.output_transcript
        exchange.source = "playback_completed"
        exchange.created_at_ms = int(time.time() * 1000)
        session.committed_exchanges.append(exchange)
        session.gateway_history_committed_turns += 1
        session.gateway_history_completed_turns += 1
        completed_at = time.monotonic()
        LOGGER.info(
            "gateway_conversation_turn_committed call_id=%s session_id=%s "
            "turn=%s committed_history_turns=%s input_transcript=%s "
            "output_transcript=%s playback_done_ms=%s "
            "playback_since_model_done_ms=%s playback_duration_ms=%s "
            "max_send_gap_ms=%s",
            session.call_id,
            session.session_id,
            turn_id,
            len(session.committed_exchanges),
            exchange.input_transcript,
            exchange.output_transcript,
            _elapsed_ms(session.turn_speech_started_at.get(turn_id), completed_at),
            _elapsed_ms(session.turn_model_done_at.get(turn_id), completed_at),
            _elapsed_ms(session.turn_first_playback_at.get(turn_id), completed_at),
            session.turn_max_playback_send_gap_ms.get(turn_id, 0),
        )

    @staticmethod
    def _played_audio_ms_for_turn(
        session: RealtimePhoneSessionStats,
        turn_id: int,
    ) -> int:
        first = session.turn_first_playback_at.get(turn_id)
        last = session.turn_last_playback_at.get(turn_id)
        if first is None or last is None or last < first:
            return 0
        return int((last - first) * 1000)

    def _abandon_pending_turn(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int | None,
        *,
        reason: str,
    ) -> None:
        if turn_id is None:
            return
        if turn_id == OPENING_TURN_ID:
            return

        session.closed_output_turn_ids.add(turn_id)
        exchange = session.pending_exchanges.pop(turn_id, None)
        output_transcript = session.output_transcripts_by_turn.get(turn_id, "")
        if exchange is None:
            exchange = ConversationExchange(
                turn_id=turn_id,
                output_transcript=output_transcript,
            )
        elif output_transcript and not exchange.output_transcript:
            exchange.output_transcript = output_transcript

        if not exchange.input_transcript and not exchange.output_transcript:
            return

        existing_exchange = next(
            (
                item
                for item in session.committed_exchanges
                if item.turn_id == turn_id
            ),
            None,
        )
        if existing_exchange is not None:
            if exchange.input_transcript and not existing_exchange.input_transcript:
                existing_exchange.input_transcript = exchange.input_transcript
            if exchange.output_transcript and not existing_exchange.output_transcript:
                existing_exchange.output_transcript = exchange.output_transcript
            return

        exchange.status = "interrupted"
        exchange.playback_completed = False
        exchange.played_audio_ms = self._played_audio_ms_for_turn(session, turn_id)
        exchange.heard_output_transcript = ""
        exchange.source = "client_interrupt"
        exchange.created_at_ms = int(time.time() * 1000)
        session.committed_exchanges.append(exchange)
        session.gateway_history_committed_turns += 1
        session.gateway_history_interrupted_turns += 1
        if not exchange.output_transcript:
            session.gateway_history_missing_output_turns += 1

        LOGGER.info(
            "gateway_conversation_turn_interrupted_committed "
            "call_id=%s session_id=%s turn=%s reason=%s "
            "committed_history_turns=%s played_audio_ms=%s",
            session.call_id,
            session.session_id,
            turn_id,
            reason,
            len(session.committed_exchanges),
            exchange.played_audio_ms,
        )

    async def _handle_control_message(
        self,
        websocket: WebSocketServerProtocol,
        session: RealtimePhoneSessionStats,
        raw_message: str,
    ) -> None:
        message_type = "unknown"
        with contextlib.suppress(json.JSONDecodeError):
            payload = json.loads(raw_message)
            if isinstance(payload, dict):
                message_type = str(payload.get("type", "unknown"))

        session.control_messages.append(message_type)
        LOGGER.info(
            "freeswitch_realtime_control_message call_id=%s session_id=%s type=%s",
            session.call_id,
            session.session_id,
            message_type,
        )

        if message_type == "ping":
            await websocket.send(
                json.dumps(
                    {
                        "type": "pong",
                        "call_id": session.call_id,
                        "session_id": session.session_id,
                    }
                )
        )

    def _complete_played_turn_if_ready(
        self,
        session: RealtimePhoneSessionStats,
        turn_id: int,
    ) -> bool:
        if session.current_output_turn_id != turn_id:
            return False
        if turn_id not in session.model_done_turns:
            return False
        if session.playback_active or not session.playback_queue.empty():
            return False
        if (
            self._waits_for_freeswitch_playback_completion()
            and turn_id not in session.freeswitch_completed_turns
        ):
            return False

        if turn_id == OPENING_TURN_ID:
            session.opening_playback_completed_at = time.time()
            session.opening_barge_in_detector = None
            LOGGER.info(
                "opening_playback_completed call_id=%s session_id=%s text_hash=%s "
                "frames=%s",
                session.call_id,
                session.session_id,
                session.opening_text_hash,
                session.opening_playback_frames,
            )
        else:
            self._commit_played_turn(session, turn_id)
        session.current_output_turn_id = None
        session.model_done_turns.discard(turn_id)
        session.freeswitch_completed_turns.discard(turn_id)
        session.jitter_prefilled_turns.discard(turn_id)
        session.playout_pacing_states.pop(turn_id, None)
        return True

    def _waits_for_freeswitch_playback_completion(self) -> bool:
        return self.playback_control is not None

    async def _handle_freeswitch_playback_event(
        self,
        event: PlaybackProgressEvent,
    ) -> None:
        session = self._session_by_call_id(event.uuid)
        if session is None:
            LOGGER.debug(
                "freeswitch_playback_event_without_active_session uuid=%s event=%s",
                event.uuid,
                event.event,
            )
            return

        session.freeswitch_playback_events += 1
        session.freeswitch_last_playback_remaining = event.remaining
        if event.event == "queue_completed":
            session.freeswitch_queue_completed_events += 1

        if event.is_queue_completed:
            turn_id = session.current_output_turn_id
            if turn_id is not None:
                session.freeswitch_completed_turns.add(turn_id)
                self._complete_played_turn_if_ready(session, turn_id)

        LOGGER.debug(
            "freeswitch_playback_event call_id=%s session_id=%s event=%s "
            "seq=%s remaining=%s total_chunks=%s",
            session.call_id,
            session.session_id,
            event.event,
            event.seq,
            event.remaining,
            event.total_chunks,
        )

    async def _clear_current_playback(
        self,
        session: RealtimePhoneSessionStats,
        *,
        reason: str,
    ) -> None:
        session.interruptions += 1
        interrupted_output_turn_id = session.current_output_turn_id
        if interrupted_output_turn_id == OPENING_TURN_ID:
            session.opening_playback_interrupted = True
            session.opening_barge_in_detector = None
        dropped_frames = self._clear_playback_queue(session)
        session.dropped_playback_frames += dropped_frames
        self._abandon_pending_turn(
            session,
            interrupted_output_turn_id,
            reason=reason,
        )
        session.current_output_turn_id = None
        session.playback_buffers.clear()
        session.model_done_turns.clear()
        session.freeswitch_completed_turns.clear()
        session.jitter_prefilled_turns.clear()
        session.playout_pacing_states.clear()
        session.playback_active = False
        realtime_session = self._realtime_sessions.get(session.session_id)
        await asyncio.gather(
            self._interrupt_realtime_playback_context(
                session,
                realtime_session,
                reason=reason,
                interrupted_output_turn_id=interrupted_output_turn_id,
            ),
            self._break_freeswitch_playback(session, reason=reason),
        )

        LOGGER.info(
            "realtime_phone_playback_cleared call_id=%s session_id=%s "
            "reason=%s interruptions=%s dropped_playback_frames=%s "
            "freeswitch_break_requests=%s freeswitch_break_failures=%s "
            "realtime_interrupt_requests=%s realtime_interrupt_failures=%s "
            "context_repair_requests=%s",
            session.call_id,
            session.session_id,
            reason,
            session.interruptions,
            dropped_frames,
            session.freeswitch_break_requests,
            session.freeswitch_break_failures,
            session.realtime_interrupt_requests,
            session.realtime_interrupt_failures,
            session.context_repair_requests,
        )

    async def _interrupt_realtime_playback_context(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol | None,
        *,
        reason: str,
        interrupted_output_turn_id: int | None,
    ) -> None:
        if realtime_session is None:
            return

        interrupted_output_text = None
        if interrupted_output_turn_id is not None:
            interrupted_output_text = session.output_transcripts_by_turn.get(
                interrupted_output_turn_id
            )
        session.realtime_interrupt_requests += 1
        try:
            restart_on_interruption = getattr(
                realtime_session,
                "restart_on_interruption",
                True,
            )
            LOGGER.info(
                "realtime_playback_context_repair_started call_id=%s "
                "session_id=%s reason=%s interrupted_turn=%s "
                "restart_on_interruption=%s interrupted_text_hash=%s "
                "interrupted_text_chars=%s",
                session.call_id,
                session.session_id,
                reason,
                interrupted_output_turn_id,
                restart_on_interruption,
                _text_hash(interrupted_output_text)
                if interrupted_output_text
                else None,
                len(interrupted_output_text or ""),
            )
            if restart_on_interruption:
                await asyncio.wait_for(
                    self._restart_realtime_session_after_interruption(
                        session,
                        realtime_session,
                        reason=reason,
                    ),
                    timeout=8,
                )
            else:
                try:
                    await realtime_session.handle_playback_interruption(
                        interrupted_output_text=interrupted_output_text,
                    )
                finally:
                    async with session.realtime_lock:
                        current = self._realtime_sessions.get(session.session_id)
                        if current is realtime_session:
                            session.repair_replay_frames_16k.clear()
                            session.interruption_repair_active = False
                            LOGGER.info(
                                "realtime_interruption_audio_discarded "
                                "call_id=%s session_id=%s reason=%s",
                                session.call_id,
                                session.session_id,
                                reason,
                            )
            session.context_repair_requests += 1
        except AttributeError:
            try:
                await asyncio.wait_for(realtime_session.cancel_response(), timeout=1)
            except Exception:
                session.realtime_interrupt_failures += 1
                LOGGER.warning(
                    "realtime_playback_interrupt_failed call_id=%s session_id=%s "
                    "reason=%s",
                    session.call_id,
                    session.session_id,
                    reason,
                    exc_info=True,
                )
        except Exception:
            session.realtime_interrupt_failures += 1
            LOGGER.warning(
                "realtime_playback_context_repair_failed call_id=%s session_id=%s "
                "reason=%s",
                session.call_id,
                session.session_id,
                reason,
                exc_info=True,
            )

    async def _restart_realtime_session_after_interruption(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol,
        *,
        reason: str,
    ) -> None:
        async with session.realtime_lock:
            current = self._realtime_sessions.get(session.session_id)
            if current is None:
                return
            if current is not realtime_session:
                realtime_session = current

            with contextlib.suppress(Exception):
                await realtime_session.cancel_response()
            await realtime_session.close()

            session.current_capture_turn_id = None
            session.realtime_session_restarts += 1
            replacement = self._create_realtime_session(session)
            self._realtime_sessions[session.session_id] = replacement
            await replacement.connect()

            replayed_frames, replayed_bytes = (
                await self._replay_repair_audio_locked(
                    session,
                    replacement,
                    reason=reason,
                )
            )

        LOGGER.info(
            "realtime_session_restarted_after_interruption call_id=%s "
            "session_id=%s reason=%s committed_history_turns=%s "
            "replayed_input_frames=%s replayed_input_bytes=%s",
            session.call_id,
            session.session_id,
            reason,
            len(session.committed_exchanges),
            replayed_frames,
            replayed_bytes,
        )

    async def _replay_repair_audio_locked(
        self,
        session: RealtimePhoneSessionStats,
        realtime_session: RealtimeSessionProtocol,
        *,
        reason: str,
    ) -> tuple[int, int]:
        replay_frames = list(session.repair_replay_frames_16k)
        replay_payload = b"".join(replay_frames)
        if replay_payload:
            await realtime_session.append_audio(replay_payload)
            session.replayed_input_frames += len(replay_frames)
            session.replayed_input_bytes += len(replay_payload)
            session.streamed_input_bytes += len(replay_payload)

        session.repair_replay_frames_16k.clear()
        session.interruption_repair_active = False
        LOGGER.info(
            "realtime_interruption_audio_replayed call_id=%s session_id=%s "
            "reason=%s replayed_input_frames=%s replayed_input_bytes=%s",
            session.call_id,
            session.session_id,
            reason,
            len(replay_frames),
            len(replay_payload),
        )
        return len(replay_frames), len(replay_payload)

    async def _break_freeswitch_playback(
        self,
        session: RealtimePhoneSessionStats,
        *,
        reason: str,
    ) -> None:
        if self.playback_control is None:
            return

        session.freeswitch_break_requests += 1
        try:
            success = await asyncio.wait_for(
                self.playback_control.break_playback(session.call_id),
                timeout=1,
            )
        except Exception:
            session.freeswitch_break_failures += 1
            LOGGER.warning(
                "freeswitch_playback_break_failed call_id=%s session_id=%s "
                "reason=%s",
                session.call_id,
                session.session_id,
                reason,
                exc_info=True,
            )
            return

        if not success:
            session.freeswitch_break_failures += 1

        LOGGER.info(
            "freeswitch_playback_break_requested call_id=%s session_id=%s "
            "reason=%s success=%s",
            session.call_id,
            session.session_id,
            reason,
            success,
        )

    @staticmethod
    def _clear_playback_queue(session: RealtimePhoneSessionStats) -> int:
        dropped = 0
        while True:
            try:
                item = session.playback_queue.get_nowait()
            except asyncio.QueueEmpty:
                return dropped
            if item is not None:
                dropped += 1

    async def _shutdown_session(
        self,
        session: RealtimePhoneSessionStats,
        playback_task: asyncio.Task[None],
    ) -> None:
        if session.background_tasks:
            for task in list(session.background_tasks):
                task.cancel()
            await asyncio.gather(*session.background_tasks, return_exceptions=True)
            session.background_tasks.clear()

        realtime_session = self._realtime_sessions.get(session.session_id)
        if realtime_session is not None:
            await realtime_session.close()
        await session.playback_queue.put(None)
        with contextlib.suppress(asyncio.CancelledError):
            await playback_task

        current = self.active_sessions.pop(session.session_id, None)
        self._realtime_sessions.pop(session.session_id, None)
        if current is None:
            return

        session.disconnected_at = time.time()
        self.completed_sessions.append(session)
        LOGGER.info(
            "freeswitch_realtime_session_finished call_id=%s session_id=%s "
            "turn_mode=server_vad inbound_frames=%s inbound_bytes=%s "
            "streamed_model_input_bytes=%s outbound_frames=%s outbound_bytes=%s "
            "inbound_rms_min=%s inbound_rms_max=%s inbound_rms_avg=%s "
            "inbound_rms_last=%s inbound_high_rms_frames=%s "
            "inbound_first_high_rms_frame=%s "
            "invalid_frame_count=%s interruptions=%s dropped_playback_frames=%s "
            "dropped_stale_frames=%s playback_underruns=%s "
            "max_playback_queue_frames=%s max_playback_send_gap_ms=%s "
            "playback_send_gap_overruns=%s playback_fast_send_frames=%s "
            "playback_realtime_send_frames=%s playback_pacing_switches=%s "
            "flushed_tail_frames=%s "
            "tail_silence_frames=%s "
            "freeswitch_playback_events=%s freeswitch_queue_completed_events=%s "
            "freeswitch_break_requests=%s freeswitch_break_failures=%s "
            "realtime_interrupt_requests=%s realtime_interrupt_failures=%s "
            "context_repair_requests=%s local_barge_in_events=%s "
            "realtime_session_restarts=%s "
            "gateway_history_committed_turns=%s "
            "gateway_history_abandoned_turns=%s replayed_input_frames=%s "
            "replayed_input_bytes=%s "
            "opening_trigger_rms=%s opening_trigger_rms_min=%s "
            "opening_trigger_rms_max=%s opening_trigger_rms_avg=%s "
            "opening_trigger_best_playback_correlation=%s "
            "opening_trigger_best_playback_frame=%s "
            "opening_trigger_best_playback_rms=%s "
            "opening_trigger_last_playback_age_ms=%s "
            "turns_started=%s turns_committed=%s turns_completed=%s "
            "turns_failed=%s duration_ms=%s",
            session.call_id,
            session.session_id,
            session.inbound_frames,
            session.inbound_bytes,
            session.streamed_input_bytes,
            session.outbound_frames,
            session.outbound_bytes,
            session.inbound_rms_min,
            session.inbound_rms_max,
            _inbound_rms_avg(session),
            session.inbound_rms_last,
            session.inbound_high_rms_frames,
            session.inbound_first_high_rms_frame,
            session.invalid_frame_count,
            session.interruptions,
            session.dropped_playback_frames,
            session.dropped_stale_frames,
            session.playback_underruns,
            session.max_playback_queue_frames,
            session.max_playback_send_gap_ms,
            session.playback_send_gap_overruns,
            session.playback_fast_send_frames,
            session.playback_realtime_send_frames,
            session.playback_pacing_switches,
            session.flushed_tail_frames,
            session.tail_silence_frames,
            session.freeswitch_playback_events,
            session.freeswitch_queue_completed_events,
            session.freeswitch_break_requests,
            session.freeswitch_break_failures,
            session.realtime_interrupt_requests,
            session.realtime_interrupt_failures,
            session.context_repair_requests,
            session.local_barge_in_events,
            session.realtime_session_restarts,
            session.gateway_history_committed_turns,
            session.gateway_history_abandoned_turns,
            session.replayed_input_frames,
            session.replayed_input_bytes,
            session.opening_trigger_rms,
            session.opening_trigger_rms_min,
            session.opening_trigger_rms_max,
            session.opening_trigger_rms_avg,
            _format_correlation(session.opening_trigger_best_playback_correlation),
            session.opening_trigger_best_playback_frame,
            session.opening_trigger_best_playback_rms,
            session.opening_trigger_last_playback_age_ms,
            session.turns_started,
            session.turns_committed,
            session.turns_completed,
            session.turns_failed,
            int((session.disconnected_at - session.connected_at) * 1000),
        )
        self._enqueue_call_result(session)

    def _enqueue_call_result(self, session: RealtimePhoneSessionStats) -> None:
        if session.handoff_requested:
            LOGGER.info(
                "call_result_deferred_for_handoff call_id=%s session_id=%s",
                session.call_id,
                session.session_id,
            )
            return
        if self.call_result_writer is None:
            return
        payload = self._build_call_result_payload(session)
        if not self.call_result_writer.enqueue_nowait(payload):
            LOGGER.warning(
                "call_result_enqueue_failed call_id=%s session_id=%s",
                session.call_id,
                session.session_id,
            )

    def _build_call_result_payload(
        self,
        session: RealtimePhoneSessionStats,
    ) -> dict:
        disconnected_at = session.disconnected_at or time.time()
        prompt = (
            session.prompt_snapshot.to_dict()
            if session.prompt_snapshot is not None
            else {
                "scene": session.prompt_scene,
                "version": "inline",
                "instructions": self.instructions,
            }
        )
        committed_exchanges = [
            {
                "turn_id": exchange.turn_id,
                "status": exchange.status,
                "question_id": exchange.question_id,
                "reply_id": exchange.reply_id,
                "input_transcript": exchange.input_transcript,
                "output_transcript": exchange.output_transcript,
                "heard_output_transcript": exchange.heard_output_transcript,
                "played_audio_ms": exchange.played_audio_ms,
                "playback_completed": exchange.playback_completed,
                "source": exchange.source,
                "created_at_ms": exchange.created_at_ms,
            }
            for exchange in session.committed_exchanges
        ]
        turns = self._build_call_result_turns(session)
        completed_history_turns = sum(
            1 for exchange in session.committed_exchanges if exchange.status == "completed"
        )
        interrupted_history_turns = sum(
            1
            for exchange in session.committed_exchanges
            if exchange.status == "interrupted"
        )
        missing_output_turns = sum(
            1 for exchange in session.committed_exchanges if not exchange.output_transcript
        )
        return {
            "call_id": session.call_id,
            "session_id": session.session_id,
            "status": "failed" if session.failure_reason else "completed",
            "failure_reason": session.failure_reason,
            "error": session.failure_error,
            "recording_path": session.recording_path,
            "context": session.context,
            "connected_at_ms": int(session.connected_at * 1000),
            "disconnected_at_ms": int(disconnected_at * 1000),
            "duration_ms": int((disconnected_at - session.connected_at) * 1000),
            "prompt": prompt,
            "opening": {
                "text": session.opening_text,
                "text_hash": session.opening_text_hash,
                "voice": session.opening_voice,
                "speaker": session.opening_speaker,
                "playback_frames": session.opening_playback_frames,
                "playback_interrupted": session.opening_playback_interrupted,
            },
            "turns": turns,
            "committed_exchanges": committed_exchanges,
            "metrics": {
                "inbound_frames": session.inbound_frames,
                "inbound_bytes": session.inbound_bytes,
                "streamed_input_bytes": session.streamed_input_bytes,
                "outbound_frames": session.outbound_frames,
                "outbound_bytes": session.outbound_bytes,
                "invalid_frame_count": session.invalid_frame_count,
                "interruptions": session.interruptions,
                "local_barge_in_events": session.local_barge_in_events,
                "dropped_playback_frames": session.dropped_playback_frames,
                "dropped_stale_frames": session.dropped_stale_frames,
                "playback_underruns": session.playback_underruns,
                "max_playback_queue_frames": session.max_playback_queue_frames,
                "max_playback_send_gap_ms": session.max_playback_send_gap_ms,
                "playback_send_gap_overruns": session.playback_send_gap_overruns,
                "freeswitch_playback_events": session.freeswitch_playback_events,
                "freeswitch_queue_completed_events": (
                    session.freeswitch_queue_completed_events
                ),
                "freeswitch_break_requests": session.freeswitch_break_requests,
                "freeswitch_break_failures": session.freeswitch_break_failures,
                "realtime_interrupt_requests": session.realtime_interrupt_requests,
                "realtime_interrupt_failures": session.realtime_interrupt_failures,
                "context_repair_requests": session.context_repair_requests,
                "realtime_session_restarts": session.realtime_session_restarts,
                "gateway_history_committed_turns": (
                    session.gateway_history_committed_turns
                ),
                "gateway_history_abandoned_turns": (
                    session.gateway_history_abandoned_turns
                ),
                "gateway_history_completed_turns": max(
                    session.gateway_history_completed_turns,
                    completed_history_turns,
                ),
                "gateway_history_interrupted_turns": max(
                    session.gateway_history_interrupted_turns,
                    interrupted_history_turns,
                ),
                "gateway_history_missing_output_turns": max(
                    session.gateway_history_missing_output_turns,
                    missing_output_turns,
                ),
                "replayed_input_frames": session.replayed_input_frames,
                "replayed_input_bytes": session.replayed_input_bytes,
                "turns_started": session.turns_started,
                "turns_committed": session.turns_committed,
                "turns_completed": session.turns_completed,
                "turns_failed": session.turns_failed,
                "opening_trigger_rms": session.opening_trigger_rms,
                "opening_trigger_rms_min": session.opening_trigger_rms_min,
                "opening_trigger_rms_max": session.opening_trigger_rms_max,
                "opening_trigger_rms_avg": session.opening_trigger_rms_avg,
                "opening_trigger_best_playback_correlation": (
                    session.opening_trigger_best_playback_correlation
                ),
                "opening_trigger_best_playback_frame": (
                    session.opening_trigger_best_playback_frame
                ),
                "opening_trigger_best_playback_rms": (
                    session.opening_trigger_best_playback_rms
                ),
                "opening_trigger_last_playback_age_ms": (
                    session.opening_trigger_last_playback_age_ms
                ),
            },
        }

    def _build_call_result_turns(
        self,
        session: RealtimePhoneSessionStats,
    ) -> list[dict[str, str]]:
        turns: list[dict[str, str]] = []

        def append_turn(role: str, text: str | None) -> None:
            normalized = (text or "").strip()
            if normalized:
                turns.append({"role": role, "text": normalized})

        append_turn("assistant", session.opening_text)
        for exchange in session.committed_exchanges:
            append_turn("user", exchange.input_transcript)
            append_turn("assistant", exchange.output_transcript)
        return turns

    def _session_is_busy(self, session: RealtimePhoneSessionStats) -> bool:
        return (
            session.current_capture_turn_id is not None
            or session.current_output_turn_id is not None
            or self._has_playback(session)
        )

    def _has_playback(self, session: RealtimePhoneSessionStats) -> bool:
        return session.playback_active or not session.playback_queue.empty()

    def _session_by_call_id(
        self,
        call_id: str,
    ) -> RealtimePhoneSessionStats | None:
        for session in self.active_sessions.values():
            if session.call_id == call_id:
                return session
        return None


class DownsampledPlaybackBuffer:
    def __init__(self, *, source_rate: int, target_rate: int, frame_bytes: int) -> None:
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.frame_bytes = frame_bytes
        self._source_pending = bytearray()
        self._target_pending = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        self._source_pending.extend(pcm)
        aligned_len = len(self._source_pending) - (len(self._source_pending) % 2)
        if aligned_len > 0:
            source_chunk = bytes(self._source_pending[:aligned_len])
            del self._source_pending[:aligned_len]
            self._target_pending.extend(
                resample_pcm_s16le_mono(
                    source_chunk,
                    self.source_rate,
                    self.target_rate,
                )
            )
        return self._drain_frames(pad_last=False)

    def flush(self, *, pad_last: bool) -> list[bytes]:
        if self._source_pending:
            if len(self._source_pending) % 2:
                self._source_pending.append(0)
            self._target_pending.extend(
                resample_pcm_s16le_mono(
                    bytes(self._source_pending),
                    self.source_rate,
                    self.target_rate,
                )
            )
            self._source_pending.clear()
        return self._drain_frames(pad_last=pad_last)

    def _drain_frames(self, *, pad_last: bool) -> list[bytes]:
        frames: list[bytes] = []
        while len(self._target_pending) >= self.frame_bytes:
            frames.append(bytes(self._target_pending[: self.frame_bytes]))
            del self._target_pending[: self.frame_bytes]

        if pad_last and self._target_pending:
            frames.append(
                bytes(self._target_pending)
                + b"\x00" * (self.frame_bytes - len(self._target_pending))
            )
            self._target_pending.clear()

        return frames


def _call_id_from_path(path: str) -> str | None:
    prefixes = ("/media/fs/", "/media/")
    for prefix in prefixes:
        if not path.startswith(prefix):
            continue
        call_id = path[len(prefix) :].strip("/")
        if call_id:
            return call_id
    return None


@dataclass(frozen=True)
class PlaybackReferenceMatch:
    correlation: float | None
    frame_number: int | None
    rms: int | None


def _record_inbound_audio_rms(
    session: RealtimePhoneSessionStats,
    payload: bytes,
    *,
    threshold: int,
) -> int:
    rms = pcm_s16le_rms(payload)
    session.inbound_rms_last = rms
    session.inbound_rms_sum += rms
    session.inbound_rms_count += 1
    if session.inbound_rms_min is None or rms < session.inbound_rms_min:
        session.inbound_rms_min = rms
    if session.inbound_rms_max is None or rms > session.inbound_rms_max:
        session.inbound_rms_max = rms
    if rms >= threshold:
        session.inbound_high_rms_frames += 1
        if session.inbound_first_high_rms_frame is None:
            session.inbound_first_high_rms_frame = session.inbound_frames
    return rms


def _inbound_rms_avg(session: RealtimePhoneSessionStats) -> int | None:
    if session.inbound_rms_count <= 0:
        return None
    return round(session.inbound_rms_sum / session.inbound_rms_count)


def _record_opening_playback_frame(
    session: RealtimePhoneSessionStats,
    payload: bytes,
    sent_at: float,
) -> None:
    session.opening_playback_sent_frames += 1
    session.opening_last_playback_at = sent_at
    session.opening_last_playback_rms = pcm_s16le_rms(payload)
    session.opening_recent_playback_frames.append(payload)
    session.opening_recent_playback_frame_numbers.append(
        session.opening_playback_sent_frames
    )


def _business_dialog_system_role(
    employee_name: str,
    identity_name: str,
    *,
    business_instructions: str | None = None,
) -> str:
    role = _business_identity_role(identity_name)
    lines = [
        f"你是{employee_name}，{role}",
        "你代表物业中心联系小区业主或相关费用联系人，进行物业费用事项提醒、信息核实和服务协助。",
        "禁止自称豆包、AI、智能助手、机器人、模型或语音助手。",
    ]
    if business_instructions:
        lines.extend(
            [
                "",
                "# 业务提示词",
                business_instructions.strip(),
            ]
        )
    return "\n".join(lines)


def _business_identity_role(identity_name: str) -> str:
    if identity_name == "项目员工":
        return "是小区物业项目员工。"
    if identity_name == "企业客服":
        return "是物业客服中心工作人员。"
    if identity_name == "企业法务":
        return "是物业公司法务部工作人员。"
    if identity_name in {"律师", "第三方律师"}:
        return "是受物业公司委托的法律事务联系人。"
    return "是物业服务工作人员。"


def _log_dialog_prompt_lengths(
    *,
    call_id: str,
    session_id: str,
    system_role: str,
    speaking_style: str | None,
) -> None:
    system_role_chars = len(system_role)
    speaking_style_chars = len(speaking_style or "")
    total_chars = system_role_chars + speaking_style_chars
    LOGGER.info(
        "dialog_prompt_lengths call_id=%s session_id=%s "
        "system_role_chars=%s speaking_style_chars=%s total_chars=%s "
        "soft_limit_chars=%s",
        call_id,
        session_id,
        system_role_chars,
        speaking_style_chars,
        total_chars,
        DIALOG_PROMPT_SOFT_LIMIT_CHARS,
    )
    if total_chars <= DIALOG_PROMPT_SOFT_LIMIT_CHARS:
        return

    LOGGER.warning(
        "dialog_prompt_soft_limit_exceeded call_id=%s session_id=%s "
        "system_role_chars=%s speaking_style_chars=%s total_chars=%s "
        "soft_limit_chars=%s",
        call_id,
        session_id,
        system_role_chars,
        speaking_style_chars,
        total_chars,
        DIALOG_PROMPT_SOFT_LIMIT_CHARS,
    )


def _dialog_bot_name(value: str) -> str | None:
    text = _dialog_text(value)
    if not text:
        return None
    return text[:MAX_DIALOG_BOT_NAME_CHARS]


def _dialog_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _redact_spoken_amounts(text: str) -> str:
    return SPOKEN_AMOUNT_RE.sub("[金额已隐藏]", text)


def _contains_spoken_amount(text: str) -> bool:
    return SPOKEN_AMOUNT_RE.search(text) is not None


def _detect_handoff_request(text: str) -> str | None:
    normalized = re.sub(r"[\s，。！？、,.!?；;：:]+", "", text or "")
    if not normalized:
        return None
    if HANDOFF_NEGATED_REQUEST_RE.search(normalized):
        return None
    if HANDOFF_IDENTITY_QUESTION_RE.search(normalized):
        return None
    if HANDOFF_REQUEST_RE.search(normalized):
        return "request_human"
    return None


def _detect_agent_takeover_suggestion(text: str) -> str | None:
    normalized = re.sub(r"[\s，。！？、,.!?；;：:]+", "", text or "")
    if normalized == "我想投诉":
        return "complaint"
    return None


def _best_playback_reference_match(
    session: RealtimePhoneSessionStats,
    inbound_payload: bytes,
) -> PlaybackReferenceMatch:
    best_correlation: float | None = None
    best_frame_number: int | None = None
    best_rms: int | None = None
    for frame_number, playback_payload in zip(
        session.opening_recent_playback_frame_numbers,
        session.opening_recent_playback_frames,
    ):
        correlation = _pcm_abs_correlation(inbound_payload, playback_payload)
        if correlation is None:
            continue
        if best_correlation is None or correlation > best_correlation:
            best_correlation = correlation
            best_frame_number = frame_number
            best_rms = pcm_s16le_rms(playback_payload)
    return PlaybackReferenceMatch(
        correlation=best_correlation,
        frame_number=best_frame_number,
        rms=best_rms,
    )


def _opening_barge_in_looks_like_playback_echo(
    reference_match: PlaybackReferenceMatch,
    last_playback_age_ms: int | None,
) -> bool:
    if reference_match.correlation is None:
        return False
    if reference_match.correlation < OPENING_ECHO_CORRELATION_THRESHOLD:
        return False
    if last_playback_age_ms is None:
        return False
    return last_playback_age_ms <= OPENING_ECHO_MAX_LAST_PLAYBACK_AGE_MS


def _pcm_abs_correlation(left_pcm: bytes, right_pcm: bytes) -> float | None:
    if len(left_pcm) != len(right_pcm) or not left_pcm:
        return None
    left_samples = pcm_s16le_to_samples(left_pcm)
    right_samples = pcm_s16le_to_samples(right_pcm)
    if len(left_samples) != len(right_samples) or not left_samples:
        return None

    dot_product = 0
    left_square_sum = 0
    right_square_sum = 0
    for left, right in zip(left_samples, right_samples):
        dot_product += left * right
        left_square_sum += left * left
        right_square_sum += right * right
    if left_square_sum == 0 or right_square_sum == 0:
        return None
    return abs(dot_product) / math.sqrt(left_square_sum * right_square_sum)


def _int_window_stats(values: deque[int]) -> tuple[int | None, int | None, int | None]:
    if not values:
        return None, None, None
    return min(values), max(values), round(sum(values) / len(values))


def _format_correlation(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.3f}"


def _text_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _elapsed_ms(start: float | None, end: float | None = None) -> int | None:
    if start is None:
        return None
    if end is None:
        end = time.monotonic()
    return int((end - start) * 1000)
