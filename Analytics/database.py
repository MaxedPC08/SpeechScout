"""Create and populate SpeechScout's local analytics SQLite database.

Run from the repository root:

    python Analytics/database.py

The importer is idempotent: source-file hashes prevent a scouting JSON file
from being recorded twice.  Raw JSON is retained in ``import_files`` so an
import can always be audited or re-exported later.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid4, uuid5


SCHEMA_VERSION = 11
PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
ANALYTICS_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = ANALYTICS_DIRECTORY / "speechscout.sqlite3"
DEFAULT_GAME_CONFIG_PATH = ANALYTICS_DIRECTORY / "game.json"
DEFAULT_SCHEDULE_PATH = PROJECT_DIRECTORY / "data" / "match_schedule.json"
DEFAULT_MATCHES_DIRECTORY = PROJECT_DIRECTORY / "matches"
TBA_API_BASE_URL = "https://www.thebluealliance.com/api/v3"
STATBOTICS_API_BASE_URL = "https://api.statbotics.io/v3"

LEGACY_FILE_PATTERN = re.compile(r"_(?P<team>\d+)_(?P<match>\d+)\.json$")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
    event_key TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    game_config_json TEXT NOT NULL,
    game_config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    team_number INTEGER PRIMARY KEY CHECK(team_number > 0),
    display_name TEXT
);

CREATE TABLE IF NOT EXISTS scouts (
    scout_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
    match_key TEXT PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES events(event_key),
    match_number INTEGER NOT NULL CHECK(match_number > 0),
    match_type TEXT NOT NULL DEFAULT 'qm',
    red_score INTEGER,
    blue_score INTEGER,
    red_auto_points INTEGER,
    red_teleop_points INTEGER,
    red_endgame_points INTEGER,
    red_penalty_points INTEGER,
    blue_auto_points INTEGER,
    blue_teleop_points INTEGER,
    blue_endgame_points INTEGER,
    blue_penalty_points INTEGER,
    tba_score_breakdown_json TEXT,
    winner_alliance TEXT CHECK(winner_alliance IN ('red', 'blue', 'tie')),
    result_status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK(result_status IN ('scheduled', 'final', 'unknown')),
    UNIQUE(event_key, match_type, match_number)
);

CREATE TABLE IF NOT EXISTS match_teams (
    match_key TEXT NOT NULL REFERENCES matches(match_key) ON DELETE CASCADE,
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    alliance TEXT NOT NULL CHECK(alliance IN ('red', 'blue')),
    station INTEGER NOT NULL CHECK(station BETWEEN 1 AND 3),
    PRIMARY KEY (match_key, team_number),
    UNIQUE(match_key, alliance, station)
);

CREATE TABLE IF NOT EXISTS import_batches (
    import_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS import_files (
    file_hash TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES import_batches(import_id),
    source_path TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('imported', 'review_required', 'invalid')),
    issue_message TEXT
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    match_key TEXT NOT NULL REFERENCES matches(match_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    scout_id TEXT NOT NULL REFERENCES scouts(scout_id),
    predicted_winner TEXT CHECK(predicted_winner IN ('red', 'blue')),
    robot_broken INTEGER NOT NULL DEFAULT 0 CHECK(robot_broken IN (0, 1)),
    reported_total_points INTEGER,
    source_schema_version INTEGER NOT NULL,
    schedule_alignment TEXT NOT NULL
        CHECK(schedule_alignment IN ('matched', 'team_not_scheduled')),
    source_file_hash TEXT NOT NULL UNIQUE REFERENCES import_files(file_hash),
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS score_events (
    score_event_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observations(observation_id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK(sequence_number >= 0),
    event_type TEXT NOT NULL,
    label TEXT NOT NULL,
    points INTEGER NOT NULL,
    alternate_points INTEGER,
    timestamp_ms INTEGER NOT NULL CHECK(timestamp_ms >= 0),
    transcript TEXT,
    UNIQUE(observation_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS notes (
    note_id TEXT PRIMARY KEY,
    observation_id TEXT NOT NULL REFERENCES observations(observation_id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK(sequence_number >= 0),
    timestamp_ms INTEGER NOT NULL CHECK(timestamp_ms >= 0),
    text TEXT NOT NULL,
    UNIQUE(observation_id, sequence_number)
);

-- These remain empty until the summaries and semantic-search phases.
CREATE TABLE IF NOT EXISTS match_team_summaries (
    match_key TEXT NOT NULL REFERENCES matches(match_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    summary TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (match_key, team_number, source_hash)
);

CREATE TABLE IF NOT EXISTS team_summaries (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    summary TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number, source_hash)
);

CREATE TABLE IF NOT EXISTS embedding_chunks (
    chunk_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    match_key TEXT REFERENCES matches(match_key),
    chunk_type TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB NOT NULL,
    source_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Statbotics is an optional online enrichment. Values are cached locally so
-- EPA stays available after the dashboard goes offline.
CREATE TABLE IF NOT EXISTS statbotics_team_epa (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    total_epa REAL NOT NULL,
    auto_epa REAL,
    teleop_epa REAL,
    endgame_epa REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number)
);

-- Official current-event standings, refreshed with TBA qualification results.
CREATE TABLE IF NOT EXISTS event_rankings (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    official_rank INTEGER,
    ranking_points REAL,
    ranking_points_label TEXT,
    matches_played INTEGER,
    wins INTEGER,
    losses INTEGER,
    ties INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number)
);

CREATE TABLE IF NOT EXISTS pick_lists (
    list_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES events(event_key),
    scout_id TEXT NOT NULL REFERENCES scouts(scout_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_key, scout_id, name)
);

CREATE TABLE IF NOT EXISTS pick_list_teams (
    list_id TEXT NOT NULL REFERENCES pick_lists(list_id) ON DELETE CASCADE,
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    added_at TEXT NOT NULL,
    PRIMARY KEY (list_id, team_number)
);

-- Event-level alliance selection tracking. A team can only be selected once,
-- but an alliance captain can come from anywhere in the standings.
CREATE TABLE IF NOT EXISTS alliance_selections (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    alliance_number INTEGER NOT NULL CHECK(alliance_number BETWEEN 1 AND 8),
    selection_kind TEXT NOT NULL CHECK(selection_kind IN ('captain', 'pick')),
    selected_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number)
);

CREATE INDEX IF NOT EXISTS idx_matches_event_number
    ON matches(event_key, match_type, match_number);
CREATE INDEX IF NOT EXISTS idx_match_teams_team
    ON match_teams(team_number, match_key);
CREATE INDEX IF NOT EXISTS idx_observations_team_match
    ON observations(team_number, match_key);
CREATE INDEX IF NOT EXISTS idx_observations_scout
    ON observations(scout_id);
CREATE INDEX IF NOT EXISTS idx_score_events_observation_time
    ON score_events(observation_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_notes_observation_time
    ON notes(observation_id, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_statbotics_team_epa_event_total
    ON statbotics_team_epa(event_key, total_epa DESC);
CREATE INDEX IF NOT EXISTS idx_event_rankings_event_rank
    ON event_rankings(event_key, official_rank);
CREATE INDEX IF NOT EXISTS idx_pick_lists_event_scout
    ON pick_lists(event_key, scout_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_alliance_selections_event_alliance
    ON alliance_selections(event_key, alliance_number, selected_at);

CREATE VIEW IF NOT EXISTS v_team_match_totals AS
SELECT
    o.match_key,
    o.team_number,
    o.observation_id,
    o.schedule_alignment,
    COUNT(se.score_event_id) AS scored_event_count,
    COALESCE(SUM(CASE WHEN se.event_type = 'score' THEN se.points ELSE 0 END), 0)
        AS scouted_points,
    COALESCE(SUM(CASE WHEN se.event_type = 'penalty_committed' THEN 1 ELSE 0 END), 0)
        AS penalties_committed,
    COALESCE(SUM(CASE WHEN se.event_type = 'breakdown' THEN 1 ELSE 0 END), 0)
        AS breakdown_count
FROM observations AS o
LEFT JOIN score_events AS se ON se.observation_id = o.observation_id
GROUP BY o.observation_id;

CREATE VIEW IF NOT EXISTS v_scout_quality AS
WITH note_stats AS (
    SELECT
        observation_id,
        COUNT(*) AS note_count,
        AVG(LENGTH(text)) AS average_note_length
    FROM notes
    GROUP BY observation_id
)
SELECT
    s.scout_id,
    s.display_name,
    COUNT(o.observation_id) AS matches_scouted,
    AVG(COALESCE(note_stats.note_count, 0)) AS average_notes_per_match,
    AVG(note_stats.average_note_length) AS average_note_length,
    SUM(CASE WHEN m.winner_alliance IN ('red', 'blue') THEN 1 ELSE 0 END)
        AS predictions_with_result,
    SUM(
        CASE
            WHEN o.predicted_winner = m.winner_alliance
                AND m.winner_alliance IN ('red', 'blue') THEN 1
            ELSE 0
        END
    ) AS correct_predictions,
    CAST(
        SUM(
            CASE
                WHEN o.predicted_winner = m.winner_alliance
                    AND m.winner_alliance IN ('red', 'blue') THEN 1
                ELSE 0
            END
        ) AS REAL
    ) / NULLIF(SUM(CASE WHEN m.winner_alliance IN ('red', 'blue') THEN 1 ELSE 0 END), 0)
        AS prediction_accuracy
FROM scouts AS s
LEFT JOIN observations AS o ON o.scout_id = s.scout_id
LEFT JOIN matches AS m ON m.match_key = o.match_key
LEFT JOIN note_stats ON note_stats.observation_id = o.observation_id
GROUP BY s.scout_id;
"""

