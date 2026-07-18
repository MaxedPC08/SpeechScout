import json
import sqlite3
from pathlib import Path
from typing import Any

from scouting_models import ScoutingEvent, ScoutingSession, TranscriptChunk


SCHEMA_VERSION = 1


class ScoutingStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.migrate()

    def migrate(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                scout_name TEXT NOT NULL,
                event_code TEXT NOT NULL,
                team_number INTEGER NOT NULL,
                match_number INTEGER NOT NULL,
                alliance TEXT,
                driver_station TEXT,
                game_config_id TEXT NOT NULL,
                started_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transcript_chunks (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                said_at REAL NOT NULL,
                is_final INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );

            CREATE TABLE IF NOT EXISTS scouting_events (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                said_at REAL NOT NULL,
                value TEXT,
                game_piece TEXT,
                points INTEGER,
                source_text TEXT NOT NULL,
                confidence REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            """
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
        self.connection.commit()

    def save_session(self, session: ScoutingSession):
        self.connection.execute(
            """
            INSERT OR REPLACE INTO sessions (
                id,
                scout_name,
                event_code,
                team_number,
                match_number,
                alliance,
                driver_station,
                game_config_id,
                started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.scout_name,
                session.event_code,
                session.robot.team_number,
                session.robot.match_number,
                session.robot.alliance,
                session.robot.driver_station,
                session.game_config_id,
                session.started_at,
            ),
        )
        self.connection.commit()

    def save_chunk(self, chunk: TranscriptChunk):
        self.connection.execute(
            """
            INSERT OR REPLACE INTO transcript_chunks (
                id,
                session_id,
                text,
                said_at,
                is_final
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                chunk.id,
                chunk.session_id,
                chunk.text,
                chunk.said_at,
                int(chunk.is_final),
            ),
        )
        self.connection.commit()

    def save_events(self, events: list[ScoutingEvent]):
        self.connection.executemany(
            """
            INSERT OR REPLACE INTO scouting_events (
                id,
                session_id,
                event_type,
                said_at,
                value,
                game_piece,
                points,
                source_text,
                confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    event.id,
                    event.session_id,
                    event.event_type,
                    event.said_at,
                    None if event.value is None else str(event.value),
                    event.game_piece,
                    event.points,
                    event.source_text,
                    event.confidence,
                )
                for event in events
            ],
        )
        self.connection.commit()

    def export_bundle(self, export_path: str | Path):
        data = {
            "schema_version": SCHEMA_VERSION,
            "sessions": self._rows("SELECT * FROM sessions ORDER BY started_at, id"),
            "transcript_chunks": self._rows(
                "SELECT * FROM transcript_chunks ORDER BY said_at, id"
            ),
            "scouting_events": self._rows(
                "SELECT * FROM scouting_events ORDER BY said_at, id"
            ),
        }

        export_path = Path(export_path)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        with export_path.open("w", encoding="utf-8") as bundle_file:
            json.dump(data, bundle_file, indent=2)

    def import_bundle(self, bundle_path: str | Path):
        with Path(bundle_path).open("r", encoding="utf-8") as bundle_file:
            data = json.load(bundle_file)

        for session in data.get("sessions", ()):
            self.connection.execute(
                """
                INSERT OR REPLACE INTO sessions (
                    id,
                    scout_name,
                    event_code,
                    team_number,
                    match_number,
                    alliance,
                    driver_station,
                    game_config_id,
                    started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_values(
                    session,
                    "id",
                    "scout_name",
                    "event_code",
                    "team_number",
                    "match_number",
                    "alliance",
                    "driver_station",
                    "game_config_id",
                    "started_at",
                ),
            )

        for chunk in data.get("transcript_chunks", ()):
            self.connection.execute(
                """
                INSERT OR REPLACE INTO transcript_chunks (
                    id,
                    session_id,
                    text,
                    said_at,
                    is_final
                ) VALUES (?, ?, ?, ?, ?)
                """,
                row_values(chunk, "id", "session_id", "text", "said_at", "is_final"),
            )

        for event in data.get("scouting_events", ()):
            self.connection.execute(
                """
                INSERT OR REPLACE INTO scouting_events (
                    id,
                    session_id,
                    event_type,
                    said_at,
                    value,
                    game_piece,
                    points,
                    source_text,
                    confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row_values(
                    event,
                    "id",
                    "session_id",
                    "event_type",
                    "said_at",
                    "value",
                    "game_piece",
                    "points",
                    "source_text",
                    "confidence",
                ),
            )

        self.connection.commit()

    def close(self):
        self.connection.close()

    def _rows(self, query: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(query).fetchall()]


def row_values(row: dict[str, Any], *keys: str) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in keys)
