#!/bin/sh
set -eu

APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"
STATE_DB_PATH="${STATE_DB_PATH:-${STATE_FILE:-/tmp/torrent-tidy-state.db}}"
STATE_DIR="$(dirname "$STATE_DB_PATH")"

if [ "$(id -u)" -eq 0 ]; then
  if [ ! -d "$STATE_DIR" ]; then
    mkdir -p "$STATE_DIR" || true
  fi

  if [ -d "$STATE_DIR" ]; then
    chown -R "$APP_UID:$APP_GID" "$STATE_DIR" || true
  fi

  exec gosu "$APP_UID:$APP_GID" python /app/torrent-tidy.py
fi

exec python /app/torrent-tidy.py
