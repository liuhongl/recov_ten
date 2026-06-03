from pathlib import Path


def test_local_freeswitch_exposes_webrtc_and_matching_rtp_ports():
    compose = Path("freeswitch-local/docker-compose.yml").read_text(encoding="utf-8")

    assert '"5066:5066/tcp"' in compose
    assert '"26384-26484:16384-16484/udp"' in compose
    assert '"16384-16484:16384-16484/udp"' not in compose


def test_local_freeswitch_mounts_recordings_directory():
    compose = Path("freeswitch-local/docker-compose.yml").read_text(encoding="utf-8")

    assert "./recordings:/var/lib/freeswitch/recordings" in compose


def test_realtime_lua_starts_full_call_recording_when_path_is_present():
    script = Path("freeswitch-local/scripts/sip_realtime_audio_stream_start.lua").read_text(
        encoding="utf-8"
    )

    assert "sip_realtime_recording_path" in script
    assert "record_session" in script
