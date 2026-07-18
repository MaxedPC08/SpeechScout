from datetime import UTC, datetime
from pathlib import Path

from event_extraction import SpeechEventExtractor
from game_config import GameConfig
from scouting_models import RobotRef, ScoutingSession, TranscriptChunk
from scouting_store import ScoutingStore


def main():
    config = GameConfig.from_json_file("sample_game_config.json")
    store = ScoutingStore(Path("data") / "scouting.sqlite3")
    extractor = SpeechEventExtractor(config)

    session = ScoutingSession.create(
        scout_name="demo",
        event_code="test-event",
        robot=RobotRef(team_number=1234, match_number=1, alliance="blue"),
        game_config_id=config.id,
        started_at=datetime.now(UTC).isoformat(),
    )
    store.save_session(session)

    spoken_text = "Start match. Robot scored a coral high and then played defense."
    chunk = TranscriptChunk.create(session.id, spoken_text, said_at=3.2, is_final=True)
    store.save_chunk(chunk)
    store.save_events(extractor.extract(session.id, chunk.text, chunk.said_at))
    store.export_bundle(Path("data") / "scouting-export.json")
    store.close()


if __name__ == "__main__":
    main()
