from ten_runtime import AudioFrame, AsyncExtension, AsyncTenEnv


class SipMediaBridgeTestEchoExtension(AsyncExtension):
    async def on_init(self, ten_env: AsyncTenEnv) -> None:
        await super().on_init(ten_env)
        ten_env.log_info("sip_media_bridge_test_echo on_init")

    async def on_start(self, ten_env: AsyncTenEnv) -> None:
        await super().on_start(ten_env)
        ten_env.log_info("sip_media_bridge_test_echo started")

    async def on_stop(self, ten_env: AsyncTenEnv) -> None:
        ten_env.log_info("sip_media_bridge_test_echo stopping")
        await super().on_stop(ten_env)

    async def on_deinit(self, ten_env: AsyncTenEnv) -> None:
        ten_env.log_info("sip_media_bridge_test_echo on_deinit")
        await super().on_deinit(ten_env)

    async def on_audio_frame(
        self,
        ten_env: AsyncTenEnv,
        audio_frame: AudioFrame,
    ) -> None:
        payload = bytes(audio_frame.get_buf())
        ten_env.log_info(
            "sip_media_bridge_test_echo received_audio_frame: "
            f"bytes={len(payload)}, "
            f"sample_rate={audio_frame.get_sample_rate()}, "
            f"channels={audio_frame.get_number_of_channels()}, "
            f"bytes_per_sample={audio_frame.get_bytes_per_sample()}, "
            f"samples_per_channel={audio_frame.get_samples_per_channel()}"
        )
        await ten_env.send_audio_frame(audio_frame=audio_frame)
