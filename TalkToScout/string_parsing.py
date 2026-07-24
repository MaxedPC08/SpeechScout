import re
from typing import Dict, List, Union

from config import SpokenWord, TextBlock

Timestamp = Union[float, str]
WORD_PATTERN = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?")

SCORE = "score"
PENALTY = "penalty"
ALL = "all"

def words_with_timestamp(text: str, said_at: Timestamp) -> list[SpokenWord]:
    return [
        SpokenWord(word=match.group(0), said_at=said_at)
        for match in WORD_PATTERN.finditer(text)
    ]

class StringParser:
    def __init__(self, game_config):
        self.config = game_config

    def clense_word(self, word: str) -> str:

        s = word.lower().strip()

        digit_map = {
            "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
            "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
        }

        def digits_to_words(match):
            digits = match.group(0)
            return " ".join(digit_map[d] for d in digits)

        s = re.sub(r"\d+", digits_to_words, s)
        s = re.sub(r"[^\w\s]", "", s)
        s = re.sub(r"\s+", " ", s)

        return s

    def get_period_from_time(self, elapsed_time):
        time_counter = 0
        for period, duration in self.config.get("periods", {}).items():
            time_counter += duration
            if elapsed_time <= time_counter:
                return period
        return None
    
    def stitch_text_blocks(
        self, text_blocks: List[TextBlock], strip_punctuation: bool = False
    ) -> tuple[str, list[float]]:
        result = " ".join(block.text for block in text_blocks)
        if strip_punctuation:
            result = result.strip(".,!?;:")
        return result, [
            block.timestamp for block in text_blocks
        ]
    
    def get_scored_in_string(self, text: List[TextBlock]):
        transcript, timestamps = self.stitch_text_blocks(text)
        words = self.clense_word(transcript).split()
        matches = []

        for name, scored_object in self.scored_objects(type=ALL).items():
            for keyword in scored_object.get("keywords", []):
                keyword_words = self.clense_word(keyword).split()
                if not keyword_words:
                    continue
                width = len(keyword_words)
                for start in range(len(words) - width + 1):
                    if words[start : start + width] == keyword_words:
                        matches.append((start + width, width, name, scored_object))

        if matches:
            end, _, name, scored_object = max(matches, key=lambda match: match[:2])
            timestamp_index = min(end - 1, len(timestamps) - 1)
            return (
                name,
                scored_object.get("points"),
                scored_object.get("alt_points"),
                timestamps[timestamp_index] - self.config.get("stt_latency", 0),
            )
        return None, None, None, None

    def score_match_count(self, text: str, name: str) -> int:
        """Return how many configured phrases for one score appear in text."""
        return self._configured_match_count(text, name, self.scored_objects(type=ALL))

    def get_breakdown_in_string(self, text: List[TextBlock]):
        """Return the configured breakdown marker and when it was spoken."""
        transcript, timestamps = self.stitch_text_blocks(text)
        words = self.clense_word(transcript).split()
        matches = []

        for name, breakdown in self.config.get("breakdowns", {}).items():
            for keyword in breakdown.get("keywords", []):
                keyword_words = self.clense_word(keyword).split()
                if not keyword_words:
                    continue
                width = len(keyword_words)
                for start in range(len(words) - width + 1):
                    if words[start : start + width] == keyword_words:
                        matches.append((start + width, width, name))

        if not matches:
            return None, None
        end, _, name = max(matches, key=lambda match: match[:2])
        timestamp_index = min(end - 1, len(timestamps) - 1)
        return name, timestamps[timestamp_index] - self.config.get("stt_latency", 0)

    def breakdown_match_count(self, text: str, name: str) -> int:
        """Return how many configured phrases for one breakdown appear in text."""
        return self._configured_match_count(text, name, self.config.get("breakdowns", {}))

    def _configured_match_count(self, text: str, name: str, configured_objects: Dict) -> int:
        words = self.clense_word(text).split()
        scored_object = configured_objects.get(name, {})
        count = 0
        for keyword in scored_object.get("keywords", []):
            keyword_words = self.clense_word(keyword).split()
            width = len(keyword_words)
            count += sum(
                words[start : start + width] == keyword_words
                for start in range(len(words) - width + 1)
            )
        return count

    def scored_objects(self, type: str = ALL) -> Dict:
        combined_scored_objects = self.config.get("scores", {}) | self.config.get("penalties", {})
        return combined_scored_objects if type == ALL else self.config.get("scores" if type == SCORE else "penalties", {})
