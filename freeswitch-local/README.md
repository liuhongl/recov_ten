# Local FreeSWITCH Test Runtime

This directory contains the local Docker FreeSWITCH runtime used by the
`sip-realtime-voice-gateway` 9199 test path.

It is intentionally kept under this standalone gateway project so the gateway
can be copied into a new repository without depending on the old TEN
`ai_agents/local` directory.

## Contents

```text
docker-compose.yml
conf/
scripts/
image/Dockerfile
image/mod_audio_stream.so
mod_audio_stream/README.playback.md
mod_audio_stream/IMPORTANT.md
```

`image/mod_audio_stream.so` is loaded into the local FreeSWITCH image so
`uuid_audio_stream` can connect the call media to the gateway WebSocket.

## Start

Stop any older local FreeSWITCH container that already uses ports `5060`,
`5080`, `18021`, or `16384-16484`, then run:

```powershell
cd freeswitch-local
docker compose up -d --build
```

The container name is:

```text
sip_realtime_freeswitch
```

## 9199 Path

The local dialplan maps `9199` to:

```text
ws://host.docker.internal:9101/media/fs/${uuid}
```

The Lua startup script is:

```text
scripts/sip_realtime_audio_stream_start.lua
```

The gateway must already be running on host port `9101`.

## Security Note

The copied local FreeSWITCH config intentionally does not include TLS `.pem`
files. The local `9199` test path uses plain SIP/RTP on loopback/Docker ports.
Generate environment-specific certificates separately before enabling TLS or
WSS in a real deployment.
