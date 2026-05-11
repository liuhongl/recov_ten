#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/artifacts/logs"
GATEWAY_CMD=(
  "$HOME/.local/bin/uv"
  run
  --python
  /opt/homebrew/bin/python3.12
  python
  -m
  app.main
  --config
  configs/local.example.toml
  --env-file
  .env
  --media-mode
  realtime
)
GATEWAY_PATTERN="python -m app.main --config configs/local.example.toml --env-file .env --media-mode realtime"
RELAY_PATTERN="rtp_host_relay.py .*--target-start 26384|rtp_host_relay.py$"
GATEWAY_SCREEN_SESSION="sip-realtime-gateway"
GATEWAY_LAUNCHD_LABEL="local.sip-realtime-voice-gateway"
GATEWAY_LAUNCHD_PLIST="$LOG_DIR/gateway.launchd.plist"

usage() {
  cat <<'EOF'
usage: scripts/dev-local.sh <start|check|restart|stop>

start    Start FreeSWITCH, RTP relay, and realtime gateway if needed.
check    Print local runtime health and registrations.
restart  Stop the gateway, then run start.
stop     Stop the gateway only. FreeSWITCH remains managed by Docker Compose.
EOF
}

ensure_log_dir() {
  mkdir -p "$LOG_DIR"
}

require_env_file() {
  if [[ ! -f "$ROOT/.env" ]]; then
    echo "missing .env; create it with Doubao and FreeSWITCH local secrets" >&2
    exit 1
  fi
}

start_freeswitch() {
  echo "==> starting FreeSWITCH container"
  docker compose -f "$ROOT/freeswitch-local/docker-compose.yml" up -d --build
  wait_for_freeswitch
}

wait_for_freeswitch() {
  echo "==> waiting for FreeSWITCH readiness"
  local attempt health
  for attempt in {1..60}; do
    health="$(docker inspect --format '{{.State.Health.Status}}' sip_realtime_freeswitch 2>/dev/null || true)"
    if [[ "$health" == "healthy" ]] &&
      docker exec sip_realtime_freeswitch fs_cli -x status >/dev/null 2>&1; then
      sleep 1
      echo "FreeSWITCH is ready"
      return
    fi
    sleep 1
  done

  echo "FreeSWITCH did not become ready in time" >&2
  docker ps --filter name=sip_realtime_freeswitch \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' >&2 || true
  exit 1
}

relay_is_running() {
  pgrep -f "$RELAY_PATTERN" >/dev/null 2>&1
}

start_relay() {
  echo "==> checking RTP relay"
  if relay_is_running; then
    echo "RTP relay already running"
    return
  fi

  if lsof -nP -iUDP:16384 >/dev/null 2>&1; then
    echo "UDP 16384 is already in use, but not by the expected RTP relay" >&2
    lsof -nP -iUDP:16384 >&2 || true
    exit 1
  fi

  ensure_log_dir
  nohup python3 "$ROOT/freeswitch-local/scripts/rtp_host_relay.py" \
    >"$LOG_DIR/rtp_host_relay.log" 2>&1 &
  sleep 0.5
  if ! relay_is_running; then
    echo "failed to start RTP relay; see $LOG_DIR/rtp_host_relay.log" >&2
    exit 1
  fi
  echo "RTP relay started"
}

gateway_is_running() {
  pgrep -f "$GATEWAY_PATTERN" >/dev/null 2>&1
}

write_gateway_launchd_plist() {
  cat >"$GATEWAY_LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$GATEWAY_LAUNCHD_LABEL</string>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HOME/.local/bin/uv</string>
    <string>run</string>
    <string>--python</string>
    <string>/opt/homebrew/bin/python3.12</string>
    <string>python</string>
    <string>-m</string>
    <string>app.main</string>
    <string>--config</string>
    <string>configs/local.example.toml</string>
    <string>--env-file</string>
    <string>.env</string>
    <string>--media-mode</string>
    <string>realtime</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>$HOME</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/gateway.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/gateway.err.log</string>
</dict>
</plist>
EOF
}

