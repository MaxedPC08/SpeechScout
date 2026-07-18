from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from threading import Lock
from time import monotonic

from typing import Dict, List, Optional

from config import Match, TextBlock
from string_parsing import StringParser

SCORE_CONTEXT_BLOCKS = 6
DUPLICATE_SCORE_WINDOW_SECONDS = 2.0
NOTE_MERGE_WINDOW_SECONDS = 2
NOTE_FINAL_WINDOW_SECONDS = 3.0
TRANSCRIPTION_SIMILARITY_THRESHOLD = 5
@dataclass(frozen=True)
class ScoreEvent:
    name: str
    points: int
    timestamp: float
    transcript: str
    alt_points: Optional[int] = None


@dataclass(frozen=False)
class MatchNote:
    timestamp: float
    text: str


class MatchHandler:
    """Owns a match's live transcript and score ledger."""

    def __init__(self, config: Dict, personal_config: Dict, match: Match):
        self.config = config
        self.root_path = personal_config["root_path"]
        self.scout_name = personal_config["scout_name"]
        self.scout_number = personal_config["scout_number"]
        self.predicted_winner: Optional[str] = None
        self.path: Optional[str] = None
        self.match = match
        self.ignore_words = set(config.get("ignore_words", []))
        self.stt_latency = config.get("stt_latency", 0.5)
        self.parser = StringParser(config)
        self._lock = Lock()
        self._started_at: Optional[float] = None
        self._text_blocks: List[TextBlock] = []
        self._events: List[ScoreEvent] = []
        self._notes: List[MatchNote] = []
        self._last_transcript = ""
        self._last_score_key: Optional[str] = None
        self._last_score_at: Optional[float] = None
        self._last_note_chunk_at: Optional[float] = None

    def start_match(self, predicted_winner: Optional[str] = None, path: Optional[str] = None) -> None:
        if predicted_winner not in {"red", "blue"}:
            raise ValueError("Predicted winner must be 'red' or 'blue'.")
        with self._lock:
            self._started_at = monotonic()
            self._text_blocks = []
            self._events = []
            self._notes = []
            self._last_transcript = ""
            self._last_score_key = None
            self._last_score_at = None
            self._last_note_chunk_at = None
            self.predicted_winner = predicted_winner
            
            if path is not None:
                self.path = path
            else:
                filename = (
                    f"{self.match.competition_name}_"
                    f"match_{self.match.match_number}_scout_{self.scout_number}.json"
                )
                self.path = str(Path(self.root_path) / filename)

    def save_match(self) -> None:
        with self._lock:
            match_data = asdict(self.match)
            match_data["events"] = [asdict(event) for event in self._events]
            match_data["notes"] = [asdict(note) for note in self._notes]
            match_data["total_points"] = sum(event.points for event in self._events)
            match_data["scout_name"] = self.scout_name
            match_data["scout_number"] = self.scout_number
            match_data["predicted_winner"] = self.predicted_winner
            if self.path is None:
                raise ValueError("Match path is not set.")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(match_data, f, indent=4)

    def stop_match(self) -> None:
        with self._lock:
            if self._started_at is None:
                return
        self.save_match()
        with self._lock:
            self._started_at = None
            self._text_blocks = []

    def process_chunk(self, transcript: str, is_final: bool = True) -> Optional[ScoreEvent]:
        """Record one STT chunk and return its score event, if it creates one."""
        transcript = transcript.strip()

        if not transcript:
            return None

        with self._lock:
            previous_transcript = self._last_transcript
            parsed_transcript = self.parser.clean_word(transcript.strip(".")).split()
            parsed_last_transcript = self.parser.clean_word(self._last_transcript).split()

            def is_similar(a: list[str], b: list[str]) -> bool:
                index = 0
                intersection = []
                for word in a:
                    if word in b[index:]:
                        index = b.index(word, index) + 1
                        intersection.append(word)

                if min(len(a), len(b)) == 0 or len(intersection) == 0:
                    return False
                
                return (max(len(a), len(b)) - len(intersection) <= TRANSCRIPTION_SIMILARITY_THRESHOLD) and (self._last_note_chunk_at is not None and monotonic() - self._started_at - self._last_note_chunk_at < NOTE_MERGE_WINDOW_SECONDS and len(a) >= len(b) - 1)
            
            is_update = is_similar(parsed_transcript, parsed_last_transcript)

            self._last_transcript = transcript
            if self._started_at is None:
                return None

            said_at = monotonic() - self._started_at
            new_blocks = [TextBlock(text=block, timestamp=said_at) for block in self._last_transcript.split(" ")]

            name, points, _alt_points, scored_at = self.parser.get_scored_in_string(
                new_blocks
            )
            if name is None or points is None:
                self._save_note(said_at, transcript, is_update=is_update)
                return None

            common_prefix = 0
            for old_word, new_word in zip(parsed_last_transcript, parsed_transcript):
                if old_word.casefold() != new_word.casefold():
                    break
                common_prefix += 1
            is_cumulative_update = common_prefix >= 2 or common_prefix == len(parsed_last_transcript) > 0
            if (
                is_cumulative_update
                and self.parser.score_match_count(transcript, name)
                <= self.parser.score_match_count(previous_transcript, name)
            ):
                return None

            score_key = name
            if (
                score_key == self._last_score_key
                and self._last_score_at is not None
                and said_at - self._last_score_at < DUPLICATE_SCORE_WINDOW_SECONDS
            ):
                return None

            self._discard_pending_note()
            event = ScoreEvent(
                name=name,
                points=int(points),
                alt_points=int(_alt_points) if _alt_points is not None else None,
                timestamp=max(0.0, scored_at if scored_at is not None else said_at),
                transcript=transcript,
            )
            self._events.append(event)
            self._last_score_key = score_key
            self._last_score_at = said_at
            return event

    def _save_note(self, said_at: float, transcript: str, is_update: bool) -> None:
        if is_update and len(self._notes) > 0:
            self._notes[-1].text = transcript
        else:
            self._notes.append(MatchNote(said_at, transcript))
        self._last_note_chunk_at = said_at

    def _discard_pending_note(self) -> None:
        if self._last_note_chunk_at is not None and self._notes:
            self._notes.pop()
        self._last_note_chunk_at = None

    def snapshot(self) -> Dict:
        return {
            "active": self._started_at is not None,
            "match_number": self.match.match_number,
            "competition_name": self.match.competition_name,
            "total_points": sum(event.points for event in self._events),
            "last_transcript": self._last_transcript,
            "events": self._events,
            "notes": self._notes,
        }
