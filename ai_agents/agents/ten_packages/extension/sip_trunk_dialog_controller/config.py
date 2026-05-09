from dataclasses import dataclass


@dataclass
class SipTrunkDialogControllerConfig:
    llm_extension: str = "llm"
    tts_extension: str = "tts"
    response_language: str = "zh-CN"
    temperature: float = 0.7
    max_context_messages: int = 10
