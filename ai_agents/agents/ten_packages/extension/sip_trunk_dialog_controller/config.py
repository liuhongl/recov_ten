from dataclasses import dataclass


@dataclass
class SipTrunkDialogControllerConfig:
    llm_extension: str = "llm"
    tts_extension: str = "tts"
    media_bridge_extension: str = "sip_media_bridge"
    response_language: str = "zh-CN"
    temperature: float = 0.7
    max_context_messages: int = 10
    interrupt_on_partial: bool = True
    interrupt_min_chars: int = 2
    interrupt_min_duration_ms: int = 600
    urgent_interrupt_commands: str = (
        "停,停止,停一下,先停,暂停,等一下,等等,等会,先等一下,"
        "别说了,不要说了,不用说了,别讲了,不要讲了,打住,stop"
    )
    final_interrupt_min_chars: int = 3
    final_interrupt_min_duration_ms: int = 1200
    final_interrupt_min_remaining_ms: int = 700
    speech_start_interrupt_min_rms: int = 1200
    speech_start_interrupt_min_peak: int = 4000
    barge_in_candidate_ttl_ms: int = 1600
    max_spoken_chars_per_turn: int = 28
    tts_playback_guard_ms: int = 300
