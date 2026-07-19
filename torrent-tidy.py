#!/usr/bin/env python3

import os
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
import sqlite3
from datetime import datetime, timezone


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default):
    value = os.environ.get(name)
    if value is None:
        return [item.strip().lower() for item in default if str(item).strip()]

    value = value.strip()
    if not value:
        return [item.strip().lower() for item in default if str(item).strip()]

    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip().lower() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    return [item.strip().lower() for item in value.split(",") if item.strip()]


QB_API = os.environ["QB_API_TT"].rstrip("/")
QB_USERNAME = os.environ["QB_USERNAME"]
QB_PASSWORD = os.environ["QB_PASSWORD"]

SONARR_API = os.environ.get("SONARR_API", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

RADARR_API = os.environ.get("RADARR_API", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "43200"))  # default 12 hours
LOGIN_RETRY_DELAY = int(os.environ.get("LOGIN_RETRY_DELAY", "30"))
HISTORY_PAGE_SIZE = int(os.environ.get("HISTORY_PAGE_SIZE", "250"))
HISTORY_MAX_PAGES = int(os.environ.get("HISTORY_MAX_PAGES", "40"))
HISTORY_SINCE_OVERLAP_SECONDS = int(os.environ.get("HISTORY_SINCE_OVERLAP_SECONDS", "120"))
STATE_DB_PATH = os.environ.get("STATE_DB_PATH", os.environ.get("STATE_FILE", "/tmp/torrent-tidy-state.db"))

SEEDING_TIME_LIMIT_HOURS = int(os.environ.get("SEEDING_TIME_LIMIT_HOURS", "720")) # default 30 days
SEEDING_TIME_LIMIT_SECONDS = int(SEEDING_TIME_LIMIT_HOURS * 3600)
RATIO_LIMIT = float(os.environ.get("RATIO_LIMIT", "2.0"))
CATEGORY_FILTER = env_list("CATEGORY_FILTER", ["radarr", "tv-sonarr"])

DELETE_FILES = env_bool("DELETE_FILES", False)
DRY_RUN = env_bool("DRY_RUN", True)

logging.basicConfig(
    level=logging.INFO,
    format="[torrent-tidy] %(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

cookiejar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar))


