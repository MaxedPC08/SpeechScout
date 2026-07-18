from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


@dataclass(frozen=True)
class RobotRef:
    team_number: int
    match_number: int
    alliance: str | None = None
    driver_station: str | None = None


@dataclass(frozen=True)
class ScoutingSession:
    id: str
    scout_name: str
    event_code: str
    robot: RobotRef
    game_config_id: str
    started_at: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["robot"] = asdict(self.robot)
        return data

    @classmethod
    def create(
        cls,
        scout_name: str,
        event_code: str,
        robot: RobotRef,
        game_config_id: str,
        started_at: str,
    ) -> "ScoutingSession":
        return cls(
            id=new_id(),
            scout_name=scout_name,
            event_code=event_code,
            robot=robot,
            game_config_id=game_config_id,
            started_at=started_at,
        )


@dataclass(frozen=True)
class TranscriptChunk:
    id: str
    session_id: str
    text: str
    said_at: float
    is_final: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        session_id: str,
        text: str,
        said_at: float,
        is_final: bool = False,
    ) -> "TranscriptChunk":
        return cls(
            id=new_id(),
            session_id=session_id,
            text=text,
            said_at=said_at,
            is_final=is_final,
        )


@dataclass(frozen=True)
class ScoutingEvent:
    id: str
    session_id: str
    event_type: str
    said_at: float
    value: str | int | float | None = None
    game_piece: str | None = None
    points: int | None = None
    source_text: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        session_id: str,
        event_type: str,
        said_at: float,
        value: str | int | float | None = None,
        game_piece: str | None = None,
        points: int | None = None,
        source_text: str = "",
        confidence: float = 1.0,
    ) -> "ScoutingEvent":
        return cls(
            id=new_id(),
            session_id=session_id,
            event_type=event_type,
            said_at=said_at,
            value=value,
            game_piece=game_piece,
            points=points,
            source_text=source_text,
            confidence=confidence,
        )
