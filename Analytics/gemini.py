"""OpenAI-compatible AI enrichment for locally stored SpeechScout data.

The caller supplies the endpoint, model, and API key. Generated summaries and
embeddings are cached locally so they remain available after the network is
gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


SUMMARY_PROMPT_VERSION = "match-role-v1"
EMBEDDING_PROMPT_VERSION = "retrieval-v1"
MAX_SUMMARY_CHARS = 420
# A request gets one initial attempt plus this many retries. The wait is capped
# so a background request cannot leave the desktop UI stuck indefinitely.
MAX_RATE_LIMIT_RETRIES = 4
MAX_RATE_LIMIT_WAIT_SECONDS = 60
# Models occasionally ignore the JSON-only instruction. Retry a bad summary
# response once, then leave that one work item for a later run.
MAX_UNUSABLE_SUMMARY_RETRIES = 1

ProgressCallback = Callable[[int, int, str], None]
StatusCallback = Callable[[str], None]


class GeminiEnrichmentError(RuntimeError):
    """Raised for a recoverable AI-provider or local enrichment failure."""


class UnusableSummaryResponseError(GeminiEnrichmentError):
    """A model response was malformed or incomplete after its retry."""


@dataclass(frozen=True)
class OpenAICompatibleConfig:
    """Connection details for a standard OpenAI-compatible REST server."""

    endpoint: str
    model: str
    api_key: str
    embedding_model: str = ""

    def validated(self) -> "OpenAICompatibleConfig":
        endpoint = self.endpoint.strip().rstrip("/")
        model = self.model.strip()
        api_key = self.api_key.strip()
        if not endpoint.startswith(("http://", "https://")):
            raise GeminiEnrichmentError("AI endpoint must start with http:// or https://.")
        if not model:
            raise GeminiEnrichmentError("Set ai_model in personal.json before generating summaries.")
        if not api_key:
            raise GeminiEnrichmentError("Set ai_api_key in personal.json before generating summaries.")
        return OpenAICompatibleConfig(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            embedding_model=self.embedding_model.strip() or model,
        )


@dataclass(frozen=True)
class EnrichmentReport:
    match_summaries: int
    team_summaries: int
    embedding_chunks: int
    json_files_updated: int
    skipped_match_batches: int
    skipped_team_summaries: int


@dataclass(frozen=True)
class TeamSearchResult:
    team_number: int
    score: float
    match_hits: int
    best_match_score: float
    team_summary_score: float | None


def enrich_database(
    database_path: Path | str,
    provider: OpenAICompatibleConfig,
    progress_callback: ProgressCallback | None = None,
) -> EnrichmentReport:
    """Generate short summaries and a local semantic-search index.

    A match generation request contains every imported event and note for that
    match, not only the target team's observation.  Team summaries use all of
    that team's imported observations plus the generated match summaries.
    """
    provider = provider.validated()
    connection = sqlite3.connect(Path(database_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        event = connection.execute(
            "SELECT event_key FROM events ORDER BY event_key LIMIT 1"
        ).fetchone()
        if event is None:
            raise GeminiEnrichmentError("No event has been imported into the analytics database yet.")
        event_key = str(event["event_key"])

        _report_progress(progress_callback, 0, 0, "Checking which AI summaries are already current…")
        completed_work = 0
        total_work = _pending_match_summary_batch_count(connection)
        _report_progress(
            progress_callback,
            completed_work,
            total_work,
            _work_status("Generating match summaries", completed_work, total_work),
        )
        match_summaries, json_updates, skipped_match_batches = _enrich_match_summaries(
            connection,
            provider,
            progress_callback=progress_callback,
            completed_work=completed_work,
            total_work=total_work,
        )
        completed_work = total_work

        team_work = _pending_team_summary_count(connection, event_key)
        total_work += team_work
        _report_progress(
            progress_callback,
            completed_work,
            total_work,
            _work_status("Generating team role summaries", 0, team_work),
        )
        team_summaries, skipped_team_summaries = _enrich_team_summaries(
            connection,
            event_key,
            provider,
            progress_callback=progress_callback,
            completed_work=completed_work,
            total_work=total_work,
        )
        completed_work = total_work

        embedding_work = _pending_embedding_chunk_count(connection, event_key, provider)
        total_work += embedding_work
        _report_progress(
            progress_callback,
            completed_work,
            total_work,
            _work_status("Building the AI search index", 0, embedding_work),
        )
        embedding_chunks = _refresh_embedding_chunks(
            connection,
            event_key,
            provider,
            progress_callback=progress_callback,
            completed_work=completed_work,
            total_work=total_work,
        )
        connection.commit()
        _report_progress(progress_callback, total_work, total_work, "AI summaries and search index are ready.")
        return EnrichmentReport(
            match_summaries=match_summaries,
            team_summaries=team_summaries,
            embedding_chunks=embedding_chunks,
            json_files_updated=json_updates,
            skipped_match_batches=skipped_match_batches,
            skipped_team_summaries=skipped_team_summaries,
        )
    except sqlite3.DatabaseError as error:
        connection.rollback()
        raise GeminiEnrichmentError(f"Could not update the local analytics database: {error}") from error
    finally:
        connection.close()


def search_teams(
    database_path: Path | str,
    provider: OpenAICompatibleConfig,
    query: str,
) -> list[TeamSearchResult]:
    """Refresh missing embeddings, then rank teams by summary similarity."""
    clean_query = " ".join(query.split())
    if not clean_query:
        return []
    provider = provider.validated()
    refresh_search_embeddings(database_path, provider)
    connection = sqlite3.connect(f"{Path(database_path).resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        chunks = connection.execute(
            """
            SELECT team_number, chunk_type, embedding
            FROM embedding_chunks
            WHERE chunk_type IN ('match_summary', 'team_summary')
            """
        ).fetchall()
    except sqlite3.DatabaseError as error:
        raise GeminiEnrichmentError(f"Could not read the local search index: {error}") from error
    finally:
        connection.close()

    if not chunks:
        return []
    query_embedding = _embed_text(provider, clean_query)

    match_scores: dict[int, list[float]] = {}
    role_scores: dict[int, float] = {}
    for chunk in chunks:
        try:
            values = json.loads(bytes(chunk["embedding"]).decode("utf-8"))
            score = _cosine_similarity(query_embedding, values)
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        team_number = int(chunk["team_number"])
        if str(chunk["chunk_type"]) == "match_summary":
            match_scores.setdefault(team_number, []).append(score)
        else:
            role_scores[team_number] = score

    results: list[TeamSearchResult] = []
    for team_number in set(match_scores) | set(role_scores):
        scores = match_scores.get(team_number, [])
        # A single excellent match observation should not be diluted by several
        # unrelated matches. Keep a little of the average to reward repeated
        # evidence for the requested role.
        match_signal = (
            0.70 * max(scores) + 0.30 * (sum(scores) / len(scores))
            if scores
            else 0.0
        )
        role_signal = role_scores.get(team_number)
        final_score = (
            match_signal
            if role_signal is None
            else 0.85 * match_signal + 0.15 * role_signal
        )
        results.append(
            TeamSearchResult(
                team_number=team_number,
                score=final_score,
                match_hits=len(scores),
                best_match_score=max(scores) if scores else -1.0,
                team_summary_score=role_signal,
            )
        )
    return _relevant_team_results(results)


def _relevant_team_results(results: Iterable[TeamSearchResult]) -> list[TeamSearchResult]:
    """Return only the clearest matches instead of every nonzero cosine score."""
    ranked = sorted(results, key=lambda item: (-item.score, -item.match_hits, item.team_number))
    if not ranked:
        return []
    scores = [item.score for item in ranked]
    average = sum(scores) / len(scores)
    spread = (sum((score - average) ** 2 for score in scores) / len(scores)) ** 0.5
    best_score = ranked[0].score
    # The score must be meaningfully better than the event's typical result and
    # close to the best match. Twelve is enough for scouting without burying the
    # strong candidates in a full event list.
    threshold = max(0.18, average + 0.35 * spread, best_score - 0.16)
    relevant = [item for item in ranked if item.score >= threshold]
    return (relevant or ranked[:1])[:12]


def refresh_search_embeddings(
    database_path: Path | str, provider: OpenAICompatibleConfig
) -> int:
    """Generate every missing current-summary embedding before a search runs."""
    connection = sqlite3.connect(Path(database_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        event = connection.execute(
            "SELECT event_key FROM events ORDER BY event_key LIMIT 1"
        ).fetchone()
        if event is None:
            raise GeminiEnrichmentError("No event has been imported into the analytics database yet.")
        created = _refresh_embedding_chunks(connection, str(event["event_key"]), provider)
        connection.commit()
        return created
    except sqlite3.DatabaseError as error:
        connection.rollback()
        raise GeminiEnrichmentError(f"Could not update the local search index: {error}") from error
    finally:
        connection.close()


def _enrich_match_summaries(
    connection: sqlite3.Connection,
    provider: OpenAICompatibleConfig,
    *,
    progress_callback: ProgressCallback | None = None,
    completed_work: int = 0,
    total_work: int = 0,
) -> tuple[int, int, int]:
    rows = connection.execute(
        """
        SELECT DISTINCT m.match_key, m.match_number
        FROM matches AS m
        JOIN observations AS o ON o.match_key = m.match_key
        ORDER BY m.match_number
        """
    ).fetchall()
    created_summaries = 0
    json_updates = 0
    skipped_batches = 0
    current_work = completed_work
    for row in rows:
        match_key = str(row["match_key"])
        context = _match_context(connection, match_key)
        target_teams = sorted({int(item["team_number"]) for item in context["observations"]})
        source_hash = _hash_json(context)
        pending = [
            team
            for team in target_teams
            if not _summary_exists(connection, "match_team_summaries", match_key, team, source_hash)
        ]
        if pending:
            match_number = int(row["match_number"])
            _report_progress(
                progress_callback,
                current_work,
                total_work,
                f"Writing match {match_number} contribution summaries…",
            )
            try:
                summaries = _generate_match_summaries(
                    provider,
                    context,
                    pending,
                    status_callback=lambda message, number=match_number: _report_progress(
                        progress_callback,
                        current_work,
                        total_work,
                        f"Match {number}: {message}",
                    ),
                )
            except UnusableSummaryResponseError:
                skipped_batches += 1
                current_work += 1
                _report_progress(
                    progress_callback,
                    current_work,
                    total_work,
                    f"Skipped match {match_number}: its AI response stayed unusable after one retry.",
                )
                continue
            now = _utc_now()
            for team_number in pending:
                summary = summaries[team_number]
                connection.execute(
                    """
                    INSERT INTO match_team_summaries(
                        match_key, team_number, summary, source_hash, prompt_version, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (match_key, team_number, summary, source_hash, SUMMARY_PROMPT_VERSION, now),
                )
                created_summaries += 1
                json_updates += _store_summary_in_source_json(
                    connection, match_key, team_number, summary
                )
            # Keep this completed match even if a later request fails.
            connection.commit()
            current_work += 1
            _report_progress(
                progress_callback,
                current_work,
                total_work,
                f"Finished match {match_number} contribution summaries.",
            )
    return created_summaries, json_updates, skipped_batches


