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
    if ! chown -R "$APP_UID:$APP_GID" "$STATE_DIR" 2>/dev/null; then
      echo "[torrent-tidy] WARN could not chown $STATE_DIR; continuing with current ownership" >&2
    fi
  fi

  if gosu "$APP_UID:$APP_GID" true >/dev/null 2>&1; then
    exec gosu "$APP_UID:$APP_GID" python /app/torrent-tidy.py
  fi

  echo "[torrent-tidy] WARN could not drop privileges to $APP_UID:$APP_GID; running as root" >&2
  exec python /app/torrent-tidy.py
fi

exec python /app/torrent-tidy.py
