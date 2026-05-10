import audioop
import asyncio
import contextlib
import json
import os
import time
from array import array
from urllib.parse import quote

from ten_runtime import AsyncExtension, AudioFrame, Data
from ten_runtime.async_ten_env import AsyncTenEnv
from ten_runtime.audio_frame import AudioFrameDataFmt
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import WebSocketClientProtocol, connect

from .config import SipMediaBridgeConfig

PCM_CHANNELS = 1
PCM_BYTES_PER_SAMPLE = 2
PCM_FRAME_DURATION_MS = 20
PCM_FRAME_DURATION_SECONDS = PCM_FRAME_DURATION_MS / 1000
OUTGOING_START_BUFFER_FRAMES = 3
OUTGOING_START_BUFFER_SECONDS = (
    OUTGOING_START_BUFFER_FRAMES * PCM_FRAME_DURATION_SECONDS
)


class SipMediaBridgeExtension(AsyncExtension):
    def __init__(self, name: str):
        super().__init__(name)
        self.config = SipMediaBridgeConfig()
        self._ws: WebSocketClientProtocol | None = None
        self._connection_task: asyncio.Task | None = None
        self._closing = False
        self._send_lock = asyncio.Lock()
        self._flush_generation = 0
        self._media_peer_connected = False
        self._downsample_2x_pending_sample: int | None = None
        self._outgoing_pcm_pending = b""
        self._outgoing_next_send_at: float | None = None
        self._outgoing_silence_hold: list[bytes] = []
        self._outgoing_voice_started = False
        self._outgoing_dump_file_path: str | None = None
        self._outgoing_dump_failed = False
        self._inbound_speech_frames = 0
        self._inbound_silence_frames = 0
        self._inbound_speech_started = False
        self._last_speech_start_sent_at = 0.0

    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        ten_env.log_info("sip_media_bridge on_init")

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        await super().on_start(ten_env)
        self.config = await self._load_config(ten_env)
        ten_env.log_info(
            "sip_media_bridge stage1 skeleton started: "
            f"channel={self.config.channel}, "
            f"media_hub_base_url={self.config.media_hub_base_url}, "
            f"sample_rate={self.config.sample_rate}, "
            f"output_gain={self.config.output_gain}, "
            f"output_peak_limit={self.config.output_peak_limit}, "
            f"dump={self.config.dump}, "
            f"dump_path={self.config.dump_path}, "
            f"speech_start_detection_enabled="
            f"{self.config.speech_start_detection_enabled}, "
            f"speech_start_min_rms={self.config.speech_start_min_rms}, "
            f"speech_start_min_peak={self.config.speech_start_min_peak}, "
            f"speech_start_min_frames={self.config.speech_start_min_frames}, "
            f"outgoing_silence_trim_enabled="
            f"{self.config.outgoing_silence_trim_enabled}, "
            f"outgoing_silence_rms_threshold="
            f"{self.config.outgoing_silence_rms_threshold}, "
            f"outgoing_silence_max_hold_ms="
            f"{self.config.outgoing_silence_max_hold_ms}"
        )
        self._init_outgoing_dump(ten_env)
        self._closing = False
        self._connection_task = asyncio.create_task(
            self._run_media_hub_connection(ten_env)
        )

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        self._closing = True
        if self._connection_task is not None:
            self._connection_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connection_task
            self._connection_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        ten_env.log_info(
            "sip_media_bridge stage1 skeleton stopping: "
            f"channel={self.config.channel}"
        )
        await super().on_stop(ten_env)

    async def on_deinit(self, ten_env: AsyncTenEnv) -> None:
        ten_env.log_info("sip_media_bridge on_deinit")
        await super().on_deinit(ten_env)

    async def on_audio_frame(
        self,
        ten_env: AsyncTenEnv,
        audio_frame: AudioFrame,
    ) -> None:
        if self._ws is None:
            ten_env.log_warn(
                "sip_media_bridge dropped outgoing audio frame without "
                f"media hub connection: channel={self.config.channel}"
            )
            return
        if not self._media_peer_connected:
            ten_env.log_info(
                "sip_media_bridge dropped outgoing audio without fs peer: "
                f"channel={self.config.channel}"
            )
            return

        payload = self._normalize_outgoing_pcm(audio_frame, ten_env)
        if payload is None:
            return
        if len(payload) == 0:
            return

        frame_size = self._outgoing_frame_size_bytes()
        send_generation = self._flush_generation
        sent_chunks = 0
        sent_bytes = 0
        async with self._send_lock:
            if self._should_abort_outgoing_send(send_generation):
                ten_env.log_info(
                    "sip_media_bridge dropped outgoing audio after flush: "
                    f"channel={self.config.channel}"
                )
                return
            ws = self._ws
            if ws is None:
                ten_env.log_warn(
                    "sip_media_bridge dropped outgoing audio frame without "
                    f"media hub connection: channel={self.config.channel}"
                )
                return
            chunks = self._append_outgoing_pcm(payload, frame_size)
            chunks = self._trim_outgoing_silence(chunks, final=False)
            for chunk in chunks:
                if self._should_abort_outgoing_send(send_generation):
                    ten_env.log_info(
                        "sip_media_bridge aborted outgoing audio after flush: "
                        f"channel={self.config.channel}, bytes={sent_bytes}, "
                        f"chunks={sent_chunks}"
                    )
                    return
                try:
                    await self._wait_for_outgoing_send_slot()
                    if self._should_abort_outgoing_send(send_generation):
                        ten_env.log_info(
                            "sip_media_bridge aborted outgoing audio after flush: "
                            f"channel={self.config.channel}, bytes={sent_bytes}, "
                            f"chunks={sent_chunks}"
                        )
                        return
                    self._dump_outgoing_pcm(chunk, ten_env)
                    await ws.send(chunk)
                except ConnectionClosed as err:
                    ten_env.log_warn(
                        "sip_media_bridge failed to send pcm to media hub: "
                        f"channel={self.config.channel}, code={err.code}"
                    )
                    return
                sent_chunks += 1
                sent_bytes += len(chunk)

        ten_env.log_info(
            "sip_media_bridge sent_pcm_to_media_hub: "
            f"channel={self.config.channel}, bytes={sent_bytes}, "
            f"chunks={sent_chunks}, chunk_bytes={frame_size}, "
            f"pending_bytes={len(self._outgoing_pcm_pending)}, "
            f"sample_rate={self.config.sample_rate}"
        )

    async def on_data(self, ten_env: AsyncTenEnv, data: Data) -> None:
        data_name = data.get_name()
        if data_name == "tts_audio_end":
            await self._flush_pending_outgoing_pcm(ten_env, pad=True, final=True)
            self._outgoing_next_send_at = None
            return

        if data_name == "tts_flush_end":
            self._clear_outgoing_audio_state()
            ten_env.log_info(
                "sip_media_bridge cleared outgoing audio after tts_flush_end: "
                f"channel={self.config.channel}"
            )
            return

        if data_name != "tts_flush":
            ten_env.log_info("sip_media_bridge ignored data: " f"name={data_name}")
            return

        self._flush_generation += 1
        self._clear_outgoing_audio_state()
        ten_env.log_info(
            "sip_media_bridge flushed outgoing audio: "
            f"channel={self.config.channel}, "
            f"generation={self._flush_generation}"
        )

    def _should_abort_outgoing_send(self, generation: int) -> bool:
        return generation != self._flush_generation

    def _clear_outgoing_audio_state(self) -> None:
        self._outgoing_pcm_pending = b""
        self._downsample_2x_pending_sample = None
        self._outgoing_next_send_at = None
        self._outgoing_silence_hold = []
        self._outgoing_voice_started = False

    def _normalize_outgoing_pcm(
        self,
        audio_frame: AudioFrame,
        ten_env: AsyncTenEnv,
    ) -> bytes | None:
        payload = bytes(audio_frame.get_buf())
        if len(payload) == 0:
            return payload

        source_sample_rate = audio_frame.get_sample_rate()
        source_channels = audio_frame.get_number_of_channels()
        source_bytes_per_sample = audio_frame.get_bytes_per_sample()

        if source_sample_rate <= 0:
            source_sample_rate = self.config.sample_rate

        if source_bytes_per_sample != PCM_BYTES_PER_SAMPLE:
            ten_env.log_warn(
                "sip_media_bridge dropped unsupported outgoing audio: "
                f"channel={self.config.channel}, "
                f"bytes_per_sample={source_bytes_per_sample}"
            )
            return None

        if source_channels <= 0:
            source_channels = PCM_CHANNELS

        try:
            normalized = payload

            if source_channels == 2:
                normalized = audioop.tomono(
                    normalized,
                    source_bytes_per_sample,
                    0.5,
                    0.5,
                )
                source_channels = PCM_CHANNELS
            elif source_channels != PCM_CHANNELS:
                ten_env.log_warn(
                    "sip_media_bridge dropped unsupported outgoing audio: "
                    f"channel={self.config.channel}, "
                    f"channels={source_channels}"
                )
                return None

            if source_sample_rate != self.config.sample_rate:
                bytes_before = len(normalized)
                normalized = self._resample_outgoing_pcm(
                    normalized,
                    source_sample_rate,
                    source_bytes_per_sample,
                    ten_env,
                )
                if normalized is None:
                    return None
                self._log_outgoing_resample(
                    ten_env,
                    source_sample_rate,
                    bytes_before,
                    len(normalized),
                )

            return self._apply_output_level(normalized, ten_env)
        except Exception as err:
            ten_env.log_warn(
                "sip_media_bridge failed to normalize outgoing audio: "
                f"channel={self.config.channel}, error={err}"
            )
            return None

    def _resample_outgoing_pcm(
        self,
        payload: bytes,
        source_sample_rate: int,
        source_bytes_per_sample: int,
        ten_env: AsyncTenEnv,
    ) -> bytes | None:
        if source_sample_rate == self.config.sample_rate * 2:
            return self._downsample_2x_mono_16bit(payload)

        normalized, _ = audioop.ratecv(
            payload,
            source_bytes_per_sample,
            PCM_CHANNELS,
            source_sample_rate,
            self.config.sample_rate,
            None,
        )
        ten_env.log_info(
            "sip_media_bridge used generic outgoing resampler: "
            f"channel={self.config.channel}, "
            f"from={source_sample_rate}, to={self.config.sample_rate}"
        )
        return normalized

    def _downsample_2x_mono_16bit(self, payload: bytes) -> bytes:
        usable_bytes = len(payload) - (len(payload) % PCM_BYTES_PER_SAMPLE)
        if usable_bytes <= 0:
            return b""

        samples = array("h")
        samples.frombytes(payload[:usable_bytes])
        output = array("h")

        start_index = 0
        if self._downsample_2x_pending_sample is not None:
            output.append(
                (self._downsample_2x_pending_sample + int(samples[start_index])) // 2
            )
            self._downsample_2x_pending_sample = None
            start_index = 1

        sample_count = len(samples) - start_index
        paired_count = sample_count - (sample_count % 2)
        for index in range(start_index, start_index + paired_count, 2):
            output.append((int(samples[index]) + int(samples[index + 1])) // 2)

        if paired_count != sample_count:
            self._downsample_2x_pending_sample = int(samples[-1])
        return output.tobytes()

    def _log_outgoing_resample(
        self,
        ten_env: AsyncTenEnv,
        source_sample_rate: int,
        bytes_before: int,
        bytes_after: int,
    ) -> None:
        ten_env.log_info(
            "sip_media_bridge resampled outgoing audio: "
            f"channel={self.config.channel}, "
            f"from={source_sample_rate}, "
            f"to={self.config.sample_rate}, "
            f"bytes_before={bytes_before}, "
            f"bytes_after={bytes_after}"
        )

    def _apply_output_level(
        self,
        payload: bytes,
        ten_env: AsyncTenEnv,
    ) -> bytes:
        if len(payload) == 0:
            return payload

        adjusted = payload
        peak_before = audioop.max(adjusted, PCM_BYTES_PER_SAMPLE)
        gain = self.config.output_gain
        if gain > 0 and gain != 1.0:
            adjusted = audioop.mul(adjusted, PCM_BYTES_PER_SAMPLE, gain)

        peak_after_gain = audioop.max(adjusted, PCM_BYTES_PER_SAMPLE)
        peak_limit = self.config.output_peak_limit
        if peak_limit > 0 and peak_after_gain > peak_limit:
            adjusted = audioop.mul(
                adjusted,
                PCM_BYTES_PER_SAMPLE,
                peak_limit / peak_after_gain,
            )

        peak_after = audioop.max(adjusted, PCM_BYTES_PER_SAMPLE)
        if adjusted is not payload:
            ten_env.log_info(
                "sip_media_bridge adjusted outgoing audio level: "
                f"channel={self.config.channel}, "
                f"gain={gain}, peak_limit={peak_limit}, "
                f"peak_before={peak_before}, peak_after={peak_after}"
            )
        return adjusted

    def _init_outgoing_dump(self, ten_env: AsyncTenEnv) -> None:
        self._outgoing_dump_file_path = None
        self._outgoing_dump_failed = False
        if not self.config.dump:
            return

        try:
            os.makedirs(self.config.dump_path, exist_ok=True)
            channel = quote(self.config.channel, safe="") or "default"
            file_name = (
                f"{channel}-{os.getpid()}-{int(time.time())}-"
                f"outgoing-{self.config.sample_rate}hz.pcm"
            )
            self._outgoing_dump_file_path = os.path.join(
                self.config.dump_path,
                file_name,
            )
            ten_env.log_info(
                "sip_media_bridge outgoing dump enabled: "
                f"path={self._outgoing_dump_file_path}"
            )
        except Exception as err:
            self._outgoing_dump_failed = True
            ten_env.log_warn(
                "sip_media_bridge failed to initialize outgoing dump: "
                f"path={self.config.dump_path}, error={err}"
            )

    def _dump_outgoing_pcm(
        self,
        payload: bytes,
        ten_env: AsyncTenEnv,
    ) -> None:
        if (
            not self.config.dump
            or self._outgoing_dump_file_path is None
            or self._outgoing_dump_failed
            or len(payload) == 0
        ):
            return

        try:
            with open(self._outgoing_dump_file_path, "ab") as dump_file:
                dump_file.write(payload)
        except Exception as err:
            self._outgoing_dump_failed = True
            ten_env.log_warn(
                "sip_media_bridge failed to write outgoing dump: "
                f"path={self._outgoing_dump_file_path}, error={err}"
            )

    def _outgoing_frame_size_bytes(self) -> int:
        samples_per_frame = self.config.sample_rate * PCM_FRAME_DURATION_MS // 1000
        return samples_per_frame * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE

    def _append_outgoing_pcm(
        self,
        payload: bytes,
        frame_size: int,
    ) -> list[bytes]:
        self._outgoing_pcm_pending += payload
        chunks = []
        while len(self._outgoing_pcm_pending) >= frame_size:
            chunks.append(self._outgoing_pcm_pending[:frame_size])
            self._outgoing_pcm_pending = self._outgoing_pcm_pending[frame_size:]
        return chunks

    def _trim_outgoing_silence(
        self,
        chunks: list[bytes],
        final: bool,
    ) -> list[bytes]:
        if not self.config.outgoing_silence_trim_enabled:
            return chunks

        threshold = max(self.config.outgoing_silence_rms_threshold, 0)
        max_hold_frames = max(
            1,
            self.config.outgoing_silence_max_hold_ms // PCM_FRAME_DURATION_MS,
        )
        ready_chunks: list[bytes] = []

        for chunk in chunks:
            if audioop.rms(chunk, PCM_BYTES_PER_SAMPLE) <= threshold:
                if not self._outgoing_voice_started:
                    continue
                self._outgoing_silence_hold.append(chunk)
                if len(self._outgoing_silence_hold) > max_hold_frames:
                    ready_chunks.append(self._outgoing_silence_hold.pop(0))
                continue

            if self._outgoing_voice_started and self._outgoing_silence_hold:
                ready_chunks.extend(self._outgoing_silence_hold)
                self._outgoing_silence_hold = []

            self._outgoing_voice_started = True
            ready_chunks.append(chunk)

        if final:
            ready_chunks.extend(
                chunk
                for chunk in self._outgoing_silence_hold
                if audioop.max(chunk, PCM_BYTES_PER_SAMPLE) > 0
            )
            self._outgoing_silence_hold = []
            self._outgoing_voice_started = False

        return ready_chunks

    async def _wait_for_outgoing_send_slot(self) -> None:
        now = time.monotonic()
        if self._outgoing_next_send_at is None:
            self._outgoing_next_send_at = now + OUTGOING_START_BUFFER_SECONDS
        elif now - self._outgoing_next_send_at >= PCM_FRAME_DURATION_SECONDS:
            self._outgoing_next_send_at = now

        delay = self._outgoing_next_send_at - now
        send_at = self._outgoing_next_send_at
        if delay > 0:
            await asyncio.sleep(delay)
        self._outgoing_next_send_at = send_at + PCM_FRAME_DURATION_SECONDS

    async def _flush_pending_outgoing_pcm(
        self,
        ten_env: AsyncTenEnv,
        pad: bool,
        final: bool = False,
    ) -> None:
        if len(self._outgoing_pcm_pending) == 0 and not final:
            return

        frame_size = self._outgoing_frame_size_bytes()
        send_generation = self._flush_generation
        sent_chunks = 0
        sent_bytes = 0
        async with self._send_lock:
            if self._should_abort_outgoing_send(send_generation):
                self._outgoing_pcm_pending = b""
                return

            chunk = self._outgoing_pcm_pending
            self._outgoing_pcm_pending = b""
            if pad and 0 < len(chunk) < frame_size:
                chunk = chunk + b"\x00" * (frame_size - len(chunk))
            chunks = [chunk] if len(chunk) > 0 else []
            chunks = self._trim_outgoing_silence(chunks, final=final)
            if not chunks:
                return

            ws = self._ws
            if ws is None:
                ten_env.log_warn(
                    "sip_media_bridge dropped pending outgoing audio without "
                    f"media hub connection: channel={self.config.channel}, "
                    f"bytes={len(chunk)}"
                )
                return

            for chunk in chunks:
                try:
                    await self._wait_for_outgoing_send_slot()
                    if self._should_abort_outgoing_send(send_generation):
                        return
                    self._dump_outgoing_pcm(chunk, ten_env)
                    await ws.send(chunk)
                except ConnectionClosed as err:
                    ten_env.log_warn(
                        "sip_media_bridge failed to send pending pcm to media hub: "
                        f"channel={self.config.channel}, code={err.code}"
                    )
                    return
                sent_chunks += 1
                sent_bytes += len(chunk)

        ten_env.log_info(
            "sip_media_bridge flushed pending_pcm_to_media_hub: "
            f"channel={self.config.channel}, bytes={sent_bytes}, "
            f"chunks={sent_chunks}, "
            f"chunk_bytes={frame_size}, sample_rate={self.config.sample_rate}"
        )

    async def _load_config(
        self,
        ten_env: AsyncTenEnv,
    ) -> SipMediaBridgeConfig:
        channel = await self._get_string_property(
            ten_env,
            "channel",
            SipMediaBridgeConfig.channel,
        )
        media_hub_base_url = await self._get_string_property(
            ten_env,
            "media_hub_base_url",
            SipMediaBridgeConfig.media_hub_base_url,
        )
        sample_rate = await self._get_int_property(
            ten_env,
            "sample_rate",
            SipMediaBridgeConfig.sample_rate,
        )
        output_gain = await self._get_float_property(
            ten_env,
            "output_gain",
            SipMediaBridgeConfig.output_gain,
        )
        output_peak_limit = await self._get_int_property(
            ten_env,
            "output_peak_limit",
            SipMediaBridgeConfig.output_peak_limit,
        )
        dump = await self._get_bool_property(
            ten_env,
            "dump",
            SipMediaBridgeConfig.dump,
        )
        dump_path = await self._get_string_property(
            ten_env,
            "dump_path",
            SipMediaBridgeConfig.dump_path,
        )
        heartbeat_interval_seconds = await self._get_int_property(
            ten_env,
            "heartbeat_interval_seconds",
            SipMediaBridgeConfig.heartbeat_interval_seconds,
        )
        registration_timeout_seconds = await self._get_int_property(
            ten_env,
            "registration_timeout_seconds",
            SipMediaBridgeConfig.registration_timeout_seconds,
        )
        speech_start_detection_enabled = await self._get_bool_property(
            ten_env,
            "speech_start_detection_enabled",
            SipMediaBridgeConfig.speech_start_detection_enabled,
        )
        speech_start_min_rms = await self._get_int_property(
            ten_env,
            "speech_start_min_rms",
            SipMediaBridgeConfig.speech_start_min_rms,
        )
        speech_start_min_peak = await self._get_int_property(
            ten_env,
            "speech_start_min_peak",
            SipMediaBridgeConfig.speech_start_min_peak,
        )
        speech_start_min_frames = await self._get_int_property(
            ten_env,
            "speech_start_min_frames",
            SipMediaBridgeConfig.speech_start_min_frames,
        )
        speech_start_rearm_silence_frames = await self._get_int_property(
            ten_env,
            "speech_start_rearm_silence_frames",
            SipMediaBridgeConfig.speech_start_rearm_silence_frames,
        )
        speech_start_cooldown_ms = await self._get_int_property(
            ten_env,
            "speech_start_cooldown_ms",
            SipMediaBridgeConfig.speech_start_cooldown_ms,
        )
        outgoing_silence_trim_enabled = await self._get_bool_property(
            ten_env,
            "outgoing_silence_trim_enabled",
            SipMediaBridgeConfig.outgoing_silence_trim_enabled,
        )
        outgoing_silence_rms_threshold = await self._get_int_property(
            ten_env,
            "outgoing_silence_rms_threshold",
            SipMediaBridgeConfig.outgoing_silence_rms_threshold,
        )
        outgoing_silence_max_hold_ms = await self._get_int_property(
            ten_env,
            "outgoing_silence_max_hold_ms",
            SipMediaBridgeConfig.outgoing_silence_max_hold_ms,
        )
        return SipMediaBridgeConfig(
            channel=channel,
            media_hub_base_url=media_hub_base_url,
            sample_rate=sample_rate,
            output_gain=output_gain,
            output_peak_limit=output_peak_limit,
            dump=dump,
            dump_path=dump_path,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            registration_timeout_seconds=registration_timeout_seconds,
            speech_start_detection_enabled=speech_start_detection_enabled,
            speech_start_min_rms=speech_start_min_rms,
            speech_start_min_peak=speech_start_min_peak,
            speech_start_min_frames=speech_start_min_frames,
            speech_start_rearm_silence_frames=speech_start_rearm_silence_frames,
            speech_start_cooldown_ms=speech_start_cooldown_ms,
            outgoing_silence_trim_enabled=outgoing_silence_trim_enabled,
            outgoing_silence_rms_threshold=outgoing_silence_rms_threshold,
            outgoing_silence_max_hold_ms=outgoing_silence_max_hold_ms,
        )

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

    async def _run_media_hub_connection(
        self,
        ten_env: AsyncTenEnv,
    ) -> None:
        url = self._media_hub_url()
        retry_delay_seconds = 1

        while not self._closing:
            try:
                ten_env.log_info(
                    "sip_media_bridge connecting media hub: "
                    f"url={url}, channel={self.config.channel}"
                )
                async with connect(
                    url,
                    ping_interval=None,
                    close_timeout=1,
                ) as websocket:
                    self._ws = websocket
                    await self._register_with_media_hub(websocket, ten_env)
                    await self._run_control_loops(websocket, ten_env)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if not self._closing:
                    ten_env.log_warn(
                        "sip_media_bridge media hub connection failed: "
                        f"channel={self.config.channel}, error={err}"
                    )
                    await asyncio.sleep(retry_delay_seconds)
            finally:
                self._ws = None

    async def _register_with_media_hub(
        self,
        websocket: WebSocketClientProtocol,
        ten_env: AsyncTenEnv,
    ) -> None:
        register_message = {
            "type": "register",
            "role": "ten",
            "channel": self.config.channel,
            "sample_rate": self.config.sample_rate,
        }
        await websocket.send(json.dumps(register_message))

        raw_message = await asyncio.wait_for(
            websocket.recv(),
            timeout=self.config.registration_timeout_seconds,
        )
        message = json.loads(raw_message)
        if message.get("type") != "registered":
            raise RuntimeError(f"unexpected media hub response: {message}")

        ten_env.log_info(
            "sip_media_bridge media hub registered: "
            f"channel={self.config.channel}, session_id={message.get('session_id')}"
        )

    async def _run_control_loops(
        self,
        websocket: WebSocketClientProtocol,
        ten_env: AsyncTenEnv,
    ) -> None:
        tasks = [
            asyncio.create_task(self._heartbeat_loop(websocket, ten_env)),
            asyncio.create_task(self._receive_loop(websocket, ten_env)),
        ]
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    async def _heartbeat_loop(
        self,
        websocket: WebSocketClientProtocol,
        ten_env: AsyncTenEnv,
    ) -> None:
        while not self._closing:
            await asyncio.sleep(self.config.heartbeat_interval_seconds)
            message = {
                "type": "ping",
                "channel": self.config.channel,
                "ts": time.time(),
            }
            await websocket.send(json.dumps(message))
            ten_env.log_info(
                "sip_media_bridge media hub heartbeat sent: "
                f"channel={self.config.channel}"
            )

    async def _receive_loop(
        self,
        websocket: WebSocketClientProtocol,
        ten_env: AsyncTenEnv,
    ) -> None:
        try:
            async for raw_message in websocket:
                if isinstance(raw_message, bytes):
                    await self._send_pcm_to_graph(raw_message, ten_env)
                    continue

                message = json.loads(raw_message)
                await self._handle_media_hub_control_message(message, ten_env)
        except ConnectionClosed as err:
            ten_env.log_info(
                "sip_media_bridge media hub connection closed: "
                f"channel={self.config.channel}, code={err.code}"
            )

    async def _handle_media_hub_control_message(
        self,
        message: dict,
        ten_env: AsyncTenEnv,
    ) -> None:
        message_type = message.get("type")
        if message_type == "pong":
            ten_env.log_info(
                "sip_media_bridge media hub heartbeat acknowledged: "
                f"channel={self.config.channel}"
            )
            return

        if message_type == "media_connected":
            self._media_peer_connected = True
            self._reset_inbound_speech_state()
            await self._send_peer_event_to_graph(
                "sip_peer_connected",
                message,
                ten_env,
                peer_role="fs",
                reason="media_connected",
            )
            return

        if message_type == "peer_disconnected":
            peer_role = str(message.get("peer_role", ""))
            if peer_role == "fs":
                self._media_peer_connected = False
                self._flush_generation += 1
                self._clear_outgoing_audio_state()
                self._reset_inbound_speech_state()
                await self._send_peer_event_to_graph(
                    "sip_peer_disconnected",
                    message,
                    ten_env,
                    peer_role=peer_role,
                    reason=str(message.get("reason", "peer_disconnected")),
                )
            return

        ten_env.log_info(
            "sip_media_bridge media hub control message: "
            f"channel={self.config.channel}, type={message_type}"
        )

    async def _send_peer_event_to_graph(
        self,
        name: str,
        message: dict,
        ten_env: AsyncTenEnv,
        peer_role: str,
        reason: str,
    ) -> None:
        data = Data.create(name)
        data.set_property_from_json(
            None,
            json.dumps(
                {
                    "channel": str(message.get("channel", self.config.channel)),
                    "peer_role": peer_role,
                    "reason": reason,
                }
            ),
        )
        await ten_env.send_data(data)
        ten_env.log_info(
            "sip_media_bridge sent_peer_event: "
            f"name={name}, channel={self.config.channel}, "
            f"peer_role={peer_role}, reason={reason}"
        )

    async def _send_pcm_to_graph(
        self,
        payload: bytes,
        ten_env: AsyncTenEnv,
    ) -> None:
        if len(payload) == 0:
            return

        audio_frame = AudioFrame.create("pcm_frame")
        audio_frame.alloc_buf(len(payload))
        buf = audio_frame.lock_buf()
        try:
            buf[:] = payload
        finally:
            audio_frame.unlock_buf(buf)

        audio_frame.set_sample_rate(self.config.sample_rate)
        audio_frame.set_number_of_channels(PCM_CHANNELS)
        audio_frame.set_bytes_per_sample(PCM_BYTES_PER_SAMPLE)
        audio_frame.set_data_fmt(AudioFrameDataFmt.INTERLEAVE)
        audio_frame.set_samples_per_channel(
            len(payload) // (PCM_CHANNELS * PCM_BYTES_PER_SAMPLE)
        )

        await ten_env.send_audio_frame(audio_frame=audio_frame)
        await self._maybe_send_user_speech_start(payload, ten_env)
        ten_env.log_info(
            "sip_media_bridge received_pcm_from_media_hub: "
            f"channel={self.config.channel}, bytes={len(payload)}, "
            f"sample_rate={self.config.sample_rate}"
        )

    async def _maybe_send_user_speech_start(
        self,
        payload: bytes,
        ten_env: AsyncTenEnv,
    ) -> None:
        if not self.config.speech_start_detection_enabled:
            return
        if len(payload) < PCM_BYTES_PER_SAMPLE:
            return

        try:
            rms = audioop.rms(payload, PCM_BYTES_PER_SAMPLE)
            peak = audioop.max(payload, PCM_BYTES_PER_SAMPLE)
        except audioop.error:
            return

        is_speech = (
            rms >= max(self.config.speech_start_min_rms, 0)
            and peak >= max(self.config.speech_start_min_peak, 0)
        )

        if not is_speech:
            self._inbound_silence_frames += 1
            if self._inbound_speech_started and self._inbound_silence_frames >= max(
                self.config.speech_start_rearm_silence_frames,
                1,
            ):
                self._reset_inbound_speech_state()
            elif not self._inbound_speech_started:
                self._inbound_speech_frames = 0
            return

        self._inbound_silence_frames = 0
        self._inbound_speech_frames += 1
        if self._inbound_speech_started:
            return

        min_frames = max(self.config.speech_start_min_frames, 1)
        if self._inbound_speech_frames < min_frames:
            return

        now = time.monotonic()
        cooldown_seconds = max(self.config.speech_start_cooldown_ms, 0) / 1000
        if (
            cooldown_seconds > 0
            and now - self._last_speech_start_sent_at < cooldown_seconds
        ):
            self._inbound_speech_started = True
            return

        self._inbound_speech_started = True
        self._last_speech_start_sent_at = now
        await self._send_user_speech_start_event(
            ten_env,
            rms=rms,
            peak=peak,
            consecutive_frames=self._inbound_speech_frames,
        )

    async def _send_user_speech_start_event(
        self,
        ten_env: AsyncTenEnv,
        rms: int,
        peak: int,
        consecutive_frames: int,
    ) -> None:
        data = Data.create("sip_user_speech_start")
        data.set_property_from_json(
            None,
            json.dumps(
                {
                    "channel": self.config.channel,
                    "rms": rms,
                    "peak": peak,
                    "consecutive_frames": consecutive_frames,
                    "sample_rate": self.config.sample_rate,
                    "frame_duration_ms": PCM_FRAME_DURATION_MS,
                }
            ),
        )
        await ten_env.send_data(data)
        ten_env.log_info(
            "sip_media_bridge sent_user_speech_start: "
            f"channel={self.config.channel}, rms={rms}, peak={peak}, "
            f"consecutive_frames={consecutive_frames}, "
            f"generation={self._flush_generation}"
        )

    def _reset_inbound_speech_state(self) -> None:
        self._inbound_speech_frames = 0
        self._inbound_silence_frames = 0
        self._inbound_speech_started = False

    def _media_hub_url(self) -> str:
        base_url = self.config.media_hub_base_url.rstrip("/")
        channel = quote(self.config.channel, safe="")
        return f"{base_url}/media/ten/{channel}"