def _enrich_team_summaries(
    connection: sqlite3.Connection,
    event_key: str,
    provider: OpenAICompatibleConfig,
    *,
    progress_callback: ProgressCallback | None = None,
    completed_work: int = 0,
    total_work: int = 0,
) -> tuple[int, int]:
    teams = connection.execute(
        "SELECT DISTINCT team_number FROM observations ORDER BY team_number"
    ).fetchall()
    created = 0
    skipped = 0
    current_work = completed_work
    for row in teams:
        team_number = int(row["team_number"])
        context = _team_context(connection, team_number)
        source_hash = _hash_json(context)
        exists = connection.execute(
            """
            SELECT 1 FROM team_summaries
            WHERE event_key = ? AND team_number = ? AND source_hash = ?
            """,
            (event_key, team_number, source_hash),
        ).fetchone()
        if exists is not None:
            continue
        _report_progress(
            progress_callback,
            current_work,
            total_work,
            f"Writing Team {team_number}'s role summary…",
        )
        try:
            summary = _generate_team_summary(
                provider,
                context,
                status_callback=lambda message, number=team_number: _report_progress(
                    progress_callback,
                    current_work,
                    total_work,
                    f"Team {number}: {message}",
                ),
            )
        except UnusableSummaryResponseError:
            skipped += 1
            current_work += 1
            _report_progress(
                progress_callback,
                current_work,
                total_work,
                f"Skipped Team {team_number}'s role summary: its AI response stayed unusable after one retry.",
            )
            continue
        connection.execute(
            """
            INSERT INTO team_summaries(event_key, team_number, summary, source_hash, prompt_version, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_key, team_number, summary, source_hash, SUMMARY_PROMPT_VERSION, _utc_now()),
        )
        # Keep this completed role summary even if a later request fails.
        connection.commit()
        created += 1
        current_work += 1
        _report_progress(
            progress_callback,
            current_work,
            total_work,
            f"Finished Team {team_number}'s role summary.",
        )
    return created, skipped


def _refresh_embedding_chunks(
    connection: sqlite3.Connection,
    event_key: str,
    provider: OpenAICompatibleConfig,
    *,
    progress_callback: ProgressCallback | None = None,
    completed_work: int = 0,
    total_work: int = 0,
) -> int:
    documents = _current_summary_documents(connection, event_key)
    created = 0
    current_work = completed_work
    for document in documents:
        source_hash = _embedding_source_hash(provider, document)
        exists = connection.execute(
            "SELECT 1 FROM embedding_chunks WHERE source_hash = ?", (source_hash,)
        ).fetchone()
        if exists is not None:
            continue
        subject = (
            f"Match {document['match_key']} / Team {document['team_number']}"
            if document["match_key"] is not None
            else f"Team {document['team_number']}"
        )
        _report_progress(
            progress_callback,
            current_work,
            total_work,
            f"Adding {subject} to the AI search index…",
        )
        embedding = _embed_text(
            provider,
            str(document["text"]),
            status_callback=lambda message, item=subject: _report_progress(
                progress_callback,
                current_work,
                total_work,
                f"{item}: {message}",
            ),
        )
        connection.execute(
            """
            DELETE FROM embedding_chunks
            WHERE team_number = ?
              AND COALESCE(match_key, '') = COALESCE(?, '')
              AND chunk_type = ?
            """,
            (document["team_number"], document["match_key"], document["chunk_type"]),
        )
        connection.execute(
            """
            INSERT INTO embedding_chunks(
                chunk_id, event_key, team_number, match_key, chunk_type, text, embedding, source_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid4().hex,
                event_key,
                document["team_number"],
                document["match_key"],
                document["chunk_type"],
                document["text"],
                sqlite3.Binary(json.dumps(embedding, separators=(",", ":")).encode("utf-8")),
                source_hash,
                _utc_now(),
            ),
        )
        # Keep this completed index item even if a later request fails.
        connection.commit()
        created += 1
        current_work += 1
        _report_progress(
            progress_callback,
            current_work,
            total_work,
            f"Added {subject} to the AI search index.",
        )
    return created


