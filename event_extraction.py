import re

from game_config import GameConfig
from scouting_models import ScoutingEvent


WORD_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


class SpeechEventExtractor:
    def __init__(self, config: GameConfig):
        self.config = config

    def extract(
        self,
        session_id: str,
        text: str,
        said_at: float,
    ) -> list[ScoutingEvent]:
        normalized_text = normalize_text(text)
        events = self._extract_match_control_events(session_id, normalized_text, said_at)

        for rule in self.config.event_rules:
            if not contains_any(normalized_text, rule.verbs):
                continue

            piece_name = None
            if rule.game_pieces:
                piece_name = self._matched_piece_name(rule.game_pieces, normalized_text)
                if piece_name is None:
                    continue

            value = first_match(normalized_text, rule.values)
            events.append(
                ScoutingEvent.create(
                    session_id=session_id,
                    event_type=rule.event_type,
                    said_at=said_at,
                    value=value,
                    game_piece=piece_name,
                    points=rule.points,
                    source_text=text,
                )
            )

        return events

    def _extract_match_control_events(
        self,
        session_id: str,
        normalized_text: str,
        said_at: float,
    ) -> list[ScoutingEvent]:
        events: list[ScoutingEvent] = []

        if contains_phrase(normalized_text, "start match", "match start", "game start"):
            events.append(
                ScoutingEvent.create(
                    session_id=session_id,
                    event_type="match_started",
                    said_at=said_at,
                    source_text=normalized_text,
                )
            )

        for phase in self.config.phases:
            if contains_any(normalized_text, phase.aliases):
                events.append(
                    ScoutingEvent.create(
                        session_id=session_id,
                        event_type="phase_called",
                        said_at=said_at,
                        value=phase.name,
                        source_text=normalized_text,
                    )
                )

        return events

    def _matched_piece_name(
        self,
        rule_piece_names: tuple[str, ...],
        normalized_text: str,
    ) -> str | None:
        for alias, piece_name in self.config.piece_aliases(rule_piece_names).items():
            if contains_phrase(normalized_text, alias):
                return piece_name

        return None


def normalize_text(text: str) -> str:
    return " ".join(WORD_PATTERN.findall(text.lower()))


def contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(contains_phrase(text, phrase) for phrase in phrases)


def contains_phrase(text: str, *phrases: str) -> bool:
    padded_text = f" {text} "
    return any(f" {normalize_text(phrase)} " in padded_text for phrase in phrases)


def first_match(text: str, phrases: tuple[str, ...]) -> str | None:
    for phrase in phrases:
        if contains_phrase(text, phrase):
            return phrase

    return None
