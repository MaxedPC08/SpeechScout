"""Fetch and read a compact local match schedule from The Blue Alliance."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_BASE_URL = "https://www.thebluealliance.com/api/v3"


class ScheduleError(RuntimeError):
    """A schedule could not be downloaded or read."""


def load_schedule(path: Path) -> dict[str, Any]:
    """Load a cached schedule, returning an empty schedule when none exists."""
    if not path.exists():
        return {"matches": []}
    try:
        with path.open(encoding="utf-8") as schedule_file:
            schedule = json.load(schedule_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ScheduleError(f"Could not read {path}: {error}") from error
    if not isinstance(schedule, dict) or not isinstance(schedule.get("matches"), list):
        raise ScheduleError(f"{path} is not a valid match schedule.")
    return schedule


def teams_for_match(schedule: dict[str, Any], match_number: int) -> list[int]:
    """Return teams in the schedule's field/station order for one match."""
    for match in schedule.get("matches", []):
        if match.get("match_number") == match_number:
            return [team["number"] for team in match.get("teams", []) if isinstance(team.get("number"), int)]
    return []


def download_schedule(event_key: str, api_key: str, destination: Path) -> dict[str, Any]:
    """Download qualification matches from TBA and save the normalized schedule."""
    event_key = event_key.strip()
    api_key = api_key.strip()
    if not event_key or not api_key:
        raise ScheduleError("Set tba_event_key in game.json and tba_api_key in personal.json first.")

    request = Request(
        f"{API_BASE_URL}/event/{quote(event_key, safe='')}/matches/simple",
        headers={"X-TBA-Auth-Key": api_key, "User-Agent": "SpeechScout/1.0"},
    )
    try:
        with urlopen(request, timeout=15) as response:
            raw_matches = json.load(response)
    except HTTPError as error:
        message = "The Blue Alliance rejected the request. Check the event key and API key."
        raise ScheduleError(f"{message} (HTTP {error.code})") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ScheduleError(f"Could not download the schedule: {error}") from error

    if not isinstance(raw_matches, list):
        raise ScheduleError("The Blue Alliance returned an invalid match schedule.")

    matches = []
    for raw_match in raw_matches:
        if raw_match.get("comp_level") != "qm" or not isinstance(raw_match.get("match_number"), int):
            continue
        teams = []
        for alliance in ("red", "blue"):
            alliance_data = raw_match.get("alliances", {}).get(alliance, {})
            for station, team_key in enumerate(alliance_data.get("team_keys", []), start=1):
                if not isinstance(team_key, str) or not team_key.startswith("frc"):
                    continue
                try:
                    number = int(team_key[3:])
                except ValueError:
                    continue
                teams.append({"number": number, "alliance": alliance, "station": station})
        matches.append(
            {
                "match_number": raw_match["match_number"],
                "match_key": raw_match.get("key", ""),
                "teams": teams,
            }
        )

    schedule = {
        "event_key": event_key,
        "event_name": "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "matches": sorted(matches, key=lambda match: match["match_number"]),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as schedule_file:
        json.dump(schedule, schedule_file, indent=2)
    return schedule
