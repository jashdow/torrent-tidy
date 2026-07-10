# Torrent Tidy

A small service that checks completed qBittorrent torrents and deletes only those that:

1. Are no longer tracked by Sonarr or Radarr.
2. Have reached a specified seeding time limit or ratio limit.

### Quick Start

1. Copy `.env.example` to `.env` and update credentials/API keys.
2. Pull and run from GHCR:

```bash
docker compose up -d
```

3. Watch logs:

```bash
docker compose logs -f torrent-tidy
```

### Compose Integration In A Larger Stack

If qBittorrent/Sonarr/Radarr are in your existing compose stack, add this service and ensure it shares the same network:

```yaml
services:
  torrent-tidy:
    image: ghcr.io/jashdow/torrent-tidy:latest
    pull_policy: always
    container_name: torrent-tidy
    restart: unless-stopped
    env_file:
      - ./torrent-tidy/.env
```

### Environment File Strategy

You do not have to use a dedicated env file if your existing compose `.env` already contains all required values.

Use whichever is cleaner for your stack:

- Shared stack `.env` (simple, common):
  - Keep values in the existing `.env` next to your compose file.
  - Keep `env_file: - .env` on this service.

- Dedicated service env file (better isolation):
  - Put only torrent-tidy values in something like `/opt/stacks/media/torrent-tidy.env`.
  - Point this service to that file using `env_file`.

Good practice guidance:

- Shared `.env` is fine for homelab and small stacks.
- Dedicated env file is better when multiple services/people share the stack or when you want tighter secrets scope.
- In both cases, protect file permissions because API keys are stored there.

### Important Environment Variables

- `QB_API_TT`, `QB_USERNAME`, `QB_PASSWORD` qbittorrent creds and api location
- `SONARR_API`, `SONARR_API_KEY` same for sonarr
- `RADARR_API`, `RADARR_API_KEY` same for radarr
- `CHECK_INTERVAL` in seconds (default `43200`, i.e. twice daily)
- `SEEDING_TIME_LIMIT_HOURS` (default `720`, i.e. 30 days)
- `RATIO_LIMIT` (default `2.0`)
- `CATEGORY_FILTER` (default `["radarr","tv-sonarr"]`, accepts JSON array or CSV), only torrents of this category will be deleted
- `DELETE_FILES` (`true` deletes payload data too)
- `DRY_RUN` (`true` logs actions without deleting)

### Recommended First Run

Run with `DRY_RUN=true` first, review logs, then set `DRY_RUN=false` once behavior matches what you expect.

If qBittorrent runs with `network_mode: service:gluetun`, set `QB_API_TT` to `http://gluetun:8080` from the torrent-tidy container.