MIGRATION_2_SQL = """
-- Older imports stored a generic foul as penalty_unknown.  They all represent
-- committed penalties now that drawn penalties are not tracked.
UPDATE score_events
SET event_type = 'penalty_committed'
WHERE event_type IN ('penalty_unknown', 'penalty_drawn');

DROP VIEW IF EXISTS v_team_match_totals;
CREATE VIEW v_team_match_totals AS
SELECT
    o.match_key,
    o.team_number,
    o.observation_id,
    o.schedule_alignment,
    COUNT(se.score_event_id) AS scored_event_count,
    COALESCE(SUM(CASE WHEN se.event_type = 'score' THEN se.points ELSE 0 END), 0)
        AS scouted_points,
    COALESCE(SUM(CASE WHEN se.event_type = 'penalty_committed' THEN 1 ELSE 0 END), 0)
        AS penalties_committed,
    COALESCE(SUM(CASE WHEN se.event_type = 'breakdown' THEN 1 ELSE 0 END), 0)
        AS breakdown_count
FROM observations AS o
LEFT JOIN score_events AS se ON se.observation_id = o.observation_id
GROUP BY o.observation_id;
"""

MIGRATION_3_SQL = """
DROP VIEW IF EXISTS v_scout_quality;
CREATE VIEW v_scout_quality AS
WITH note_stats AS (
    SELECT
        observation_id,
        COUNT(*) AS note_count,
        AVG(LENGTH(text)) AS average_note_length
    FROM notes
    GROUP BY observation_id
)
SELECT
    s.scout_id,
    s.display_name,
    COUNT(o.observation_id) AS matches_scouted,
    AVG(COALESCE(note_stats.note_count, 0)) AS average_notes_per_match,
    AVG(note_stats.average_note_length) AS average_note_length,
    SUM(CASE WHEN m.winner_alliance IN ('red', 'blue') THEN 1 ELSE 0 END)
        AS predictions_with_result,
    SUM(
        CASE
            WHEN o.predicted_winner = m.winner_alliance
                AND m.winner_alliance IN ('red', 'blue') THEN 1
            ELSE 0
        END
    ) AS correct_predictions,
    CAST(
        SUM(
            CASE
                WHEN o.predicted_winner = m.winner_alliance
                    AND m.winner_alliance IN ('red', 'blue') THEN 1
                ELSE 0
            END
        ) AS REAL
    ) / NULLIF(SUM(CASE WHEN m.winner_alliance IN ('red', 'blue') THEN 1 ELSE 0 END), 0)
        AS prediction_accuracy
FROM scouts AS s
LEFT JOIN observations AS o ON o.scout_id = s.scout_id
LEFT JOIN matches AS m ON m.match_key = o.match_key
LEFT JOIN note_stats ON note_stats.observation_id = o.observation_id
GROUP BY s.scout_id;
"""

MIGRATION_4_SQL = """
ALTER TABLE matches ADD COLUMN red_auto_points INTEGER;
ALTER TABLE matches ADD COLUMN red_teleop_points INTEGER;
ALTER TABLE matches ADD COLUMN red_endgame_points INTEGER;
ALTER TABLE matches ADD COLUMN blue_auto_points INTEGER;
ALTER TABLE matches ADD COLUMN blue_teleop_points INTEGER;
ALTER TABLE matches ADD COLUMN blue_endgame_points INTEGER;
ALTER TABLE matches ADD COLUMN tba_score_breakdown_json TEXT;
"""