def _pending_match_summary_batch_count(connection: sqlite3.Connection) -> int:
    """Count provider calls needed for match summaries without changing data."""
    rows = connection.execute(
        """
        SELECT DISTINCT m.match_key
        FROM matches AS m
        JOIN observations AS o ON o.match_key = m.match_key
        ORDER BY m.match_number
        """
    ).fetchall()
    pending_batches = 0
    for row in rows:
        match_key = str(row["match_key"])
        context = _match_context(connection, match_key)
        source_hash = _hash_json(context)
        for team_number in {int(item["team_number"]) for item in context["observations"]}:
            if not _summary_exists(
                connection, "match_team_summaries", match_key, team_number, source_hash
            ):
                pending_batches += 1
                break
    return pending_batches


def _pending_team_summary_count(connection: sqlite3.Connection, event_key: str) -> int:
    """Count role summaries that will require a provider call."""
    teams = connection.execute(
        "SELECT DISTINCT team_number FROM observations ORDER BY team_number"
    ).fetchall()
    pending = 0
    for row in teams:
        team_number = int(row["team_number"])
        source_hash = _hash_json(_team_context(connection, team_number))
        exists = connection.execute(
            """
            SELECT 1 FROM team_summaries
            WHERE event_key = ? AND team_number = ? AND source_hash = ?
            """,
            (event_key, team_number, source_hash),
        ).fetchone()
        if exists is None:
            pending += 1
    return pending


