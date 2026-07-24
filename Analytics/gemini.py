"""Gemini enrichment for locally stored SpeechScout scouting data.

The API key is supplied by the caller and is deliberately never written to the
database, source JSON, or configuration.  Generated summaries and embeddings
are cached locally so they remain available after the network is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import uuid4


GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# This is intentionally a constant rather than a saved user preference: a
# user can update it in one obvious place when Google retires a model.
GEMINI_SUMMARY_MODEL = "gemini-3.1-flash-lite"
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"
SUMMARY_PROMPT_VERSION = "match-role-v1"
EMBEDDING_PROMPT_VERSION = "retrieval-v1"
MAX_SUMMARY_CHARS = 420


class GeminiEnrichmentError(RuntimeError):
    """Raised for a recoverable Gemini API or local enrichment failure."""


@dataclass(frozen=True)
class EnrichmentReport:
    match_summaries: int
    team_summaries: int
    embedding_chunks: int
    json_files_updated: int


@dataclass(frozen=True)
class TeamSearchResult:
    team_number: int
    score: float
    match_hits: int
    best_match_score: float
    team_summary_score: float | None


def enrich_database(
    database_path: Path | str,
    api_key: str,
    *,
    summary_model: str = GEMINI_SUMMARY_MODEL,
    embedding_model: str = GEMINI_EMBEDDING_MODEL,
) -> EnrichmentReport:
    """Generate short summaries and a local semantic-search index.

    A match generation request contains every imported event and note for that
    match, not only the target team's observation.  Team summaries use all of
    that team's imported observations plus the generated match summaries.
    """
    clean_key = _required_api_key(api_key)
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

        match_summaries, json_updates = _enrich_match_summaries(
            connection, clean_key, summary_model
        )
        team_summaries = _enrich_team_summaries(
            connection, event_key, clean_key, summary_model
        )
        embedding_chunks = _refresh_embedding_chunks(
            connection, event_key, clean_key, embedding_model
        )
        connection.commit()
        return EnrichmentReport(
            match_summaries=match_summaries,
            team_summaries=team_summaries,
            embedding_chunks=embedding_chunks,
            json_files_updated=json_updates,
        )
    except sqlite3.DatabaseError as error:
        connection.rollback()
        raise GeminiEnrichmentError(f"Could not update the local analytics database: {error}") from error
    finally:
        connection.close()


def search_teams(
    database_path: Path | str,
    api_key: str,
    query: str,
    *,
    embedding_model: str = GEMINI_EMBEDDING_MODEL,
) -> list[TeamSearchResult]:
    """Rank teams by cached summary embeddings, with match summaries dominant."""
    clean_query = " ".join(query.split())
    if not clean_query:
        return []
    query_embedding = _embed_text(
        _required_api_key(api_key),
        clean_query,
        embedding_model,
        task_type="RETRIEVAL_QUERY",
    )
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
        scores = sorted(match_scores.get(team_number, []), reverse=True)
        # Only clear semantic matches count toward the frequency bonus.  The
        # average of the best three rewards repeated, high-quality role fits.
        relevant = [score for score in scores if score >= 0.20]
        quality = sum(scores[:3]) / min(3, len(scores)) if scores else 0.0
        frequency = min(len(relevant), 3) / 3
        match_signal = 0.75 * quality + 0.25 * frequency
        role_signal = role_scores.get(team_number)
        final_score = match_signal if role_signal is None else 0.90 * match_signal + 0.10 * role_signal
        results.append(
            TeamSearchResult(
                team_number=team_number,
                score=final_score,
                match_hits=len(relevant),
                best_match_score=max(scores) if scores else -1.0,
                team_summary_score=role_signal,
            )
        )
    return sorted(results, key=lambda item: (-item.score, -item.match_hits, item.team_number))


def _enrich_match_summaries(
    connection: sqlite3.Connection, api_key: str, model: str
) -> tuple[int, int]:
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
            summaries = _generate_match_summaries(api_key, model, context, pending)
            now = _utc_now()
            for team_number in pending:
                summary = summaries.get(team_number)
                if not summary:
                    raise GeminiEnrichmentError(
                        f"Gemini did not return a usable summary for Team {team_number} in Match {row['match_number']}."
                    )
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
    return created_summaries, json_updates


def _enrich_team_summaries(
    connection: sqlite3.Connection, event_key: str, api_key: str, model: str
) -> int:
    teams = connection.execute(
        "SELECT DISTINCT team_number FROM observations ORDER BY team_number"
    ).fetchall()
    created = 0
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
        summary = _generate_team_summary(api_key, model, context)
        connection.execute(
            """
            INSERT INTO team_summaries(event_key, team_number, summary, source_hash, prompt_version, generated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_key, team_number, summary, source_hash, SUMMARY_PROMPT_VERSION, _utc_now()),
        )
        created += 1
    return created


