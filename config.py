import os
from dataclasses import dataclass


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
            import json

            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip().lower() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    return [item.strip().lower() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class AppConfig:
    qb_api: str
    qb_username: str
    qb_password: str
    sonarr_api: str
    sonarr_api_key: str
    radarr_api: str
    radarr_api_key: str
    check_interval: int
    login_retry_delay: int
    history_page_size: int
    history_max_pages: int
    history_since_overlap_seconds: int
    state_db_path: str
    seeding_time_limit_hours: int
    seeding_time_limit_seconds: int
    ratio_limit: float
    category_filter: list[str]
    delete_files: bool
    dry_run: bool


def load_config():
    seeding_time_limit_hours = int(os.environ.get("SEEDING_TIME_LIMIT_HOURS", "720"))

    return AppConfig(
        qb_api=os.environ["QB_API_TT"].rstrip("/"),
        qb_username=os.environ["QB_USERNAME"],
        qb_password=os.environ["QB_PASSWORD"],
        sonarr_api=os.environ.get("SONARR_API", "").rstrip("/"),
        sonarr_api_key=os.environ.get("SONARR_API_KEY", ""),
        radarr_api=os.environ.get("RADARR_API", "").rstrip("/"),
        radarr_api_key=os.environ.get("RADARR_API_KEY", ""),
        check_interval=int(os.environ.get("CHECK_INTERVAL", "43200")),
        login_retry_delay=int(os.environ.get("LOGIN_RETRY_DELAY", "30")),
        history_page_size=int(os.environ.get("HISTORY_PAGE_SIZE", "250")),
        history_max_pages=int(os.environ.get("HISTORY_MAX_PAGES", "40")),
        history_since_overlap_seconds=int(os.environ.get("HISTORY_SINCE_OVERLAP_SECONDS", "120")),
        state_db_path=os.environ.get("STATE_DB_PATH", os.environ.get("STATE_FILE", "/tmp/torrent-tidy-state.db")),
        seeding_time_limit_hours=seeding_time_limit_hours,
        seeding_time_limit_seconds=seeding_time_limit_hours * 3600,
        ratio_limit=float(os.environ.get("RATIO_LIMIT", "2.0")),
        category_filter=env_list("CATEGORY_FILTER", ["radarr", "tv-sonarr"]),
        delete_files=env_bool("DELETE_FILES", False),
        dry_run=env_bool("DRY_RUN", True),
    )
