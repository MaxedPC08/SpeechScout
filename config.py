import json
from dataclasses import dataclass
from typing import List, Dict
import re

@dataclass
class Period:
    duration: int
    name: str

@dataclass
class ScoredObject:
    name: str
    points: int
    alt_points: int
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
    number: float

@dataclass
class Match:
    match_number: int
    competition_name: str
    teams: List[Team]

class GameConfig:
    def __init__(self, config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
    
    def get_config(self):
        return self.config
    