MIGRATION_5_SQL = """
ALTER TABLE matches ADD COLUMN red_penalty_points INTEGER;
ALTER TABLE matches ADD COLUMN blue_penalty_points INTEGER;
"""

MIGRATION_6_SQL = """
CREATE TABLE IF NOT EXISTS statbotics_team_epa (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    total_epa REAL NOT NULL,
    auto_epa REAL,
    teleop_epa REAL,
    endgame_epa REAL,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number)
);
CREATE INDEX IF NOT EXISTS idx_statbotics_team_epa_event_total
    ON statbotics_team_epa(event_key, total_epa DESC);
"""

MIGRATION_7_SQL = """
CREATE TABLE IF NOT EXISTS pick_lists (
    list_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL REFERENCES events(event_key),
    scout_id TEXT NOT NULL REFERENCES scouts(scout_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_key, scout_id, name)
);
CREATE TABLE IF NOT EXISTS pick_list_teams (
    list_id TEXT NOT NULL REFERENCES pick_lists(list_id) ON DELETE CASCADE,
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    added_at TEXT NOT NULL,
    PRIMARY KEY (list_id, team_number)
);
CREATE INDEX IF NOT EXISTS idx_pick_lists_event_scout
    ON pick_lists(event_key, scout_id, updated_at DESC);
"""

MIGRATION_8_SQL = """
CREATE TABLE IF NOT EXISTS event_rankings (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    official_rank INTEGER,
    ranking_points REAL,
    ranking_points_label TEXT,
    matches_played INTEGER,
    wins INTEGER,
    losses INTEGER,
    ties INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number)
);
CREATE INDEX IF NOT EXISTS idx_event_rankings_event_rank
    ON event_rankings(event_key, official_rank);
"""

MIGRATION_9_SQL = """
CREATE INDEX IF NOT EXISTS idx_team_summaries_team_generated
    ON team_summaries(team_number, generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_match_team_summaries_lookup
    ON match_team_summaries(match_key, team_number, generated_at DESC);
"""

MIGRATION_10_SQL = """
CREATE TABLE IF NOT EXISTS alliance_selections (
    event_key TEXT NOT NULL REFERENCES events(event_key),
    team_number INTEGER NOT NULL REFERENCES teams(team_number),
    alliance_number INTEGER NOT NULL CHECK(alliance_number BETWEEN 1 AND 8),
    selection_kind TEXT NOT NULL CHECK(selection_kind IN ('captain', 'pick')),
    selected_at TEXT NOT NULL,
    PRIMARY KEY (event_key, team_number)
);
CREATE INDEX IF NOT EXISTS idx_alliance_selections_event_alliance
    ON alliance_selections(event_key, alliance_number, selected_at);
"""

MIGRATION_11_SQL = """
ALTER TABLE observations ADD COLUMN robot_broken INTEGER NOT NULL DEFAULT 0
    CHECK(robot_broken IN (0, 1));
"""


class ResultSyncError(RuntimeError):
    """Raised when official-match sync cannot safely update the local database."""


class StatboticsSyncError(RuntimeError):
    """Raised when Statbotics EPA data cannot safely update the local cache."""


class PickListError(RuntimeError):
    """Raised when a scout pick list cannot safely update."""


class TeamSummaryError(RuntimeError):
    """Raised when an edited team role summary cannot be saved."""


@dataclass(frozen=True)
class StatboticsSyncReport:
    event_key: str
    teams_updated: int


@dataclass(frozen=True)
class PickListAddResult:
    list_name: str
    team_added: bool


def ensure_dashboard_schema(database_path: Path | str) -> None:
    """Make optional dashboard caches available to existing databases."""
    connection = sqlite3.connect(Path(database_path))
    try:
        with connection:
            connection.executescript(MIGRATION_6_SQL)
            connection.executescript(MIGRATION_7_SQL)
            connection.executescript(MIGRATION_8_SQL)
            connection.executescript(MIGRATION_9_SQL)
            connection.executescript(MIGRATION_10_SQL)
    finally:
        connection.close()


def ensure_statbotics_schema(database_path: Path | str) -> None:
    """Backward-compatible alias for the dashboard schema initializer."""
    ensure_dashboard_schema(database_path)


