#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:9100}"
FREESWITCH_CONTAINER="${FREESWITCH_CONTAINER:-sip_realtime_freeswitch}"
EXTENSION="${EXTENSION:-1000}"
EXPECTED_PASSWORD="${EXPECTED_PASSWORD:-tenlocal1000}"
RELAY_PATTERN="rtp_host_relay.py .*--target-start 26384|rtp_host_relay.py$"

failures=0
warnings=0

ok() {
  printf '[ok] %s\n' "$1"
}

warn() {
  warnings=$((warnings + 1))
  printf '[warn] %s\n' "$1"
}

fail() {
  failures=$((failures + 1))
  printf '[fail] %s\n' "$1"
}

section() {
  printf '\n== %s ==\n' "$1"
}

detect_host_ip() {
  local iface ip
  for iface in en0 en1; do
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    if [[ -n "$ip" ]]; then
      printf '%s\n' "$ip"
      return 0
    fi
  done
  iface="$(route get default 2>/dev/null | awk '/interface:/{print $2; exit}')"
  if [[ -n "$iface" ]]; then
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    if [[ -n "$ip" ]]; then
      printf '%s\n' "$ip"
      return 0
    fi
  fi
  return 1
}

json_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
value = payload
for part in sys.argv[2].split("."):
    value = value[part]
print(value)
PY
}

section "Local SIP target"
host_ip="$(detect_host_ip || true)"
if [[ -z "$host_ip" ]]; then
  fail "could not detect Mac LAN IP"
else
  ok "Mac LAN IP: $host_ip"
fi
printf 'Softphone account: %s / %s / UDP 5060\n' "$EXTENSION" "$EXPECTED_PASSWORD"
if [[ -n "$host_ip" ]]; then
  printf 'Softphone server: sip:%s:5060\n' "$host_ip"
fi

section "Gateway"
health_json="$(curl -fsS "$GATEWAY_URL/health" 2>/dev/null || true)"
if [[ -z "$health_json" ]]; then
  fail "gateway health is not reachable: $GATEWAY_URL/health"
else
  ok "gateway health: $health_json"
fi

ready_json="$(curl -fsS "$GATEWAY_URL/ready" 2>/dev/null || true)"
if [[ -z "$ready_json" ]]; then
  fail "gateway ready endpoint is not reachable: $GATEWAY_URL/ready"
else
  outbound_enabled="$(json_field "$ready_json" config.outbound.enabled 2>/dev/null || printf unknown)"
  recording_enabled="$(json_field "$ready_json" config.call_recording.enabled 2>/dev/null || printf unknown)"
  ok "outbound.enabled=$outbound_enabled"
  if [[ "$recording_enabled" != "True" && "$recording_enabled" != "true" ]]; then
    warn "call_recording.enabled=$recording_enabled; calls can be tested, but full-call recording will not be generated"
  else
    ok "call_recording.enabled=$recording_enabled"
  fi
fi

if curl -fsS -o /dev/null "$GATEWAY_URL/outbound-test" 2>/dev/null; then
  ok "outbound test page: $GATEWAY_URL/outbound-test"
else
  fail "outbound test page is not reachable: $GATEWAY_URL/outbound-test"
fi

section "FreeSWITCH"
if ! docker ps --format '{{.Names}}' | grep -qx "$FREESWITCH_CONTAINER"; then
  fail "FreeSWITCH container is not running: $FREESWITCH_CONTAINER"
else
  health="$(docker inspect --format '{{.State.Health.Status}}' "$FREESWITCH_CONTAINER" 2>/dev/null || true)"
  if [[ "$health" == "healthy" ]]; then
    ok "FreeSWITCH container is healthy"
  else
    fail "FreeSWITCH container health is '$health'"
  fi

  if docker exec "$FREESWITCH_CONTAINER" fs_cli -x status >/dev/null 2>&1; then
    ok "FreeSWITCH Event Socket responds"
  else
    fail "FreeSWITCH fs_cli status failed"
  fi
fi

section "FreeSWITCH local IP config"
vars_file="$ROOT/freeswitch-local/conf/vars.xml"
if [[ ! -f "$vars_file" ]]; then
  fail "missing $vars_file"
elif [[ -n "$host_ip" ]]; then
  for key in domain external_sip_ip external_rtp_ip; do
    value="$(grep -E "data=\"$key=" "$vars_file" | sed -E "s/.*data=\"$key=([^\"]+)\".*/\\1/" | tail -n 1)"
    if [[ "$value" == "$host_ip" ]]; then
      ok "$key=$value"
    else
      fail "$key=$value, expected $host_ip"
    fi
  done
fi

section "RTP relay"
if pgrep -f "$RELAY_PATTERN" >/dev/null 2>&1; then
  ok "RTP relay is running"
else
  fail "RTP relay is not running"
fi

section "Softphone registration"
registrations="$(docker exec "$FREESWITCH_CONTAINER" fs_cli -x 'show registrations' 2>/dev/null || true)"
if printf '%s\n' "$registrations" | grep -Eq "(^|,)$EXTENSION(,|@|$)"; then
  ok "extension $EXTENSION is registered"
  printf '%s\n' "$registrations"
else
  fail "extension $EXTENSION is not registered"
  printf '%s\n' "$registrations"
fi

linphone_ports="$(lsof -nP -iUDP:5060 -iTCP:5060 2>/dev/null | grep -i linphone || true)"
if [[ -n "$linphone_ports" ]]; then
  printf '%s\n' "$linphone_ports"
  if [[ -n "$host_ip" ]] && printf '%s\n' "$linphone_ports" | grep -q "$host_ip:5060"; then
    ok "Linphone appears to be pointed at local FreeSWITCH"
  else
    warn "Linphone does not appear to be pointed at local FreeSWITCH ($host_ip:5060)"
  fi
else
  warn "no Linphone SIP socket on port 5060 was found"
fi

section "Latest outbound call"
latest_json="$(curl -fsS "$GATEWAY_URL/calls?limit=1" 2>/dev/null || true)"
if [[ -n "$latest_json" ]]; then
  python3 - "$latest_json" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
calls = payload.get("calls") or []
if not calls:
    print("No calls yet.")
else:
    call = calls[0]
    print(f"call_id={call.get('call_id')}")
    print(f"destination={call.get('destination')}")
    print(f"status={call.get('status')}")
    print(f"phase={call.get('phase')}")
    failure = call.get("failure_label") or call.get("error")
    if failure:
        print(f"failure={failure}")
PY
else
  warn "could not read latest outbound call"
fi

section "Result"
if (( failures > 0 )); then
  printf 'NOT READY: %d failed check(s), %d warning(s).\n' "$failures" "$warnings"
  printf 'Fix failed items before pressing the outbound-test call button.\n'
  exit 1
fi

printf 'READY: 0 failed check(s), %d warning(s).\n' "$warnings"
printf 'Open %s/outbound-test and call extension %s.\n' "$GATEWAY_URL" "$EXTENSION"
