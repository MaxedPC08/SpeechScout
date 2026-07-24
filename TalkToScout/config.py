from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class Period:
    duration: int
    name: str

@dataclass
class ScoredObject:
    name: str
    points: int
    alt_points: Optional[int]
    keywords: List[str]

@dataclass
class Game:
    periods: Dict[str, Period]

@dataclass
class TextBlock:
    text: str
    timestamp: float

@dataclass
class Team:
    name: str
    number: int

@dataclass
class Match:
    match_number: int
    competition_name: str
    teams: List[Team]

@dataclass(frozen=True)
class SpokenWord:
    word: str
    said_at: float | str

@dataclass(frozen=True)
class ScoreEvent:
    name: str
    points: int
    timestamp: float
    transcript: str
    alt_points: Optional[int] = None

@dataclass
class MatchNote:
    timestamp: float
    text: str
