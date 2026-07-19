import os
import sqlite3


class StateStore:
    def __init__(self, db_path):
        self.db_path = db_path

    def init(self):
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_download_sources (
                    app_name TEXT NOT NULL,
                    download_id TEXT NOT NULL,
                    source_title TEXT NOT NULL,
                    PRIMARY KEY (app_name, download_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_download_entities (
                    app_name TEXT NOT NULL,
                    download_id TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    PRIMARY KEY (app_name, download_id, entity_key)
                )
                """
            )

            for app_name in ("sonarr", "radarr"):
                conn.execute(
                    "INSERT OR IGNORE INTO app_state (app_name, last_event_time) VALUES (?, 0)",
                    (app_name,),
                )

    def get_last_event_time(self, app_name):
        with sqlite3.connect(self.db_path) as conn:
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

    def get_active_ids(self, app_name):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT download_id FROM active_download_ids WHERE app_name = ?",
                (app_name,),
            ).fetchall()
        return {row[0] for row in rows}

    def get_active_source_titles(self, app_name):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT download_id, source_title FROM active_download_sources WHERE app_name = ?",
                (app_name,),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_active_entities(self, app_name):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT download_id, entity_key FROM active_download_entities WHERE app_name = ?",
                (app_name,),
            ).fetchall()

        entities = {}
        for download_id, entity_key in rows:
            entities.setdefault(download_id, set()).add(entity_key)
        return entities

    def write_state(self, app_name, active_ids, active_sources, last_event_time, active_entities=None):
        if active_sources is None:
            active_sources = {}
        if active_entities is None:
            active_entities = {}

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (app_name, last_event_time) VALUES (?, ?)",
                (app_name, float(last_event_time or 0)),
            )
            conn.execute("DELETE FROM active_download_ids WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM active_download_sources WHERE app_name = ?", (app_name,))
            conn.execute("DELETE FROM active_download_entities WHERE app_name = ?", (app_name,))
            conn.executemany(
                "INSERT INTO active_download_ids (app_name, download_id) VALUES (?, ?)",
                [(app_name, download_id) for download_id in sorted(active_ids)],
            )
            conn.executemany(
                "INSERT INTO active_download_sources (app_name, download_id, source_title) VALUES (?, ?, ?)",
                [
                    (app_name, download_id, source_title)
                    for download_id, source_title in sorted(active_sources.items())
                    if download_id in active_ids and source_title
                ],
            )
            conn.executemany(
                "INSERT INTO active_download_entities (app_name, download_id, entity_key) VALUES (?, ?, ?)",
                [
                    (app_name, download_id, entity_key)
                    for download_id, entity_keys in sorted(active_entities.items())
                    for entity_key in sorted(entity_keys)
                    if download_id in active_ids and entity_key
                ],
            )
