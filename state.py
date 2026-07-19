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

    def write_state(self, app_name, active_ids, last_event_time):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_state (app_name, last_event_time) VALUES (?, ?)",
                (app_name, float(last_event_time or 0)),
            )
            conn.execute("DELETE FROM active_download_ids WHERE app_name = ?", (app_name,))
            conn.executemany(
                "INSERT INTO active_download_ids (app_name, download_id) VALUES (?, ?)",
                [(app_name, download_id) for download_id in sorted(active_ids)],
            )