def _refresh_embedding_chunks(
    connection: sqlite3.Connection, event_key: str, api_key: str, model: str
) -> int:
    documents = _current_summary_documents(connection, event_key)
    created = 0
    for document in documents:
        source_hash = _hash_text(
            f"{EMBEDDING_PROMPT_VERSION}|{model}|{document['chunk_type']}|{document['text']}"
        )
        exists = connection.execute(
            "SELECT 1 FROM embedding_chunks WHERE source_hash = ?", (source_hash,)
        ).fetchone()
        if exists is not None:
            continue
        embedding = _embed_text(api_key, str(document["text"]), model, task_type="RETRIEVAL_DOCUMENT")
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
        created += 1
    return created


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
    api_key: str, model: str, context: dict[str, Any], teams: Iterable[int]
) -> dict[int, str]:
    prompt = (
        "You are writing concise, evidence-grounded FRC scouting notes. "
        "The JSON below contains the complete imported context for one match: every team's "
        "score events and scout notes. Write one 1–2 sentence summary for each requested team, "
        "describing its actual contribution. Mention observed scoring levels/timing, defense, "
        "breakdowns, and penalties only when the supplied records support them. Do not infer or "
        "invent missing behavior. Return JSON only in this exact shape: "
        '{"summaries":[{"team_number":123,"summary":"..."}]}. '
        f"Requested teams: {list(teams)}\n\nMatch context:\n{json.dumps(context, separators=(',', ':'))}"
    )
    payload = _generate_json(api_key, model, prompt)
    raw_items = payload.get("summaries") if isinstance(payload, dict) else None
    summaries: dict[int, str] = {}
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            team_number = _integer(item.get("team_number"))
            text = _clean_summary(item.get("summary"))
            if team_number is not None and text:
                summaries[team_number] = text
    if not summaries and isinstance(payload, dict):
        # Gracefully accept a direct {"123": "summary"} response as well.
        for key, value in payload.items():
            team_number = _integer(key)
            text = _clean_summary(value)
            if team_number is not None and text:
                summaries[team_number] = text
    return summaries


def _generate_team_summary(api_key: str, model: str, context: dict[str, Any]) -> str:
    prompt = (
        "You are writing a concise FRC team role summary from scouting evidence. "
        "Use every supplied match summary, score event, and note. Describe the team's recurring "
        "role, scoring pattern, reliability, and any evidence-backed strengths or limits relative "
        "to the supplied event averages. Do not invent facts. Return JSON only as "
        '{"summary":"..."}. Keep the summary to at most two sentences.\n\nTeam context:\n'
        f"{json.dumps(context, separators=(',', ':'))}"
    )
    payload = _generate_json(api_key, model, prompt)
    summary = _clean_summary(payload.get("summary") if isinstance(payload, dict) else None)
    if not summary:
        raise GeminiEnrichmentError("Gemini returned no usable team role summary.")
    return summary


def _generate_json(api_key: str, model: str, prompt: str) -> dict[str, Any]:
    payload = _post_gemini(
        api_key,
        f"models/{quote(model, safe='.-_')}:generateContent",
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.15,
                "maxOutputTokens": 700,
                "responseMimeType": "application/json",
            },
        },
    )
    try:
        text = "".join(
            str(part.get("text", ""))
            for part in payload["candidates"][0]["content"]["parts"]
            if isinstance(part, dict)
        )
        parsed = json.loads(text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise GeminiEnrichmentError("Gemini returned an unreadable summary response.") from error
    if not isinstance(parsed, dict):
        raise GeminiEnrichmentError("Gemini returned a summary response in an unexpected format.")
    return parsed


def _embed_text(api_key: str, text: str, model: str, *, task_type: str) -> list[float]:
    payload = _post_gemini(
        api_key,
        f"models/{quote(model, safe='.-_')}:embedContent",
        {
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
        },
    )
    try:
        values = payload["embedding"]["values"]
        embedding = [float(value) for value in values]
    except (KeyError, TypeError, ValueError) as error:
        raise GeminiEnrichmentError("Gemini returned an unreadable embedding response.") from error
    if not embedding:
        raise GeminiEnrichmentError("Gemini returned an empty embedding.")
    return embedding


def _post_gemini(api_key: str, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{GEMINI_API_BASE_URL}/{endpoint}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "SpeechScout Analytics/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            message = "Gemini rejected that API key. Check the key and try again."
        elif error.code == 404:
            message = "Gemini could not find the configured model. Check its availability for this API key."
        elif error.code == 429:
            message = "Gemini rate limited this request. Wait briefly and try again."
        else:
            message = f"Gemini returned HTTP {error.code}."
        raise GeminiEnrichmentError(message) from error
    except (URLError, TimeoutError, OSError) as error:
        raise GeminiEnrichmentError("Could not reach Gemini. Check the network connection and try again.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise GeminiEnrichmentError("Gemini returned an unreadable response.") from error
    if not isinstance(result, dict):
        raise GeminiEnrichmentError("Gemini returned an unexpected response.")
    return result


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


def _required_api_key(api_key: str) -> str:
    clean_key = api_key.strip()
    if not clean_key:
        raise GeminiEnrichmentError("A Gemini API key is required for summaries and semantic search.")
    return clean_key


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
