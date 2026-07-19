import logging
import time
import urllib.parse
from datetime import datetime, timezone

from clients import ArrClient, QbClient
from state import StateStore


def arr_iso_to_epoch_seconds(value):
    if not value:
        return None

    try:
        normalized = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def epoch_seconds_to_arr_iso(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def extract_arr_records(payload):
    if isinstance(payload, list):
        return payload

    if isinstance(payload, dict):
        records = payload.get("records")
        if isinstance(records, list):
            return records

    return []


def paged_download_ids(arr_client, page_size):
    page = 1
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
        payload = arr_client.fetch_json(f"/queue?{query}")

        records = extract_arr_records(payload)
        for record in records:
            download_id = record.get("downloadId")
            if download_id:
                download_ids.add(download_id.lower())

        total_records = payload.get("totalRecords") if isinstance(payload, dict) else None
        if total_records is not None:
            if page * page_size >= total_records:
                break
        elif len(records) < page_size:
            break

        page += 1

    return download_ids


def history_bootstrap_state(arr_client, page_size, max_pages):
    latest_event_by_id = {}
    max_event_time = 0.0

    for page in range(1, max_pages + 1):
        query = urllib.parse.urlencode(
            {
                "page": page,
                "pageSize": page_size,
                "sortKey": "date",
                "sortDirection": "descending",
            }
        )
        payload = arr_client.fetch_json(f"/history?{query}")

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

            if download_id in latest_event_by_id:
                continue

            latest_event_by_id[download_id] = (record.get("eventType") or "").strip().lower()

        total_records = payload.get("totalRecords") if isinstance(payload, dict) else None
        if total_records is not None and page * page_size >= total_records:
            break

    active_ids = set()
    for download_id, event_type in latest_event_by_id.items():
        if "deleted" not in event_type:
            active_ids.add(download_id)

    return active_ids, max_event_time


def history_since_records(arr_client, since_epoch, overlap_seconds):
    since_with_overlap = max(0.0, since_epoch - overlap_seconds)
    since_value = urllib.parse.quote(epoch_seconds_to_arr_iso(since_with_overlap))
    payload = arr_client.fetch_json(f"/history/since?date={since_value}")
    return extract_arr_records(payload)


def sync_history_state(app_name, arr_client, state_store, page_size, max_pages, overlap_seconds, log):
    active_ids = state_store.get_active_ids(app_name)
    last_event_time = state_store.get_last_event_time(app_name)

    if last_event_time <= 0:
        active_ids, max_event_time = history_bootstrap_state(arr_client, page_size, max_pages)
        state_store.write_state(app_name, active_ids, max_event_time)
        log.info("Bootstrapped %s history: active_ids=%d", app_name, len(active_ids))
        return active_ids

    records = history_since_records(arr_client, last_event_time, overlap_seconds)
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

    state_store.write_state(app_name, active_ids, max_event_time)
    log.info("Updated %s history: new_events=%d active_ids=%d", app_name, len(records), len(active_ids))
    return active_ids


def get_known_download_ids(config, state_store, sonarr_client, radarr_client, log):
    known_ids = set()

    if sonarr_client.is_configured():
        try:
            sonarr_queue_ids = paged_download_ids(sonarr_client, config.history_page_size)
            sonarr_history_ids = sync_history_state(
                "sonarr",
                sonarr_client,
                state_store,
                config.history_page_size,
                config.history_max_pages,
                config.history_since_overlap_seconds,
                log,
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

    if radarr_client.is_configured():
        try:
            radarr_queue_ids = paged_download_ids(radarr_client, config.history_page_size)
            radarr_history_ids = sync_history_state(
                "radarr",
                radarr_client,
                state_store,
                config.history_page_size,
                config.history_max_pages,
                config.history_since_overlap_seconds,
                log,
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


def should_delete_torrent(torrent, properties, known_download_ids, ratio_limit, seeding_time_limit_seconds):
    torrent_hash = (torrent.get("hash") or "").lower()
    torrent_name = torrent.get("name", "<unnamed>")

    if not torrent_hash:
        return False, f"torrent {torrent_name} missing hash"

    if torrent_hash in known_download_ids:
        return False, f"torrent {torrent_name} still tracked by Sonarr/Radarr"

    ratio = properties.get("share_ratio")
    seeding_time = properties.get("seeding_time")

    ratio_over_limit = isinstance(ratio, (int, float)) and ratio >= ratio_limit
    time_over_limit = (
        isinstance(seeding_time, int) and seeding_time >= seeding_time_limit_seconds
    )

    if ratio_over_limit or time_over_limit:
        return True, f"torrent {torrent_name} orphaned and over ratio/time limit"

    return False, f"torrent {torrent_name} orphaned but under ratio/time limits"


def run(config):
    logging.basicConfig(
        level=logging.INFO,
        format="[torrent-tidy] %(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    log = logging.getLogger(__name__)

    log.info("Starting torrent tidy up...")
    log.info(
        "Config: dry_run=%s, delete_files=%s, ratio_limit=%s, seeding_time_limit_hours=%s, category_filter=%s, state_db_path=%s",
        config.dry_run,
        config.delete_files,
        config.ratio_limit,
        config.seeding_time_limit_hours,
        config.category_filter,
        config.state_db_path,
    )

    qb_client = QbClient(config.qb_api, config.qb_username, config.qb_password, log)
    sonarr_client = ArrClient(config.sonarr_api, config.sonarr_api_key)
    radarr_client = ArrClient(config.radarr_api, config.radarr_api_key)
    state_store = StateStore(config.state_db_path)
    state_store.init()

    logged_in = False

    while True:
        sleep_seconds = config.check_interval
        try:
            if not logged_in:
                try:
                    qb_client.login()
                    logged_in = True
                except Exception:
                    log.exception("Failed to login to qBittorrent API, will retry...")
                    sleep_seconds = config.login_retry_delay
                    continue

            known_download_ids = get_known_download_ids(
                config, state_store, sonarr_client, radarr_client, log
            )

            torrent_list = qb_client.with_reauth(qb_client.get_torrent_list)
            if torrent_list is None:
                log.debug("No torrent list available, will retry...")
                continue

            processed = 0
            deleted = 0

            for torrent in torrent_list:
                torrent_hash = (torrent.get("hash") or "").lower()
                if not torrent_hash:
                    continue

                if config.category_filter:
                    category = (torrent.get("category") or "").strip().lower()
                    if category not in config.category_filter:
                        continue

                try:
                    properties = qb_client.with_reauth(
                        qb_client.get_torrent_properties, torrent_hash
                    )
                except Exception as e:
                    log.warning(
                        "Failed to fetch properties for torrent %s: %s", torrent_hash, e
                    )
                    continue

                should_delete, reason = should_delete_torrent(
                    torrent,
                    properties,
                    known_download_ids,
                    config.ratio_limit,
                    config.seeding_time_limit_seconds,
                )
                processed += 1

                if not should_delete:
                    continue

                ratio = properties.get("share_ratio")
                seeding_time = properties.get("seeding_time")
                name = torrent.get("name", "<unnamed>")

                if config.dry_run:
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
                    qb_client.with_reauth(
                        qb_client.delete_torrent, torrent_hash, config.delete_files
                    )
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
