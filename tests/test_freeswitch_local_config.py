from pathlib import Path


def test_local_freeswitch_exposes_webrtc_and_matching_rtp_ports():
    compose = Path("freeswitch-local/docker-compose.yml").read_text(encoding="utf-8")

    assert '"5066:5066/tcp"' in compose
    assert '"16384-16484:16384-16484/udp"' in compose
