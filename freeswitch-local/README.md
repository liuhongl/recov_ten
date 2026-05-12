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
`5080`, `18021`, or `26384-26484`, then run:

```powershell
cd freeswitch-local
docker compose up -d --build
```

The container name is:

```text
sip_realtime_freeswitch
```

For the local macOS softphone workflow, prefer the project helper:

```bash
scripts/dev-local.sh start
scripts/dev-local.sh check
```

It starts/checks the FreeSWITCH container, the host RTP relay, and the realtime
gateway health endpoint.

## macOS Docker Desktop RTP Relay

On macOS Docker Desktop, local softphones such as Linphone do not send RTP
directly to the FreeSWITCH container. The working local structure is:

```text
Softphone RTP 16384-16484
  -> host rtp_host_relay.py
  -> host 26384-26484
  -> Docker port mapping
  -> FreeSWITCH container 16384-16484
```

This mirrors the old TEN local runtime that was proven to work. Without the
relay, SIP registration and calls can succeed while FreeSWITCH skips inbound
RTP packets and records silence.

Start the relay on the host if it is not already running:

```bash
python3 freeswitch-local/scripts/rtp_host_relay.py
```

Expected listener:

```text
0.0.0.0:16384-16484 -> 127.0.0.1:26384-26484
```

Docker Compose must publish the alternate host RTP range:

```yaml
ports:
  - "26384-26484:16384-16484/udp"
```

Keep FreeSWITCH SDP advertising the host LAN IP and the normal RTP range
`16384-16484`; the relay owns that normal range on the host.

## sip-provider Sandbox

The local runtime includes a first-pass `sip-provider` trunk sandbox. It is a
FreeSWITCH-only simulation of the real IP-allowlisted trunk shape:

```text
external profile
  -> gateway sip-provider-sandbox
  -> sip-provider-sandbox profile on UDP 5089
  -> sip_provider_sandbox dialplan context
```

The sandbox gateway should look like:

```text
State  NOREG
Status UP
From   sip:037123124845@47.94.86.132
```

Check it with:

```bash
docker exec sip_realtime_freeswitch fs_cli -x "sofia status gateway sip-provider-sandbox"
docker exec sip_realtime_freeswitch fs_cli -x "sofia status profile sip-provider-sandbox"
```

Use these endpoint overrides from `/outbound-test` or `POST /calls`:

```text
sofia/gateway/sip-provider-sandbox/15800967789  -> answer
sofia/gateway/sip-provider-sandbox/18518968743  -> 183 then answer
sofia/gateway/sip-provider-sandbox/19900000000  -> 408 timeout
sofia/gateway/sip-provider-sandbox/19900000001  -> 486 busy
sofia/gateway/sip-provider-sandbox/19900000002  -> 603 decline
sofia/gateway/sip-provider-sandbox/19900000003  -> 508 upstream failure
sofia/gateway/sip-provider-sandbox/19900000004  -> 503 trunk unavailable
```

The sandbox validates the main local contract: original domestic number format,
`From` caller ID `037123124845`, PCMA/PCMU, `ptime=20`, RFC2833 DTMF, and common
provider status outcomes. It does not fully reproduce public-network NAT,
provider SBC private behavior, or real carrier routing.

## Real sip-provider Template

The real trunk template is intentionally not auto-loaded:

```text
conf/sip_profiles/external/sip-provider.xml.template
```

FreeSWITCH includes `external/*.xml`, so keep the template suffix on local Mac
and Docker runs. Only copy it to `sip-provider.xml` on a SIP/RTP reachable
server after confirming public `external_sip_ip`, `external_rtp_ip`, firewall
rules, and provider IP allowlisting.

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