start_gateway_process() {
  if command -v screen >/dev/null 2>&1; then
    screen -S "$GATEWAY_SCREEN_SESSION" -X quit >/dev/null 2>&1 || true
    screen -dmS "$GATEWAY_SCREEN_SESSION" /bin/zsh -lc \
      "cd '$ROOT' && exec '$HOME/.local/bin/uv' run --python /opt/homebrew/bin/python3.12 python -m app.main --config configs/local.example.toml --env-file .env --media-mode realtime >> '$LOG_DIR/gateway.log' 2>> '$LOG_DIR/gateway.err.log'"
    return
  fi

  if [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1; then
    stop_gateway_launchd >/dev/null 2>&1 || true
    write_gateway_launchd_plist
    launchctl bootstrap "gui/$(id -u)" "$GATEWAY_LAUNCHD_PLIST"
    launchctl kickstart -k "gui/$(id -u)/$GATEWAY_LAUNCHD_LABEL" >/dev/null 2>&1 || true
    return
  fi

  (
    cd "$ROOT"
    nohup "${GATEWAY_CMD[@]}" >"$LOG_DIR/gateway.log" 2>&1 &
  )
}

stop_gateway_launchd() {
  if [[ "$(uname -s)" != "Darwin" ]] || ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  launchctl bootout "gui/$(id -u)/$GATEWAY_LAUNCHD_LABEL" 2>/dev/null || true
  if [[ -f "$GATEWAY_LAUNCHD_PLIST" ]]; then
    launchctl bootout "gui/$(id -u)" "$GATEWAY_LAUNCHD_PLIST" 2>/dev/null || true
  fi
}

start_gateway() {
  echo "==> checking realtime gateway"
  require_env_file
  if gateway_is_running; then
    echo "gateway already running"
    return
  fi

  ensure_log_dir
  start_gateway_process
  local attempt
  for attempt in {1..30}; do
    if curl -fsS http://127.0.0.1:9100/health >/dev/null 2>&1; then
      echo "gateway started"
      return
    fi
    if ! gateway_is_running; then
      echo "failed to start gateway; see $LOG_DIR/gateway.log" >&2
      exit 1
    fi
    sleep 1
  done

  echo "gateway did not become healthy in time; see $LOG_DIR/gateway.log" >&2
  exit 1
}

stop_gateway() {
  echo "==> stopping realtime gateway"
  if command -v screen >/dev/null 2>&1; then
    screen -S "$GATEWAY_SCREEN_SESSION" -X quit >/dev/null 2>&1 || true
  fi
  stop_gateway_launchd
  local pids
  pids="$(pgrep -f "$GATEWAY_PATTERN" || true)"
  if [[ -z "$pids" ]]; then
    echo "gateway is not running"
    return
  fi
  kill $pids
}

check_runtime() {
  echo "==> gateway health"
  curl -fsS http://127.0.0.1:9100/health || true
  echo

  echo "==> FreeSWITCH container"
  docker ps --filter name=sip_realtime_freeswitch \
    --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

  echo "==> RTP relay"
  if relay_is_running; then
    pgrep -f "$RELAY_PATTERN" | while read -r pid; do
      ps -p "$pid" -o pid=,command=
    done
  else
    echo "not running"
  fi

  echo "==> FreeSWITCH registrations"
  docker exec sip_realtime_freeswitch fs_cli -x "show registrations" || true
}

case "${1:-}" in
  start)
    start_freeswitch
    start_relay
    start_gateway
    check_runtime
    ;;
  check)
    check_runtime
    ;;
  restart)
    stop_gateway
    sleep 1
    start_freeswitch
    start_relay
    start_gateway
    check_runtime
    ;;
  stop)
    stop_gateway
    ;;
  *)
    usage
    exit 2
    ;;
esac
