from dataclasses import dataclass


@dataclass
class SipMediaBridgeConfig:
    channel: str = "default"
    media_hub_base_url: str = "ws://127.0.0.1:9000"
    sample_rate: int = 8000
    output_gain: float = 1.0
    output_peak_limit: int = 0
    dump: bool = False
    dump_path: str = "/tmp"
    heartbeat_interval_seconds: int = 5
    registration_timeout_seconds: int = 5
    speech_start_detection_enabled: bool = True
    speech_start_min_rms: int = 80
    speech_start_min_peak: int = 500
    speech_start_min_frames: int = 3
    speech_start_rearm_silence_frames: int = 12
    speech_start_cooldown_ms: int = 800
    outgoing_silence_trim_enabled: bool = False
    outgoing_silence_rms_threshold: int = 0
    outgoing_silence_max_hold_ms: int = 1000