def _pending_embedding_chunk_count(
    connection: sqlite3.Connection,
    event_key: str,
    provider: OpenAICompatibleConfig,
) -> int:
    """Count summaries whose current text has not been embedded yet."""
    pending = 0
    for document in _current_summary_documents(connection, event_key):
        exists = connection.execute(
            "SELECT 1 FROM embedding_chunks WHERE source_hash = ?",
            (_embedding_source_hash(provider, document),),
        ).fetchone()
        if exists is None:
            pending += 1
    return pending


def _embedding_source_hash(
    provider: OpenAICompatibleConfig, document: dict[str, Any]
) -> str:
    return _hash_text(
        f"{EMBEDDING_PROMPT_VERSION}|{provider.endpoint}|{provider.embedding_model}|"
        f"{document['chunk_type']}|{document['text']}"
    )


def _current_summary_documents(
    connection: sqlite3.Connection, event_key: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT mts.match_key, mts.team_number, mts.summary, m.match_number
        FROM match_team_summaries AS mts
        JOIN matches AS m ON m.match_key = mts.match_key
        WHERE mts.generated_at = (
            SELECT MAX(newer.generated_at)
            FROM match_team_summaries AS newer
            WHERE newer.match_key = mts.match_key AND newer.team_number = mts.team_number
        )
        ORDER BY m.match_number, mts.team_number
        """
    ).fetchall()
    documents: list[dict[str, Any]] = [
        {
            "team_number": int(row["team_number"]),
            "match_key": str(row["match_key"]),
            "chunk_type": "match_summary",
            "text": f"Match {row['match_number']} | Team {row['team_number']} | {row['summary']}",
        }
        for row in rows
    ]
    rows = connection.execute(
        """
        SELECT team_number, summary
        FROM team_summaries
        WHERE event_key = ? AND generated_at = (
            SELECT MAX(newer.generated_at)
            FROM team_summaries AS newer
            WHERE newer.event_key = team_summaries.event_key
              AND newer.team_number = team_summaries.team_number
        )
        ORDER BY team_number
        """,
        (event_key,),
    ).fetchall()
    documents.extend(
        {
            "team_number": int(row["team_number"]),
            "match_key": None,
            "chunk_type": "team_summary",
            "text": f"Team {row['team_number']} role summary | {row['summary']}",
        }
        for row in rows
    )
    return documents


def _match_context(connection: sqlite3.Connection, match_key: str) -> dict[str, Any]:
    match = connection.execute(
        """
        SELECT match_key, match_number, red_score, blue_score, winner_alliance, result_status
        FROM matches WHERE match_key = ?
        """,
        (match_key,),
    ).fetchone()
    if match is None:
        raise GeminiEnrichmentError("A requested match no longer exists in the local database.")
    observations = connection.execute(
        """
        SELECT o.observation_id, o.team_number, o.reported_total_points,
               o.predicted_winner, o.schedule_alignment, s.display_name AS scout_name
        FROM observations AS o
        JOIN scouts AS s ON s.scout_id = o.scout_id
        WHERE o.match_key = ?
        ORDER BY o.team_number, o.observation_id
        """,
        (match_key,),
    ).fetchall()
    return {
        "match_number": int(match["match_number"]),
        "official_result": {
            "red_score": match["red_score"],
            "blue_score": match["blue_score"],
            "winner": match["winner_alliance"],
            "status": match["result_status"],
        },
        "observations": [
            _observation_context(connection, observation) for observation in observations
        ],
    }


def _team_context(connection: sqlite3.Connection, team_number: int) -> dict[str, Any]:
    rows = connection.execute(
        """
        SELECT o.observation_id, o.team_number, o.reported_total_points,
               o.predicted_winner, o.schedule_alignment, s.display_name AS scout_name,
               m.match_number, m.winner_alliance
        FROM observations AS o
        JOIN scouts AS s ON s.scout_id = o.scout_id
        JOIN matches AS m ON m.match_key = o.match_key
        WHERE o.team_number = ?
        ORDER BY m.match_number, o.observation_id
        """,
        (team_number,),
    ).fetchall()
    team_stats = connection.execute(
        """
        SELECT AVG(scouted_points) AS average_points,
               AVG(penalties_committed) AS average_penalties,
               AVG(breakdown_count) AS average_breakdowns
        FROM v_team_match_totals WHERE team_number = ?
        """,
        (team_number,),
    ).fetchone()
    event_stats = connection.execute(
        """
        SELECT AVG(scouted_points) AS average_points,
               AVG(penalties_committed) AS average_penalties,
               AVG(breakdown_count) AS average_breakdowns
        FROM v_team_match_totals
        """
    ).fetchone()
    match_summaries = connection.execute(
        """
        SELECT m.match_number, mts.summary
        FROM match_team_summaries AS mts
        JOIN matches AS m ON m.match_key = mts.match_key
        WHERE mts.team_number = ?
          AND mts.generated_at = (
              SELECT MAX(newer.generated_at)
              FROM match_team_summaries AS newer
              WHERE newer.match_key = mts.match_key AND newer.team_number = mts.team_number
          )
        ORDER BY m.match_number
        """,
        (team_number,),
    ).fetchall()
    return {
        "team_number": team_number,
        "team_average": dict(team_stats) if team_stats is not None else {},
        "event_average": dict(event_stats) if event_stats is not None else {},
        "match_summaries": [dict(row) for row in match_summaries],
        "observations": [
            {
                "match_number": int(row["match_number"]),
                "winner": row["winner_alliance"],
                **_observation_context(connection, row),
            }
            for row in rows
        ],
    }


def _observation_context(connection: sqlite3.Connection, observation: sqlite3.Row) -> dict[str, Any]:
    observation_id = str(observation["observation_id"])
    events = connection.execute(
        """
        SELECT event_type, label, points, timestamp_ms, transcript
        FROM score_events WHERE observation_id = ? ORDER BY timestamp_ms, sequence_number
        """,
        (observation_id,),
    ).fetchall()
    notes = connection.execute(
        "SELECT timestamp_ms, text FROM notes WHERE observation_id = ? ORDER BY timestamp_ms, sequence_number",
        (observation_id,),
    ).fetchall()
    return {
        "team_number": int(observation["team_number"]),
        "scout": str(observation["scout_name"]),
        "reported_total_points": observation["reported_total_points"],
        "predicted_winner": observation["predicted_winner"],
        "schedule_alignment": observation["schedule_alignment"],
        "events": [
            {
                "time_s": round(int(event["timestamp_ms"]) / 1000, 1),
                "type": event["event_type"],
                "label": event["label"],
                "points": event["points"],
                "transcript": event["transcript"],
            }
            for event in events
        ],
        "notes": [
            {"time_s": round(int(note["timestamp_ms"]) / 1000, 1), "text": note["text"]}
            for note in notes
        ],
    }


def _generate_match_summaries(
    provider: OpenAICompatibleConfig,
    context: dict[str, Any],
    teams: Iterable[int],
    *,
    status_callback: StatusCallback | None = None,
) -> dict[int, str]:
    requested_teams = list(teams)
    prompt = (
        "You are writing concise, evidence-grounded FRC scouting notes. "
        "The JSON below contains the complete imported context for one match: every team's "
        "score events and scout notes. Write one 1–2 sentence summary for each requested team, "
        "describing its actual contribution. Mention observed defense, "
        "breakdowns, and penalties only when the supplied records support them. Focus on qualitative information about"
        "the robots, as we already track scoring. If the bot played defense, detail how and how effectively. Do not infer or "
        "invent missing behavior. Return JSON only in this exact shape: "
        '{"summaries":[{"team_number":123,"summary":"..."}]}. '
        f"Requested teams: {requested_teams}\n\nMatch context:\n{json.dumps(context, separators=(',', ':'))}"
    )
    for retry_count in range(MAX_UNUSABLE_SUMMARY_RETRIES + 1):
        try:
            payload = _generate_json(provider, prompt, status_callback=status_callback)
            summaries = _match_summaries_from_payload(payload)
            missing_teams = [team for team in requested_teams if team not in summaries]
            if missing_teams:
                raise UnusableSummaryResponseError(
                    "The AI provider did not return every requested match summary."
                )
            return {team: summaries[team] for team in requested_teams}
        except UnusableSummaryResponseError:
            if retry_count >= MAX_UNUSABLE_SUMMARY_RETRIES:
                raise
            _report_status(
                status_callback,
                "The AI provider returned an incomplete or unreadable match summary. "
                f"Retrying once…",
            )
    raise AssertionError("The summary retry loop should always return or raise.")


def _match_summaries_from_payload(payload: dict[str, Any]) -> dict[int, str]:
    raw_items = payload.get("summaries")
    summaries: dict[int, str] = {}
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            team_number = _integer(item.get("team_number"))
            text = _clean_summary(item.get("summary"))
            if team_number is not None and text:
                summaries[team_number] = text
    if not summaries:
        # Gracefully accept a direct {"123": "summary"} response as well.
        for key, value in payload.items():
            team_number = _integer(key)
            text = _clean_summary(value)
            if team_number is not None and text:
                summaries[team_number] = text
    return summaries


def _generate_team_summary(
    provider: OpenAICompatibleConfig,
    context: dict[str, Any],
    *,
    status_callback: StatusCallback | None = None,
) -> str:
    prompt = (
        "You are writing a concise FRC team role summary from scouting evidence. "
        "Use every supplied match summary, score event, and note. Describe the team's recurring "
        "role, scoring pattern, reliability, and any evidence-backed strengths or limits relative "
        "to the supplied event averages. Do not invent facts. Return JSON only as "
        '{"summary":"..."}. Keep the summary to at most two sentences.\n\nTeam context:\n'
        f"{json.dumps(context, separators=(',', ':'))}"
    )
    for retry_count in range(MAX_UNUSABLE_SUMMARY_RETRIES + 1):
        try:
            payload = _generate_json(provider, prompt, status_callback=status_callback)
            summary = _clean_summary(payload.get("summary"))
            if not summary:
                raise UnusableSummaryResponseError(
                    "The AI provider returned no usable team role summary."
                )
            return summary
        except UnusableSummaryResponseError:
            if retry_count >= MAX_UNUSABLE_SUMMARY_RETRIES:
                raise
            _report_status(
                status_callback,
                "The AI provider returned an incomplete or unreadable role summary. Retrying once…",
            )
    raise AssertionError("The summary retry loop should always return or raise.")


def _generate_json(
    provider: OpenAICompatibleConfig,
    prompt: str,
    *,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    request_payload = {
        "model": provider.model,
        "messages": [
            {
                "role": "system",
                "content": "Return only valid JSON. Do not use Markdown fences.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 700,
    }
    payload = _post_openai_compatible(
        provider,
        "chat/completions",
        request_payload,
        status_callback=status_callback,
    )
    parsed = _read_summary_json(payload)
    if parsed is None:
        raise UnusableSummaryResponseError(
            "The AI provider returned an unreadable summary response."
        )
    return parsed


def _read_summary_json(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract JSON from common OpenAI-compatible chat response shapes."""
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        text = "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        ).strip()
    else:
        return None
    fenced_json = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    if fenced_json is not None:
        text = fenced_json.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _embed_text(
    provider: OpenAICompatibleConfig,
    text: str,
    *,
    status_callback: StatusCallback | None = None,
) -> list[float]:
    payload = _post_openai_compatible(
        provider,
        "embeddings",
        {
            "model": provider.embedding_model,
            "input": text,
            "encoding_format": "float",
        },
        status_callback=status_callback,
    )
    try:
        values = payload["data"][0]["embedding"]
        embedding = [float(value) for value in values]
    except (KeyError, TypeError, ValueError) as error:
        raise GeminiEnrichmentError("The AI provider returned an unreadable embedding response.") from error
    if not embedding:
        raise GeminiEnrichmentError("The AI provider returned an empty embedding.")
    return embedding


def _post_openai_compatible(
    provider: OpenAICompatibleConfig,
    route: str,
    payload: dict[str, Any],
    *,
    status_callback: StatusCallback | None = None,
) -> dict[str, Any]:
    for retry_count in range(MAX_RATE_LIMIT_RETRIES + 1):
        request = Request(
            f"{provider.endpoint}/{route}",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {provider.api_key}",
                "User-Agent": "SpeechScout Analytics/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            if error.code == 429 and retry_count < MAX_RATE_LIMIT_RETRIES:
                delay_seconds = _rate_limit_delay_seconds(error, retry_count)
                _report_status(
                    status_callback,
                    "Rate limited by the AI provider. Retrying in "
                    f"{delay_seconds} second{'s' if delay_seconds != 1 else ''} "
                    f"(retry {retry_count + 1} of {MAX_RATE_LIMIT_RETRIES})…",
                )
                time.sleep(delay_seconds)
                continue
            if error.code in {401, 403}:
                message = "The AI provider rejected that API key. Check the key and try again."
            elif error.code == 404:
                message = "The AI provider could not find the endpoint or configured model."
            elif error.code == 429:
                message = (
                    "The AI provider is still rate limiting requests after "
                    f"{MAX_RATE_LIMIT_RETRIES} retries. Wait a few minutes and try again."
                )
            else:
                message = f"The AI provider returned HTTP {error.code}."
            raise GeminiEnrichmentError(message) from error
        except (URLError, TimeoutError, OSError) as error:
            raise GeminiEnrichmentError(
                "Could not reach the AI endpoint. Check the endpoint and network connection."
            ) from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise GeminiEnrichmentError("The AI provider returned an unreadable response.") from error
        break
    if not isinstance(result, dict):
        raise GeminiEnrichmentError("The AI provider returned an unexpected response.")
    return result


def _rate_limit_delay_seconds(error: HTTPError, retry_count: int) -> int:
    """Use a provider's Retry-After hint, with a bounded exponential fallback."""
    retry_after = error.headers.get("Retry-After") if error.headers is not None else None
    if retry_after:
        try:
            return _bounded_wait_seconds(float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds_until_retry = (retry_at - datetime.now(timezone.utc)).total_seconds()
                return _bounded_wait_seconds(seconds_until_retry)
            except (TypeError, ValueError, IndexError, OverflowError):
                pass
    return min(MAX_RATE_LIMIT_WAIT_SECONDS, 2 ** (retry_count + 1))


def _bounded_wait_seconds(seconds: float) -> int:
    if seconds != seconds:  # NaN is not a usable delay.
        raise ValueError("Retry-After was not a number.")
    return max(1, min(MAX_RATE_LIMIT_WAIT_SECONDS, int(seconds + 0.999)))


def _report_progress(
    callback: ProgressCallback | None, completed: int, total: int, message: str
) -> None:
    if callback is not None:
        callback(max(0, completed), max(0, total), message)


def _report_status(callback: StatusCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _work_status(label: str, completed: int, total: int) -> str:
    if total == 0:
        return f"{label}: nothing new to generate."
    return f"{label} ({completed} of {total})…"


def _store_summary_in_source_json(
    connection: sqlite3.Connection, match_key: str, team_number: int, summary: str
) -> int:
    paths = connection.execute(
        """
        SELECT DISTINCT import_files.source_path
        FROM observations
        JOIN import_files ON import_files.file_hash = observations.source_file_hash
        WHERE observations.match_key = ? AND observations.team_number = ?
        """,
        (match_key, team_number),
    ).fetchall()
    updated = 0
    for row in paths:
        source_path = Path(str(row["source_path"]))
        if not source_path.is_file() or source_path.suffix.lower() != ".json":
            continue
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        analytics = payload.get("analytics")
        analytics_data = dict(analytics) if isinstance(analytics, dict) else {}
        if analytics_data.get("match_summary") == summary:
            continue
        analytics_data.update(
            {
                "match_summary": summary,
                "summary_prompt_version": SUMMARY_PROMPT_VERSION,
                "summary_generated_at": _utc_now(),
            }
        )
        payload["analytics"] = analytics_data
        try:
            source_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            continue
        updated += 1
    return updated


def _summary_exists(
    connection: sqlite3.Connection, table: str, match_key: str, team_number: int, source_hash: str
) -> bool:
    row = connection.execute(
        f"SELECT 1 FROM {table} WHERE match_key = ? AND team_number = ? AND source_hash = ?",
        (match_key, team_number, source_hash),
    ).fetchone()
    return row is not None


def _clean_summary(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rsplit(" ", 1)[0].rstrip(".,;:") + "."
    return text


def _cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    if not left_values or len(left_values) != len(right_values):
        return -1.0
    dot = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = sum(value * value for value in left_values) ** 0.5
    right_norm = sum(value * value for value in right_values) ** 0.5
    return dot / (left_norm * right_norm) if left_norm and right_norm else -1.0


def _integer(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _hash_json(value: Any) -> str:
    return _hash_text(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
