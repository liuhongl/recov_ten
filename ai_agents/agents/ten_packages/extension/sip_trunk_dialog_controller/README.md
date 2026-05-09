# SIP Trunk Dialog Controller

Minimal phone-call dialog controller for the SIP trunk staged example.

It consumes final `asr_result` data, sends a streaming `chat_completion`
command to the configured LLM extension, and forwards complete response
sentences to the configured TTS extension as `tts_text_input` data.

This extension intentionally does not manage SIP, RTP, FreeSWITCH, Media Hub,
or audio playback. Those responsibilities stay in `sip_media_bridge`.
