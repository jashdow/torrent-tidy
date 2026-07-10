#!/usr/bin/env python3

import os
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar


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


QB_API = os.environ["QB_API"].rstrip("/")
QB_USERNAME = os.environ["QB_USERNAME"]
QB_PASSWORD = os.environ["QB_PASSWORD"]

SONARR_API = os.environ.get("SONARR_API", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

RADARR_API = os.environ.get("RADARR_API", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "43200"))  # default 12 hours
LOGIN_RETRY_DELAY = int(os.environ.get("LOGIN_RETRY_DELAY", "30"))

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
    page_size = 250
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
        url = service_url(base_url, f"/queue?{query}")
        req = urllib.request.Request(url)
        req.add_header("X-Api-Key", api_key)

        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())

        records = payload.get("records", [])
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


def get_known_download_ids():
    """Combine tracked download IDs from Sonarr and Radarr queues."""
    known_ids = set()

    if SONARR_API and SONARR_API_KEY:
        try:
            sonarr_ids = paged_download_ids(SONARR_API, SONARR_API_KEY)
            known_ids.update(sonarr_ids)
            log.info("Fetched %d tracked download IDs from Sonarr queue", len(sonarr_ids))
        except Exception as e:
            log.warning("Failed to fetch Sonarr queue IDs: %s", e)
    else:
        log.info("Sonarr not configured; skipping Sonarr check")

    if RADARR_API and RADARR_API_KEY:
        try:
            radarr_ids = paged_download_ids(RADARR_API, RADARR_API_KEY)
            known_ids.update(radarr_ids)
            log.info("Fetched %d tracked download IDs from Radarr queue", len(radarr_ids))
        except Exception as e:
            log.warning("Failed to fetch Radarr queue IDs: %s", e)
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
        "Config: dry_run=%s, delete_files=%s, ratio_limit=%s, seeding_time_limit_hours=%s, category_filter=%s",
        DRY_RUN,
        DELETE_FILES,
        RATIO_LIMIT,
        SEEDING_TIME_LIMIT_HOURS,
        CATEGORY_FILTER,
    )
    logged_in = False

    while True:
        try:
            if not logged_in:
                try:
                    login_to_qbittorrent()
                    logged_in = True
                except Exception:
                    log.exception("Failed to login to qBittorrent API, will retry...")
                    time.sleep(LOGIN_RETRY_DELAY)
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
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