def arr_iso_to_epoch_seconds(value):
    """Convert Arr ISO-8601 timestamp to epoch seconds.

    Arr commonly returns UTC timestamps with a trailing "Z". `fromisoformat`
    parses explicit offsets, so we normalize "Z" to "+00:00" first.
    """
    if not value:
        return None

    try:
        normalized = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def epoch_seconds_to_arr_iso(value):
    """Convert epoch seconds to Arr-compatible UTC ISO-8601 timestamp."""
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def init_state_db():
    parent = os.path.dirname(STATE_DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                app_name TEXT PRIMARY KEY,
                last_event_time REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_download_ids (
                app_name TEXT NOT NULL,
                download_id TEXT NOT NULL,
                PRIMARY KEY (app_name, download_id)
            )
            """
        )

        for app_name in ("sonarr", "radarr"):
            conn.execute(
                "INSERT OR IGNORE INTO app_state (app_name, last_event_time) VALUES (?, 0)",
                (app_name,),
            )


def get_app_last_event_time(app_name):
    with sqlite3.connect(STATE_DB_PATH) as conn:
        row = conn.execute(
            "SELECT last_event_time FROM app_state WHERE app_name = ?",
            (app_name,),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO app_state (app_name, last_event_time) VALUES (?, 0)",
                (app_name,),
            )
            return 0.0
        return float(row[0] or 0)


def get_app_active_ids(app_name):
    with sqlite3.connect(STATE_DB_PATH) as conn:
        rows = conn.execute(
            "SELECT download_id FROM active_download_ids WHERE app_name = ?",
            (app_name,),
        ).fetchall()
    return {row[0] for row in rows}


def write_app_state(app_name, active_ids, last_event_time):
    with sqlite3.connect(STATE_DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_state (app_name, last_event_time) VALUES (?, ?)",
            (app_name, float(last_event_time or 0)),
        )
        conn.execute("DELETE FROM active_download_ids WHERE app_name = ?", (app_name,))
        conn.executemany(
            "INSERT INTO active_download_ids (app_name, download_id) VALUES (?, ?)",
            [(app_name, download_id) for download_id in sorted(active_ids)],
        )


def extract_arr_records(payload):
    """Normalize Arr API responses to a list of records.

    Most Arr endpoints return an object with a `records` list, while
    `history/since` may return a plain list depending on version.
    """
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return records

    return []


def fetch_arr_json(base_url, api_key, path):
    """GET and decode JSON from a Sonarr/Radarr endpoint path."""
    req = urllib.request.Request(service_url(base_url, path))
    req.add_header("X-Api-Key", api_key)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def qb_url(path):
    """Build qBittorrent API URL from either root or /api/v2 base."""
    if QB_API.endswith("/api/v2"):
        return f"{QB_API}{path}"
    return f"{QB_API}/api/v2{path}"


def service_url(base_url, path):
    """Build Sonarr/Radarr API URL from either root or /api/v3 base."""
    if base_url.endswith("/api/v3"):
        return f"{base_url}{path}"
    return f"{base_url}/api/v3{path}"


def login_to_qbittorrent():
    """Authenticate with qBittorrent's API and store cookies for subsequent requests."""
    data = urllib.parse.urlencode({"username": QB_USERNAME, "password": QB_PASSWORD}).encode()

    req = urllib.request.Request(qb_url("/auth/login"), data=data, method="POST")

    with opener.open(req, timeout=5) as resp:
        body = resp.read().decode().strip()
        if resp.status != 200 or body != "Ok.":
            raise RuntimeError(f"Failed to login to qBittorrent API status:{resp.status}, body:{body}")

    log.info("Successfully logged in to qBittorrent API")


def with_reauth(func, *args, **kwargs):
    """Wrapper to re-authenticate if qBittorrent session has expired."""
    try:
        return func(*args, **kwargs)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log.info("qBittorrent session expired, logging in again")
            login_to_qbittorrent()
            return func(*args, **kwargs)
        raise


def get_torrent_list():
    """Fetch completed torrents from qBittorrent."""
    req = urllib.request.Request(qb_url("/torrents/info?filter=completed"))
    try:
        with opener.open(req, timeout=5) as resp:
            torrent_list = json.loads(resp.read().decode())
            return torrent_list
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise
    
        log.warning("Failed to connect to qBittorrent API with status %s", e.code)
        try:
            body = e.read().decode(errors="replace")
            log.warning("Response body: %s", body)
        except Exception:
            pass
        return None

    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.warning("Error fetching torrent list from qBittorrent: %s", e)
        return None


def get_torrent_properties(torrent_hash):
    """Fetch seedtime and ratio info for a specific torrent."""
    req = urllib.request.Request(qb_url(f"/torrents/properties?hash={torrent_hash}"))
    with opener.open(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def paged_download_ids(base_url, api_key):
    """Read all queue pages from Sonarr/Radarr and return known download IDs."""
    if not base_url or not api_key:
        return set()

    page = 1
    page_size = HISTORY_PAGE_SIZE
    download_ids = set()

    while True:
        query = urllib.parse.urlencode(
            {
                "page": page,
                "pageSize": page_size,
                "includeUnknownSeriesItems": "true",
                "includeUnknownMovieItems": "true",
            }
        )
        payload = fetch_arr_json(base_url, api_key, f"/queue?{query}")

        records = extract_arr_records(payload)
        for record in records:
            download_id = record.get("downloadId")
            if download_id:
                download_ids.add(download_id.lower())

        total_records = payload.get("totalRecords")
        if total_records is not None:
            if page * page_size >= total_records:
                break
        elif len(records) < page_size:
            break

        page += 1

    return download_ids


def history_bootstrap_state(base_url, api_key):
    """Build active download ID state from paged history (one-time bootstrap)."""
    if not base_url or not api_key:
        return set(), 0.0

    latest_event_by_id = {}
    max_event_time = 0.0

    for page in range(1, HISTORY_MAX_PAGES + 1):
        query = urllib.parse.urlencode(
            {
                "page": page,
                "pageSize": HISTORY_PAGE_SIZE,
                "sortKey": "date",
                "sortDirection": "descending",
            }
        )
        payload = fetch_arr_json(base_url, api_key, f"/history?{query}")

        records = extract_arr_records(payload)
        if not records:
            break

        for record in records:
            download_id = (record.get("downloadId") or "").strip().lower()
            event_time = arr_iso_to_epoch_seconds(record.get("date"))
            if event_time and event_time > max_event_time:
                max_event_time = event_time

            if not download_id:
                continue

            # History is requested in descending order by date, so first hit is latest.
            if download_id in latest_event_by_id:
                continue

            latest_event_by_id[download_id] = (record.get("eventType") or "").strip().lower()

        total_records = payload.get("totalRecords")
        if total_records is not None and page * HISTORY_PAGE_SIZE >= total_records:
            break

    active_ids = set()
    for download_id, event_type in latest_event_by_id.items():
        if "deleted" not in event_type:
            active_ids.add(download_id)

    return active_ids, max_event_time


def history_since_records(base_url, api_key, since_epoch):
    """Fetch incremental history events after a timestamp."""
    since_with_overlap = max(0.0, since_epoch - HISTORY_SINCE_OVERLAP_SECONDS)
    since_value = urllib.parse.quote(epoch_seconds_to_arr_iso(since_with_overlap))
    payload = fetch_arr_json(base_url, api_key, f"/history/since?date={since_value}")
    return extract_arr_records(payload)


def sync_history_state(app_name, base_url, api_key):
    """Update app history state incrementally and return current active IDs."""
    active_ids = get_app_active_ids(app_name)
    last_event_time = get_app_last_event_time(app_name)

    if last_event_time <= 0:
        active_ids, max_event_time = history_bootstrap_state(base_url, api_key)
        write_app_state(app_name, active_ids, max_event_time)
        log.info("Bootstrapped %s history: active_ids=%d", app_name, len(active_ids))
        return active_ids

    records = history_since_records(base_url, api_key, last_event_time)
    max_event_time = last_event_time

    for record in records:
        download_id = (record.get("downloadId") or "").strip().lower()
        if not download_id:
            continue

        event_type = (record.get("eventType") or "").strip().lower()
        if "deleted" in event_type:
            active_ids.discard(download_id)
        else:
            active_ids.add(download_id)

        event_time = arr_iso_to_epoch_seconds(record.get("date"))
        if event_time and event_time > max_event_time:
            max_event_time = event_time

    write_app_state(app_name, active_ids, max_event_time)
    log.info("Updated %s history: new_events=%d active_ids=%d", app_name, len(records), len(active_ids))
    return active_ids


def get_known_download_ids():
    """Combine tracked download IDs from Sonarr and Radarr queues and history."""
    known_ids = set()

    if SONARR_API and SONARR_API_KEY:
        try:
            sonarr_queue_ids = paged_download_ids(SONARR_API, SONARR_API_KEY)
            sonarr_history_ids = sync_history_state(
                "sonarr",
                SONARR_API,
                SONARR_API_KEY,
            )
            sonarr_ids = sonarr_queue_ids.union(sonarr_history_ids)
            known_ids.update(sonarr_ids)
            log.info(
                "Fetched Sonarr IDs: queue=%d history_active=%d total=%d",
                len(sonarr_queue_ids),
                len(sonarr_history_ids),
                len(sonarr_ids),
            )
        except Exception as e:
            log.warning("Failed to fetch Sonarr IDs: %s", e)
    else:
        log.info("Sonarr not configured; skipping Sonarr check")

    if RADARR_API and RADARR_API_KEY:
        try:
            radarr_queue_ids = paged_download_ids(RADARR_API, RADARR_API_KEY)
            radarr_history_ids = sync_history_state(
                "radarr",
                RADARR_API,
                RADARR_API_KEY,
            )
            radarr_ids = radarr_queue_ids.union(radarr_history_ids)
            known_ids.update(radarr_ids)
            log.info(
                "Fetched Radarr IDs: queue=%d history_active=%d total=%d",
                len(radarr_queue_ids),
                len(radarr_history_ids),
                len(radarr_ids),
            )
        except Exception as e:
            log.warning("Failed to fetch Radarr IDs: %s", e)
    else:
        log.info("Radarr not configured; skipping Radarr check")

    return known_ids


def should_delete_torrent(torrent, properties, known_download_ids):
    """Return True when torrent is orphaned and exceeds configured ratio/time limits."""
    torrent_hash = (torrent.get("hash") or "").lower()
    torrent_name = torrent.get("name", "<unnamed>")
    
    if not torrent_hash:
        return False, f"torrent {torrent_name} missing hash"

    if torrent_hash in known_download_ids:
        return False, f"torrent {torrent_name} still tracked by Sonarr/Radarr"

    ratio = properties.get("share_ratio")
    seeding_time = properties.get("seeding_time")

    ratio_over_limit = isinstance(ratio, (int, float)) and ratio >= RATIO_LIMIT
    time_over_limit = isinstance(seeding_time, int) and seeding_time >= SEEDING_TIME_LIMIT_SECONDS

    if ratio_over_limit or time_over_limit:
        return True, f"torrent {torrent_name} orphaned and over ratio/time limit"

    return False, f"torrent {torrent_name} orphaned but under ratio/time limits"  


def delete_torrent(torrent_hash):
    """Delete torrent and data by hash using qBittorrent API."""
    data = urllib.parse.urlencode(
        {
            "hashes": torrent_hash,
        }
    ).encode()

    delete_path = "/torrents/deletePerm" if DELETE_FILES else "/torrents/delete"

    req = urllib.request.Request(qb_url(delete_path), data=data, method="POST")
    with opener.open(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Failed deleting torrent {torrent_hash}: status {resp.status}")


def main():
    log.info("Starting torrent tidy up...")
    log.info(
        "Config: dry_run=%s, delete_files=%s, ratio_limit=%s, seeding_time_limit_hours=%s, category_filter=%s, state_db_path=%s",
        DRY_RUN,
        DELETE_FILES,
        RATIO_LIMIT,
        SEEDING_TIME_LIMIT_HOURS,
        CATEGORY_FILTER,
        STATE_DB_PATH,
    )
    logged_in = False
    init_state_db()

    while True:
        sleep_seconds = CHECK_INTERVAL
        try:
            if not logged_in:
                try:
                    login_to_qbittorrent()
                    logged_in = True
                except Exception:
                    log.exception("Failed to login to qBittorrent API, will retry...")
                    sleep_seconds = LOGIN_RETRY_DELAY
                    continue

            known_download_ids = get_known_download_ids()

            torrent_list = with_reauth(get_torrent_list)
            if torrent_list is None:
                log.debug("No torrent list available, will retry...")
                continue

            processed = 0
            deleted = 0

            for torrent in torrent_list:
                torrent_hash = (torrent.get("hash") or "").lower()
                if not torrent_hash:
                    continue

                if CATEGORY_FILTER:
                    category = (torrent.get("category") or "").strip().lower()
                    if category not in CATEGORY_FILTER:
                        continue

                try:
                    properties = with_reauth(get_torrent_properties, torrent_hash)
                except Exception as e:
                    log.warning("Failed to fetch properties for torrent %s: %s", torrent_hash, e)
                    continue

                should_delete, reason = should_delete_torrent(torrent, properties, known_download_ids)
                processed += 1

                if not should_delete:
                    continue

                ratio = properties.get("share_ratio")
                seeding_time = properties.get("seeding_time")
                name = torrent.get("name", "<unnamed>")

                if DRY_RUN:
                    log.info(
                        "[DRY RUN] Would delete %s (%s): ratio=%s seeding_time_s=%s reason=%s",
                        name,
                        torrent_hash,
                        ratio,
                        seeding_time,
                        reason,
                    )
                    deleted += 1
                    continue

                try:
                    with_reauth(delete_torrent, torrent_hash)
                    log.info(
                        "Deleted %s (%s): ratio=%s seeding_time_s=%s reason=%s",
                        name,
                        torrent_hash,
                        ratio,
                        seeding_time,
                        reason,
                    )
                    deleted += 1
                except Exception as e:
                    log.warning("Failed to delete %s (%s): %s", name, torrent_hash, e)

            log.info(
                "Cycle complete: processed=%d completed torrents, matched_ids=%d, candidates=%d",
                processed,
                len(known_download_ids),
                deleted,
            )
            
        except Exception:
            logged_in = False
            log.exception("Unexpected error, will retry...")

        finally:
            time.sleep(sleep_seconds)

if __name__ == "__main__":
    main()
