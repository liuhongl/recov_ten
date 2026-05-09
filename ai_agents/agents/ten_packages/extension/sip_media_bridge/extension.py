import audioop
import asyncio
import contextlib
import json
import time
from urllib.parse import quote

from ten_runtime import AsyncExtension, AudioFrame
from ten_runtime.async_ten_env import AsyncTenEnv
from ten_runtime.audio_frame import AudioFrameDataFmt
from websockets.exceptions import ConnectionClosed
from websockets.legacy.client import WebSocketClientProtocol, connect

from .config import SipMediaBridgeConfig

PCM_CHANNELS = 1
PCM_BYTES_PER_SAMPLE = 2
PCM_FRAME_DURATION_MS = 20


class SipMediaBridgeExtension(AsyncExtension):
    def __init__(self, name: str):
        super().__init__(name)
        self.config = SipMediaBridgeConfig()
        self._ws: WebSocketClientProtocol | None = None
        self._connection_task: asyncio.Task | None = None
        self._closing = False

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
            f"sample_rate={self.config.sample_rate}"
        )
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

        payload = self._normalize_outgoing_pcm(audio_frame, ten_env)
        if payload is None:
            return
        if len(payload) == 0:
            return

        frame_size = self._outgoing_frame_size_bytes()
        sent_chunks = 0
        sent_bytes = 0
        for chunk in self._iter_outgoing_pcm_chunks(payload, frame_size):
            try:
                await self._ws.send(chunk)
            except ConnectionClosed as err:
                ten_env.log_warn(
                    "sip_media_bridge failed to send pcm to media hub: "
                    f"channel={self.config.channel}, code={err.code}"
                )
                return
            sent_chunks += 1
            sent_bytes += len(chunk)
            await asyncio.sleep(PCM_FRAME_DURATION_MS / 1000)

        ten_env.log_info(
            "sip_media_bridge sent_pcm_to_media_hub: "
            f"channel={self.config.channel}, bytes={sent_bytes}, "
            f"chunks={sent_chunks}, chunk_bytes={frame_size}, "
            f"sample_rate={self.config.sample_rate}"
        )

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
                normalized, _ = audioop.ratecv(
                    normalized,
                    source_bytes_per_sample,
                    PCM_CHANNELS,
                    source_sample_rate,
                    self.config.sample_rate,
                    None,
                )
                ten_env.log_info(
                    "sip_media_bridge resampled outgoing audio: "
                    f"channel={self.config.channel}, "
                    f"from={source_sample_rate}, "
                    f"to={self.config.sample_rate}, "
                    f"bytes_before={len(payload)}, "
                    f"bytes_after={len(normalized)}"
                )

            return normalized
        except Exception as err:
            ten_env.log_warn(
                "sip_media_bridge failed to normalize outgoing audio: "
                f"channel={self.config.channel}, error={err}"
            )
            return None

    def _outgoing_frame_size_bytes(self) -> int:
        samples_per_frame = self.config.sample_rate * PCM_FRAME_DURATION_MS // 1000
        return samples_per_frame * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE

    def _iter_outgoing_pcm_chunks(
        self,
        payload: bytes,
        frame_size: int,
    ):
        for offset in range(0, len(payload), frame_size):
            chunk = payload[offset : offset + frame_size]
            if len(chunk) < frame_size:
                chunk = chunk + b"\x00" * (frame_size - len(chunk))
            yield chunk

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
        return SipMediaBridgeConfig(
            channel=channel,
            media_hub_base_url=media_hub_base_url,
            sample_rate=sample_rate,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            registration_timeout_seconds=registration_timeout_seconds,
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
                message_type = message.get("type")
                if message_type == "pong":
                    ten_env.log_info(
                        "sip_media_bridge media hub heartbeat acknowledged: "
                        f"channel={self.config.channel}"
                    )
                else:
                    ten_env.log_info(
                        "sip_media_bridge media hub control message: "
                        f"channel={self.config.channel}, type={message_type}"
                    )
        except ConnectionClosed as err:
            ten_env.log_info(
                "sip_media_bridge media hub connection closed: "
                f"channel={self.config.channel}, code={err.code}"
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
        ten_env.log_info(
            "sip_media_bridge received_pcm_from_media_hub: "
            f"channel={self.config.channel}, bytes={len(payload)}, "
            f"sample_rate={self.config.sample_rate}"
        )

    def _media_hub_url(self) -> str:
        base_url = self.config.media_hub_base_url.rstrip("/")
        channel = quote(self.config.channel, safe="")
        return f"{base_url}/media/ten/{channel}"