def sync_official_results_from_tba(
    database_path: Path | str,
    event_key: str,
    api_key: str,
) -> int:
    """Fetch final qualification results from TBA and update scheduled local matches.

    The caller owns the API key; this function never writes it to disk.  Only
    matches that already exist in this event's local schedule are updated.
    """
    cleaned_event_key = event_key.strip()
    cleaned_api_key = api_key.strip()
    if not cleaned_event_key:
        raise ResultSyncError("The local database does not contain a TBA event key.")
    if not cleaned_api_key:
        raise ResultSyncError("A TBA API key is required to fetch official results.")

    request = Request(
        f"{TBA_API_BASE_URL}/event/{quote(cleaned_event_key, safe='')}/matches",
        headers={
            "X-TBA-Auth-Key": cleaned_api_key,
            "Accept": "application/json",
            "User-Agent": "SpeechScout Analytics/1.0",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            message = "TBA rejected that API key. Check the key and try again."
        else:
            message = f"TBA returned HTTP {error.code}."
        raise ResultSyncError(message) from error
    except (URLError, TimeoutError, OSError) as error:
        raise ResultSyncError("Could not reach TBA. Check the network connection and try again.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ResultSyncError("TBA returned an unreadable response.") from error

    if not isinstance(payload, list):
        raise ResultSyncError("TBA returned an unexpected match-results response.")

    rankings_request = Request(
        f"{TBA_API_BASE_URL}/event/{quote(cleaned_event_key, safe='')}/rankings",
        headers={
            "X-TBA-Auth-Key": cleaned_api_key,
            "Accept": "application/json",
            "User-Agent": "SpeechScout Analytics/1.0",
        },
    )
    try:
        with urlopen(rankings_request, timeout=15) as response:
            rankings_payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        # TBA returns 404 before an event has standings. Match-result syncing
        # should still work in that situation, and any last-known standings are
        # retained until official rankings become available.
        if error.code == 404:
            rankings_payload = None
        elif error.code in {401, 403}:
            raise ResultSyncError("TBA rejected that API key. Check the key and try again.") from error
        else:
            raise ResultSyncError(f"TBA returned HTTP {error.code} for event rankings.") from error
    except (URLError, TimeoutError, OSError) as error:
        raise ResultSyncError("Could not reach TBA. Check the network connection and try again.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ResultSyncError("TBA returned an unreadable rankings response.") from error

    connection = sqlite3.connect(Path(database_path))
    updated_matches = 0
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(MIGRATION_8_SQL)
        with connection:
            for remote_match in payload:
                if not isinstance(remote_match, dict) or remote_match.get("comp_level") != "qm":
                    continue
                match_number = integer(remote_match.get("match_number"))
                alliances = remote_match.get("alliances")
                if match_number is None or match_number < 1 or not isinstance(alliances, dict):
                    continue
                red = alliances.get("red")
                blue = alliances.get("blue")
                if not isinstance(red, dict) or not isinstance(blue, dict):
                    continue
                red_score = integer(red.get("score"))
                blue_score = integer(blue.get("score"))
                # TBA represents unplayed matches with a null or negative score.
                if red_score is None or blue_score is None or red_score < 0 or blue_score < 0:
                    continue

                winner = remote_match.get("winning_alliance")
                if winner not in {"red", "blue", "tie"}:
                    winner = "red" if red_score > blue_score else "blue" if blue_score > red_score else "tie"
                score_breakdown = remote_match.get("score_breakdown")
                if not isinstance(score_breakdown, dict):
                    score_breakdown = {}
                red_breakdown = score_breakdown.get("red")
                blue_breakdown = score_breakdown.get("blue")
                if not isinstance(red_breakdown, dict):
                    red_breakdown = {}
                if not isinstance(blue_breakdown, dict):
                    blue_breakdown = {}
                red_endgame_points = score_breakdown_value(red_breakdown, "endgame")
                blue_endgame_points = score_breakdown_value(blue_breakdown, "endgame")
                cursor = connection.execute(
                    """
                    UPDATE matches
                    SET
                        red_score = ?, blue_score = ?,
                        red_auto_points = ?, red_teleop_points = ?, red_endgame_points = ?,
                        red_penalty_points = ?,
                        blue_auto_points = ?, blue_teleop_points = ?, blue_endgame_points = ?,
                        blue_penalty_points = ?,
                        tba_score_breakdown_json = ?,
                        winner_alliance = ?, result_status = 'final'
                    WHERE event_key = ? AND match_type = 'qm' AND match_number = ?
                    """,
                    (
                        red_score,
                        blue_score,
                        score_breakdown_value(red_breakdown, "auto"),
                        teleop_points_without_endgame(red_breakdown, red_endgame_points),
                        red_endgame_points,
                        score_breakdown_value(red_breakdown, "penalties"),
                        score_breakdown_value(blue_breakdown, "auto"),
                        teleop_points_without_endgame(blue_breakdown, blue_endgame_points),
                        blue_endgame_points,
                        score_breakdown_value(blue_breakdown, "penalties"),
                        canonical_json(score_breakdown) if score_breakdown else None,
                        winner,
                        cleaned_event_key,
                        match_number,
                    ),
                )
                updated_matches += cursor.rowcount
            _cache_event_rankings(connection, cleaned_event_key, rankings_payload)
    except sqlite3.DatabaseError as error:
        raise ResultSyncError(f"Could not update the local results database: {error}") from error
    finally:
        connection.close()
    return updated_matches


def _cache_event_rankings(
    connection: sqlite3.Connection,
    event_key: str,
    payload: Any,
) -> None:
    """Store official TBA ranks without guessing game-specific ranking rules."""
    if not isinstance(payload, dict):
        return
    rankings = payload.get("rankings")
    if not isinstance(rankings, list):
        return
    local_teams = {
        int(row[0]) for row in connection.execute("SELECT team_number FROM teams")
    }
    ranking_field = _ranking_points_field(payload)
    rows: list[tuple[Any, ...]] = []
    for ranking in rankings:
        if not isinstance(ranking, dict):
            continue
        team_key = str(ranking.get("team_key") or "")
        team_number = integer(team_key.removeprefix("frc"))
        if team_number is None or team_number not in local_teams:
            continue
        record = ranking.get("record")
        if not isinstance(record, dict):
            record = {}
        ranking_points = None
        ranking_points_label = None
        if ranking_field is not None:
            values_key, value_index, field_label = ranking_field
            values = ranking.get(values_key)
            if isinstance(values, list) and value_index < len(values):
                ranking_points = _finite_float(values[value_index])
                if ranking_points is not None:
                    ranking_points_label = field_label
        rows.append(
            (
                event_key,
                team_number,
                integer(ranking.get("rank")),
                ranking_points,
                ranking_points_label,
                integer(ranking.get("matches_played")),
                integer(record.get("wins")),
                integer(record.get("losses")),
                integer(record.get("ties")),
                utc_now(),
            )
        )
    connection.execute("DELETE FROM event_rankings WHERE event_key = ?", (event_key,))
    connection.executemany(
        """
        INSERT INTO event_rankings(
            event_key, team_number, official_rank, ranking_points, ranking_points_label,
            matches_played, wins, losses, ties, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def _ranking_points_field(payload: dict[str, Any]) -> tuple[str, int, str] | None:
    """Find TBA's ranking-points field from its event-specific metadata."""
    candidates: list[tuple[int, str, int, str]] = []
    for metadata_key, values_key in (
        ("extra_stats_info", "extra_stats"),
        ("sort_order_info", "sort_orders"),
    ):
        metadata = payload.get(metadata_key)
        if not isinstance(metadata, list):
            continue
        for index, field in enumerate(metadata):
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or "").strip()
            normalized = " ".join(field_name.casefold().replace("_", " ").split())
            if "total ranking point" in normalized:
                priority = 0
            elif "ranking point" in normalized or "rank point" in normalized:
                priority = 1
            elif "ranking score" in normalized:
                priority = 2
            else:
                continue
            candidates.append((priority, values_key, index, field_name))
    if not candidates:
        return None
    _, values_key, index, field_name = min(candidates, key=lambda item: item[0])
    return values_key, index, field_name


def sync_statbotics_epa(
    database_path: Path | str,
    event_key: str,
) -> StatboticsSyncReport:
    """Fetch this event's Statbotics EPA values and cache them locally."""
    cleaned_event_key = event_key.strip()
    if not cleaned_event_key:
        raise StatboticsSyncError("The local database does not contain a Statbotics event key.")

    query = urlencode({"event": cleaned_event_key, "limit": 1000})
    request = Request(
        f"{STATBOTICS_API_BASE_URL}/team_events?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "SpeechScout Analytics/1.0",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 404:
            message = f"Statbotics has no data for event {cleaned_event_key}."
        elif error.code == 429:
            message = "Statbotics rate limited this sync. Wait briefly and try again."
        else:
            message = f"Statbotics returned HTTP {error.code}."
        raise StatboticsSyncError(message) from error
    except (URLError, TimeoutError, OSError) as error:
        raise StatboticsSyncError(
            "Could not reach Statbotics. Check the network connection and try again."
        ) from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise StatboticsSyncError("Statbotics returned an unreadable response.") from error

    if not isinstance(payload, list):
        raise StatboticsSyncError("Statbotics returned an unexpected event-EPA response.")

    connection = sqlite3.connect(Path(database_path))
    updated = 0
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(MIGRATION_6_SQL)
        local_teams = {
            int(row[0])
            for row in connection.execute("SELECT team_number FROM teams")
        }
        with connection:
            for item in payload:
                values = _statbotics_epa_values(item)
                if values is None:
                    continue
                team_number, total_epa, auto_epa, teleop_epa, endgame_epa = values
                if team_number not in local_teams:
                    continue
                connection.execute(
                    """
                    INSERT INTO statbotics_team_epa(
                        event_key, team_number, total_epa, auto_epa, teleop_epa, endgame_epa, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key, team_number) DO UPDATE SET
                        total_epa = excluded.total_epa,
                        auto_epa = excluded.auto_epa,
                        teleop_epa = excluded.teleop_epa,
                        endgame_epa = excluded.endgame_epa,
                        synced_at = excluded.synced_at
                    """,
                    (
                        cleaned_event_key,
                        team_number,
                        total_epa,
                        auto_epa,
                        teleop_epa,
                        endgame_epa,
                        utc_now(),
                    ),
                )
                updated += 1
    except sqlite3.DatabaseError as error:
        raise StatboticsSyncError(f"Could not update the local EPA cache: {error}") from error
    finally:
        connection.close()
    return StatboticsSyncReport(event_key=cleaned_event_key, teams_updated=updated)


def _statbotics_epa_values(
    item: Any,
) -> tuple[int, float, float | None, float | None, float | None] | None:
    """Read the stable score-EPA fields from a Statbotics team-event record."""
    if not isinstance(item, dict):
        return None
    team_number = integer(item.get("team"))
    epa = item.get("epa")
    if team_number is None or not isinstance(epa, dict):
        return None
    total_points = epa.get("total_points")
    breakdown = epa.get("breakdown")
    total_epa = (
        _finite_float(total_points.get("mean"))
        if isinstance(total_points, dict)
        else None
    )
    if total_epa is None and isinstance(breakdown, dict):
        total_epa = _finite_float(breakdown.get("total_points"))
    if total_epa is None:
        return None
    return (
        team_number,
        total_epa,
        _finite_float(breakdown.get("auto_points")) if isinstance(breakdown, dict) else None,
        _finite_float(breakdown.get("teleop_points")) if isinstance(breakdown, dict) else None,
        _finite_float(breakdown.get("endgame_points")) if isinstance(breakdown, dict) else None,
    )


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def add_team_to_pick_list(
    database_path: Path | str,
    event_key: str,
    scout_id: str,
    list_name: str,
    team_number: int,
) -> PickListAddResult:
    """Create or reuse a scout's event pick list and add one team to it."""
    clean_event_key = event_key.strip()
    clean_scout_id = scout_id.strip()
    clean_name = " ".join(list_name.split())
    if not clean_event_key or not clean_scout_id or not clean_name:
        raise PickListError("Choose a scout and give the pick list a name.")
    if team_number <= 0:
        raise PickListError("Pick lists can only contain valid team numbers.")

    connection = sqlite3.connect(Path(database_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(MIGRATION_7_SQL)
        with connection:
            event_exists = connection.execute(
                "SELECT 1 FROM events WHERE event_key = ?", (clean_event_key,)
            ).fetchone()
            scout_exists = connection.execute(
                "SELECT 1 FROM scouts WHERE scout_id = ?", (clean_scout_id,)
            ).fetchone()
            team_exists = connection.execute(
                "SELECT 1 FROM teams WHERE team_number = ?", (team_number,)
            ).fetchone()
            if event_exists is None or scout_exists is None or team_exists is None:
                raise PickListError("The selected event, scout, or team is no longer in this database.")

            existing = connection.execute(
                """
                SELECT list_id FROM pick_lists
                WHERE event_key = ? AND scout_id = ? AND name = ?
                """,
                (clean_event_key, clean_scout_id, clean_name),
            ).fetchone()
            now = utc_now()
            if existing is None:
                list_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO pick_lists(list_id, event_key, scout_id, name, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (list_id, clean_event_key, clean_scout_id, clean_name, now, now),
                )
            else:
                list_id = str(existing[0])
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO pick_list_teams(list_id, team_number, added_at)
                VALUES (?, ?, ?)
                """,
                (list_id, team_number, now),
            )
            if cursor.rowcount:
                connection.execute(
                    "UPDATE pick_lists SET updated_at = ? WHERE list_id = ?", (now, list_id)
                )
    except sqlite3.DatabaseError as error:
        raise PickListError(f"Could not update the pick list: {error}") from error
    finally:
        connection.close()
    return PickListAddResult(list_name=clean_name, team_added=bool(cursor.rowcount))


def add_alliance_selection(
    database_path: Path | str,
    event_key: str,
    alliance_number: int,
    team_number: int,
    selection_kind: str,
) -> None:
    """Record a captain or picked team for an event alliance."""
    if alliance_number not in range(1, 9) or team_number <= 0:
        raise PickListError("Choose a valid alliance and team.")
    if selection_kind not in {"captain", "pick"}:
        raise PickListError("Choose whether this team is a captain or a pick.")
    connection = sqlite3.connect(Path(database_path))
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(MIGRATION_10_SQL)
        with connection:
            team_exists = connection.execute(
                "SELECT 1 FROM teams WHERE team_number = ?", (team_number,)
            ).fetchone()
            if team_exists is None:
                raise PickListError("That team is no longer in this database.")
            connection.execute(
                """
                INSERT INTO alliance_selections(
                    event_key, team_number, alliance_number, selection_kind, selected_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event_key, team_number, alliance_number, selection_kind, utc_now()),
            )
    except sqlite3.IntegrityError as error:
        raise PickListError("That team has already been selected by an alliance.") from error
    except sqlite3.DatabaseError as error:
        raise PickListError(f"Could not update alliance selections: {error}") from error
    finally:
        connection.close()


def remove_alliance_selection(
    database_path: Path | str, event_key: str, team_number: int
) -> None:
    """Return a team to the available pool for the current event."""
    connection = sqlite3.connect(Path(database_path))
    try:
        with connection:
            connection.execute(
                "DELETE FROM alliance_selections WHERE event_key = ? AND team_number = ?",
                (event_key, team_number),
            )
    except sqlite3.DatabaseError as error:
        raise PickListError(f"Could not remove alliance selection: {error}") from error
    finally:
        connection.close()


def remove_latest_alliance_selection(database_path: Path | str, event_key: str) -> bool:
    """Undo the most recently recorded alliance selection for an event."""
    connection = sqlite3.connect(Path(database_path))
    try:
        with connection:
            cursor = connection.execute(
                """
                DELETE FROM alliance_selections
                WHERE rowid = (
                    SELECT rowid FROM alliance_selections
                    WHERE event_key = ?
                    ORDER BY selected_at DESC, rowid DESC
                    LIMIT 1
                )
                """,
                (event_key,),
            )
            return bool(cursor.rowcount)
    except sqlite3.DatabaseError as error:
        raise PickListError(f"Could not undo alliance selection: {error}") from error
    finally:
        connection.close()


def update_team_summary(
    database_path: Path | str, event_key: str, team_number: int, summary: str
) -> None:
    """Save a user-edited role summary and rebuild its semantic entry on demand."""
    clean_summary = " ".join(summary.split())
    if not clean_summary:
        raise TeamSummaryError("Role summaries cannot be empty.")
    connection = sqlite3.connect(Path(database_path))
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE team_summaries
                SET summary = ?, generated_at = ?
                WHERE event_key = ? AND team_number = ?
                  AND generated_at = (
                      SELECT MAX(generated_at) FROM team_summaries
                      WHERE event_key = ? AND team_number = ?
                  )
                """,
                (clean_summary, utc_now(), event_key, team_number, event_key, team_number),
            )
            if not cursor.rowcount:
                raise TeamSummaryError("Generate a role summary before editing it.")
            connection.execute(
                """
                DELETE FROM embedding_chunks
                WHERE event_key = ? AND team_number = ? AND chunk_type = 'team_summary'
                """,
                (event_key, team_number),
            )
    except sqlite3.DatabaseError as error:
        raise TeamSummaryError(f"Could not save the role summary: {error}") from error
    finally:
        connection.close()


@dataclass
class ImportReport:
    database_path: Path
    status_counts: Counter[str] = field(default_factory=Counter)

    def add(self, status: str) -> None:
        self.status_counts[status] += 1


class AnalyticsDatabase:
    """SQLite database and importer for current SpeechScout JSON files."""

    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        self.connection.executescript(SCHEMA_SQL)
        applied_versions = {
            row[0] for row in self.connection.execute("SELECT version FROM schema_migrations")
        }
        if 1 not in applied_versions:
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        if 2 not in applied_versions:
            self.connection.executescript(MIGRATION_2_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        if 3 not in applied_versions:
            self.connection.executescript(MIGRATION_3_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
        if 4 not in applied_versions:
            match_columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(matches)")
            }
            if "red_auto_points" not in match_columns:
                self.connection.executescript(MIGRATION_4_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (4)")
        if 5 not in applied_versions:
            match_columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(matches)")
            }
            if "red_penalty_points" not in match_columns:
                self.connection.executescript(MIGRATION_5_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (5)")
        if 6 not in applied_versions:
            self.connection.executescript(MIGRATION_6_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (6)")
        if 7 not in applied_versions:
            self.connection.executescript(MIGRATION_7_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (7)")
        if 8 not in applied_versions:
            self.connection.executescript(MIGRATION_8_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (8)")
        if 9 not in applied_versions:
            self.connection.executescript(MIGRATION_9_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (9)")
        if 10 not in applied_versions:
            self.connection.executescript(MIGRATION_10_SQL)
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (10)")
        # Check the physical table even when the migration ledger says version 11
        # was applied.  A previous interrupted/manual upgrade can leave that ledger
        # out of sync, and the analytics queries must remain safe to open.
        observation_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(observations)")
        }
        if "robot_broken" not in observation_columns:
            self.connection.executescript(MIGRATION_11_SQL)
        if 11 not in applied_versions:
            self.connection.execute("INSERT INTO schema_migrations(version) VALUES (11)")
        self.connection.commit()

    def import_existing_data(
        self,
        game_config_path: Path,
        schedule_path: Path,
        matches_directory: Path,
    ) -> ImportReport:
        game_config = load_json_object(game_config_path, "game configuration")
        event_key = required_text(game_config.get("tba_event_key"), "game.json tba_event_key")
        event_name = str(game_config.get("competition_name") or event_key)
        report = ImportReport(database_path=self.database_path)
        import_id = uuid4().hex

        with self.connection:
            self.connection.execute(
                "INSERT INTO import_batches(import_id, source_name, imported_at) VALUES (?, ?, ?)",
                (import_id, "legacy JSON import", utc_now()),
            )
            self._upsert_event(event_key, event_name, game_config)

        self._import_schedule(import_id, event_key, event_name, game_config, schedule_path, report)
        for match_file in sorted(matches_directory.glob("*.json")):
            self._import_observation_file(import_id, event_key, match_file, game_config, report)

        with self.connection:
            self.connection.execute(
                "UPDATE import_batches SET file_count = ? WHERE import_id = ?",
                (sum(report.status_counts.values()), import_id),
            )
        return report

    def table_counts(self) -> dict[str, int]:
        tables = (
            "events",
            "teams",
            "scouts",
            "matches",
            "match_teams",
            "observations",
            "score_events",
            "notes",
            "import_files",
        )
        return {
            table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }

    def _upsert_event(self, event_key: str, event_name: str, game_config: dict[str, Any]) -> None:
        config_json = canonical_json(game_config)
        self.connection.execute(
            """
            INSERT INTO events(event_key, event_name, game_config_json, game_config_hash, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(event_key) DO UPDATE SET
                event_name = excluded.event_name,
                game_config_json = excluded.game_config_json,
                game_config_hash = excluded.game_config_hash
            """,
            (event_key, event_name, config_json, sha256_text(config_json), utc_now()),
        )

    def _import_schedule(
        self,
        import_id: str,
        event_key: str,
        event_name: str,
        game_config: dict[str, Any],
        schedule_path: Path,
        report: ImportReport,
    ) -> None:
        if not schedule_path.is_file():
            report.add("schedule_missing")
            return

        raw_bytes = schedule_path.read_bytes()
        file_hash = sha256_bytes(raw_bytes)
        raw_json = decode_source(raw_bytes)
        try:
            schedule = json.loads(raw_json)
            if not isinstance(schedule, dict) or not isinstance(schedule.get("matches"), list):
                raise ValueError("expected a JSON object with a matches list")
        except (json.JSONDecodeError, ValueError) as error:
            with self.connection:
                self._record_import_file(
                    file_hash, import_id, schedule_path, raw_json, "invalid", f"Schedule: {error}"
                )
            report.add("invalid")
            return

        with self.connection:
            if not self._record_import_file(
                file_hash, import_id, schedule_path, raw_json, "imported", None
            ):
                report.add("duplicate")
                return
            self._upsert_event(
                str(schedule.get("event_key") or event_key),
                str(schedule.get("event_name") or event_name),
                game_config,
            )
            for match in schedule["matches"]:
                self._upsert_scheduled_match(event_key, match)
        report.add("imported")

    def _upsert_scheduled_match(self, event_key: str, match: Any) -> None:
        if not isinstance(match, dict):
            return
        match_number = integer(match.get("match_number"))
        if match_number is None or match_number < 1:
            return
        match_key = str(match.get("match_key") or f"{event_key}_qm{match_number}")
        self.connection.execute(
            """
            INSERT INTO matches(match_key, event_key, match_number, match_type)
            VALUES (?, ?, ?, 'qm')
            ON CONFLICT(match_key) DO UPDATE SET
                event_key = excluded.event_key,
                match_number = excluded.match_number
            """,
            (match_key, event_key, match_number),
        )
        teams = match.get("teams")
        if not isinstance(teams, list):
            return
        for team in teams:
            if not isinstance(team, dict):
                continue
            team_number = integer(team.get("number"))
            alliance = team.get("alliance")
            station = integer(team.get("station"))
            if team_number is None or alliance not in {"red", "blue"} or station not in {1, 2, 3}:
                continue
            self._ensure_team(team_number)
            self.connection.execute(
                """
                INSERT INTO match_teams(match_key, team_number, alliance, station)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(match_key, team_number) DO UPDATE SET
                    alliance = excluded.alliance,
                    station = excluded.station
                """,
                (match_key, team_number, alliance, station),
            )

    def _import_observation_file(
        self,
        import_id: str,
        event_key: str,
        source_path: Path,
        game_config: dict[str, Any],
        report: ImportReport,
    ) -> None:
        raw_bytes = source_path.read_bytes()
        file_hash = sha256_bytes(raw_bytes)
        raw_json = decode_source(raw_bytes)
        try:
            payload = json.loads(raw_json)
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
        except (json.JSONDecodeError, ValueError) as error:
            with self.connection:
                self._record_import_file(
                    file_hash, import_id, source_path, raw_json, "invalid", f"Invalid JSON: {error}"
                )
            report.add("invalid")
            return

        # AI enrichment writes its cached result under ``analytics`` in the original
        # scouting JSON.  That metadata must not turn an otherwise identical
        # observation into a second import when files are re-imported or merged.
        if self._imported_scouting_payload_exists(payload):
            report.add("duplicate")
            return

        problem = self._legacy_observation_problem(payload, source_path)
        if problem is not None:
            with self.connection:
                if self._record_import_file(
                    file_hash, import_id, source_path, raw_json, "review_required", problem
                ):
                    report.add("review_required")
                else:
                    report.add("duplicate")
            return

        filename_match = LEGACY_FILE_PATTERN.search(source_path.name)
        assert filename_match is not None  # Guaranteed by _legacy_observation_problem.
        team_number = integer(payload.get("team_number")) or int(filename_match.group("team"))
        match_number = integer(payload.get("match_number"))
        assert team_number is not None and match_number is not None
        match_key = self._match_key(event_key, match_number)
        scout_name = str(payload.get("scout_name") or "Unknown scout").strip() or "Unknown scout"
        scout_id = str(payload.get("scout_id") or uuid5(NAMESPACE_URL, f"legacy-scout:{scout_name}"))
        observation_id = str(
            payload.get("observation_id") or uuid5(NAMESPACE_URL, f"legacy-observation:{file_hash}")
        )

        try:
            with self.connection:
                if not self._record_import_file(
                    file_hash, import_id, source_path, raw_json, "imported", None
                ):
                    report.add("duplicate")
                    return
                self._upsert_event(event_key, str(game_config.get("competition_name") or event_key), game_config)
                self._ensure_match(event_key, match_key, match_number)
                self._ensure_team(team_number)
                self._ensure_scout(scout_id, scout_name)
                alignment = self._schedule_alignment(match_key, team_number)
                self.connection.execute(
                    """
                    INSERT INTO observations(
                        observation_id, match_key, team_number, scout_id, predicted_winner,
                        robot_broken, reported_total_points, source_schema_version, schedule_alignment,
                        source_file_hash, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation_id,
                        match_key,
                        team_number,
                        scout_id,
                        nullable_alliance(payload.get("predicted_winner")),
                        1 if payload.get("robot_broken") is True else 0,
                        integer(payload.get("total_points")),
                        integer(payload.get("schema_version")) or 1,
                        alignment,
                        file_hash,
                        utc_now(),
                    ),
                )
                self._insert_score_events(observation_id, payload.get("events"), game_config)
                self._insert_notes(observation_id, payload.get("notes"))
        except (sqlite3.DatabaseError, TypeError, ValueError) as error:
            # No partial observation is committed; preserve the source and surface the issue.
            with self.connection:
                self._record_import_file(
                    file_hash, import_id, source_path, raw_json, "invalid", f"Import error: {error}"
                )
            report.add("invalid")
            return
        report.add("imported")

    def _legacy_observation_problem(self, payload: dict[str, Any], source_path: Path) -> str | None:
        filename_match = LEGACY_FILE_PATTERN.search(source_path.name)
        team_number = integer(payload.get("team_number"))
        match_number = integer(payload.get("match_number"))
        if match_number is None or match_number < 1:
            return "Missing or invalid match_number."
        if team_number is not None and team_number < 1:
            return "Invalid team_number."
        if team_number is None and filename_match is None:
            return "Could not determine the scouted team from the JSON or filename."
        if filename_match is not None and team_number is not None:
            filename_team = int(filename_match.group("team"))
            if team_number != filename_team:
                return "team_number disagrees with the legacy filename."
        return None

    def _imported_scouting_payload_exists(self, payload: dict[str, Any]) -> bool:
        scouting_hash = scouting_payload_hash(payload)
        rows = self.connection.execute(
            "SELECT raw_json FROM import_files WHERE status = 'imported'"
        ).fetchall()
        for row in rows:
            try:
                prior_payload = json.loads(str(row["raw_json"]))
            except json.JSONDecodeError:
                continue
            if isinstance(prior_payload, dict) and scouting_payload_hash(prior_payload) == scouting_hash:
                return True
        return False

    def _match_key(self, event_key: str, match_number: int) -> str:
        row = self.connection.execute(
            "SELECT match_key FROM matches WHERE event_key = ? AND match_type = 'qm' AND match_number = ?",
            (event_key, match_number),
        ).fetchone()
        return str(row["match_key"]) if row else f"{event_key}_qm{match_number}"

    def _ensure_match(self, event_key: str, match_key: str, match_number: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO matches(match_key, event_key, match_number, match_type) VALUES (?, ?, ?, 'qm')",
            (match_key, event_key, match_number),
        )

    def _ensure_team(self, team_number: int) -> None:
        self.connection.execute("INSERT OR IGNORE INTO teams(team_number) VALUES (?)", (team_number,))

    def _ensure_scout(self, scout_id: str, scout_name: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO scouts(scout_id, display_name, created_at) VALUES (?, ?, ?)",
            (scout_id, scout_name, utc_now()),
        )

    def _schedule_alignment(self, match_key: str, team_number: int) -> str:
        match_team = self.connection.execute(
            "SELECT 1 FROM match_teams WHERE match_key = ? AND team_number = ?",
            (match_key, team_number),
        ).fetchone()
        return "matched" if match_team else "team_not_scheduled"

    def _insert_score_events(
        self, observation_id: str, raw_events: Any, game_config: dict[str, Any]
    ) -> None:
        if not isinstance(raw_events, list):
            return
        penalty_labels = set(game_config.get("penalties", {}))
        breakdown_labels = set(game_config.get("breakdowns", {}))
        for sequence_number, raw_event in enumerate(raw_events):
            if not isinstance(raw_event, dict):
                raise ValueError(f"Event {sequence_number + 1} is not an object")
            label = str(raw_event.get("name") or "").strip()
            points = integer(raw_event.get("points"))
            timestamp_ms = milliseconds(raw_event.get("timestamp"))
            if not label or points is None or timestamp_ms is None:
                raise ValueError(f"Event {sequence_number + 1} is missing a name, score, or timestamp")
            if label in breakdown_labels:
                event_type = "breakdown"
            elif label in penalty_labels:
                event_type = "penalty_committed"
            else:
                event_type = "score"
            self.connection.execute(
                """
                INSERT INTO score_events(
                    score_event_id, observation_id, sequence_number, event_type, label,
                    points, alternate_points, timestamp_ms, transcript
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid5(NAMESPACE_URL, f"{observation_id}:event:{sequence_number}")),
                    observation_id,
                    sequence_number,
                    event_type,
                    label,
                    points,
                    integer(raw_event.get("alt_points")),
                    timestamp_ms,
                    nullable_text(raw_event.get("transcript")),
                ),
            )

    def _insert_notes(self, observation_id: str, raw_notes: Any) -> None:
        if not isinstance(raw_notes, list):
            return
        for sequence_number, raw_note in enumerate(raw_notes):
            if not isinstance(raw_note, dict):
                raise ValueError(f"Note {sequence_number + 1} is not an object")
            text = str(raw_note.get("text") or "").strip()
            timestamp_ms = milliseconds(raw_note.get("timestamp"))
            if not text or timestamp_ms is None:
                raise ValueError(f"Note {sequence_number + 1} is missing text or a timestamp")
            self.connection.execute(
                "INSERT INTO notes(note_id, observation_id, sequence_number, timestamp_ms, text) VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid5(NAMESPACE_URL, f"{observation_id}:note:{sequence_number}")),
                    observation_id,
                    sequence_number,
                    timestamp_ms,
                    text,
                ),
            )

    def _record_import_file(
        self,
        file_hash: str,
        import_id: str,
        source_path: Path,
        raw_json: str,
        status: str,
        issue_message: str | None,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO import_files(
                file_hash, import_id, source_path, raw_json, status, issue_message
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (file_hash, import_id, str(source_path), raw_json, status, issue_message),
        )
        return cursor.rowcount == 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def scouting_payload_hash(payload: dict[str, Any]) -> str:
    """Hash the scouting record while excluding generated analytics metadata."""
    source_record = dict(payload)
    source_record.pop("analytics", None)
    return sha256_text(canonical_json(source_record))


def decode_source(raw_bytes: bytes) -> str:
    return raw_bytes.decode("utf-8", errors="replace")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def score_breakdown_value(breakdown: dict[str, Any], period: str) -> int | None:
    """Read the common TBA score-breakdown names without inventing a score."""
    field_names = {
        # 2026 uses total* names; prior games commonly use the shorter forms.
        "auto": ("totalAutoPoints", "autoPoints", "auto_points", "autoScore"),
        "teleop": ("totalTeleopPoints", "teleopPoints", "teleop_points", "teleopScore"),
        "endgame": (
            "endGameTowerPoints",
            "endgameTowerPoints",
            "totalTowerPoints",
            "endGamePoints",
            "endgamePoints",
            "end_game_points",
            "endgameScore",
        ),
        "penalties": ("foulPoints", "penaltyPoints"),
    }
    for field_name in field_names[period]:
        value = integer(breakdown.get(field_name))
        if value is not None:
            return value
    return None


def teleop_points_without_endgame(
    breakdown: dict[str, Any], endgame_points: int | None
) -> int | None:
    """TBA's total teleop score includes endgame; show the non-endgame portion."""
    teleop_points = score_breakdown_value(breakdown, "teleop")
    if teleop_points is None or endgame_points is None:
        return teleop_points
    return max(0, teleop_points - endgame_points)


def milliseconds(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        milliseconds_value = round(float(value) * 1000)
    except (TypeError, ValueError):
        return None
    return milliseconds_value if milliseconds_value >= 0 else None


def nullable_alliance(value: Any) -> str | None:
    return value if value in {"red", "blue"} else None


def nullable_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} must be set before importing analytics data.")
    return text


def load_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as file:
            value = json.load(file)
    except FileNotFoundError as error:
        raise ValueError(f"Could not find {description}: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Could not parse {description} {path}: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description.capitalize()} must contain a JSON object: {path}")
    return value


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and import the local SpeechScout analytics database.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument("--game-config", type=Path, default=DEFAULT_GAME_CONFIG_PATH)
    parser.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE_PATH)
    parser.add_argument("--matches", type=Path, default=DEFAULT_MATCHES_DIRECTORY)
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    database = AnalyticsDatabase(arguments.database)
    try:
        report = database.import_existing_data(
            arguments.game_config, arguments.schedule, arguments.matches
        )
        print(f"Analytics database: {report.database_path}")
        print("Import results:", ", ".join(
            f"{status}={count}" for status, count in sorted(report.status_counts.items())
        ))
        print("Table counts:", ", ".join(
            f"{table}={count}" for table, count in database.table_counts().items()
        ))
    finally:
        database.close()


if __name__ == "__main__":
    main()
