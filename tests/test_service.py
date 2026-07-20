import tempfile
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from service import should_delete_torrent, sync_history_state
from state import StateStore


class FakeArrClient:
    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def fetch_json(self, path):
        self.requests.append(path)
        for prefix, payload in self.responses:
            if path.startswith(prefix):
                return payload
        raise AssertionError(f"Unexpected path requested: {path}")


class _SilentLog:
    def info(self, *args, **kwargs):
        pass


@pytest.fixture
def torrent():
    return {"hash": "ABC123", "name": "Example Torrent"}


@pytest.fixture
def state_store():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = f"{temp_dir}/state.db"
        store = StateStore(db_path)
        store.init()
        yield store


def test_skips_when_still_tracked(torrent):
    should_delete, reason = should_delete_torrent(
        torrent=torrent,
        properties={"share_ratio": 10.0, "seeding_time": 999999},
        known_download_ids={"abc123"},
        library_names=set(),
        ratio_limit=2.0,
        seeding_time_limit_seconds=720 * 3600,
    )

    assert not should_delete
    assert "still tracked" in reason


def test_skips_when_still_present_in_library(torrent):
    should_delete, reason = should_delete_torrent(
        torrent=torrent,
        properties={"share_ratio": 10.0, "seeding_time": 999999},
        known_download_ids=set(),
        library_names={"example torrent"},
        ratio_limit=2.0,
        seeding_time_limit_seconds=720 * 3600,
    )

    assert not should_delete
    assert "still present in Sonarr/Radarr library" in reason


def test_deletes_when_orphaned_and_ratio_limit_reached(torrent):
    should_delete, reason = should_delete_torrent(
        torrent=torrent,
        properties={"share_ratio": 2.1, "seeding_time": 60},
        known_download_ids=set(),
        library_names=set(),
        ratio_limit=2.0,
        seeding_time_limit_seconds=720 * 3600,
    )

    assert should_delete
    assert "orphaned and over ratio/time limit" in reason


def test_deletes_when_orphaned_and_time_limit_reached(torrent):
    should_delete, reason = should_delete_torrent(
        torrent=torrent,
        properties={"share_ratio": 0.1, "seeding_time": 720 * 3600},
        known_download_ids=set(),
        library_names=set(),
        ratio_limit=2.0,
        seeding_time_limit_seconds=720 * 3600,
    )

    assert should_delete
    assert "orphaned and over ratio/time limit" in reason


def test_keeps_when_orphaned_but_under_limits(torrent):
    should_delete, reason = should_delete_torrent(
        torrent=torrent,
        properties={"share_ratio": 1.0, "seeding_time": 100},
        known_download_ids=set(),
        library_names=set(),
        ratio_limit=2.0,
        seeding_time_limit_seconds=720 * 3600,
    )

    assert not should_delete
    assert "orphaned but under ratio/time limits" in reason


def test_incremental_history_updates_state(state_store):
    # Existing state before this run.
    state_store.write_state(
        "sonarr",
        {"aaa", "bbb"},
        {"aaa": "title-a", "bbb": "title-b"},
        100.0,
    )

    arr_client = FakeArrClient(
        responses=[
            (
                "/history/since",
                [
                    {
                        "downloadId": "bbb",
                        "eventType": "downloadFolderImportedDeleted",
                        "date": "2026-07-19T10:00:10Z",
                    },
                    {
                        "downloadId": "ccc",
                        "eventType": "grabbed",
                        "date": "2026-07-19T10:00:20Z",
                    },
                ],
            )
        ]
    )

    active_ids = sync_history_state(
        app_name="sonarr",
        arr_client=arr_client,
        state_store=state_store,
        page_size=250,
        max_pages=40,
        overlap_seconds=120,
        log=_SilentLog(),
    )

    assert active_ids == {"aaa", "ccc"}
    assert any(path.startswith("/history/since") for path in arr_client.requests)
    assert state_store.get_last_event_time("sonarr") >= 1752919220.0


def test_delete_event_without_download_id_uses_source_title_fallback(state_store):
    state_store.write_state(
        "sonarr",
        {"aaa", "bbb"},
        {"aaa": "show one s01e01", "bbb": "show one s01e02"},
        100.0,
    )

    arr_client = FakeArrClient(
        responses=[
            (
                "/history/since",
                [
                    {
                        "downloadId": "",
                        "eventType": "episodeFileDeleted",
                        "sourceTitle": "Show One S01E01",
                        "date": "2026-07-19T10:00:30Z",
                    }
                ],
            )
        ]
    )

    active_ids = sync_history_state(
        app_name="sonarr",
        arr_client=arr_client,
        state_store=state_store,
        page_size=250,
        max_pages=40,
        overlap_seconds=120,
        log=_SilentLog(),
    )

    assert active_ids == {"bbb"}


def test_delete_event_without_download_id_uses_entity_ids(state_store):
    state_store.write_state(
        "radarr",
        {"aaa", "bbb"},
        {"aaa": "movie one 2024", "bbb": "movie two 2024"},
        100.0,
        {
            "aaa": {"movie:123"},
            "bbb": {"movie:456"},
        },
    )

    arr_client = FakeArrClient(
        responses=[
            (
                "/history/since",
                [
                    {
                        "downloadId": "",
                        "eventType": "movieFileDeleted",
                        "movieId": 123,
                        "date": "2026-07-19T10:00:40Z",
                    }
                ],
            )
        ]
    )

    active_ids = sync_history_state(
        app_name="radarr",
        arr_client=arr_client,
        state_store=state_store,
        page_size=250,
        max_pages=40,
        overlap_seconds=120,
        log=_SilentLog(),
    )

    assert active_ids == {"bbb"}
