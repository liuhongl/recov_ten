from dataclasses import dataclass


@dataclass
class SipMediaBridgeConfig:
    channel: str = "default"
    media_hub_base_url: str = "ws://127.0.0.1:9000"
    sample_rate: int = 8000
    heartbeat_interval_seconds: int = 5
    registration_timeout_seconds: int = 5
