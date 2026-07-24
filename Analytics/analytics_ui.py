"""Qt desktop dashboard for the offline SpeechScout analytics database.

Run from the repository root with the project virtual environment:

    .venv/bin/python Analytics/analytics_ui.py
"""

from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable, Iterable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
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
        ResultSyncError,
        sync_official_results_from_tba,
    )
    from .gemini import (
        EnrichmentReport,
        GeminiEnrichmentError,
        TeamSearchResult,
        enrich_database,
        search_teams,
    )
except ImportError:  # Supports ``python Analytics/analytics_ui.py``.
    from database import (
        ANALYTICS_DIRECTORY,
        DEFAULT_DATABASE_PATH,
        ResultSyncError,
        sync_official_results_from_tba,
    )
    from gemini import (
        EnrichmentReport,
        GeminiEnrichmentError,
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
CHART_COLORS = (RED, ACCENT, BLUE, PURPLE, WARNING, "#ff9f6e")

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
QLabel#pageTitle {{
    color: {TEXT};
    font-size: 24px;
    font-weight: 700;
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
    background: {SURFACE};
    border: 1px solid #273247;
    border-radius: 14px;
}}
QFrame#clickableCard {{
    background: {SURFACE};
    border: 1px solid #273247;
    border-radius: 14px;
}}
QFrame#clickableCard:hover {{
    background: #1a2433;
    border: 1px solid #3c5474;
}}
QFrame#metricCard {{
    background: {SURFACE};
    border: 1px solid #273247;
    border-radius: 12px;
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
    border-radius: 8px;
    font-weight: 600;
    padding: 8px 12px;
}}
QPushButton#primary {{
    background: {ACCENT};
    color: #05241a;
}}
QPushButton#primary:hover {{
    background: #75e7bd;
}}
QPushButton#secondary {{
    background: {SURFACE_ALT};
    color: {TEXT};
}}
QPushButton#secondary:hover {{
    background: #31405b;
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
QLineEdit {{
    background: #111824;
    color: {TEXT};
    border: 1px solid #34435c;
    border-radius: 8px;
    padding: 8px 10px;
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}
QTabWidget::pane {{
    border: 0;
    background: {BACKGROUND};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {MUTED};
    border: 0;
    border-radius: 8px;
    padding: 9px 16px;
    margin-right: 4px;
    font-weight: 600;
}}
QTabBar::tab:hover {{
    background: #171f2d;
    color: {TEXT};
}}
QTabBar::tab:selected {{
    background: {SURFACE_ALT};
    color: {TEXT};
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
    """Read-only queries used by the Qt dashboard."""

    def __init__(self, database_path: Path):
        self.database_path = database_path.resolve()
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
            WITH stats AS (
                SELECT
                    team_number,
                    COUNT(*) AS observations,
                    AVG(scouted_points) AS average_points,
                    AVG(penalties_committed) AS average_penalties_committed,
                    AVG(breakdown_count) AS average_breakdowns
                FROM v_team_match_totals
                GROUP BY team_number
            )
            SELECT
                t.team_number,
                COALESCE(s.observations, 0) AS observations,
                s.average_points,
                s.average_penalties_committed,
                s.average_breakdowns
            FROM teams AS t
            LEFT JOIN stats AS s ON s.team_number = t.team_number
            ORDER BY CASE WHEN s.observations IS NULL THEN 1 ELSE 0 END, t.team_number
            """
        )

    def team_overview(self, team_number: int) -> sqlite3.Row | None:
        return self._one(
            """
            WITH team_stats AS (
                SELECT
                    COUNT(*) AS observations,
                    AVG(scouted_points) AS average_points,
                    AVG(penalties_committed) AS average_penalties_committed,
                    AVG(breakdown_count) AS average_breakdowns
                FROM v_team_match_totals
                WHERE team_number = ?
            ),
            event_stats AS (
                SELECT
                    AVG(scouted_points) AS event_average_points,
                    AVG(penalties_committed) AS event_average_penalties_committed,
                    AVG(breakdown_count) AS event_average_breakdowns
                FROM v_team_match_totals
            )
            SELECT t.team_number, team_stats.*, event_stats.*
            FROM teams AS t
            CROSS JOIN team_stats
            CROSS JOIN event_stats
            WHERE t.team_number = ?
            """,
            (team_number, team_number),
        )

    def team_matches(self, team_number: int) -> list[sqlite3.Row]:
        return self._all(
            """
            SELECT
                m.match_key, m.match_number, v.scouted_points, v.penalties_committed,
                v.breakdown_count, v.schedule_alignment, s.display_name AS scout_name,
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
    def __init__(self, parent: QWidget | None = None, *, metric: bool = False):
        super().__init__(parent)
        self.setObjectName("metricCard" if metric else "card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.setGraphicsEffect(shadow)


class ClickableCard(Card):
    """A card that opens its detail view from any unused part of the card."""

    def __init__(
        self,
        callback: Callable[[], None],
        parent: QWidget | None = None,
        *,
        tooltip: str = "Open details",
    ):
        super().__init__(parent)
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


class GeminiEnrichmentWorker(QThread):
    """Runs summary generation and embedding outside the Qt event loop."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, database_path: Path, api_key: str):
        super().__init__()
        self.database_path = database_path
        self.api_key = api_key

    def run(self) -> None:
        try:
            report = enrich_database(self.database_path, self.api_key)
        except GeminiEnrichmentError as error:
            self.failed.emit(str(error))
        except Exception as error:  # Keep unexpected API/SQLite failures in the UI.
            self.failed.emit(f"Could not generate Gemini summaries: {error}")
        else:
            self.completed.emit(report)
        finally:
            self.api_key = ""


class GeminiSearchWorker(QThread):
    """Embeds a query without freezing the teams tab."""

    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, database_path: Path, api_key: str, query: str):
        super().__init__()
        self.database_path = database_path
        self.api_key = api_key
        self.query = query

    def run(self) -> None:
        try:
            results = search_teams(self.database_path, self.api_key, self.query)
        except GeminiEnrichmentError as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(f"Could not search Gemini summaries: {error}")
        else:
            self.completed.emit(results)
        finally:
            self.api_key = ""


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
        self._gemini_enrichment_worker: GeminiEnrichmentWorker | None = None
        self._gemini_enrichment_button: QPushButton | None = None
        self._gemini_search_worker: GeminiSearchWorker | None = None
        self._gemini_api_key = ""
        self._team_search_query = ""
        self._team_search_results: list[TeamSearchResult] | None = None
        self._team_search_button: QPushButton | None = None
        self.setWindowTitle("Analytics")
        self.setMinimumSize(980, 680)
        self.resize(1240, 820)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 16, 28, 18)
        layout.setSpacing(8)

        self.tabs = QTabWidget()
        self.matches_stack = QStackedWidget()
        self.teams_stack = QStackedWidget()
        self.scouts_stack = QStackedWidget()
        self.picking_stack = QStackedWidget()
        self.tabs.addTab(self.matches_stack, "Matches")
        self.tabs.addTab(self.teams_stack, "Teams")
        self.tabs.addTab(self.scouts_stack, "Scouts")
        self.tabs.addTab(self.picking_stack, "Picking")
        layout.addWidget(self.tabs, 1)

        self.show_matches()
        self.show_teams()
        self.show_scouts()
        self.show_picking()

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
        self._gemini_enrichment_button = button(
            "Generate Gemini summaries", "secondary", self._request_gemini_enrichment
        )
        actions.addWidget(self._gemini_enrichment_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        matches = self.repository.matches()
        if not matches:
            self._empty_state(layout, "No schedule has been imported yet.")
        for match in matches:
            layout.addWidget(self._match_card(match))
        layout.addStretch(1)
        self._replace_stack(self.matches_stack, page)

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

    def show_teams(self) -> None:
        page, layout = scroll_page()
        self._page_header(layout, "Teams", "Average team contribution per scouted match. Open a team to compare it with the event average.")
        search_controls = QHBoxLayout()
        search_controls.setContentsMargins(0, 0, 0, 0)
        search_controls.setSpacing(8)
        search_input = QLineEdit()
        search_input.setPlaceholderText("Search generated role and match summaries…")
        search_input.setText(self._team_search_query)
        search_input.returnPressed.connect(lambda: self._request_team_search(search_input.text()))
        search_controls.addWidget(search_input, 1)
        self._team_search_button = button(
            "Search summaries", "secondary", lambda _checked=False: self._request_team_search(search_input.text())
        )
        search_controls.addWidget(self._team_search_button)
        if self._team_search_query:
            search_controls.addWidget(button("Clear", "link", self._clear_team_search))
        layout.addLayout(search_controls)
        if self._team_search_results is not None:
            layout.addWidget(
                label(
                    f'Semantic results for “{self._team_search_query}”. Match summaries carry most of the ranking weight.',
                    name="muted",
                    word_wrap=True,
                )
            )
        teams = self.repository.teams()
        result_by_team = {
            result.team_number: result for result in (self._team_search_results or [])
        }
        if self._team_search_results is not None:
            teams = [team for team in teams if int(team["team_number"]) in result_by_team]
            teams.sort(key=lambda team: -result_by_team[int(team["team_number"])].score)
        event_events = self.repository.event_events()
        event_observations = self.repository.observation_count()
        events_by_team: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for event in event_events:
            events_by_team[int(event["team_number"])].append(event)
        if not teams:
            empty_message = (
                "No indexed summaries matched this search. Generate Gemini summaries first, or try another role."
                if self._team_search_results is not None
                else "No teams have been imported yet."
            )
            self._empty_state(layout, empty_message)
        for team in teams:
            team_number = int(team["team_number"])
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
            if team["observations"]:
                detail = "Imported scouting observations"
            else:
                detail = "On schedule • not yet scouted"
            identity.addWidget(label(detail, name="muted"))
            search_result = result_by_team.get(team_number)
            if search_result is not None:
                identity.addWidget(
                    label(
                        f"Role fit {search_result.score:.2f} • {search_result.match_hits} matching match summary(s)",
                        name="muted",
                    )
                )
            header.addLayout(identity, 1)

            if team["observations"]:
                score = QVBoxLayout()
                score.setSpacing(0)
                score_label = label("AVERAGE POINTS / MATCH", name="eyebrow")
                score_label.setAlignment(Qt.AlignmentFlag.AlignRight)
                score.addWidget(score_label)
                score_value = label(f"{team['average_points']:.1f}", name="teamScore")
                score_value.setAlignment(Qt.AlignmentFlag.AlignRight)
                score.addWidget(score_value)
                header.addLayout(score)
            card_layout.addLayout(header)

            if team["observations"]:
                penalty_average = float(team["average_penalties_committed"] or 0)
                breakdown_average = float(team["average_breakdowns"] or 0)
                stats = QHBoxLayout()
                stats.setContentsMargins(0, 0, 0, 0)
                stats.setSpacing(8)
                stats.addWidget(
                    self._team_stat(
                        "SCOUTED MATCHES",
                        str(int(team["observations"])),
                        "observations",
                    ),
                    1,
                )
                stats.addWidget(
                    self._team_stat("COMMITTED PENALTIES", f"{penalty_average:.1f}", "per match"), 1
                )
                stats.addWidget(
                    self._team_stat("BREAKDOWNS", f"{breakdown_average:.1f}", "per match"), 1
                )
                card_layout.addLayout(stats)

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
                    int(team["observations"]),
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

    def _team_stat(self, title: str, value: str, detail: str) -> QFrame:
        stat = QFrame()
        stat.setObjectName("teamStat")
        stat_layout = QVBoxLayout(stat)
        stat_layout.setContentsMargins(12, 8, 12, 8)
        stat_layout.setSpacing(1)
        stat_layout.addWidget(label(title, name="eyebrow"))
        stat_layout.addWidget(label(value, name="teamStatValue"))
        stat_layout.addWidget(label(detail, name="teamStatLabel"))
        return stat

    def show_team_detail(self, team_number: int) -> None:
        overview = self.repository.team_overview(team_number)
        if overview is None:
            self.show_teams()
            return
        page, layout = scroll_page()
        layout.addWidget(button("←  Back to teams", "secondary", self.show_teams), alignment=Qt.AlignmentFlag.AlignLeft)
        self._page_header(layout, f"Team {team_number}", "All comparisons use the event-wide average of imported scouting observations.")
        metrics = QGridLayout()
        metrics.setHorizontalSpacing(9)
        metrics.setVerticalSpacing(9)
        values = (
            ("Scouted matches", str(int(overview["observations"] or 0)), "Imported observations"),
            ("Average points", numeric_metric(overview["average_points"], "pts"), comparison_text(overview["event_average_points"], "pts")),
            ("Penalties committed", numeric_metric(overview["average_penalties_committed"]), comparison_text(overview["event_average_penalties_committed"])),
            ("Breakdowns", numeric_metric(overview["average_breakdowns"]), comparison_text(overview["event_average_breakdowns"])),
        )
        for column, (metric_name, value, detail) in enumerate(values):
            metric_card = Card(metric=True)
            metric_layout = QVBoxLayout(metric_card)
            metric_layout.setContentsMargins(15, 13, 15, 13)
            metric_layout.addWidget(label(metric_name, name="muted"))
            metric_layout.addWidget(label(value, name="metric"))
            metric_layout.addWidget(label(detail, name="muted", word_wrap=True))
            metrics.addWidget(metric_card, 0, column)
        metrics_host = QWidget()
        metrics_host.setLayout(metrics)
        layout.addWidget(metrics_host)

        summary = self.repository.latest_team_summary(team_number)
        layout.addWidget(self._text_card("Role summary", summary or "Not generated yet. Gemini summaries are added in the enrichment stage."))
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
            history_layout.addWidget(button(f"Match {match['match_number']}  •  {match['scouted_points']} pts  •  {match['breakdown_count']} breakdowns  •  {match['scout_name']}{mismatch}", "link", lambda _checked=False, key=match["match_key"]: self.show_match_detail(key)))
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
            metric_card = Card(metric=True)
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

    def _request_tba_results(self) -> None:
        if self._result_sync_worker is not None and self._result_sync_worker.isRunning():
            return
        event_key = self.repository.event_key()
        if event_key is None:
            QMessageBox.warning(self, "TBA results", "No event key has been imported into this database yet.")
            return
        api_key, accepted = QInputDialog.getText(
            self,
            "Sync official TBA results",
            f"TBA API key for {event_key} (used once and not saved):",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not api_key.strip():
            return

        worker = TbaResultWorker(self.repository.database_path, event_key, api_key)
        self._result_sync_worker = worker
        worker.completed.connect(self._official_results_synced)
        worker.failed.connect(self._official_results_sync_failed)
        worker.finished.connect(self._result_sync_finished)
        worker.finished.connect(worker.deleteLater)
        if self._result_sync_button is not None:
            self._result_sync_button.setEnabled(False)
            self._result_sync_button.setText("Syncing official results…")
        worker.start()

    def _official_results_synced(self, updated_matches: int) -> None:
        self.show_matches()
        self.show_scouts()
        detail = (
            f"Updated {updated_matches} local qualification match"
            f"{'es' if updated_matches != 1 else ''} with final TBA results."
        )
        QMessageBox.information(self, "Official results synced", detail)

    def _official_results_sync_failed(self, message: str) -> None:
        if self._result_sync_button is not None:
            self._result_sync_button.setEnabled(True)
            self._result_sync_button.setText("Sync official results from TBA")
        QMessageBox.warning(self, "Could not sync TBA results", message)

    def _result_sync_finished(self) -> None:
        if self._result_sync_worker is not None and not self._result_sync_worker.isRunning():
            self._result_sync_worker = None

    def _request_gemini_enrichment(self) -> None:
        if self._gemini_enrichment_worker is not None and self._gemini_enrichment_worker.isRunning():
            return
        api_key = self._request_gemini_api_key("Generate Gemini summaries")
        if not api_key:
            return
        worker = GeminiEnrichmentWorker(self.repository.database_path, api_key)
        self._gemini_enrichment_worker = worker
        worker.completed.connect(self._gemini_enrichment_completed)
        worker.failed.connect(self._gemini_enrichment_failed)
        worker.finished.connect(self._gemini_enrichment_finished)
        worker.finished.connect(worker.deleteLater)
        if self._gemini_enrichment_button is not None:
            self._gemini_enrichment_button.setEnabled(False)
            self._gemini_enrichment_button.setText("Generating Gemini summaries…")
        worker.start()

    def _gemini_enrichment_completed(self, report: EnrichmentReport) -> None:
        self._refresh_repository()
        self._team_search_query = ""
        self._team_search_results = None
        self.show_matches()
        self.show_teams()
        self.tabs.setCurrentWidget(self.matches_stack)
        QMessageBox.information(
            self,
            "Gemini summaries generated",
            (
                f"Saved {report.match_summaries} match contribution summar"
                f"{'y' if report.match_summaries == 1 else 'ies'}, "
                f"{report.team_summaries} team role summar"
                f"{'y' if report.team_summaries == 1 else 'ies'}, and "
                f"{report.embedding_chunks} search chunk"
                f"{'s' if report.embedding_chunks != 1 else ''}.\n\n"
                f"Updated {report.json_files_updated} scouting JSON file"
                f"{'s' if report.json_files_updated != 1 else ''} with their match summary."
            ),
        )

    def _gemini_enrichment_failed(self, message: str) -> None:
        if self._gemini_enrichment_button is not None:
            self._gemini_enrichment_button.setEnabled(True)
            self._gemini_enrichment_button.setText("Generate Gemini summaries")
        QMessageBox.warning(self, "Could not generate Gemini summaries", message)

    def _gemini_enrichment_finished(self) -> None:
        if (
            self._gemini_enrichment_worker is not None
            and not self._gemini_enrichment_worker.isRunning()
        ):
            self._gemini_enrichment_worker = None

    def _request_team_search(self, query: str) -> None:
        clean_query = " ".join(query.split())
        if not clean_query:
            self._clear_team_search()
            return
        if self._gemini_search_worker is not None and self._gemini_search_worker.isRunning():
            return
        api_key = self._request_gemini_api_key("Search Gemini summaries")
        if not api_key:
            return
        self._team_search_query = clean_query
        worker = GeminiSearchWorker(self.repository.database_path, api_key, clean_query)
        self._gemini_search_worker = worker
        worker.completed.connect(self._team_search_completed)
        worker.failed.connect(self._team_search_failed)
        worker.finished.connect(self._team_search_finished)
        worker.finished.connect(worker.deleteLater)
        if self._team_search_button is not None:
            self._team_search_button.setEnabled(False)
            self._team_search_button.setText("Searching…")
        worker.start()

    def _team_search_completed(self, results: object) -> None:
        self._team_search_results = list(results) if isinstance(results, list) else []
        self.show_teams()
        self.tabs.setCurrentWidget(self.teams_stack)

    def _team_search_failed(self, message: str) -> None:
        if self._team_search_button is not None:
            self._team_search_button.setEnabled(True)
            self._team_search_button.setText("Search summaries")
        QMessageBox.warning(self, "Could not search summaries", message)

    def _team_search_finished(self) -> None:
        if self._gemini_search_worker is not None and not self._gemini_search_worker.isRunning():
            self._gemini_search_worker = None

    def _clear_team_search(self) -> None:
        self._team_search_query = ""
        self._team_search_results = None
        self.show_teams()
        self.tabs.setCurrentWidget(self.teams_stack)

    def _request_gemini_api_key(self, title: str) -> str | None:
        if self._gemini_api_key:
            return self._gemini_api_key
        api_key, accepted = QInputDialog.getText(
            self,
            title,
            "Gemini API key (kept only while this app is open; never saved):",
            QLineEdit.EchoMode.Password,
        )
        if not accepted or not api_key.strip():
            return None
        self._gemini_api_key = api_key.strip()
        return self._gemini_api_key

    def _refresh_repository(self) -> None:
        database_path = self.repository.database_path
        self.repository.close()
        self.repository = AnalyticsRepository(database_path)

    def show_picking(self) -> None:
        page, layout = scroll_page()
        self._page_header(layout, "Picking", "Alliance-selection tools will appear here once the scouting model is complete.")
        self._empty_state(layout, "Picking is intentionally blank for now.")
        layout.addStretch(1)
        self._replace_stack(self.picking_stack, page)

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
        layout.addWidget(self._text_card("Nothing here yet", text))

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
        self._gemini_api_key = ""
        self.repository.close()
        super().closeEvent(event)


def numeric_metric(value: Any, suffix: str = "") -> str:
    return "—" if value is None else f"{float(value):.1f}{(' ' + suffix) if suffix else ''}"


def official_number(value: Any) -> str:
    return "—" if value is None else str(int(value))


def comparison_text(value: Any, suffix: str = "") -> str:
    return "No event baseline yet" if value is None else f"Event avg. {float(value):.1f}{(' ' + suffix) if suffix else ''}"


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
