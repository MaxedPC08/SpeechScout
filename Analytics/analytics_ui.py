"""Qt desktop dashboard for the offline SpeechScout analytics database.

Run from the repository root with the project virtual environment:

    .venv/bin/python Analytics/analytics_ui.py
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Iterable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

try:
    from .database import (
        ANALYTICS_DIRECTORY,
        DEFAULT_DATABASE_PATH,
        PickListError,
        ResultSyncError,
        StatboticsSyncError,
        StatboticsSyncReport,
        TeamSummaryError,
        add_alliance_selection,
        add_team_to_pick_list,
        ensure_dashboard_schema,
        remove_alliance_selection,
        remove_latest_alliance_selection,
        sync_statbotics_epa,
        sync_official_results_from_tba,
        update_team_summary,
    )
    from .gemini import (
        EnrichmentReport,
        GeminiEnrichmentError,
        OpenAICompatibleConfig,
        TeamSearchResult,
        enrich_database,
        search_teams,
    )
except ImportError:  # Supports ``python Analytics/analytics_ui.py``.
    from database import (
        ANALYTICS_DIRECTORY,
        DEFAULT_DATABASE_PATH,
        PickListError,
        ResultSyncError,
        StatboticsSyncError,
        StatboticsSyncReport,
        TeamSummaryError,
        add_alliance_selection,
        add_team_to_pick_list,
        ensure_dashboard_schema,
        remove_alliance_selection,
        remove_latest_alliance_selection,
        sync_statbotics_epa,
        sync_official_results_from_tba,
        update_team_summary,
    )
    from gemini import (
        EnrichmentReport,
        GeminiEnrichmentError,
        OpenAICompatibleConfig,
        TeamSearchResult,
        enrich_database,
        search_teams,
    )


BACKGROUND = "#0d111a"
SURFACE = "#171d29"
SURFACE_ALT = "#222b3d"
TEXT = "#f4f7fb"
MUTED = "#97a5bc"
ACCENT = "#52d4a2"
RED = "#ff7182"
BLUE = "#79a9ff"
WARNING = "#ffbf69"
PURPLE = "#c6a2ff"
BORDER = "#273247"
BORDER_STRONG = "#3c5474"
CHART_COLORS = (RED, ACCENT, BLUE, PURPLE, WARNING, "#ff9f6e")
# Analytics shares the scout-side local configuration, including the provider
# settings, instead of creating a second credentials file.
PERSONAL_CONFIG_PATH = ANALYTICS_DIRECTORY.parent / "TalkToScout" / "personal.json"
DEFAULT_PERSONAL_CONFIG: dict[str, Any] = {
    "tba_api_key": "",
    "ai_endpoint": "",
    "ai_model": "",
    "ai_api_key": "",
    "ai_embedding_model": "",
}


def load_personal_config() -> dict[str, Any]:
    if not PERSONAL_CONFIG_PATH.exists():
        with PERSONAL_CONFIG_PATH.open("w", encoding="utf-8") as config_file:
            json.dump(DEFAULT_PERSONAL_CONFIG, config_file, indent=4)
            config_file.write("\n")
        return dict(DEFAULT_PERSONAL_CONFIG)

    with PERSONAL_CONFIG_PATH.open(encoding="utf-8") as config_file:
        personal_config = json.load(config_file)
    if not isinstance(personal_config, dict):
        raise ValueError("personal.json must contain a JSON object.")
    return DEFAULT_PERSONAL_CONFIG | personal_config


APP_STYLESHEET = f"""
QMainWindow, QWidget#root {{
    background: {BACKGROUND};
    color: {TEXT};
    font-family: Inter, Arial, sans-serif;
}}
QLabel {{
    color: {TEXT};
}}
QLabel#muted {{
    color: {MUTED};
}}
QLabel#brandTitle {{
    color: {TEXT};
    font-size: 19px;
    font-weight: 800;
}}
QLabel#brandSubtitle {{
    color: {MUTED};
    font-size: 12px;
    font-weight: 500;
}}
QFrame#headerBar {{
    border-bottom: 1px solid {BORDER};
}}
QLabel#pageTitle {{
    color: {TEXT};
    font-size: 24px;
    font-weight: 700;
}}
QLabel#emptyIcon {{
    color: {ACCENT};
    font-size: 32px;
}}
QLabel#sectionTitle {{
    color: {TEXT};
    font-size: 15px;
    font-weight: 700;
}}
QLabel#metric {{
    color: {ACCENT};
    font-size: 24px;
    font-weight: 700;
}}
QLabel#eyebrow {{
    color: {MUTED};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#teamScore {{
    color: {ACCENT};
    font-size: 31px;
    font-weight: 700;
}}
QLabel#teamStatValue {{
    color: {TEXT};
    font-size: 19px;
    font-weight: 700;
}}
QLabel#teamStatLabel {{
    color: {MUTED};
    font-size: 11px;
}}
QLabel#teamLegend {{
    color: {ACCENT};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#averageLegend {{
    color: #e8edf7;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#officialTotal {{
    color: {TEXT};
    font-size: 54px;
    font-weight: 800;
}}
QLabel#officialComponent {{
    color: {TEXT};
    font-size: 19px;
    font-weight: 700;
}}
QFrame#card {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1a2130, stop:1 #141a26);
    border: 1px solid #273247;
    border-radius: 14px;
}}
QFrame#clickableCard {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1a2130, stop:1 #141a26);
    border: 1px solid #273247;
    border-radius: 14px;
}}
QFrame#clickableCard:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #202a3c, stop:1 #182030);
    border: 1px solid #4a7a68;
}}
QFrame#metricCard {{
    background: {SURFACE};
    border: 1px solid #273247;
    border-left: 3px solid rgba(82, 212, 162, 0.45);
    border-radius: 10px;
}}
QFrame#teamStat {{
    background: #111824;
    border: 1px solid #263148;
    border-radius: 9px;
}}
QFrame#teamTimeline {{
    background: #111824;
    border: 1px solid #263148;
    border-radius: 10px;
}}
QPushButton {{
    border: 0;
    border-radius: 9px;
    font-size: 13px;
    font-weight: 600;
    padding: 9px 14px;
}}
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #6fe8bd, stop:1 {ACCENT});
    color: #05241a;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #85edc9, stop:1 #75e7bd);
}}
QPushButton#primary:pressed {{
    background: #45c093;
}}
QPushButton#primary:disabled {{
    background: #2c4d42;
    color: #6f8d81;
}}
QPushButton#secondary {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
}}
QPushButton#secondary:hover {{
    background: #31405b;
    border: 1px solid {BORDER_STRONG};
}}
QPushButton#secondary:pressed {{
    background: #29354a;
}}
QPushButton#secondary:disabled {{
    color: {MUTED};
    background: #161d29;
    border: 1px solid #1e2738;
}}
QPushButton#link {{
    background: transparent;
    color: {ACCENT};
    padding: 2px 0;
    text-align: left;
}}
QPushButton#link:hover {{
    color: #83ecc5;
    text-decoration: underline;
}}
QProgressBar#aiEnrichmentProgress {{
    background: #111824;
    color: {TEXT};
    border: 1px solid #34435c;
    border-radius: 7px;
    min-height: 18px;
    text-align: center;
    font-size: 11px;
    font-weight: 600;
}}
QProgressBar#aiEnrichmentProgress::chunk {{
    background: {ACCENT};
    border-radius: 6px;
}}
QLineEdit {{
    background: #111824;
    color: {TEXT};
    border: 1px solid #34435c;
    border-radius: 9px;
    padding: 9px 12px;
    font-size: 13px;
    selection-background-color: {ACCENT};
    selection-color: #05241a;
}}
QLineEdit:hover {{
    border: 1px solid #445a7a;
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox {{
    background: #111824;
    color: {TEXT};
    border: 1px solid #34435c;
    border-radius: 9px;
    padding: 9px 12px;
    font-size: 13px;
    font-weight: 500;
    min-height: 16px;
}}
QComboBox:hover {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 24px;
    border-left-width: 0px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    color: {TEXT};
    selection-background-color: {SURFACE_ALT};
    selection-color: {ACCENT};
    border: 1px solid #34435c;
    border-radius: 6px;
    padding: 4px;
}}
QTabWidget::pane {{
    border: 0;
    border-top: 1px solid {BORDER};
    background: {BACKGROUND};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    border: 0;
    border-bottom: 2px solid transparent;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    padding: 11px 18px 9px 18px;
    margin-right: 6px;
    font-size: 13px;
    font-weight: 600;
}}
QTabBar::tab:hover {{
    color: {TEXT};
    background: rgba(255, 255, 255, 0.04);
}}
QTabBar::tab:selected {{
    color: {TEXT};
    background: rgba(82, 212, 162, 0.12);
    border-bottom: 2px solid {ACCENT};
}}
QScrollArea {{
    border: 0;
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px;
}}
QScrollBar::handle:vertical {{
    background: #344158;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""


def resolve_database_path() -> Path:
    """Prefer the configured path while supporting the current root database."""
    root_database = ANALYTICS_DIRECTORY / "speechscout.sqlite3"
    for candidate in (DEFAULT_DATABASE_PATH, root_database):
        if candidate.is_file():
            return candidate
    return DEFAULT_DATABASE_PATH


class AnalyticsRepository:
    """Queries used by the Qt dashboard and its locally cached enrichments."""

    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
        ensure_dashboard_schema(self.database_path)
        self.connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def matches(self) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT
                m.match_key,
                m.match_number,
                m.red_score,
                m.blue_score,
                m.red_auto_points,
                m.red_teleop_points,
                m.red_endgame_points,
                m.red_penalty_points,
                m.blue_auto_points,
                m.blue_teleop_points,
                m.blue_endgame_points,
                m.blue_penalty_points,
                m.winner_alliance,
                m.result_status,
                (
                    SELECT GROUP_CONCAT(team_number, ', ')
                    FROM (
                        SELECT team_number FROM match_teams
                        WHERE match_key = m.match_key AND alliance = 'red'
                        ORDER BY station
                    )
                ) AS red_teams,
                (
                    SELECT GROUP_CONCAT(team_number, ', ')
                    FROM (
                        SELECT team_number FROM match_teams
                        WHERE match_key = m.match_key AND alliance = 'blue'
                        ORDER BY station
                    )
                ) AS blue_teams,
                (SELECT COUNT(*) FROM observations WHERE match_key = m.match_key) AS observation_count
            FROM matches AS m
            ORDER BY m.match_number DESC
            """
        )

    def match(self, match_key: str) -> sqlite3.Row | None:
        return self._one(
            """
            SELECT
                match_key, match_number, red_score, blue_score,
                red_auto_points, red_teleop_points, red_endgame_points, red_penalty_points,
                blue_auto_points, blue_teleop_points, blue_endgame_points, blue_penalty_points,
                winner_alliance, result_status
            FROM matches
            WHERE match_key = ?
            """,
            (match_key,),
        )

    def event_key(self) -> str | None:
        row = self._one("SELECT event_key FROM events ORDER BY event_key LIMIT 1")
        return None if row is None else str(row["event_key"])

    def match_teams(self, match_key: str) -> list[sqlite3.Row]:
        return self._all(
            """
            WITH totals AS (
                SELECT
                    team_number,
                    SUM(scouted_points) AS scouted_points,
                    COUNT(*) AS observation_count,
                    SUM(penalties_committed) AS penalties_committed,
                    SUM(breakdown_count) AS breakdown_count
                FROM v_team_match_totals
                WHERE match_key = ?
                GROUP BY team_number
            )
            SELECT
                mt.team_number, mt.alliance, mt.station,
                COALESCE(t.scouted_points, 0) AS scouted_points,
                COALESCE(t.observation_count, 0) AS observation_count,
                COALESCE(t.penalties_committed, 0) AS penalties_committed,
                COALESCE(t.breakdown_count, 0) AS breakdown_count
            FROM match_teams AS mt
            LEFT JOIN totals AS t ON t.team_number = mt.team_number
            WHERE mt.match_key = ?
            ORDER BY mt.alliance, mt.station
            """,
            (match_key, match_key),
        )

    def unmatched_match_observations(self, match_key: str) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT
                o.team_number,
                s.scout_id,
                s.display_name AS scout_name,
                v.scouted_points,
                v.penalties_committed,
                v.breakdown_count,
                o.schedule_alignment
            FROM observations AS o
            JOIN scouts AS s ON s.scout_id = o.scout_id
            JOIN v_team_match_totals AS v ON v.observation_id = o.observation_id
            LEFT JOIN match_teams AS mt ON mt.match_key = o.match_key AND mt.team_number = o.team_number
            WHERE o.match_key = ? AND mt.team_number IS NULL
            ORDER BY o.team_number
            """,
            (match_key,),
        )

    def match_events(self, match_key: str) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT o.team_number, se.label, se.points, se.timestamp_ms
            FROM score_events AS se
            JOIN observations AS o ON o.observation_id = se.observation_id
            WHERE o.match_key = ? AND se.event_type = 'score'
            ORDER BY se.timestamp_ms, o.team_number, se.sequence_number
            """,
            (match_key,),
        )

    def match_level_totals(self, match_key: str) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT o.team_number, se.label, COUNT(*) AS count, SUM(se.points) AS points
            FROM score_events AS se
            JOIN observations AS o ON o.observation_id = se.observation_id
            WHERE o.match_key = ? AND se.event_type = 'score'
            GROUP BY o.team_number, se.label
            ORDER BY o.team_number, se.label
            """,
            (match_key,),
        )

    def match_notes(self, match_key: str) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT o.team_number, s.display_name AS scout_name, n.timestamp_ms, n.text
            FROM notes AS n
            JOIN observations AS o ON o.observation_id = n.observation_id
            JOIN scouts AS s ON s.scout_id = o.scout_id
            WHERE o.match_key = ?
            ORDER BY o.team_number, n.timestamp_ms
            """,
            (match_key,),
        )

    def match_team_scouts(self, match_key: str) -> dict[int, list[sqlite3.Row]]:
        rows = self._all(
            """
            SELECT DISTINCT o.team_number, s.scout_id, s.display_name
            FROM observations AS o
            JOIN scouts AS s ON s.scout_id = o.scout_id
            WHERE o.match_key = ?
            ORDER BY o.team_number, s.display_name COLLATE NOCASE
            """,
            (match_key,),
        )
        scouts_by_team: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            scouts_by_team[int(row["team_number"])].append(row)
        return scouts_by_team

    def latest_match_team_summary(self, match_key: str, team_number: int) -> str | None:
        row = self._one(
            """
            SELECT summary
            FROM match_team_summaries
            WHERE match_key = ? AND team_number = ?
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (match_key, team_number),
        )
        return None if row is None else str(row["summary"])

    def teams(self) -> list[sqlite3.Row]:
        return self._all(
            """
            WITH match_results AS (
                SELECT
                    mt.team_number,
                    COUNT(m.match_key) AS matches_played,
                    SUM(CASE WHEN m.winner_alliance = mt.alliance THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN m.winner_alliance != 'tie' AND m.winner_alliance != mt.alliance THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN m.winner_alliance = 'tie' THEN 1 ELSE 0 END) AS ties,
                    AVG(CASE WHEN mt.alliance = 'red' THEN m.red_score ELSE m.blue_score END) AS average_actual_points
                FROM match_teams AS mt
                JOIN matches AS m ON m.match_key = mt.match_key
                WHERE m.result_status = 'final' OR m.winner_alliance IS NOT NULL
                GROUP BY mt.team_number
            ),
            stats AS (
                SELECT
                    team_number,
                    COUNT(*) AS observations,
                    AVG(scouted_points) AS average_scouted_points,
                    AVG(penalties_committed) AS average_penalties_committed,
                    AVG(breakdown_count) AS average_breakdowns,
                    COUNT(DISTINCT CASE WHEN robot_broken = 1 THEN match_key END) AS broken_match_count,
                    MAX(CASE WHEN match_number = latest_match_number THEN robot_broken ELSE 0 END) AS currently_broken
                FROM (
                    SELECT v.*, o.robot_broken, m.match_number,
                        MAX(m.match_number) OVER (PARTITION BY v.team_number) AS latest_match_number
                    FROM v_team_match_totals AS v
                    JOIN observations AS o ON o.observation_id = v.observation_id
                    JOIN matches AS m ON m.match_key = v.match_key
                )
                GROUP BY team_number
            )
            SELECT
                t.team_number,
                COALESCE(s.observations, 0) AS observations,
                COALESCE(mr.matches_played, 0) AS matches_played,
                COALESCE(mr.wins, 0) AS wins,
                COALESCE(mr.losses, 0) AS losses,
                COALESCE(mr.ties, 0) AS ties,
                CASE
                    WHEN COALESCE(mr.wins + mr.losses + mr.ties, 0) > 0
                    THEN (CAST(mr.wins AS REAL) + 0.5 * mr.ties) / (mr.wins + mr.losses + mr.ties)
                    ELSE 0.0
                END AS win_ratio,
                COALESCE(mr.average_actual_points, 0.0) AS average_actual_points,
                s.average_scouted_points,
                s.average_scouted_points AS average_points,
                s.average_penalties_committed,
                s.average_breakdowns,
                COALESCE(s.broken_match_count, 0) AS broken_match_count,
                COALESCE(s.currently_broken, 0) AS currently_broken,
                epa.total_epa AS statbotics_total_epa,
                epa.auto_epa AS statbotics_auto_epa,
                epa.teleop_epa AS statbotics_teleop_epa,
                epa.endgame_epa AS statbotics_endgame_epa,
                epa.synced_at AS statbotics_synced_at,
                standings.official_rank,
                standings.ranking_points,
                standings.ranking_points_label,
                standings.matches_played AS official_matches_played,
                standings.wins AS official_wins,
                standings.losses AS official_losses,
                standings.ties AS official_ties,
                standings.synced_at AS rankings_synced_at
            FROM teams AS t
            LEFT JOIN stats AS s ON s.team_number = t.team_number
            LEFT JOIN match_results AS mr ON mr.team_number = t.team_number
            LEFT JOIN statbotics_team_epa AS epa
                ON epa.team_number = t.team_number
                AND epa.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            LEFT JOIN event_rankings AS standings
                ON standings.team_number = t.team_number
                AND standings.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            ORDER BY t.team_number
            """
        )

    def team_overview(self, team_number: int) -> sqlite3.Row | None:
        return self._one(
            """
            WITH team_match_results AS (
                SELECT
                    COUNT(m.match_key) AS matches_played,
                    SUM(CASE WHEN m.winner_alliance = mt.alliance THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN m.winner_alliance != 'tie' AND m.winner_alliance != mt.alliance THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN m.winner_alliance = 'tie' THEN 1 ELSE 0 END) AS ties,
                    AVG(CASE WHEN mt.alliance = 'red' THEN m.red_score ELSE m.blue_score END) AS average_actual_points
                FROM match_teams AS mt
                JOIN matches AS m ON m.match_key = mt.match_key
                WHERE mt.team_number = ? AND (m.result_status = 'final' OR m.winner_alliance IS NOT NULL)
            ),
            event_match_results AS (
                SELECT
                    AVG(CASE WHEN mt.alliance = 'red' THEN m.red_score ELSE m.blue_score END) AS event_average_actual_points
                FROM match_teams AS mt
                JOIN matches AS m ON m.match_key = mt.match_key
                WHERE m.result_status = 'final' OR m.winner_alliance IS NOT NULL
            ),
            team_stats AS (
                SELECT
                    COUNT(*) AS observations,
                    AVG(scouted_points) AS average_points,
                    AVG(scouted_points) AS average_scouted_points,
                    AVG(penalties_committed) AS average_penalties_committed,
                    AVG(breakdown_count) AS average_breakdowns,
                    COUNT(DISTINCT CASE WHEN robot_broken = 1 THEN match_key END) AS broken_match_count,
                    MAX(CASE WHEN match_number = latest_match_number THEN robot_broken ELSE 0 END) AS currently_broken
                FROM (
                    SELECT v.*, o.robot_broken, m.match_number,
                        MAX(m.match_number) OVER (PARTITION BY v.team_number) AS latest_match_number
                    FROM v_team_match_totals AS v
                    JOIN observations AS o ON o.observation_id = v.observation_id
                    JOIN matches AS m ON m.match_key = v.match_key
                    WHERE v.team_number = ?
                )
            ),
            event_stats AS (
                SELECT
                    AVG(scouted_points) AS event_average_points,
                    AVG(penalties_committed) AS event_average_penalties_committed,
                    AVG(breakdown_count) AS event_average_breakdowns
                FROM v_team_match_totals
            )
            SELECT
                t.team_number, team_stats.*, event_stats.*, team_match_results.*, event_match_results.*,
                epa.total_epa AS statbotics_total_epa,
                epa.auto_epa AS statbotics_auto_epa,
                epa.teleop_epa AS statbotics_teleop_epa,
                epa.endgame_epa AS statbotics_endgame_epa,
                epa.synced_at AS statbotics_synced_at,
                standings.official_rank,
                standings.ranking_points,
                standings.ranking_points_label,
                standings.matches_played AS official_matches_played,
                standings.wins AS official_wins,
                standings.losses AS official_losses,
                standings.ties AS official_ties,
                standings.synced_at AS rankings_synced_at
            FROM teams AS t
            CROSS JOIN team_stats
            CROSS JOIN event_stats
            CROSS JOIN team_match_results
            CROSS JOIN event_match_results
            LEFT JOIN statbotics_team_epa AS epa
                ON epa.team_number = t.team_number
                AND epa.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            LEFT JOIN event_rankings AS standings
                ON standings.team_number = t.team_number
                AND standings.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            WHERE t.team_number = ?
            """,
            (team_number, team_number, team_number),
        )

    def team_matches(self, team_number: int) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT
                m.match_key, m.match_number, v.scouted_points, v.penalties_committed,
                v.breakdown_count, o.robot_broken, v.schedule_alignment, s.display_name AS scout_name,
                (
                    SELECT summary FROM match_team_summaries AS mts
                    WHERE mts.match_key = m.match_key AND mts.team_number = v.team_number
                    ORDER BY mts.generated_at DESC LIMIT 1
                ) AS match_summary
            FROM v_team_match_totals AS v
            JOIN matches AS m ON m.match_key = v.match_key
            JOIN observations AS o ON o.observation_id = v.observation_id
            JOIN scouts AS s ON s.scout_id = o.scout_id
            WHERE v.team_number = ?
            ORDER BY m.match_number DESC
            """,
            (team_number,),
        )

    def team_events(self, team_number: int) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT se.timestamp_ms, se.points
            FROM score_events AS se
            JOIN observations AS o ON o.observation_id = se.observation_id
            WHERE o.team_number = ? AND se.event_type = 'score'
            ORDER BY se.timestamp_ms
            """,
            (team_number,),
        )

    def event_events(self) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT o.team_number, se.timestamp_ms, se.points
            FROM score_events AS se
            JOIN observations AS o ON o.observation_id = se.observation_id
            WHERE se.event_type = 'score'
            ORDER BY se.timestamp_ms, o.team_number, se.sequence_number
            """
        )

    def observation_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS count FROM observations")
        return 0 if row is None else int(row["count"])

    def latest_team_summary(self, team_number: int) -> str | None:
        row = self._one(
            "SELECT summary FROM team_summaries WHERE team_number = ? ORDER BY generated_at DESC LIMIT 1",
            (team_number,),
        )
        return None if row is None else str(row["summary"])

    def latest_team_summaries(self) -> dict[int, str]:
        """Latest summary per team in one query, for rendering a whole team list."""
        rows = self._all(
            """
            SELECT team_number, summary
            FROM team_summaries AS ts
            WHERE generated_at = (
                SELECT MAX(generated_at) FROM team_summaries
                WHERE team_number = ts.team_number
            )
            GROUP BY team_number
            """
        )
        return {int(row["team_number"]): str(row["summary"]) for row in rows}

    def pick_lists_for_scout(self, scout_id: str) -> list[sqlite3.Row]:
        event_key = self.event_key()
        if event_key is None:
            return []
        return self._all(
            """
            SELECT pl.list_id, pl.name, pl.created_at, pl.updated_at,
                   COUNT(plt.team_number) AS team_count
            FROM pick_lists AS pl
            LEFT JOIN pick_list_teams AS plt ON plt.list_id = pl.list_id
            WHERE pl.event_key = ? AND pl.scout_id = ?
            GROUP BY pl.list_id
            ORDER BY pl.updated_at DESC, pl.name COLLATE NOCASE
            """,
            (event_key, scout_id),
        )

    def pick_list_teams(self, list_id: str) -> list[sqlite3.Row]:
        return self._all(
            """
            WITH event_notes AS (
                SELECT o.team_number, GROUP_CONCAT(n.text, ' | ') AS scout_notes
                FROM notes AS n
                JOIN observations AS o ON o.observation_id = n.observation_id
                JOIN matches AS m ON m.match_key = o.match_key
                WHERE m.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
                GROUP BY o.team_number
            )
            SELECT plt.team_number, plt.added_at,
                   epa.total_epa AS statbotics_total_epa,
                   standings.official_rank,
                   event_notes.scout_notes,
                   selection.alliance_number AS selected_alliance,
                   selection.selection_kind AS selection_kind
            FROM pick_list_teams AS plt
            LEFT JOIN statbotics_team_epa AS epa
                ON epa.team_number = plt.team_number
                AND epa.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            LEFT JOIN event_rankings AS standings
                ON standings.team_number = plt.team_number
                AND standings.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            LEFT JOIN event_notes ON event_notes.team_number = plt.team_number
            LEFT JOIN alliance_selections AS selection
                ON selection.team_number = plt.team_number
                AND selection.event_key = (SELECT event_key FROM events ORDER BY event_key LIMIT 1)
            WHERE plt.list_id = ?
            ORDER BY plt.added_at DESC, plt.team_number
            """,
            (list_id,),
        )

    def event_team_notes(self) -> dict[int, str]:
        event_key = self.event_key()
        if event_key is None:
            return {}
        rows = self._all(
            """
            SELECT o.team_number, GROUP_CONCAT(n.text, ' | ') AS scout_notes
            FROM notes AS n
            JOIN observations AS o ON o.observation_id = n.observation_id
            JOIN matches AS m ON m.match_key = o.match_key
            WHERE m.event_key = ?
            GROUP BY o.team_number
            """,
            (event_key,),
        )
        return {int(row["team_number"]): str(row["scout_notes"]) for row in rows if row["scout_notes"]}

    def alliance_selections(self) -> list[sqlite3.Row]:
        event_key = self.event_key()
        if event_key is None:
            return []
        return self._all(
            """
            SELECT alliance_number, team_number, selection_kind, selected_at
            FROM alliance_selections
            WHERE event_key = ?
            ORDER BY alliance_number, CASE selection_kind WHEN 'captain' THEN 0 ELSE 1 END, selected_at
            """,
            (event_key,),
        )

    def picking_teams(self) -> list[sqlite3.Row]:
        """Event teams ranked for alliance selection, with a selected-state marker."""
        event_key = self.event_key()
        if event_key is None:
            return []
        return self._all(
            """
            SELECT t.team_number, standings.official_rank, epa.total_epa AS statbotics_total_epa,
                   selection.alliance_number AS selected_alliance,
                   selection.selection_kind AS selection_kind
            FROM teams AS t
            JOIN (
                SELECT DISTINCT mt.team_number
                FROM match_teams AS mt
                JOIN matches AS m ON m.match_key = mt.match_key
                WHERE m.event_key = ?
            ) AS event_teams ON event_teams.team_number = t.team_number
            LEFT JOIN event_rankings AS standings
                ON standings.event_key = ? AND standings.team_number = t.team_number
            LEFT JOIN statbotics_team_epa AS epa
                ON epa.event_key = ? AND epa.team_number = t.team_number
            LEFT JOIN alliance_selections AS selection
                ON selection.event_key = ? AND selection.team_number = t.team_number
            ORDER BY standings.official_rank IS NULL, standings.official_rank, t.team_number
            """,
            (event_key, event_key, event_key, event_key),
        )

    def scouts(self) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT scout_id, display_name, matches_scouted, average_notes_per_match,
                   predictions_with_result, correct_predictions, prediction_accuracy
            FROM v_scout_quality
            ORDER BY matches_scouted DESC, display_name COLLATE NOCASE
            """
        )

    def scout_overview(self, scout_id: str) -> sqlite3.Row | None:
        return self._one(
            """
            SELECT scout_id, display_name, matches_scouted, average_notes_per_match,
                   predictions_with_result, correct_predictions, prediction_accuracy
            FROM v_scout_quality
            WHERE scout_id = ?
            """,
            (scout_id,),
        )

    def scout_matches(self, scout_id: str) -> list[sqlite3.Row]:
        return self._all(
            """
            WITH note_stats AS (
                SELECT observation_id, COUNT(*) AS note_count
                FROM notes
                GROUP BY observation_id
            )
            SELECT
                m.match_key, m.match_number, o.team_number, o.predicted_winner,
                m.winner_alliance, v.scouted_points, COALESCE(note_stats.note_count, 0) AS note_count
            FROM observations AS o
            JOIN matches AS m ON m.match_key = o.match_key
            JOIN v_team_match_totals AS v ON v.observation_id = o.observation_id
            LEFT JOIN note_stats ON note_stats.observation_id = o.observation_id
            WHERE o.scout_id = ?
            ORDER BY m.match_number DESC
            """,
            (scout_id,),
        )

    def _all(self, query: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(query, tuple(parameters)).fetchall())

    def _one(self, query: str, parameters: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.connection.execute(query, tuple(parameters)).fetchone()


class Card(QFrame):
    """A styled panel.

    ``QGraphicsDropShadowEffect`` forces Qt to composite the whole widget through
    an offscreen buffer, which gets expensive fast once dozens of instances are
    on screen at once (team/compact-table lists). Keep it for the small number
    of "hero" cards per page and skip it for anything rendered in bulk.
    """

    def __init__(self, parent: QWidget | None = None, *, metric: bool = False, shadow: bool = True):
        super().__init__(parent)
        self.setObjectName("metricCard" if metric else "card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        if shadow:
            effect = QGraphicsDropShadowEffect(self)
            effect.setBlurRadius(18)
            effect.setOffset(0, 4)
            effect.setColor(QColor(0, 0, 0, 60))
            self.setGraphicsEffect(effect)


class ClickableCard(Card):
    """A card that opens its detail view from any unused part of the card."""

    def __init__(
        self,
        callback: Callable[[], None],
        parent: QWidget | None = None,
        *,
        tooltip: str = "Open details",
        shadow: bool = True,
    ):
        super().__init__(parent, shadow=shadow)
        self._callback = callback
        self.setObjectName("clickableCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tooltip)

    def mouseReleaseEvent(self, event: Any) -> None:  # Qt invokes this method.
        if event.button() == Qt.MouseButton.LeftButton:
            self._callback()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class TbaResultWorker(QThread):
    """Keeps the network request off the Qt event loop."""

    completed = Signal(int)
    failed = Signal(str)

    def __init__(self, database_path: Path, event_key: str, api_key: str):
        super().__init__()
        self.database_path = database_path
        self.event_key = event_key
        self.api_key = api_key

    def run(self) -> None:
        try:
            updated_matches = sync_official_results_from_tba(
                self.database_path, self.event_key, self.api_key
            )
        except ResultSyncError as error:
            self.failed.emit(str(error))
        except Exception as error:  # Ensure unexpected network/SQLite errors reach the UI.
            self.failed.emit(f"Could not sync official results: {error}")
        else:
            self.completed.emit(updated_matches)
        finally:
            # The prompt key is only needed while this one request is in flight.
            self.api_key = ""


class StatboticsEpaWorker(QThread):
    """Keeps the public Statbotics EPA sync off the Qt event loop."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, database_path: Path, event_key: str):
        super().__init__()
        self.database_path = database_path
        self.event_key = event_key

    def run(self) -> None:
        try:
            report = sync_statbotics_epa(self.database_path, self.event_key)
        except StatboticsSyncError as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(f"Could not sync Statbotics EPA: {error}")
        else:
            self.completed.emit(report)


class GeminiEnrichmentWorker(QThread):
    """Runs provider-neutral summary generation outside the Qt event loop."""

    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, database_path: Path, provider: OpenAICompatibleConfig):
        super().__init__()
        self.database_path = database_path
        self.provider = provider

    def run(self) -> None:
        try:
            report = enrich_database(
                self.database_path,
                self.provider,
                progress_callback=lambda completed, total, message: self.progress.emit(
                    completed, total, message
                ),
            )
        except GeminiEnrichmentError as error:
            self.failed.emit(str(error))
        except Exception as error:  # Keep unexpected API/SQLite failures in the UI.
            self.failed.emit(f"Could not generate AI summaries: {error}")
        else:
            self.completed.emit(report)
        finally:
            self.provider = None  # type: ignore[assignment]


class GeminiSearchWorker(QThread):
    """Embeds a query without freezing the teams tab."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        database_path: Path,
        provider: OpenAICompatibleConfig,
        query: str,
    ):
        super().__init__()
        self.database_path = database_path
        self.provider = provider
        self.query = query

    def run(self) -> None:
        try:
            results = search_teams(self.database_path, self.provider, self.query)
        except GeminiEnrichmentError as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(f"Could not search AI summaries: {error}")
        else:
            self.completed.emit(results)
        finally:
            self.provider = None  # type: ignore[assignment]


class SmoothChart(QWidget):
    """A dependency-free, smoothed scoring-rate chart painted with Qt."""

    def __init__(
        self,
        events: list[sqlite3.Row],
        divisor: int = 1,
        include_average: bool = False,
        comparison_events: list[sqlite3.Row] | None = None,
        comparison_divisor: int = 1,
        comparison_label: str = "Event avg.",
        compact: bool = False,
        primary_color: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.events = events
        self.divisor = max(1, divisor)
        self.include_average = include_average
        self.comparison_events = comparison_events or []
        self.comparison_divisor = max(1, comparison_divisor)
        self.comparison_label = comparison_label
        self.compact = compact
        self.primary_color = primary_color
        self.setMinimumHeight(108 if compact else 235)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())

    def paintEvent(self, _: object) -> None:  # Qt invokes this method.
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(SURFACE))
        width, height = self.width(), self.height()
        left, right, top, bottom = (14, 12, 12, 22) if self.compact else (48, 18, 34, 30)
        if not self.events and not self.comparison_events:
            painter.setPen(QColor(MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No scoring events recorded")
            return

        by_team: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for event in self.events:
            team = str(event["team_number"]) if "team_number" in event.keys() else "Team"
            by_team[team].append((int(event["timestamp_ms"]), int(event["points"])))
        comparison_values = [
            (int(event["timestamp_ms"]), int(event["points"])) for event in self.comparison_events
        ]
        all_times = [time for values in by_team.values() for time, _ in values]
        all_times.extend(time for time, _ in comparison_values)
        end_time = max(150_000, max(all_times))
        samples = [round(index * end_time / 40) for index in range(41)]
        smoothed: dict[str, list[float]] = {}
        for team, team_events in by_team.items():
            smoothed[team] = [
                sum(points * math.exp(-0.5 * ((time - sample) / 10_000) ** 2) for time, points in team_events)
                / self.divisor
                for sample in samples
            ]
        comparison_smoothed = [
            sum(points * math.exp(-0.5 * ((time - sample) / 10_000) ** 2) for time, points in comparison_values)
            / self.comparison_divisor
            for sample in samples
        ] if comparison_values else []
        all_value_sets = [*smoothed.values(), comparison_smoothed]
        maximum = max((max(values) for values in all_value_sets if values), default=0) or 1
        plot_width = max(1, width - left - right)
        plot_height = max(1, height - top - bottom)

        grid_pen = QPen(QColor("#2a3447"), 1)
        axis_pen = QPen(QColor(MUTED), 1)
        painter.setFont(QFont("Arial", 8))
        for fraction in (0.0, 0.5, 1.0):
            y = top + plot_height * (1 - fraction)
            painter.setPen(grid_pen)
            painter.drawLine(left, round(y), width - right, round(y))
            if not self.compact:
                painter.setPen(QColor(MUTED))
                painter.drawText(2, round(y) + 4, f"{maximum * fraction:.1f}")
            x = left + plot_width * fraction
            painter.setPen(QColor(MUTED))
            painter.drawText(round(x) - 12, height - 8, f"{end_time * fraction / 1000:.0f}s")
        painter.setPen(axis_pen)
        if not self.compact:
            painter.drawLine(left, top, left, height - bottom)
        painter.drawLine(left, height - bottom, width - right, height - bottom)

        painter.setFont(QFont("Arial", 8 if self.compact else 9, QFont.Weight.Bold))
        for index, (team, values) in enumerate(sorted(smoothed.items())):
            color = QColor(self.primary_color or CHART_COLORS[index % len(CHART_COLORS)])
            path = QPainterPath()
            for position, value in enumerate(values):
                x = left + plot_width * position / (len(values) - 1)
                y = top + plot_height * (1 - value / maximum)
                if position == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.setPen(QPen(color, 2.4))
            painter.drawPath(path)
            if not self.compact:
                painter.drawText(left + index * 94, 18, team)

        if self.include_average:
            average_values = [
                sum(team_values[index] for team_values in smoothed.values()) / len(smoothed)
                for index in range(len(samples))
            ]
            average_path = QPainterPath()
            for position, value in enumerate(average_values):
                x = left + plot_width * position / (len(average_values) - 1)
                y = top + plot_height * (1 - value / maximum)
                if position == 0:
                    average_path.moveTo(x, y)
                else:
                    average_path.lineTo(x, y)
            painter.setPen(QPen(QColor("#e8edf7"), 2.4, Qt.PenStyle.DashLine))
            painter.drawPath(average_path)
            painter.setPen(QColor("#e8edf7"))
            painter.drawText(width - right - 86, 18, "Match avg.")

        if comparison_smoothed:
            comparison_path = QPainterPath()
            for position, value in enumerate(comparison_smoothed):
                x = left + plot_width * position / (len(comparison_smoothed) - 1)
                y = top + plot_height * (1 - value / maximum)
                if position == 0:
                    comparison_path.moveTo(x, y)
                else:
                    comparison_path.lineTo(x, y)
            painter.setPen(QPen(QColor("#e8edf7"), 2.4, Qt.PenStyle.DashLine))
            painter.drawPath(comparison_path)
            if not self.compact:
                painter.setPen(QColor("#e8edf7"))
                painter.drawText(width - right - 84, 18, self.comparison_label)


def label(text: str, *, name: str | None = None, word_wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(word_wrap)
    return widget


def button(text: str, style: str, callback: Callable[[], None]) -> QPushButton:
    widget = QPushButton(text)
    widget.setObjectName(style)
    widget.clicked.connect(callback)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    body.setObjectName("root")
    layout = QVBoxLayout(body)
    layout.setContentsMargins(6, 6, 16, 24)
    layout.setSpacing(10)
    scroll.setWidget(body)
    return scroll, layout


class AnalyticsWindow(QMainWindow):
    def __init__(self, repository: AnalyticsRepository):
        super().__init__()
        self.repository = repository
        self._result_sync_worker: TbaResultWorker | None = None
        self._result_sync_button: QPushButton | None = None
        self._result_sync_was_automatic = False
        self._picking_auto_sync_attempted_event_key = ""
        self._statbotics_sync_worker: StatboticsEpaWorker | None = None
        self._statbotics_sync_button: QPushButton | None = None
        self._gemini_enrichment_worker: GeminiEnrichmentWorker | None = None
        self._gemini_enrichment_button: QPushButton | None = None
        self._gemini_enrichment_progress: QProgressBar | None = None
        self._gemini_enrichment_status: QLabel | None = None
        self._gemini_enrichment_progress_state = (0, 0, "")
        self._gemini_search_worker: GeminiSearchWorker | None = None
        self.personal_config = load_personal_config()
        self._team_search_query = ""
        self._team_search_results: list[TeamSearchResult] | None = None
        self._team_search_button: QPushButton | None = None
        self._team_search_context = "teams"
        self._picking_research_query = ""
        self._picking_research_results: list[TeamSearchResult] | None = None
        self._picking_research_sort = "rank"
        self._picking_research_tab_index = 0
        self._picking_research_available_only = False
        self._match_view_mode = "cards"
        self._team_view_mode = "cards"
        self._pick_list_scout_id = ""
        self._team_sort_by = "team_number"  # "team_number", "relevance", "epa", "win_ratio", "avg_actual_points", "avg_scouted_points", "avg_penalties"
        self._team_sort_order = "asc"        # "asc", "desc"
        self._team_filter_status = "all"      # "all", "scouted", "unscouted"
        self._team_filter_min_obs = 0        # 0, 1, 3, 5
        self._team_number_query = ""
        self.setWindowTitle("Analytics")
        self.setMinimumSize(980, 680)
        self.resize(1240, 820)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 18, 28, 18)
        layout.setSpacing(12)

        header_bar = QFrame()
        header_bar.setObjectName("headerBar")
        header = QHBoxLayout(header_bar)
        header.setContentsMargins(0, 0, 0, 16)
        header.setSpacing(0)
        brand = QVBoxLayout()
        brand.setSpacing(1)
        brand.addWidget(label("SpeechScout", name="brandTitle"))
        brand.addWidget(label("Scouting analytics", name="brandSubtitle"))
        header.addLayout(brand)
        header.addStretch(1)
        layout.addWidget(header_bar)

        self.tabs = QTabWidget()
        self.matches_stack = QStackedWidget()
        self.teams_stack = QStackedWidget()
        self.scouts_stack = QStackedWidget()
        self.picking_stack = QStackedWidget()
        self.pick_lists_stack = QStackedWidget()
        self.tabs.addTab(self.matches_stack, "Matches")
        self.tabs.addTab(self.teams_stack, "Teams")
        self.tabs.addTab(self.scouts_stack, "Scouts")
        self.tabs.addTab(self.picking_stack, "Picking")
        self.tabs.addTab(self.pick_lists_stack, "Pick Lists")
        layout.addWidget(self.tabs, 1)

        self.show_matches()
        self.show_teams()
        self.show_scouts()
        self.show_picking()
        self.show_pick_lists()

    def show_matches(self) -> None:
        page, layout = scroll_page()
        self._page_header(
            layout,
            "Matches",
            "Newest qualification match first. Scouted points are team contributions, not official alliance scores.",
        )
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self._result_sync_button = button(
            "Sync official results from TBA", "primary", self._request_tba_results
        )
        actions.addWidget(self._result_sync_button)
        self._statbotics_sync_button = button(
            "Sync Statbotics EPA", "secondary", self._request_statbotics_epa
        )
        self._statbotics_sync_button.setToolTip(
            "Cache Statbotics' event EPA estimates for the teams at this event."
        )
        actions.addWidget(self._statbotics_sync_button)
        self._gemini_enrichment_button = button(
            "Generate AI summaries", "secondary", self._request_gemini_enrichment
        )
        actions.addWidget(self._gemini_enrichment_button)
        actions.addStretch(1)
        actions.addWidget(
            button(
                "Card view",
                "primary" if self._match_view_mode == "cards" else "secondary",
                lambda _checked=False: self._set_match_view_mode("cards"),
            )
        )
        actions.addWidget(
            button(
                "Compact view",
                "primary" if self._match_view_mode == "compact" else "secondary",
                lambda _checked=False: self._set_match_view_mode("compact"),
            )
        )
        layout.addLayout(actions)

        progress_area = QWidget()
        progress_layout = QVBoxLayout(progress_area)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(3)
        self._gemini_enrichment_progress = QProgressBar()
        self._gemini_enrichment_progress.setObjectName("aiEnrichmentProgress")
        self._gemini_enrichment_progress.setTextVisible(True)
        progress_layout.addWidget(self._gemini_enrichment_progress)
        self._gemini_enrichment_status = label("", name="muted")
        self._gemini_enrichment_status.setWordWrap(True)
        progress_layout.addWidget(self._gemini_enrichment_status)
        layout.addWidget(progress_area)
        self._render_gemini_enrichment_progress()

        matches = self.repository.matches()
        if not matches:
            self._empty_state(layout, "No schedule has been imported yet.")
        elif self._match_view_mode == "compact":
            layout.addWidget(self._compact_match_table(matches))
        else:
            for match in matches:
                layout.addWidget(self._match_card(match))
        layout.addStretch(1)
        self._replace_stack(self.matches_stack, page)

    def _set_match_view_mode(self, mode: str) -> None:
        if mode != self._match_view_mode:
            self._match_view_mode = mode
            self.show_matches()

    def _compact_match_table(self, matches: list[sqlite3.Row]) -> QWidget:
        table = Card()
        layout = QVBoxLayout(table)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        headers = ("MATCH", "RED ALLIANCE", "BLUE ALLIANCE", "OFFICIAL / STATUS", "SCOUTED")
        header = QGridLayout()
        header.setContentsMargins(9, 3, 9, 3)
        for column, text in enumerate(headers):
            header.addWidget(label(text, name="eyebrow"), 0, column)
            header.setColumnStretch(column, 1 if column in {0, 3, 4} else 2)
        layout.addLayout(header)
        for match in matches:
            row = ClickableCard(
                lambda key=match["match_key"]: self.show_match_detail(key),
                tooltip="Open match details",
                shadow=False,
            )
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(9, 8, 9, 8)
            row_layout.setHorizontalSpacing(10)
            values = (
                f"Match {match['match_number']}",
                str(match["red_teams"]),
                str(match["blue_teams"]),
                self._match_status(match),
                f"{int(match['observation_count'])} obs",
            )
            for column, value in enumerate(values):
                cell = label(value, name="sectionTitle" if column == 0 else "muted", word_wrap=True)
                if column == 3 and match["winner_alliance"] in ("red", "blue"):
                    color = RED if match["winner_alliance"] == "red" else BLUE
                    cell.setStyleSheet(f"color: {color}; font-weight: 700;")
                row_layout.addWidget(cell, 0, column)
                row_layout.setColumnStretch(column, 1 if column in {0, 3, 4} else 2)
            layout.addWidget(row)
        return table

    def _match_card(self, match: sqlite3.Row) -> QWidget:
        card = ClickableCard(
            lambda key=match["match_key"]: self.show_match_detail(key),
            tooltip="Open match details",
        )
        layout = QHBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(20)
        # Every direct child is vertically centered.  This keeps the title and
        # teams aligned with the middle of the taller score cards.
        match_title = label(f"Match {match['match_number']}", name="sectionTitle")
        layout.addWidget(match_title, 1, Qt.AlignmentFlag.AlignVCenter)

        red_alliance = QWidget()
        red_alliance.setLayout(self._alliance_row("Red", match["red_teams"], RED))
        layout.addWidget(red_alliance, 2, Qt.AlignmentFlag.AlignVCenter)

        # Keep the scoring-period values stacked *inside* each panel while
        # placing the two alliance cards beside one another for comparison.
        score_area = QWidget()
        scores = QHBoxLayout(score_area)
        scores.setContentsMargins(0, 0, 0, 0)
        scores.setSpacing(8)
        red_score = self._inline_score_breakdown(match, "red")
        blue_score = self._inline_score_breakdown(match, "blue", total_first=True)
        if red_score is not None and blue_score is not None:
            scores.addWidget(red_score)
            scores.addWidget(blue_score)
        else:
            scores.addWidget(label("Official score pending", name="muted"))
        layout.addWidget(score_area, 2, Qt.AlignmentFlag.AlignVCenter)

        blue_alliance = QWidget()
        blue_alliance.setLayout(self._alliance_row("Blue", match["blue_teams"], BLUE))
        layout.addWidget(blue_alliance, 2, Qt.AlignmentFlag.AlignVCenter)
        return card

    def _inline_score_breakdown(
        self, match: sqlite3.Row, alliance: str, *, total_first: bool = False
    ) -> QWidget | None:
        if (
            match["result_status"] != "final"
            or match["red_score"] is None
            or match["blue_score"] is None
        ):
            return None
        winner = str(match["winner_alliance"] or "")
        is_winner = winner == alliance
        panel_colors = {
            "blue": ("#283746", "#4b5c6d", "#0e66d5", "#79b7ff"),
            "red": ("#3e3337", "#635055", "#b52849", "#ff8fa7"),
        }
        muted_background, muted_border, winner_background, winner_border = panel_colors[alliance]
        background, border = (
            (winner_background, winner_border) if is_winner else (muted_background, muted_border)
        )
        text_color = "#ffffff" if is_winner else "#c6ccd5"
        panel = QFrame()
        panel.setObjectName("inlineOfficialScore")
        # A fixed footprint keeps every match row's two score cards aligned,
        # regardless of whether the official score has two or three digits.
        panel.setFixedWidth(280)
        panel.setMinimumHeight(72)
        panel.setToolTip("Official TBA score breakdown")
        panel.setStyleSheet(
            "QFrame#inlineOfficialScore "
            f"{{ background: {background}; border: 1px solid {border}; border-radius: 7px; }}"
        )
        panel_layout = QHBoxLayout(panel)
        panel_layout.setContentsMargins(11, 7, 12, 7)
        panel_layout.setSpacing(14)
        if self._has_official_breakdown(match):
            breakdown = QGridLayout()
            breakdown.setContentsMargins(0, 0, 0, 0)
            breakdown.setHorizontalSpacing(12)
            breakdown.setVerticalSpacing(1)
            # Penalties remain on the full match card; the overview intentionally
            # stays to the three scoring periods.
            for row, (period, value) in enumerate(
                self._official_components_for_match(match, alliance)[:3]
            ):
                period_label = label(period)
                period_label.setStyleSheet(
                    f"color: {text_color}; font-size: 11px; font-weight: 600;"
                )
                value_label = label(official_number(value))
                value_label.setStyleSheet(
                    f"color: {text_color}; font-size: 15px; font-weight: 700;"
                )
                value_label.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                breakdown.addWidget(period_label, row, 0)
                breakdown.addWidget(value_label, row, 1)
        else:
            breakdown = None
        total = label(str(match[f"{alliance}_score"]))
        total.setStyleSheet(f"color: {text_color}; font-size: 28px; font-weight: 800;")
        total.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if total_first:
            panel_layout.addWidget(total)
            if breakdown is not None:
                panel_layout.addLayout(breakdown)
        else:
            if breakdown is not None:
                panel_layout.addLayout(breakdown)
            panel_layout.addWidget(total)
        return panel

    def _alliance_row(
        self,
        alliance: str,
        teams: str | None,
        color: str,
        score_breakdown: QWidget | None = None,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        alliance_label = label(alliance)
        alliance_label.setStyleSheet(f"color: {color}; font-weight: 700;")
        alliance_label.setFixedWidth(42)
        row.addWidget(alliance_label)
        numbers = [number.strip() for number in (teams or "").split(",") if number.strip()]
        if not numbers:
            row.addWidget(label("No schedule teams", name="muted"))
        for number in numbers:
            row.addWidget(button(number, "link", lambda _checked=False, team=int(number): self.show_team_detail(team)))
        if score_breakdown is not None:
            row.addSpacing(8)
            row.addWidget(score_breakdown)
        row.addStretch(1)
        return row

    def _official_scoreboard(self, match: sqlite3.Row) -> QWidget | None:
        """Render only official TBA result fields; scouting data is never used here."""
        if (
            match["result_status"] != "final"
            or match["red_score"] is None
            or match["blue_score"] is None
        ):
            return None

        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 15, 18, 16)
        card_layout.setSpacing(10)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(label("Official result", name="sectionTitle"))
        heading.addStretch(1)
        heading.addWidget(label("TBA", name="eyebrow"))
        card_layout.addLayout(heading)

        winner = str(match["winner_alliance"] or "")
        has_breakdown = self._has_official_breakdown(match)
        score_row = QHBoxLayout()
        score_row.setContentsMargins(0, 0, 0, 0)
        score_row.setSpacing(10)
        score_row.addWidget(
            self._official_alliance_panel(
                "red",
                self._official_components_for_match(match, "red"),
                int(match["red_score"]),
                winner == "red",
                has_breakdown,
            ),
            1,
        )
        score_row.addWidget(
            self._official_alliance_panel(
                "blue",
                self._official_components_for_match(match, "blue"),
                int(match["blue_score"]),
                winner == "blue",
                has_breakdown,
            ),
            1,
        )
        card_layout.addLayout(score_row)
        return card

    def _official_components_for_match(
        self, match: sqlite3.Row, alliance: str
    ) -> tuple[tuple[str, Any], ...]:
        return (
            ("Auto", match[f"{alliance}_auto_points"]),
            ("Teleop", match[f"{alliance}_teleop_points"]),
            ("Endgame", match[f"{alliance}_endgame_points"]),
            ("Penalties", match[f"{alliance}_penalty_points"]),
        )

    def _has_official_breakdown(self, match: sqlite3.Row) -> bool:
        return any(
            match[column] is not None
            for alliance in ("blue", "red")
            for column in (
                f"{alliance}_auto_points",
                f"{alliance}_teleop_points",
                f"{alliance}_endgame_points",
                f"{alliance}_penalty_points",
            )
        )

    def _official_alliance_panel(
        self,
        alliance: str,
        components: tuple[tuple[str, Any], ...],
        total: int,
        is_winner: bool,
        has_breakdown: bool,
        compact: bool = False,
    ) -> QFrame:
        """A single opaque official-score panel for an alliance."""
        panel_colors = {
            "blue": ("#283746", "#4b5c6d", "#0e66d5", "#79b7ff"),
            "red": ("#3e3337", "#635055", "#b52849", "#ff8fa7"),
        }
        muted_background, muted_border, winner_background, winner_border = panel_colors[alliance]
        background, border = (
            (winner_background, winner_border) if is_winner else (muted_background, muted_border)
        )
        text_color = "#ffffff" if is_winner else "#c6ccd5"

        panel = QFrame()
        panel.setObjectName("officialAlliancePanel")
        panel.setMinimumHeight(102 if compact else 146)
        panel.setToolTip("Official TBA score breakdown")
        panel.setStyleSheet(
            "QFrame#officialAlliancePanel "
            f"{{ background: {background}; border: 1px solid {border}; border-radius: 12px; }}"
        )
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(14 if compact else 20, 9 if compact else 14, 16 if compact else 22, 9 if compact else 14)
        layout.setSpacing(10 if compact else 20)
        breakdown = QGridLayout()
        breakdown.setContentsMargins(0, 0, 0, 0)
        breakdown.setHorizontalSpacing(12 if compact else 22)
        breakdown.setVerticalSpacing(1 if compact else 4)
        if has_breakdown:
            for row, (period, value) in enumerate(components):
                period_label = label(period)
                period_label.setStyleSheet(
                    f"color: {text_color}; font-size: {11 if compact else 13}px; font-weight: 600;"
                )
                value_label = label(official_number(value), name="officialComponent")
                value_label.setStyleSheet(
                    f"color: {text_color}; font-size: {16 if compact else 20}px; font-weight: 700;"
                )
                value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                breakdown.addWidget(period_label, row, 0)
                breakdown.addWidget(value_label, row, 1)

        total_label = label(str(total), name="officialTotal")
        total_label.setStyleSheet(
            f"color: {text_color}; font-size: {38 if compact else 60}px; font-weight: 800;"
        )
        total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if alliance == "red":
            layout.addLayout(breakdown, 1) if has_breakdown else layout.addStretch(1)
            layout.addWidget(total_label, 1)
        else:
            layout.addWidget(total_label, 1)
            layout.addLayout(breakdown, 1) if has_breakdown else layout.addStretch(1)
        return panel

    def show_match_detail(self, match_key: str) -> None:
        match = self.repository.match(match_key)
        if match is None:
            self.show_matches()
            return
        page, layout = scroll_page()
        layout.addWidget(button("←  Back to matches", "secondary", self.show_matches), alignment=Qt.AlignmentFlag.AlignLeft)
        self._page_header(layout, f"Match {match['match_number']}", "Team contributions, score timing, and the notes recorded for this match.")
        official_scoreboard = self._official_scoreboard(match)
        if official_scoreboard is not None:
            layout.addWidget(official_scoreboard)
        scouts_by_team = self.repository.match_team_scouts(match_key)
        for team in self.repository.match_teams(match_key):
            card = Card()
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(18, 15, 18, 15)
            color = RED if team["alliance"] == "red" else BLUE
            left = QVBoxLayout()
            station = label(f"{team['alliance'].title()} {team['station']} • Team {team['team_number']}", name="sectionTitle")
            station.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: 700;")
            left.addWidget(station)
            left.addWidget(label(f"{team['observation_count']} observation(s) • {team['penalties_committed']} penalties committed • {team['breakdown_count']} breakdowns", name="muted"))
            summary = self.repository.latest_match_team_summary(
                match_key, int(team["team_number"])
            )
            if summary:
                left.addWidget(label(summary, name="muted", word_wrap=True))
            team_scouts = scouts_by_team.get(int(team["team_number"]), [])
            if team_scouts:
                scout_links = QHBoxLayout()
                scout_links.setContentsMargins(0, 0, 0, 0)
                scout_links.setSpacing(5)
                scout_links.addWidget(label("Scouted by", name="muted"))
                for scout in team_scouts:
                    scout_links.addWidget(
                        button(
                            str(scout["display_name"]),
                            "link",
                            lambda _checked=False, scout_id=str(scout["scout_id"]), name=str(scout["display_name"]): self.show_scout_detail(scout_id, name),
                        )
                    )
                scout_links.addStretch(1)
                left.addLayout(scout_links)
            card_layout.addLayout(left, 1)
            points = label(f"{team['scouted_points']} pts", name="metric")
            card_layout.addWidget(points)
            card_layout.addWidget(
                button(
                    "Add to pick list",
                    "secondary",
                    lambda _checked=False, number=int(team["team_number"]): self._add_team_to_pick_list(number),
                )
            )
            card_layout.addWidget(button("Team details", "secondary", lambda _checked=False, number=team["team_number"]: self.show_team_detail(number)))
            layout.addWidget(card)

        unmatched = self.repository.unmatched_match_observations(match_key)
        if unmatched:
            card = Card()
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(18, 15, 18, 15)
            heading = label("Imported records not on this cached schedule", name="sectionTitle")
            heading.setStyleSheet(f"color: {WARNING}; font-size: 15px; font-weight: 700;")
            card_layout.addWidget(heading)
            for observation in unmatched:
                observation_row = QHBoxLayout()
                observation_row.setContentsMargins(0, 0, 0, 0)
                observation_row.addWidget(
                    label(
                        f"Team {observation['team_number']} • {observation['scouted_points']} pts • "
                        f"{observation['penalties_committed']} penalties committed • "
                        f"{observation['breakdown_count']} breakdowns • scouted by",
                        name="muted",
                    )
                )
                observation_row.addWidget(
                    button(
                        str(observation["scout_name"]),
                        "link",
                        lambda _checked=False, scout_id=str(observation["scout_id"]), name=str(observation["scout_name"]): self.show_scout_detail(scout_id, name),
                    )
                )
                observation_row.addStretch(1)
                card_layout.addLayout(observation_row)
            layout.addWidget(card)

        layout.addWidget(self._chart_card("Smoothed scoring rate", self.repository.match_events(match_key), include_average=True))
        level_totals = self.repository.match_level_totals(match_key)
        if level_totals:
            text = "  •  ".join(
                f"Team {item['team_number']}: {item['label']} × {item['count']} ({item['points']} pts)"
                for item in level_totals
            )
            layout.addWidget(self._text_card("Scoring by level", text))
        notes = self.repository.match_notes(match_key)
        notes_card = Card()
        notes_layout = QVBoxLayout(notes_card)
        notes_layout.setContentsMargins(18, 15, 18, 15)
        notes_layout.addWidget(label("Scout notes", name="sectionTitle"))
        if notes:
            for note in notes:
                notes_layout.addWidget(label(f"{note['timestamp_ms'] / 1000:.1f}s  •  Team {note['team_number']}  •  {note['scout_name']}: {note['text']}", name="muted", word_wrap=True))
        else:
            notes_layout.addWidget(label("No notes were recorded for this match.", name="muted"))
        layout.addWidget(notes_card)
        layout.addStretch(1)
        self._replace_stack(self.matches_stack, page)
        self.tabs.setCurrentWidget(self.matches_stack)

    def _add_team_to_pick_list(self, team_number: int) -> None:
        event_key = self.repository.event_key()
        if event_key is None:
            QMessageBox.warning(
                self,
                "Pick lists",
                "Import an event before creating a pick list.",
            )
            return
        scout = self._configured_pick_list_scout()
        if scout is None:
            return
        scout_id = str(scout["scout_id"])
        scout_name = str(scout["display_name"])
        existing_lists = self.repository.pick_lists_for_scout(scout_id)
        choices = ["Create a new pick list…"] + [str(item["name"]) for item in existing_lists]
        selected_list, accepted = QInputDialog.getItem(
            self,
            "Add to pick list",
            "Pick list:",
            choices,
            0,
            False,
        )
        if not accepted:
            return
        if selected_list == choices[0]:
            list_name, accepted = QInputDialog.getText(
                self,
                "New pick list",
                "Name this pick list:",
            )
            if not accepted or not list_name.strip():
                return
        else:
            list_name = selected_list
        try:
            result = add_team_to_pick_list(
                self.repository.database_path,
                event_key,
                scout_id,
                list_name,
                team_number,
            )
        except PickListError as error:
            QMessageBox.warning(self, "Could not update pick list", str(error))
            return
        self._refresh_repository()
        self._pick_list_scout_id = scout_id
        self.show_pick_lists()
        self.tabs.setCurrentWidget(self.pick_lists_stack)
        message = (
            f"Added Team {team_number} to “{result.list_name}” for {scout_name}."
            if result.team_added
            else f"Team {team_number} is already in “{result.list_name}” for {scout_name}."
        )
        QMessageBox.information(
            self,
            "Pick list updated",
            message,
        )

    def _configured_pick_list_scout(self) -> sqlite3.Row | None:
        """Find the imported scout whose name matches ``personal.json``."""
        try:
            self.personal_config = load_personal_config()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.warning(
                self,
                "Pick lists",
                f"Could not read personal.json: {error}",
            )
            return None
        configured_name = str(self.personal_config.get("scout_name", "")).strip()
        if not configured_name:
            QMessageBox.warning(
                self,
                "Pick lists",
                "Set scout_name in TalkToScout/personal.json before adding a team to a pick list.",
            )
            return None
        matches = [
            scout
            for scout in self.repository.scouts()
            if str(scout["display_name"]).casefold() == configured_name.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            message = (
                f"No imported scout is named “{configured_name}”. "
                "Import a match recorded with that scout name before creating a pick list."
            )
        else:
            message = (
                f"More than one imported scout is named “{configured_name}”. "
                "Use unique scout names in the scouting records."
            )
        QMessageBox.warning(self, "Pick lists", message)
        return None

    def _on_team_number_filter_changed(self, text: str) -> None:
        self._team_number_query = text
        self.show_teams()

    def _on_team_sort_metric_changed(self, metric: str) -> None:
        if metric != self._team_sort_by:
            self._team_sort_by = metric
            if metric in ("relevance", "epa", "win_ratio", "avg_actual_points", "avg_scouted_points"):
                self._team_sort_order = "desc"
            elif metric in ("avg_penalties", "team_number"):
                self._team_sort_order = "asc"
            self.show_teams()

    def _on_team_sort_order_changed(self, order: str) -> None:
        if order != self._team_sort_order:
            self._team_sort_order = order
            self.show_teams()

    def _on_team_status_filter_changed(self, status: str) -> None:
        if status != self._team_filter_status:
            self._team_filter_status = status
            self.show_teams()

    def _on_team_min_obs_filter_changed(self, min_obs: int) -> None:
        if min_obs != self._team_filter_min_obs:
            self._team_filter_min_obs = min_obs
            self.show_teams()

    def _reset_team_filters(self) -> None:
        self._team_sort_by = "team_number"
        self._team_sort_order = "asc"
        self._team_filter_status = "all"
        self._team_filter_min_obs = 0
        self._team_number_query = ""
        self._team_search_query = ""
        self._team_search_results = None
        self.show_teams()

    def show_teams(self) -> None:
        page, layout = scroll_page()
        self._page_header(layout, "Teams", "Sort and filter teams by W/L ratios, match points, scouted contributions, and penalties.")

        # Sort & Filter Toolbar Container
        filter_card = Card()
        filter_card_layout = QVBoxLayout(filter_card)
        filter_card_layout.setContentsMargins(14, 12, 14, 12)
        filter_card_layout.setSpacing(10)

        # Row 1: AI summary search
        search_controls = QHBoxLayout()
        search_controls.setContentsMargins(0, 0, 0, 0)
        search_controls.setSpacing(8)
        search_input = QLineEdit()
        search_input.setPlaceholderText("Search AI summaries by description…")
        search_input.setText(self._team_search_query)
        search_input.returnPressed.connect(lambda: self._request_team_search(search_input.text()))
        search_controls.addWidget(search_input, 1)
        self._team_search_button = button(
            "Search summaries", "secondary", lambda _checked=False: self._request_team_search(search_input.text())
        )
        search_controls.addWidget(self._team_search_button)
        if self._team_search_query:
            search_controls.addWidget(button("Clear search", "link", self._clear_team_search))
        filter_card_layout.addLayout(search_controls)

        # Row 2: Metric Sort & Filter Controls
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(10)

        # Team Number Quick Filter
        team_num_box = QVBoxLayout()
        team_num_box.setSpacing(2)
        team_num_box.addWidget(label("FILTER TEAM #", name="eyebrow"))
        team_num_input = QLineEdit()
        team_num_input.setPlaceholderText("e.g. 254 (press Enter)")
        team_num_input.setText(self._team_number_query)
        team_num_input.setFixedWidth(140)
        team_num_input.returnPressed.connect(lambda: self._on_team_number_filter_changed(team_num_input.text()))
        team_num_box.addWidget(team_num_input)
        bar.addLayout(team_num_box)

        # Sort Metric
        sort_metric_box = QVBoxLayout()
        sort_metric_box.setSpacing(2)
        sort_metric_box.addWidget(label("SORT BY", name="eyebrow"))
        sort_metric_combo = QComboBox()
        sort_metric_combo.addItem("Team Number", "team_number")
        sort_metric_combo.addItem("Search Relevance", "relevance")
        sort_metric_combo.addItem("Statbotics Event EPA", "epa")
        sort_metric_combo.addItem("Win / Loss Ratio", "win_ratio")
        sort_metric_combo.addItem("Avg Points / Game (Match)", "avg_actual_points")
        sort_metric_combo.addItem("Avg Scouted Points", "avg_scouted_points")
        sort_metric_combo.addItem("Avg Penalties / Game", "avg_penalties")
        sort_metric_combo.setMinimumWidth(190)
        idx = sort_metric_combo.findData(self._team_sort_by)
        if idx >= 0:
            sort_metric_combo.setCurrentIndex(idx)
        sort_metric_combo.currentIndexChanged.connect(
            lambda _idx: self._on_team_sort_metric_changed(sort_metric_combo.currentData())
        )
        sort_metric_box.addWidget(sort_metric_combo)
        bar.addLayout(sort_metric_box)

        # Sort Direction Order
        sort_order_box = QVBoxLayout()
        sort_order_box.setSpacing(2)
        sort_order_box.addWidget(label("ORDER", name="eyebrow"))
        sort_order_combo = QComboBox()
        sort_order_combo.addItem("High to Low (Desc)", "desc")
        sort_order_combo.addItem("Low to High (Asc)", "asc")
        sort_order_combo.setMinimumWidth(150)
        idx = sort_order_combo.findData(self._team_sort_order)
        if idx >= 0:
            sort_order_combo.setCurrentIndex(idx)
        sort_order_combo.currentIndexChanged.connect(
            lambda _idx: self._on_team_sort_order_changed(sort_order_combo.currentData())
        )
        sort_order_box.addWidget(sort_order_combo)
        bar.addLayout(sort_order_box)

        # Scouting Status
        status_box = QVBoxLayout()
        status_box.setSpacing(2)
        status_box.addWidget(label("STATUS", name="eyebrow"))
        status_combo = QComboBox()
        status_combo.addItem("All Teams", "all")
        status_combo.addItem("Only Scouted", "scouted")
        status_combo.addItem("Only Unscouted", "unscouted")
        status_combo.setMinimumWidth(130)
        idx = status_combo.findData(self._team_filter_status)
        if idx >= 0:
            status_combo.setCurrentIndex(idx)
        status_combo.currentIndexChanged.connect(
            lambda _idx: self._on_team_status_filter_changed(status_combo.currentData())
        )
        status_box.addWidget(status_combo)
        bar.addLayout(status_box)

        # Min Observations
        min_obs_box = QVBoxLayout()
        min_obs_box.setSpacing(2)
        min_obs_box.addWidget(label("MIN SCOUTED", name="eyebrow"))
        min_obs_combo = QComboBox()
        min_obs_combo.addItem("Any Matches", 0)
        min_obs_combo.addItem("1+ Scouted", 1)
        min_obs_combo.addItem("3+ Scouted", 3)
        min_obs_combo.addItem("5+ Scouted", 5)
        min_obs_combo.setMinimumWidth(120)
        idx = min_obs_combo.findData(self._team_filter_min_obs)
        if idx >= 0:
            min_obs_combo.setCurrentIndex(idx)
        min_obs_combo.currentIndexChanged.connect(
            lambda _idx: self._on_team_min_obs_filter_changed(min_obs_combo.currentData())
        )
        min_obs_box.addWidget(min_obs_combo)
        bar.addLayout(min_obs_box)

        # Reset button
        reset_box = QVBoxLayout()
        reset_box.setSpacing(2)
        reset_box.addWidget(label("", name="eyebrow"))
        reset_btn = button("Reset", "secondary", self._reset_team_filters)
        reset_box.addWidget(reset_btn)
        bar.addLayout(reset_box)

        view_box = QVBoxLayout()
        view_box.setSpacing(2)
        view_box.addWidget(label("VIEW", name="eyebrow"))
        view_buttons = QHBoxLayout()
        view_buttons.setContentsMargins(0, 0, 0, 0)
        view_buttons.setSpacing(4)
        view_buttons.addWidget(
            button(
                "Cards",
                "primary" if self._team_view_mode == "cards" else "secondary",
                lambda _checked=False: self._set_team_view_mode("cards"),
            )
        )
        view_buttons.addWidget(
            button(
                "Compact",
                "primary" if self._team_view_mode == "compact" else "secondary",
                lambda _checked=False: self._set_team_view_mode("compact"),
            )
        )
        view_box.addLayout(view_buttons)
        bar.addLayout(view_box)

        filter_card_layout.addLayout(bar)
        layout.addWidget(filter_card)

        if self._team_search_results is not None:
            layout.addWidget(
                label(
                    f'Top semantic matches for “{self._team_search_query}”. Missing summary embeddings are generated before searching.',
                    name="muted",
                    word_wrap=True,
                )
            )

        teams = self.repository.teams()
        result_by_team = {
            result.team_number: result for result in (self._team_search_results or [])
        }

        # Apply filtering
        filtered_teams: list[sqlite3.Row] = []
        for team in teams:
            t_num = int(team["team_number"])
            obs = int(team["observations"] or 0)

            if self._team_filter_status == "scouted" and obs == 0:
                continue
            if self._team_filter_status == "unscouted" and obs > 0:
                continue

            if obs < self._team_filter_min_obs:
                continue

            if self._team_number_query.strip():
                number_query = self._team_number_query.strip()
                if (number_query.isdigit() and str(t_num) != number_query) or (
                    not number_query.isdigit() and number_query not in str(t_num)
                ):
                    continue

            if self._team_search_results is not None and t_num not in result_by_team:
                continue

            filtered_teams.append(team)

        # Apply sorting
        def sort_key(team: sqlite3.Row) -> tuple[float, float, int]:
            t_num = int(team["team_number"])
            obs = int(team["observations"] or 0)
            matches_played = int(team["matches_played"] or 0)
            win_ratio = float(team["win_ratio"] or 0.0)
            avg_actual = float(team["average_actual_points"] or 0.0)
            avg_scouted = float(team["average_scouted_points"] or 0.0)
            avg_penalties = float(team["average_penalties_committed"] or 0.0)
            epa = float(team["statbotics_total_epa"] or 0.0)
            search_res = result_by_team.get(t_num)

            if self._team_sort_by == "relevance":
                return (search_res.score if search_res is not None else -1.0, obs, t_num)
            elif self._team_sort_by == "epa":
                return (epa, matches_played, t_num)
            elif self._team_sort_by == "win_ratio":
                return (win_ratio, matches_played, t_num)
            elif self._team_sort_by == "avg_actual_points":
                return (avg_actual, matches_played, t_num)
            elif self._team_sort_by == "avg_scouted_points":
                return (avg_scouted, obs, t_num)
            elif self._team_sort_by == "avg_penalties":
                return (avg_penalties, obs, t_num)
            else:  # team_number
                return (float(t_num), 0.0, t_num)

        reverse = (self._team_sort_order == "desc")
        filtered_teams.sort(key=sort_key, reverse=reverse)

        # Active filter status summary
        sort_names = {
            "team_number": "Team Number",
            "relevance": "Search Relevance",
            "epa": "Statbotics Event EPA",
            "win_ratio": "Win / Loss Ratio",
            "avg_actual_points": "Avg Points / Game (Match)",
            "avg_scouted_points": "Avg Scouted Points",
            "avg_penalties": "Avg Penalties / Game",
        }
        order_name = "High to Low" if self._team_sort_order == "desc" else "Low to High"
        sort_desc = f"Sorted by {sort_names.get(self._team_sort_by, 'Team Number')} ({order_name})"

        layout.addWidget(
            label(
                f"Showing {len(filtered_teams)} of {len(teams)} teams  •  {sort_desc}",
                name="muted",
                word_wrap=True,
            )
        )

        event_events = self.repository.event_events()
        event_observations = self.repository.observation_count()
        events_by_team: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for event in event_events:
            events_by_team[int(event["team_number"])].append(event)
        team_summaries = self.repository.latest_team_summaries()

        if not filtered_teams:
            empty_message = (
                "No teams match the current filters or search query."
                if (teams or self._team_search_results is not None)
                else "No teams have been imported yet."
            )
            self._empty_state(layout, empty_message)

        if filtered_teams and self._team_view_mode == "compact":
            layout.addWidget(self._compact_team_table(filtered_teams, result_by_team))
            layout.addStretch(1)
            self._replace_stack(self.teams_stack, page)
            return

        for team in filtered_teams:
            team_number = int(team["team_number"])
            obs_count = int(team["observations"] or 0)
            matches_played = int(team["matches_played"] or 0)
            wins = int(team["wins"] or 0)
            losses = int(team["losses"] or 0)
            ties = int(team["ties"] or 0)
            win_ratio = float(team["win_ratio"] or 0.0)
            event_epa = team["statbotics_total_epa"]

            card = ClickableCard(
                lambda number=team_number: self.show_team_detail(number),
                tooltip="Open team details",
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(18, 16, 18, 16)
            card_layout.setSpacing(12)

            header = QHBoxLayout()
            header.setContentsMargins(0, 0, 0, 0)
            identity = QVBoxLayout()
            identity.setSpacing(2)
            identity.addWidget(label("TEAM", name="eyebrow"))
            identity.addWidget(label(f"Team {team_number}", name="sectionTitle"))

            detail_parts = []
            if matches_played > 0:
                detail_parts.append(f"{matches_played} matches played ({wins}-{losses}-{ties})")
            else:
                detail_parts.append("No scheduled matches played")

            if obs_count > 0:
                detail_parts.append(f"{obs_count} scouted observation(s)")
                condition = "BROKEN" if team["currently_broken"] else "Operational"
                detail_parts.append(
                    f"{condition} • {int(team['broken_match_count'] or 0)} broken match(es)"
                )
            else:
                detail_parts.append("Not yet scouted")

            identity.addWidget(label(" • ".join(detail_parts), name="muted"))

            search_result = result_by_team.get(team_number)
            if search_result is not None:
                identity.addWidget(
                    label(
                        f"Search relevance {search_result.score:.2f} • {search_result.match_hits} matching match summary(s)",
                        name="muted",
                    )
                )
            team_summary = team_summaries.get(team_number)
            if team_summary:
                preview = team_summary if len(team_summary) <= 220 else f"{team_summary[:217].rstrip()}…"
                identity.addWidget(label(preview, name="muted", word_wrap=True))
            header.addLayout(identity, 1)

            # Highlight metric badge on top right based on active sort
            score = QVBoxLayout()
            score.setSpacing(0)
            if self._team_sort_by == "relevance":
                search_score = result_by_team.get(team_number)
                badge_title = "SEARCH RELEVANCE"
                badge_val = f"{search_score.score:.2f}" if search_score is not None else "—"
            elif self._team_sort_by == "epa":
                badge_title = "EVENT EPA"
                badge_val = f"{float(event_epa):.1f}" if event_epa is not None else "—"
            elif self._team_sort_by == "win_ratio":
                badge_title = "WIN / LOSS RATIO"
                badge_val = f"{win_ratio * 100:.1f}%" if matches_played > 0 else "—"
            elif self._team_sort_by == "avg_actual_points":
                badge_title = "AVG POINTS / GAME"
                badge_val = f"{float(team['average_actual_points'] or 0):.1f}" if matches_played > 0 else "—"
            elif self._team_sort_by == "avg_scouted_points":
                badge_title = "AVG SCOUTED POINTS"
                badge_val = f"{float(team['average_scouted_points'] or 0):.1f}" if obs_count > 0 else "—"
            elif self._team_sort_by == "avg_penalties":
                badge_title = "COMMITTED PENALTIES"
                badge_val = f"{float(team['average_penalties_committed'] or 0):.1f}" if obs_count > 0 else "—"
            else:
                badge_title = "W/L RATIO"
                badge_val = f"{win_ratio * 100:.0f}%" if matches_played > 0 else ("—" if obs_count == 0 else f"{float(team['average_scouted_points'] or 0):.1f} pts")

            score_label = label(badge_title, name="eyebrow")
            score_label.setAlignment(Qt.AlignmentFlag.AlignRight)
            score.addWidget(score_label)
            score_value = label(badge_val, name="teamScore")
            score_value.setAlignment(Qt.AlignmentFlag.AlignRight)
            if self._team_sort_by in ("win_ratio", "team_number") and matches_played > 0:
                score_value.setStyleSheet(
                    f"color: {win_ratio_color(win_ratio)}; font-size: 31px; font-weight: 700;"
                )
            score.addWidget(score_value)
            header.addLayout(score)
            card_layout.addLayout(header)

            # Statbotics EPA stays alongside the locally observed statistics.
            penalty_average = float(team["average_penalties_committed"] or 0)
            scouted_average = float(team["average_scouted_points"] or 0)
            actual_average = float(team["average_actual_points"] or 0)
            epa_value = f"{float(event_epa):.1f}" if event_epa is not None else "—"

            stats = QHBoxLayout()
            stats.setContentsMargins(0, 0, 0, 0)
            stats.setSpacing(8)

            stats.addWidget(
                self._team_stat(
                    "STATBOTICS EVENT EPA",
                    epa_value,
                    "estimated point contribution" if event_epa is not None else "sync Statbotics EPA",
                ),
                1,
            )

            wl_val = f"{win_ratio * 100:.1f}%" if matches_played > 0 else "—"
            wl_detail = f"{wins}W - {losses}L - {ties}T" if matches_played > 0 else "no results"
            stats.addWidget(self._team_stat("W/L RATIO", wl_val, wl_detail), 1)

            match_pts_val = f"{actual_average:.1f}" if matches_played > 0 else "—"
            stats.addWidget(self._team_stat("AVG POINTS / GAME", match_pts_val, "official match avg"), 1)

            scouted_pts_val = f"{scouted_average:.1f}" if obs_count > 0 else "—"
            stats.addWidget(self._team_stat("SCOUTED PTS / GAME", scouted_pts_val, "observed team avg"), 1)

            penalties_val = f"{penalty_average:.1f}" if obs_count > 0 else "—"
            stats.addWidget(self._team_stat("COMMITTED PENALTIES", penalties_val, "per match"), 1)

            condition_value = "BROKEN" if team["currently_broken"] else "Operational"
            condition_detail = f"{int(team['broken_match_count'] or 0)} broken match(es)"
            stats.addWidget(self._team_stat("ROBOT CONDITION", condition_value, condition_detail), 1)

            card_layout.addLayout(stats)

            if obs_count > 0:
                timeline_panel = QFrame()
                timeline_panel.setObjectName("teamTimeline")
                timeline_layout = QVBoxLayout(timeline_panel)
                timeline_layout.setContentsMargins(12, 9, 12, 8)
                timeline_layout.setSpacing(3)
                timeline_header = QHBoxLayout()
                timeline_header.setContentsMargins(0, 0, 0, 0)
                timeline_header.addWidget(label("SCORING PACE", name="eyebrow"))
                timeline_header.addStretch(1)
                team_has_events = bool(events_by_team[team_number])
                timeline_header.addWidget(
                    label("━━ Team scoring" if team_has_events else "— No team scores", name="teamLegend")
                )
                timeline_header.addSpacing(14)
                timeline_header.addWidget(label("╌╌ Event average", name="averageLegend"))
                timeline_layout.addLayout(timeline_header)
                timeline = SmoothChart(
                    events_by_team[team_number],
                    obs_count,
                    comparison_events=event_events,
                    comparison_divisor=event_observations,
                    comparison_label="Event avg.",
                    compact=True,
                    primary_color=ACCENT,
                )
                timeline.setToolTip("Solid line: this team's scoring rate. Dashed line: event average.")
                timeline_layout.addWidget(timeline)
                card_layout.addWidget(timeline_panel)

            layout.addWidget(card)

        layout.addStretch(1)
        self._replace_stack(self.teams_stack, page)

    def _set_team_view_mode(self, mode: str) -> None:
        if mode != self._team_view_mode:
            self._team_view_mode = mode
            self.show_teams()

    def _compact_team_table(
        self,
        teams: list[sqlite3.Row],
        result_by_team: dict[int, TeamSearchResult],
    ) -> QWidget:
        table = Card()
        layout = QVBoxLayout(table)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        headers = ["TEAM", "EPA", "W/L", "MATCH PTS", "SCOUTED", "PENALTIES", "CONDITION"]
        if self._team_search_results is not None:
            headers.append("RELEVANCE")
        header = QGridLayout()
        header.setContentsMargins(9, 3, 9, 3)
        for column, text in enumerate(headers):
            header.addWidget(label(text, name="eyebrow"), 0, column)
            header.setColumnStretch(column, 1)
        layout.addLayout(header)
        for team in teams:
            team_number = int(team["team_number"])
            matches_played = int(team["matches_played"] or 0)
            win_ratio = float(team["win_ratio"] or 0.0)
            search_result = result_by_team.get(team_number)
            values = [
                f"Team {team_number}",
                numeric_metric(team["statbotics_total_epa"]),
                f"{win_ratio * 100:.0f}%" if matches_played else "—",
                numeric_metric(team["average_actual_points"]),
                numeric_metric(team["average_scouted_points"]),
                numeric_metric(team["average_penalties_committed"]),
                (
                    f"BROKEN ({int(team['broken_match_count'] or 0)})"
                    if team["currently_broken"]
                    else f"Operational ({int(team['broken_match_count'] or 0)})"
                ),
            ]
            if self._team_search_results is not None:
                values.append(f"{search_result.score:.2f}" if search_result else "—")
            row = ClickableCard(
                lambda number=team_number: self.show_team_detail(number),
                tooltip="Open team details",
                shadow=False,
            )
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(9, 8, 9, 8)
            row_layout.setHorizontalSpacing(10)
            for column, value in enumerate(values):
                row_layout.addWidget(
                    label(value, name="sectionTitle" if column == 0 else "muted"), 0, column
                )
                row_layout.setColumnStretch(column, 1)
            layout.addWidget(row)
        return table

    def _team_stat(self, title: str, value: str, detail: str) -> QFrame:
        stat = QFrame()
        stat.setObjectName("teamStat")
        stat_layout = QVBoxLayout(stat)
        stat_layout.setContentsMargins(12, 10, 12, 10)
        stat_layout.setSpacing(2)
        stat_layout.addWidget(label(title, name="teamStatLabel"))
        stat_layout.addWidget(label(value, name="teamStatValue"))
        stat_layout.addWidget(label(detail, name="muted"))
        return stat

    def show_team_detail(self, team_number: int) -> None:
        overview = self.repository.team_overview(team_number)
        if overview is None:
            self.show_teams()
            return
        page, layout = scroll_page()
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.addWidget(button("←  Back to teams", "secondary", self.show_teams))
        actions.addWidget(
            button(
                "Add to pick list",
                "primary",
                lambda _checked=False: self._add_team_to_pick_list(team_number),
            )
        )
        actions.addWidget(
            button(
                "Edit role summary",
                "secondary",
                lambda _checked=False: self._edit_team_summary(team_number),
            )
        )
        actions.addStretch(1)
        layout.addLayout(actions)
        self._page_header(layout, f"Team {team_number}", "All comparisons use official match results and event-wide scouting averages.")
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(9)
        metrics.setVerticalSpacing(9)

        matches_played = int(overview["matches_played"] or 0)
        wins = int(overview["wins"] or 0)
        losses = int(overview["losses"] or 0)
        ties = int(overview["ties"] or 0)
        win_rate = (wins + 0.5 * ties) / matches_played if matches_played > 0 else 0.0
        wl_str = f"{wins}-{losses}-{ties} ({win_rate * 100:.1f}%)" if matches_played > 0 else "No matches"
        event_epa = overview["statbotics_total_epa"]
        epa_detail = (
            "Sync Statbotics EPA from the Matches tab."
            if event_epa is None
            else (
                f"Auto {numeric_metric(overview['statbotics_auto_epa'])} • "
                f"Teleop {numeric_metric(overview['statbotics_teleop_epa'])} • "
                f"Endgame {numeric_metric(overview['statbotics_endgame_epa'])}"
            )
        )
        official_rank = overview["official_rank"]
        ranking_points = overview["ranking_points"]
        official_matches_played = overview["official_matches_played"]
        rank_detail = (
            "Sync official results from TBA on the Matches tab."
            if official_rank is None
            else f"Official TBA standings • {int(official_matches_played or 0)} qualification matches played"
        )
        ranking_points_detail = (
            "Sync official results from TBA on the Matches tab."
            if ranking_points is None
            else f"Official TBA field: {overview['ranking_points_label'] or 'ranking points'}"
        )

        values = (
            ("Official rank", "—" if official_rank is None else f"#{int(official_rank)}", rank_detail),
            ("Ranking points", numeric_metric(ranking_points), ranking_points_detail),
            ("Statbotics event EPA", numeric_metric(event_epa, "pts"), epa_detail),
            ("W/L Record & Ratio", wl_str, f"{matches_played} matches played • {int(overview['observations'] or 0)} scouted obs"),
            ("Average points / game", numeric_metric(overview["average_actual_points"], "pts"), comparison_text(overview["event_average_actual_points"], "pts")),
            ("Scouted points / game", numeric_metric(overview["average_scouted_points"], "pts"), comparison_text(overview["event_average_points"], "pts")),
            ("Penalties committed", numeric_metric(overview["average_penalties_committed"]), comparison_text(overview["event_average_penalties_committed"])),
            (
                "Robot condition",
                "BROKEN" if overview["currently_broken"] else "Operational",
                f"Broken in {int(overview['broken_match_count'] or 0)} match(es)",
            ),
        )
        for column, (metric_name, value, detail) in enumerate(values):
            metric_card = Card(metric=True, shadow=False)
            metric_layout = QVBoxLayout(metric_card)
            metric_layout.setContentsMargins(15, 13, 15, 13)
            metric_layout.addWidget(label(metric_name, name="muted"))
            metric_layout.addWidget(label(value, name="metric"))
            metric_layout.addWidget(label(detail, name="muted", word_wrap=True))
            metrics.addWidget(metric_card, column // 3, column % 3)
        metrics_host = QWidget()
        metrics_host.setLayout(metrics)
        layout.addWidget(metrics_host)

        summary = self.repository.latest_team_summary(team_number)
        layout.addWidget(self._text_card("Role summary", summary or "Not generated yet. Generate AI summaries from the Matches tab."))
        layout.addWidget(
            self._chart_card(
                "Smoothed scoring rate",
                self.repository.team_events(team_number),
                int(overview["observations"] or 1),
                comparison_events=self.repository.event_events(),
                comparison_divisor=self.repository.observation_count(),
                comparison_label="Event avg.",
                primary_color=ACCENT,
            )
        )

        matches = self.repository.team_matches(team_number)
        history_card = Card()
        history_layout = QVBoxLayout(history_card)
        history_layout.setContentsMargins(18, 15, 18, 15)
        history_layout.addWidget(label("Matches played", name="sectionTitle"))
        if not matches:
            history_layout.addWidget(label("No scouting observations for this team yet.", name="muted"))
        for match in matches:
            mismatch = " • legacy schedule mismatch" if match["schedule_alignment"] != "matched" else ""
            condition = "BROKEN" if match["robot_broken"] else "Operational"
            history_layout.addWidget(button(f"Match {match['match_number']}  •  {match['scouted_points']} pts  •  {condition}  •  {match['breakdown_count']} breakdowns  •  {match['scout_name']}{mismatch}", "link", lambda _checked=False, key=match["match_key"]: self.show_match_detail(key)))
            if match["match_summary"]:
                history_layout.addWidget(
                    label(str(match["match_summary"]), name="muted", word_wrap=True)
                )
            else:
                history_layout.addWidget(
                    label("Summary not generated yet.", name="muted")
                )
        layout.addWidget(history_card)
        layout.addStretch(1)
        self._replace_stack(self.teams_stack, page)
        self.tabs.setCurrentWidget(self.teams_stack)

    def _edit_team_summary(self, team_number: int) -> None:
        current_summary = self.repository.latest_team_summary(team_number)
        if current_summary is None:
            QMessageBox.information(
                self,
                "Role summary",
                "Generate AI summaries from the Matches tab before editing this team's role summary.",
            )
            return
        updated_summary, accepted = QInputDialog.getMultiLineText(
            self,
            f"Edit Team {team_number} role summary",
            "Role summary:",
            current_summary,
        )
        if not accepted or updated_summary == current_summary:
            return
        event_key = self.repository.event_key()
        if event_key is None:
            return
        try:
            update_team_summary(
                self.repository.database_path, event_key, team_number, updated_summary
            )
        except TeamSummaryError as error:
            QMessageBox.warning(self, "Could not save role summary", str(error))
            return
        self._refresh_repository()
        self.show_team_detail(team_number)

    def show_scouts(self) -> None:
        page, layout = scroll_page()
        self._page_header(layout, "Scouts", "Coverage, note detail, and prediction accuracy for each scout.")
        scouts = self.repository.scouts()
        if not scouts:
            self._empty_state(layout, "No scouts have been imported yet.")
        for scout in scouts:
            card = Card()
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(18, 16, 18, 16)
            left = QVBoxLayout()
            left.addWidget(label(scout["display_name"], name="sectionTitle"))
            notes = f"{scout['average_notes_per_match'] or 0:.1f} notes/match"
            accuracy = "Awaiting official results" if scout["prediction_accuracy"] is None else f"{scout['prediction_accuracy'] * 100:.0f}% correct"
            left.addWidget(label(f"{notes} • {accuracy}", name="muted"))
            card_layout.addLayout(left, 1)
            card_layout.addWidget(label(f"{scout['matches_scouted']} matches", name="metric"))
            card_layout.addWidget(button("Scout details", "secondary", lambda _checked=False, scout_id=scout["scout_id"], name=scout["display_name"]: self.show_scout_detail(scout_id, name)))
            layout.addWidget(card)
        layout.addStretch(1)
        self._replace_stack(self.scouts_stack, page)

    def show_scout_detail(self, scout_id: str, scout_name: str) -> None:
        overview = self.repository.scout_overview(scout_id)
        if overview is None:
            self.show_scouts()
            return
        page, layout = scroll_page()
        layout.addWidget(button("←  Back to scouts", "secondary", self.show_scouts), alignment=Qt.AlignmentFlag.AlignLeft)
        self._page_header(layout, scout_name, "Matches recorded by this scout. Prediction accuracy appears after final results are imported.")
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(9)
        metrics.setVerticalSpacing(9)
        accuracy_value = (
            "—" if overview["prediction_accuracy"] is None
            else f"{float(overview['prediction_accuracy']) * 100:.0f}%"
        )
        prediction_detail = (
            "Awaiting official results"
            if overview["prediction_accuracy"] is None
            else f"{int(overview['correct_predictions'] or 0)} correct of {int(overview['predictions_with_result'] or 0)}"
        )
        values = (
            ("Correct guesses", accuracy_value, prediction_detail),
            ("Matches scouted", str(int(overview["matches_scouted"] or 0)), "Imported observations"),
            (
                "Average notes / match",
                f"{float(overview['average_notes_per_match'] or 0):.1f}",
                "Notes recorded across scouted matches",
            ),
        )
        for column, (metric_name, value, detail) in enumerate(values):
            metric_card = Card(metric=True, shadow=False)
            metric_layout = QVBoxLayout(metric_card)
            metric_layout.setContentsMargins(15, 13, 15, 13)
            metric_layout.addWidget(label(metric_name, name="muted"))
            metric_layout.addWidget(label(value, name="metric"))
            metric_layout.addWidget(label(detail, name="muted", word_wrap=True))
            metrics.addWidget(metric_card, 0, column)
        metrics_host = QWidget()
        metrics_host.setLayout(metrics)
        layout.addWidget(metrics_host)

        matches = self.repository.scout_matches(scout_id)
        if not matches:
            self._empty_state(layout, "This scout has no imported observations.")
        for match in matches:
            card = Card()
            card_layout = QHBoxLayout(card)
            card_layout.setContentsMargins(18, 16, 18, 16)
            left = QVBoxLayout()
            left.addWidget(label(f"Match {match['match_number']} • Team {match['team_number']}", name="sectionTitle"))
            prediction = "No prediction"
            if match["predicted_winner"]:
                prediction = f"Predicted {match['predicted_winner']}"
                if match["winner_alliance"]:
                    prediction += " • correct" if match["predicted_winner"] == match["winner_alliance"] else " • incorrect"
            notes = f"{int(match['note_count'] or 0)} note(s)"
            left.addWidget(label(f"{prediction} • {notes}", name="muted"))
            card_layout.addLayout(left, 1)
            card_layout.addWidget(label(f"{match['scouted_points']} pts", name="metric"))
            card_layout.addWidget(button("View match", "secondary", lambda _checked=False, key=match["match_key"]: self.show_match_detail(key)))
            layout.addWidget(card)
        layout.addStretch(1)
        self._replace_stack(self.scouts_stack, page)
        self.tabs.setCurrentWidget(self.scouts_stack)

    def _request_tba_results(self, *, silent: bool = False) -> None:
        if self._result_sync_worker is not None and self._result_sync_worker.isRunning():
            return
        event_key = self.repository.event_key()
        if event_key is None:
            if not silent:
                QMessageBox.warning(self, "TBA results", "No event key has been imported into this database yet.")
            return
        try:
            self.personal_config = load_personal_config()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            if not silent:
                QMessageBox.warning(self, "TBA results", f"Could not read personal.json: {error}")
            return
        api_key = str(self.personal_config.get("tba_api_key", "")).strip()
        if not api_key:
            if not silent:
                QMessageBox.warning(
                    self,
                    "TBA results",
                    "Set tba_api_key in TalkToScout/personal.json before syncing official results.",
                )
            return

        worker = TbaResultWorker(self.repository.database_path, event_key, api_key)
        self._result_sync_worker = worker
        self._result_sync_was_automatic = silent
        worker.completed.connect(self._official_results_synced)
        worker.failed.connect(self._official_results_sync_failed)
        worker.finished.connect(self._result_sync_finished)
        worker.finished.connect(worker.deleteLater)
        if self._result_sync_button is not None:
            self._result_sync_button.setEnabled(False)
            self._result_sync_button.setText("Syncing official results…")
        worker.start()

    def _official_results_synced(self, updated_matches: int) -> None:
        automatic = self._result_sync_was_automatic
        self._refresh_repository()
        self.show_matches()
        self.show_scouts()
        self.show_picking()
        if automatic:
            return
        detail = (
            f"Updated {updated_matches} local qualification match"
            f"{'es' if updated_matches != 1 else ''} with final TBA results."
        )
        QMessageBox.information(self, "Official results synced", detail)

    def _official_results_sync_failed(self, message: str) -> None:
        if self._result_sync_button is not None:
            self._result_sync_button.setEnabled(True)
            self._result_sync_button.setText("Sync official results from TBA")
        if not self._result_sync_was_automatic:
            QMessageBox.warning(self, "Could not sync TBA results", message)

    def _result_sync_finished(self) -> None:
        if self._result_sync_worker is not None and not self._result_sync_worker.isRunning():
            self._result_sync_worker = None

    def _request_statbotics_epa(self) -> None:
        if self._statbotics_sync_worker is not None and self._statbotics_sync_worker.isRunning():
            return
        event_key = self.repository.event_key()
        if event_key is None:
            QMessageBox.warning(
                self, "Statbotics EPA", "No event key has been imported into this database yet."
            )
            return
        worker = StatboticsEpaWorker(self.repository.database_path, event_key)
        self._statbotics_sync_worker = worker
        worker.completed.connect(self._statbotics_epa_synced)
        worker.failed.connect(self._statbotics_epa_sync_failed)
        worker.finished.connect(self._statbotics_epa_sync_finished)
        worker.finished.connect(worker.deleteLater)
        if self._statbotics_sync_button is not None:
            self._statbotics_sync_button.setEnabled(False)
            self._statbotics_sync_button.setText("Syncing Statbotics EPA…")
        worker.start()

    def _statbotics_epa_synced(self, report: StatboticsSyncReport) -> None:
        self._refresh_repository()
        self.show_matches()
        self.show_teams()
        self.tabs.setCurrentWidget(self.teams_stack)
        QMessageBox.information(
            self,
            "Statbotics EPA synced",
            f"Cached EPA estimates for {report.teams_updated} team"
            f"{'s' if report.teams_updated != 1 else ''} at {report.event_key}.\n\n"
            "EPA is Statbotics' estimated point contribution, not a replacement for your scouting data.",
        )

    def _statbotics_epa_sync_failed(self, message: str) -> None:
        if self._statbotics_sync_button is not None:
            self._statbotics_sync_button.setEnabled(True)
            self._statbotics_sync_button.setText("Sync Statbotics EPA")
        QMessageBox.warning(self, "Could not sync Statbotics EPA", message)

    def _statbotics_epa_sync_finished(self) -> None:
        if (
            self._statbotics_sync_worker is not None
            and not self._statbotics_sync_worker.isRunning()
        ):
            self._statbotics_sync_worker = None

    def _request_gemini_enrichment(self) -> None:
        if self._gemini_enrichment_worker is not None and self._gemini_enrichment_worker.isRunning():
            return
        provider = self._configured_ai_provider()
        if provider is None:
            return
        worker = GeminiEnrichmentWorker(self.repository.database_path, provider)
        self._gemini_enrichment_worker = worker
        worker.completed.connect(self._gemini_enrichment_completed)
        worker.failed.connect(self._gemini_enrichment_failed)
        worker.progress.connect(self._gemini_enrichment_progressed)
        worker.finished.connect(self._gemini_enrichment_finished)
        worker.finished.connect(worker.deleteLater)
        if self._gemini_enrichment_button is not None:
            self._gemini_enrichment_button.setEnabled(False)
            self._gemini_enrichment_button.setText("Generating AI summaries…")
        self._set_gemini_enrichment_progress(
            0, 0, "Checking which AI summaries are already current…"
        )
        worker.start()

    def _gemini_enrichment_progressed(self, completed: int, total: int, message: str) -> None:
        self._set_gemini_enrichment_progress(completed, total, message)

    def _gemini_enrichment_completed(self, report: EnrichmentReport) -> None:
        self._refresh_repository()
        self._team_search_query = ""
        self._team_search_results = None
        self._set_gemini_enrichment_progress(0, 0, "")
        self.show_matches()
        self.show_teams()
        self.tabs.setCurrentWidget(self.teams_stack)
        skipped_items: list[str] = []
        if report.skipped_match_batches:
            skipped_items.append(
                f"{report.skipped_match_batches} match"
                f"{'es' if report.skipped_match_batches != 1 else ''}"
            )
        if report.skipped_team_summaries:
            skipped_items.append(
                f"{report.skipped_team_summaries} team role summar"
                f"{'ies' if report.skipped_team_summaries != 1 else 'y'}"
            )
        skipped_note = (
            "\n\nSkipped "
            + " and ".join(skipped_items)
            + " after malformed or incomplete AI responses. Run generation again later to retry them."
            if skipped_items
            else ""
        )
        QMessageBox.information(
            self,
            "AI summaries generated",
            (
                f"Saved {report.match_summaries} match contribution summar"
                f"{'y' if report.match_summaries == 1 else 'ies'}, "
                f"{report.team_summaries} team role summar"
                f"{'y' if report.team_summaries == 1 else 'ies'}, and "
                f"{report.embedding_chunks} search chunk"
                f"{'s' if report.embedding_chunks != 1 else ''}.\n\n"
                f"Updated {report.json_files_updated} scouting JSON file"
                f"{'s' if report.json_files_updated != 1 else ''} with their match summary."
                f"{skipped_note}"
            ),
        )

    def _gemini_enrichment_failed(self, message: str) -> None:
        if self._gemini_enrichment_button is not None:
            self._gemini_enrichment_button.setEnabled(True)
            self._gemini_enrichment_button.setText("Generate AI summaries")
        self._set_gemini_enrichment_progress(0, 0, f"Stopped: {message}")
        QMessageBox.warning(self, "Could not generate AI summaries", message)

    def _gemini_enrichment_finished(self) -> None:
        if (
            self._gemini_enrichment_worker is not None
            and not self._gemini_enrichment_worker.isRunning()
        ):
            self._gemini_enrichment_worker = None

    def _set_gemini_enrichment_progress(
        self, completed: int, total: int, message: str
    ) -> None:
        self._gemini_enrichment_progress_state = (max(0, completed), max(0, total), message)
        self._render_gemini_enrichment_progress()

    def _render_gemini_enrichment_progress(self) -> None:
        completed, total, message = self._gemini_enrichment_progress_state
        is_visible = bool(message)
        if self._gemini_enrichment_progress is not None:
            self._gemini_enrichment_progress.setVisible(is_visible)
            if total > 0:
                self._gemini_enrichment_progress.setRange(0, total)
                self._gemini_enrichment_progress.setValue(min(completed, total))
                self._gemini_enrichment_progress.setFormat(f"{completed} / {total} work items")
            else:
                self._gemini_enrichment_progress.setRange(0, 0)
                self._gemini_enrichment_progress.setFormat("Working…")
        if self._gemini_enrichment_status is not None:
            self._gemini_enrichment_status.setVisible(is_visible)
            self._gemini_enrichment_status.setText(message)

    def _request_team_search(self, query: str, *, context: str = "teams") -> None:
        clean_query = " ".join(query.split())
        if not clean_query:
            if context == "picking":
                self._picking_research_query = ""
                self._picking_research_results = None
                self.show_picking()
            else:
                self._clear_team_search()
            return
        if clean_query.isdigit():
            if context == "picking":
                self._picking_research_query = clean_query
                self._picking_research_results = None
                self.show_picking()
            else:
                self._team_number_query = clean_query
                self._team_search_query = ""
                self._team_search_results = None
                self.show_teams()
                self.tabs.setCurrentWidget(self.teams_stack)
            return
        if self._gemini_search_worker is not None and self._gemini_search_worker.isRunning():
            return
        provider = self._configured_ai_provider()
        if provider is None:
            return
        self._team_search_context = context
        if context == "picking":
            self._picking_research_query = clean_query
        else:
            self._team_search_query = clean_query
        worker = GeminiSearchWorker(self.repository.database_path, provider, clean_query)
        self._gemini_search_worker = worker
        worker.completed.connect(self._team_search_completed)
        worker.failed.connect(self._team_search_failed)
        worker.finished.connect(self._team_search_finished)
        worker.finished.connect(worker.deleteLater)
        if self._team_search_button is not None:
            self._team_search_button.setEnabled(False)
            self._team_search_button.setText("Building search index…")
        worker.start()

    def _team_search_completed(self, results: object) -> None:
        if self._team_search_context == "picking":
            self._picking_research_results = list(results) if isinstance(results, list) else []
            self.show_picking()
            return
        self._team_search_results = list(results) if isinstance(results, list) else []
        self._team_sort_by = "relevance"
        self._team_sort_order = "desc"
        self.show_teams()
        self.tabs.setCurrentWidget(self.teams_stack)

    def _team_search_failed(self, message: str) -> None:
        if self._team_search_button is not None:
            self._team_search_button.setEnabled(True)
            self._team_search_button.setText(
                "Search" if self._team_search_context == "picking" else "Search summaries"
            )
        QMessageBox.warning(self, "Could not search summaries", message)

    def _team_search_finished(self) -> None:
        if self._gemini_search_worker is not None and not self._gemini_search_worker.isRunning():
            self._gemini_search_worker = None

    def _clear_team_search(self) -> None:
        self._team_search_query = ""
        self._team_search_results = None
        if self._team_sort_by == "relevance":
            self._team_sort_by = "team_number"
            self._team_sort_order = "asc"
        self.show_teams()
        self.tabs.setCurrentWidget(self.teams_stack)

    def _configured_ai_provider(self) -> OpenAICompatibleConfig | None:
        try:
            # Re-read the file so model/provider changes do not require an app restart.
            self.personal_config = load_personal_config()
            return OpenAICompatibleConfig(
                endpoint=str(self.personal_config.get("ai_endpoint", "")),
                model=str(self.personal_config.get("ai_model", "")),
                api_key=str(self.personal_config.get("ai_api_key", "")),
                embedding_model=str(self.personal_config.get("ai_embedding_model", "")),
            ).validated()
        except (GeminiEnrichmentError, ValueError, OSError) as error:
            QMessageBox.warning(self, "AI provider configuration", str(error))
            return None

    def _refresh_repository(self) -> None:
        database_path = self.repository.database_path
        self.repository.close()
        self.repository = AnalyticsRepository(database_path)

    def show_picking(self) -> None:
        page, layout = scroll_page()
        self._page_header(
            layout,
            "Alliance picking",
            "The first eight available teams by rank are captains. When one is selected, the next available team moves up.",
        )
        event_key = self.repository.event_key()
        teams = self.repository.picking_teams()
        if event_key is None:
            self._empty_state(layout, "Import an event schedule before tracking alliance selections.")
            layout.addStretch(1)
            self._replace_stack(self.picking_stack, page)
            return
        if not teams:
            self._empty_state(layout, "No ranked teams are cached yet. Trying TBA now…")
            if self._picking_auto_sync_attempted_event_key != event_key:
                self._picking_auto_sync_attempted_event_key = event_key
                self._request_tba_results(silent=True)
            layout.addStretch(1)
            self._replace_stack(self.picking_stack, page)
            return

        if not any(team["official_rank"] is not None for team in teams):
            layout.addWidget(label("Official ranks are not cached yet. Syncing them from TBA…", name="muted"))
            if self._picking_auto_sync_attempted_event_key != event_key:
                self._picking_auto_sync_attempted_event_key = event_key
                self._request_tba_results(silent=True)

        selections = self.repository.alliance_selections()
        selections_by_alliance: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for selection in selections:
            selections_by_alliance[int(selection["alliance_number"])].append(selection)

        # Captains are always the highest-ranked teams that have not already
        # accepted a selection. This naturally promotes the next team when a
        # lower captain is selected by an alliance.
        automatic_captains = [team for team in teams if team["selected_alliance"] is None][:8]
        captain_by_team = {
            int(team["team_number"]): index + 1
            for index, team in enumerate(automatic_captains)
        }
        draft_order = [
            *( (alliance, 1) for alliance in range(1, 9) ),
            *( (alliance, 2) for alliance in range(8, 0, -1) ),
        ]
        next_pick = draft_order[len(selections)] if len(selections) < len(draft_order) else None

        draft_status = QHBoxLayout()
        draft_status.setContentsMargins(0, 0, 0, 0)
        if next_pick is None:
            draft_status.addWidget(label("Alliance selection complete", name="sectionTitle"))
        else:
            draft_status.addWidget(
                label(f"On deck: Alliance {next_pick[0]} pick {next_pick[1]}", name="sectionTitle")
            )
        draft_status.addStretch(1)
        undo_button = button("Undo last pick", "secondary", self._undo_latest_alliance_selection)
        undo_button.setEnabled(bool(selections))
        draft_status.addWidget(undo_button)
        layout.addLayout(draft_status)

        board = Card()
        board_layout = QGridLayout(board)
        board_layout.setContentsMargins(18, 15, 18, 15)
        board_layout.setHorizontalSpacing(18)
        board_layout.setVerticalSpacing(10)
        for alliance_number in range(1, 9):
            alliance_layout = QVBoxLayout()
            alliance_layout.setContentsMargins(0, 0, 0, 0)
            alliance_layout.setSpacing(6)
            alliance_layout.addWidget(label(f"Alliance {alliance_number}", name="sectionTitle"))
            captain = automatic_captains[alliance_number - 1] if len(automatic_captains) >= alliance_number else None
            if captain is None:
                alliance_layout.addWidget(label("Captain: pending", name="muted"))
            else:
                captain_rank = "-" if captain["official_rank"] is None else str(captain["official_rank"])
                alliance_layout.addWidget(
                    label(f"Captain: Team {captain['team_number']} (Rank {captain_rank})", name="muted")
                )
            assigned = selections_by_alliance[alliance_number]
            if assigned:
                for pick_number, selection in enumerate(assigned, start=1):
                    alliance_layout.addWidget(
                        label(f"Pick {pick_number}: Team {selection['team_number']}", name="muted")
                    )
            board_layout.addLayout(alliance_layout, (alliance_number - 1) // 4, (alliance_number - 1) % 4)
        for column in range(4):
            board_layout.setColumnStretch(column, 1)
        layout.addWidget(board)

        workspace = QHBoxLayout()
        workspace.setContentsMargins(0, 0, 0, 0)
        workspace.setSpacing(16)
        ranked_teams = QVBoxLayout()
        ranked_teams.setContentsMargins(0, 0, 0, 0)
        ranked_teams.addWidget(label("Teams by rank", name="sectionTitle"))
        team_grid = QGridLayout()
        team_grid.setContentsMargins(0, 0, 0, 0)
        team_grid.setHorizontalSpacing(12)
        team_grid.setVerticalSpacing(8)
        for column, title in enumerate(("RANK", "TEAM", "EPA", "STATUS", "")):
            team_grid.addWidget(label(title, name="eyebrow"), 0, column)
        available_teams = [
            team for team in teams
            if team["selected_alliance"] is None
            and int(team["team_number"]) not in captain_by_team
        ]
        for index, team in enumerate(available_teams):
            row = index + 1
            column = 0
            rank = "-" if team["official_rank"] is None else str(team["official_rank"])
            team_grid.addWidget(label(rank, name="muted"), row, column)
            team_grid.addWidget(label(f"Team {team['team_number']}", name="sectionTitle"), row, column + 1)
            team_grid.addWidget(label(numeric_metric(team["statbotics_total_epa"]), name="muted"), row, column + 2)
            team_grid.addWidget(label("Available", name="muted"), row, column + 3)
            select_button = button(
                "Select", "primary", lambda _checked=False, number=int(team["team_number"]): self._assign_alliance_selection(number)
            )
            select_button.setEnabled(next_pick is not None)
            team_grid.addWidget(select_button, row, column + 4)
        team_grid.setColumnStretch(1, 2)
        team_grid.setColumnStretch(3, 2)
        ranked_teams.addLayout(team_grid)
        ranked_teams.addStretch(1)
        workspace.addLayout(ranked_teams, 2)
        workspace.addWidget(self._picking_research_panel(teams), 3)
        layout.addLayout(workspace)
        layout.addStretch(1)
        self._replace_stack(self.picking_stack, page)

    def _assign_alliance_selection(self, team_number: int) -> None:
        event_key = self.repository.event_key()
        if event_key is None:
            return
        selections = self.repository.alliance_selections()
        draft_order = [
            *( (alliance, 1) for alliance in range(1, 9) ),
            *( (alliance, 2) for alliance in range(8, 0, -1) ),
        ]
        if len(selections) >= len(draft_order):
            QMessageBox.information(self, "Alliance selection", "All alliance picks have been recorded.")
            return
        alliance_number, _pick_number = draft_order[len(selections)]
        try:
            add_alliance_selection(
                self.repository.database_path,
                event_key,
                alliance_number,
                team_number,
                "pick",
            )
        except PickListError as error:
            QMessageBox.warning(self, "Could not update picking", str(error))
            return
        self._refresh_repository()
        self.show_picking()

    def _undo_latest_alliance_selection(self) -> None:
        event_key = self.repository.event_key()
        if event_key is None:
            return
        try:
            if not remove_latest_alliance_selection(self.repository.database_path, event_key):
                return
        except PickListError as error:
            QMessageBox.warning(self, "Could not undo selection", str(error))
            return
        self._refresh_repository()
        self.show_picking()

    def _remove_alliance_selection(self, team_number: int) -> None:
        event_key = self.repository.event_key()
        if event_key is None:
            return
        try:
            remove_alliance_selection(self.repository.database_path, event_key, team_number)
        except PickListError as error:
            QMessageBox.warning(self, "Could not update picking", str(error))
            return
        self._refresh_repository()
        self.show_picking()

    def _picking_research_panel(self, teams: list[sqlite3.Row]) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(540)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(14, 12, 14, 14)
        panel_layout.setSpacing(8)
        panel_layout.addWidget(label("Research", name="sectionTitle"))
        tabs = QTabWidget()
        pick_lists_page = QWidget()
        pick_lists_layout = QVBoxLayout(pick_lists_page)
        pick_lists_layout.setContentsMargins(4, 8, 4, 4)
        pick_lists_layout.setSpacing(8)
        self._render_picking_pick_lists(pick_lists_layout)
        tabs.addTab(pick_lists_page, "Pick Lists")

        research_page = QWidget()
        research_layout = QVBoxLayout(research_page)
        research_layout.setContentsMargins(4, 8, 4, 4)
        research_layout.setSpacing(8)
        self._render_picking_research(research_layout, teams)
        tabs.addTab(research_page, "Team Research")
        tabs.setCurrentIndex(self._picking_research_tab_index)
        tabs.currentChanged.connect(self._set_picking_research_tab)
        panel_layout.addWidget(tabs)
        return panel

    def _render_picking_pick_lists(self, layout: QVBoxLayout) -> None:
        scouts = self.repository.scouts()
        if not scouts:
            layout.addWidget(label("No scout pick lists yet.", name="muted"))
            layout.addStretch(1)
            return
        scout_ids = {str(scout["scout_id"]) for scout in scouts}
        if self._pick_list_scout_id not in scout_ids:
            self._pick_list_scout_id = str(scouts[0]["scout_id"])
        picker = QComboBox()
        for scout in scouts:
            picker.addItem(str(scout["display_name"]), str(scout["scout_id"]))
        picker.setCurrentIndex(max(0, picker.findData(self._pick_list_scout_id)))
        picker.currentIndexChanged.connect(
            lambda _index, box=picker: self._set_pick_list_scout(str(box.currentData()), refresh_picking=True)
        )
        layout.addWidget(picker)
        available_only = QCheckBox("Available teams only")
        available_only.setChecked(self._picking_research_available_only)
        available_only.toggled.connect(self._set_picking_research_available_only)
        layout.addWidget(available_only)
        available_team_numbers = self._available_picking_team_numbers(self.repository.picking_teams())
        team_metrics = {int(team["team_number"]): team for team in self.repository.teams()}
        pick_lists = self.repository.pick_lists_for_scout(self._pick_list_scout_id)
        if not pick_lists:
            layout.addWidget(label("No pick lists for this scout.", name="muted"))
            layout.addStretch(1)
            return
        for pick_list in pick_lists:
            list_layout = QVBoxLayout()
            list_layout.setContentsMargins(0, 8, 0, 8)
            list_layout.setSpacing(5)
            list_layout.addWidget(label(str(pick_list["name"]), name="sectionTitle"))
            for team in self.repository.pick_list_teams(str(pick_list["list_id"])):
                if (
                    self._picking_research_available_only
                    and int(team["team_number"]) not in available_team_numbers
                ):
                    continue
                rank = "-" if team["official_rank"] is None else str(team["official_rank"])
                detail = self._picking_team_metrics_detail(
                    team_metrics.get(int(team["team_number"])), team["statbotics_total_epa"]
                )
                if team["selected_alliance"] is not None:
                    detail += f"  •  Selected by Alliance {team['selected_alliance']}"
                notes = str(team["scout_notes"] or "")
                list_layout.addWidget(
                    self._picking_team_row(
                        int(team["team_number"]), rank, detail, notes,
                        selected=team["selected_alliance"] is not None,
                    )
                )
            layout.addLayout(list_layout)
        layout.addStretch(1)

    def _render_picking_research(self, layout: QVBoxLayout, teams: list[sqlite3.Row]) -> None:
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        query = QLineEdit()
        query.setPlaceholderText("Search scouting summaries")
        query.setText(self._picking_research_query)
        query.returnPressed.connect(lambda: self._request_team_search(query.text(), context="picking"))
        controls.addWidget(query, 1)
        self._team_search_button = button(
            "Search", "secondary", lambda: self._request_team_search(query.text(), context="picking")
        )
        controls.addWidget(self._team_search_button)
        layout.addLayout(controls)
        sort_picker = QComboBox()
        for title, value in (("Rank", "rank"), ("EPA", "epa"), ("Search relevance", "relevance")):
            sort_picker.addItem(title, value)
        sort_picker.setCurrentIndex(max(0, sort_picker.findData(self._picking_research_sort)))
        sort_picker.currentIndexChanged.connect(
            lambda _index, box=sort_picker: self._set_picking_research_sort(str(box.currentData()))
        )
        layout.addWidget(sort_picker)
        available_only = QCheckBox("Available teams only")
        available_only.setChecked(self._picking_research_available_only)
        available_only.toggled.connect(self._set_picking_research_available_only)
        layout.addWidget(available_only)

        results_by_team = {
            result.team_number: result for result in (self._picking_research_results or [])
        }
        research_teams = list(teams)
        if self._picking_research_available_only:
            available_team_numbers = self._available_picking_team_numbers(research_teams)
            research_teams = [
                team for team in research_teams if int(team["team_number"]) in available_team_numbers
            ]
        if self._picking_research_query.isdigit():
            research_teams = [
                team for team in research_teams
                if int(team["team_number"]) == int(self._picking_research_query)
            ]
        elif self._picking_research_results is not None:
            research_teams = [team for team in research_teams if int(team["team_number"]) in results_by_team]
        if self._picking_research_sort == "epa":
            research_teams.sort(key=lambda team: float(team["statbotics_total_epa"] or 0), reverse=True)
        elif self._picking_research_sort == "relevance":
            research_teams.sort(
                key=lambda team: (
                    results_by_team[int(team["team_number"])].score
                    if int(team["team_number"]) in results_by_team else -1.0
                ),
                reverse=True,
            )
        notes_by_team = self.repository.event_team_notes()
        summaries = self.repository.latest_team_summaries()
        team_metrics = {int(team["team_number"]): team for team in self.repository.teams()}
        if self._picking_research_results is not None:
            layout.addWidget(label(f'Semantic results for “{self._picking_research_query}”', name="muted"))
        for team in research_teams[:12]:
            number = int(team["team_number"])
            rank = "-" if team["official_rank"] is None else str(team["official_rank"])
            match = results_by_team.get(number)
            detail = self._picking_team_metrics_detail(
                team_metrics.get(number), team["statbotics_total_epa"]
            )
            if match is not None:
                detail += f"  •  Relevance {match.score:.2f}"
            summary = summaries.get(number)
            notes = notes_by_team.get(number)
            context = "  •  ".join(part for part in (summary, notes) if part)
            layout.addWidget(self._picking_team_row(number, rank, detail, context))
        if not research_teams:
            layout.addWidget(label("No teams match this research query.", name="muted"))
        layout.addStretch(1)

    def _picking_team_row(
        self, team_number: int, rank: str, detail: str, context: str, *, selected: bool = False
    ) -> ClickableCard:
        row = ClickableCard(
            lambda number=team_number: self.show_team_detail(number),
            tooltip="Open team details",
            shadow=False,
        )
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(10, 8, 10, 8)
        row_layout.setSpacing(3)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        team_label = label(f"#{rank}  Team {team_number}", name="sectionTitle")
        if selected:
            team_label.setStyleSheet("text-decoration: line-through; color: #97a5bc;")
        heading.addWidget(team_label)
        heading.addStretch(1)
        heading.addWidget(label(detail, name="muted"))
        row_layout.addLayout(heading)
        if context:
            row_layout.addWidget(label(self._short_text(context, 210), name="muted", word_wrap=True))
        return row

    @staticmethod
    def _available_picking_team_numbers(teams: list[sqlite3.Row]) -> set[int]:
        captain_numbers = {
            int(team["team_number"])
            for team in [team for team in teams if team["selected_alliance"] is None][:8]
        }
        return {
            int(team["team_number"])
            for team in teams
            if team["selected_alliance"] is None
            and int(team["team_number"]) not in captain_numbers
        }

    @staticmethod
    def _picking_team_metrics_detail(
        team: sqlite3.Row | None, event_epa: object
    ) -> str:
        detail = f"EPA {numeric_metric(event_epa)}"
        if team is None:
            return detail
        matches = int(team["matches_played"] or 0)
        if matches:
            detail += (
                f"  •  W/L {int(team['wins'] or 0)}-{int(team['losses'] or 0)}-{int(team['ties'] or 0)}"
                f"  •  Match pts {numeric_metric(team['average_actual_points'])}"
            )
        observations = int(team["observations"] or 0)
        if observations:
            detail += (
                f"  •  Scouted {numeric_metric(team['average_scouted_points'])}"
                f"  •  Penalties {numeric_metric(team['average_penalties_committed'])}"
                f"  •  {'BROKEN' if team['currently_broken'] else 'Operational'}"
                f" ({int(team['broken_match_count'] or 0)} broken match(es))"
            )
        return detail

    @staticmethod
    def _short_text(value: str, limit: int) -> str:
        return value if len(value) <= limit else f"{value[:limit - 1].rstrip()}…"

    def _set_picking_research_sort(self, sort_by: str) -> None:
        if sort_by != self._picking_research_sort:
            self._picking_research_sort = sort_by
            self.show_picking()

    def _set_picking_research_available_only(self, enabled: bool) -> None:
        if enabled != self._picking_research_available_only:
            self._picking_research_available_only = enabled
            self.show_picking()

    def _set_picking_research_tab(self, index: int) -> None:
        self._picking_research_tab_index = index

    def show_pick_lists(self) -> None:
        page, layout = scroll_page()
        self._page_header(
            layout,
            "Pick lists",
            "Each scout keeps their own event pick lists. Add teams from any match to start or update one.",
        )
        scouts = self.repository.scouts()
        if not scouts:
            self._empty_state(
                layout,
                "Import scouting data with at least one scout before creating a pick list.",
            )
            layout.addStretch(1)
            self._replace_stack(self.pick_lists_stack, page)
            return

        scout_ids = {str(scout["scout_id"]) for scout in scouts}
        if self._pick_list_scout_id not in scout_ids:
            self._pick_list_scout_id = str(scouts[0]["scout_id"])

        selector_card = Card()
        selector_layout = QHBoxLayout(selector_card)
        selector_layout.setContentsMargins(18, 14, 18, 14)
        selector_layout.setSpacing(10)
        selector_layout.addWidget(label("Viewing pick lists for", name="sectionTitle"))
        scout_picker = QComboBox()
        selected_index = 0
        for index, scout in enumerate(scouts):
            scout_id = str(scout["scout_id"])
            scout_picker.addItem(str(scout["display_name"]), scout_id)
            if scout_id == self._pick_list_scout_id:
                selected_index = index
        scout_picker.setCurrentIndex(selected_index)
        scout_picker.currentIndexChanged.connect(
            lambda _index, picker=scout_picker: self._set_pick_list_scout(
                str(picker.currentData())
            )
        )
        selector_layout.addWidget(scout_picker)
        selector_layout.addStretch(1)
        layout.addWidget(selector_card)

        selected_scout = str(scout_picker.currentText())
        pick_lists = self.repository.pick_lists_for_scout(self._pick_list_scout_id)
        if not pick_lists:
            self._empty_state(
                layout,
                f"{selected_scout} has no pick lists yet. Open a match and use “Add to pick list” beside a team.",
            )
        else:
            for pick_list in pick_lists:
                list_card = Card()
                list_layout = QVBoxLayout(list_card)
                list_layout.setContentsMargins(18, 15, 18, 15)
                list_layout.setSpacing(8)
                heading = QHBoxLayout()
                heading.setContentsMargins(0, 0, 0, 0)
                heading.addWidget(label(str(pick_list["name"]), name="sectionTitle"))
                heading.addStretch(1)
                heading.addWidget(
                    label(
                        f"{int(pick_list['team_count'])} team"
                        f"{'s' if int(pick_list['team_count']) != 1 else ''}",
                        name="eyebrow",
                    )
                )
                list_layout.addLayout(heading)
                list_layout.addWidget(
                    label(
                        f"Last updated {friendly_timestamp(pick_list['updated_at'])}",
                        name="muted",
                    )
                )
                teams = self.repository.pick_list_teams(str(pick_list["list_id"]))
                if not teams:
                    list_layout.addWidget(label("No teams in this list yet.", name="muted"))
                else:
                    team_header = QGridLayout()
                    team_header.setContentsMargins(8, 2, 8, 2)
                    team_header.addWidget(label("RANK", name="eyebrow"), 0, 0)
                    team_header.addWidget(label("TEAM", name="eyebrow"), 0, 1)
                    team_header.addWidget(label("STATBOTICS EPA", name="eyebrow"), 0, 2)
                    team_header.addWidget(label("NOTES", name="eyebrow"), 0, 3)
                    team_header.addWidget(label("ADDED", name="eyebrow"), 0, 4)
                    team_header.setColumnStretch(1, 2)
                    team_header.setColumnStretch(3, 3)
                    list_layout.addLayout(team_header)
                    for team in teams:
                        row = ClickableCard(
                            lambda number=int(team["team_number"]): self.show_team_detail(number),
                            tooltip="Open team details",
                            shadow=False,
                        )
                        row_layout = QGridLayout(row)
                        row_layout.setContentsMargins(9, 7, 9, 7)
                        row_layout.setHorizontalSpacing(10)
                        team_label = label(f"Team {team['team_number']}", name="sectionTitle")
                        if team["selected_alliance"] is not None:
                            team_label.setStyleSheet("text-decoration: line-through; color: #97a5bc;")
                            team_label.setToolTip(
                                f"Selected by Alliance {team['selected_alliance']} as a {team['selection_kind']}."
                            )
                        rank = "-" if team["official_rank"] is None else str(team["official_rank"])
                        row_layout.addWidget(label(rank, name="muted"), 0, 0)
                        row_layout.addWidget(team_label, 0, 1)
                        row_layout.addWidget(
                            label(numeric_metric(team["statbotics_total_epa"]), name="muted"), 0, 2
                        )
                        notes = str(team["scout_notes"] or "No scout notes")
                        row_layout.addWidget(label(self._short_text(notes, 180), name="muted", word_wrap=True), 0, 3)
                        row_layout.addWidget(label(friendly_timestamp(team["added_at"]), name="muted"), 0, 4)
                        row_layout.setColumnStretch(1, 2)
                        row_layout.setColumnStretch(3, 3)
                        list_layout.addWidget(row)
                layout.addWidget(list_card)
        layout.addStretch(1)
        self._replace_stack(self.pick_lists_stack, page)

    def _set_pick_list_scout(self, scout_id: str, *, refresh_picking: bool = False) -> None:
        if scout_id and scout_id != self._pick_list_scout_id:
            self._pick_list_scout_id = scout_id
            if refresh_picking:
                self.show_picking()
            else:
                self.show_pick_lists()

    def _page_header(self, layout: QVBoxLayout, title: str, subtitle: str) -> None:
        layout.addWidget(label(title, name="pageTitle"))
        layout.addWidget(label(subtitle, name="muted", word_wrap=True))

    def _chart_card(
        self,
        title: str,
        events: list[sqlite3.Row],
        divisor: int = 1,
        include_average: bool = False,
        comparison_events: list[sqlite3.Row] | None = None,
        comparison_divisor: int = 1,
        comparison_label: str = "Event avg.",
        primary_color: str | None = None,
    ) -> QWidget:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.addWidget(label(title, name="sectionTitle"))
        subtitle = "10-second Gaussian smoothing; each line represents a team in a match view."
        if include_average:
            subtitle += " The dashed white line is the match average."
        elif comparison_events is not None:
            subtitle = "10-second Gaussian smoothing. Solid: this team; dashed white: event average."
        layout.addWidget(label(subtitle, name="muted"))
        layout.addWidget(
            SmoothChart(
                events,
                divisor,
                include_average,
                comparison_events,
                comparison_divisor,
                comparison_label,
                False,
                primary_color,
            )
        )
        return card

    def _text_card(self, title: str, text: str) -> QWidget:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 15, 18, 15)
        layout.addWidget(label(title, name="sectionTitle"))
        layout.addWidget(label(text, name="muted", word_wrap=True))
        return card

    def _empty_state(self, layout: QVBoxLayout, text: str) -> None:
        card = Card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(18, 34, 18, 34)
        card_layout.setSpacing(6)
        icon = label("◇", name="emptyIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(icon)
        title = label("Nothing here yet", name="sectionTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)
        body = label(text, name="muted", word_wrap=True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(body)
        layout.addWidget(card)

    def _replace_stack(self, stack: QStackedWidget, page: QWidget) -> None:
        while stack.count():
            old_page = stack.widget(0)
            stack.removeWidget(old_page)
            old_page.deleteLater()
        stack.addWidget(page)
        stack.setCurrentWidget(page)

    def _match_status(self, match: sqlite3.Row) -> str:
        if match["result_status"] == "final" and match["red_score"] is not None:
            return f"Official: Red {match['red_score']} – Blue {match['blue_score']}"
        observation_count = int(match["observation_count"])
        return f"{observation_count} scouted observation" + ("s" if observation_count != 1 else "")

    def closeEvent(self, event: object) -> None:  # Qt invokes this method.
        self.repository.close()
        super().closeEvent(event)


def win_ratio_color(ratio: float) -> str:
    if ratio >= 0.6:
        return ACCENT
    if ratio >= 0.4:
        return WARNING
    return RED


def numeric_metric(value: Any, suffix: str = "") -> str:
    return "—" if value is None else f"{float(value):.1f}{(' ' + suffix) if suffix else ''}"


def official_number(value: Any) -> str:
    return "—" if value is None else str(int(value))


def comparison_text(value: Any, suffix: str = "") -> str:
    return "No event baseline yet" if value is None else f"Event avg. {float(value):.1f}{(' ' + suffix) if suffix else ''}"


def friendly_timestamp(value: Any) -> str:
    """Render a stored ISO-8601 timestamp (e.g. "2026-07-27T18:29:47+00:00") for humans."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    now = datetime.now(parsed.tzinfo)
    seconds_ago = (now - parsed).total_seconds()

    hour12 = parsed.hour % 12 or 12
    period = "AM" if parsed.hour < 12 else "PM"
    time_of_day = f"{hour12}:{parsed.minute:02d} {period}"

    if seconds_ago < 0:
        return f"{parsed.strftime('%b')} {parsed.day} at {time_of_day}"
    if seconds_ago < 60:
        return "Just now"
    if seconds_ago < 3600:
        minutes = int(seconds_ago // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if now.date() == parsed.date():
        hours = int(seconds_ago // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days_ago = (now.date() - parsed.date()).days
    if days_ago == 1:
        return f"Yesterday at {time_of_day}"
    if parsed.year == now.year:
        return f"{parsed.strftime('%b')} {parsed.day} at {time_of_day}"
    return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year} at {time_of_day}"


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Analytics")
    app.setStyleSheet(APP_STYLESHEET)
    database_path = resolve_database_path()
    if not database_path.is_file():
        QMessageBox.critical(
            None,
            "Analytics database missing",
            f"Could not find the analytics database at:\n{database_path}\n\nRun python Analytics/database.py first.",
        )
        return 1
    try:
        repository = AnalyticsRepository(database_path)
    except sqlite3.DatabaseError as error:
        QMessageBox.critical(None, "Analytics database", f"Could not open {database_path}: {error}")
        return 1
    window = AnalyticsWindow(repository)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
