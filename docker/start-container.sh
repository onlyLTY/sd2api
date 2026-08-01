#!/usr/bin/env bash
set -Eeuo pipefail

: "${NOVNC_PASSWORD:?NOVNC_PASSWORD must be set}"

export DISPLAY="${DISPLAY:-:99}"
screen_width="${SCREEN_WIDTH:-1280}"
screen_height="${SCREEN_HEIGHT:-900}"
screen_depth="${SCREEN_DEPTH:-24}"

mkdir -p /data/profiles /data/db /tmp/sd2api-vnc
rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}"

x11vnc -storepasswd "${NOVNC_PASSWORD}" /tmp/sd2api-vnc/passwd >/dev/null

Xvfb "${DISPLAY}" -screen 0 "${screen_width}x${screen_height}x${screen_depth}" \
  -ac +extension GLX +render -noreset &
xvfb_pid=$!

sleep 1
openbox-session >/tmp/sd2api-openbox.log 2>&1 &
tint2 >/tmp/sd2api-tint2.log 2>&1 &

x11vnc -display "${DISPLAY}" -forever -shared -rfbport 5900 \
  -rfbauth /tmp/sd2api-vnc/passwd -noxdamage -quiet &
vnc_pid=$!

websockify --web=/usr/share/novnc 6080 localhost:5900 &
novnc_pid=$!

uvicorn sd2api.main:app --host 0.0.0.0 --port 8765 &
api_pid=$!

cleanup() {
  kill "${api_pid}" "${novnc_pid}" "${vnc_pid}" "${xvfb_pid}" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait -n "${api_pid}" "${novnc_pid}" "${vnc_pid}" "${xvfb_pid}"
