from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from config import Match
from match_handler import MatchHandler
from stt import create_recorder


GAME_CONFIG_PATH = Path("game.json")
PERSONAL_CONFIG_PATH = Path("personal.json")


def load_personal_config(game_config: dict) -> dict:
    """Load persistent scout settings, creating a starter file on first run."""
    defaults = {
        "root_path": game_config.get("root_path", "matches"),
        "scout_name": "",
        "scout_number": None,
    }
    if not PERSONAL_CONFIG_PATH.exists():
        with PERSONAL_CONFIG_PATH.open("w", encoding="utf-8") as config_file:
            json.dump(defaults, config_file, indent=4)
        return defaults

    with PERSONAL_CONFIG_PATH.open(encoding="utf-8") as config_file:
        personal_config = json.load(config_file)
    if not isinstance(personal_config, dict):
        raise ValueError("personal.json must contain a JSON object.")

    settings = defaults | personal_config
    if not isinstance(settings["root_path"], str) or not settings["root_path"].strip():
        raise ValueError("personal.json root_path must be a non-empty string.")
    if not isinstance(settings["scout_name"], str):
        raise ValueError("personal.json scout_name must be a string.")
    if settings["scout_number"] is not None and (
        not isinstance(settings["scout_number"], int) or not 1 <= settings["scout_number"] <= 6
    ):
        raise ValueError("personal.json scout_number must be an integer from 1 to 6.")
    return settings


class MatchApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SpeechScout")
        self.root.minsize(620, 500)

        with GAME_CONFIG_PATH.open(encoding="utf-8") as config_file:
            game_config = json.load(config_file)
        self.game_config = game_config
        self.personal_config = load_personal_config(game_config)
        self.match = Match(match_number=1, competition_name="Practice Match", teams=[])
        self.handler = self._make_handler()
        self.recorder = None
        self.listener: threading.Thread | None = None
        self._build_ui()
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=20)
        frame.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(7, weight=1)
        frame.rowconfigure(9, weight=1)

        ttk.Label(frame, text="SpeechScout", font=("TkDefaultFont", 20, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.status = ttk.Label(frame, text="Set your personal details, then start a match.")
        self.status.grid(row=1, column=0, sticky="w", pady=(2, 14))

        settings = ttk.LabelFrame(frame, text="Personal settings", padding=10)
        settings.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        settings.columnconfigure(1, weight=1)
        self.root_path_var = tk.StringVar(value=self.personal_config["root_path"])
        self.scout_name_var = tk.StringVar(value=self.personal_config["scout_name"])
        scout_number = self.personal_config["scout_number"]
        self.scout_number_var = tk.StringVar(value="" if scout_number is None else str(scout_number))
        ttk.Label(settings, text="Root directory:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(settings, textvariable=self.root_path_var).grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(settings, text="Scout name:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(settings, textvariable=self.scout_name_var).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(settings, text="Scout #:").grid(row=1, column=2, sticky="w", padx=(10, 4), pady=(6, 0))
        ttk.Spinbox(settings, from_=1, to=6, width=3, textvariable=self.scout_number_var).grid(
            row=1, column=3, sticky="w", pady=(6, 0)
        )
        ttk.Button(settings, text="Save personal settings", command=self._save_personal_settings).grid(
            row=2, column=1, columnspan=3, sticky="e", pady=(8, 0)
        )

        controls = ttk.Frame(frame)
        controls.grid(row=3, column=0, sticky="w")
        self.start_button = ttk.Button(controls, text="Start match", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(controls, text="Stop match", command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1)

        prediction = ttk.Frame(frame)
        prediction.grid(row=4, column=0, sticky="w", pady=(14, 0))
        ttk.Label(prediction, text="Predicted winner:").grid(row=0, column=0, padx=(0, 8))
        self.predicted_winner = tk.StringVar(value="red")
        ttk.Radiobutton(prediction, text="Red", variable=self.predicted_winner, value="red").grid(row=0, column=1)
        ttk.Radiobutton(prediction, text="Blue", variable=self.predicted_winner, value="blue").grid(row=0, column=2)

        self.total = ttk.Label(frame, text="0 pts", font=("TkDefaultFont", 34, "bold"))
        self.total.grid(row=5, column=0, sticky="w", pady=(22, 8))
        ttk.Label(frame, text="Scoring events").grid(row=6, column=0, sticky="w")
        self.events = tk.Listbox(frame, activestyle="none", height=7)
        self.events.grid(row=7, column=0, sticky="nsew")

        ttk.Label(frame, text="Notes").grid(row=8, column=0, sticky="w", pady=(12, 0))
        self.notes = tk.Listbox(frame, activestyle="none", height=7)
        self.notes.grid(row=9, column=0, sticky="nsew")

        self.last_heard = ttk.Label(frame, text="Waiting for a match to start.", wraplength=560)
        self.last_heard.grid(row=10, column=0, sticky="w", pady=(12, 0))

    def _make_handler(self) -> MatchHandler:
        root_path = Path(self.personal_config["root_path"]).expanduser()
        if not root_path.is_absolute():
            root_path = PERSONAL_CONFIG_PATH.parent / root_path
        handler_config = self.personal_config | {"root_path": str(root_path)}
        return MatchHandler(self.game_config, handler_config, self.match)

    def _save_personal_settings(self) -> bool:
        root_path = self.root_path_var.get().strip()
        scout_name = self.scout_name_var.get().strip()
        try:
            scout_number = int(self.scout_number_var.get())
        except ValueError:
            scout_number = 0
        if not root_path:
            messagebox.showerror("Personal settings", "Enter a root directory.")
            return False
        if not scout_name:
            messagebox.showerror("Personal settings", "Enter your scout name.")
            return False
        if not 1 <= scout_number <= 6:
            messagebox.showerror("Personal settings", "Scout number must be from 1 to 6.")
            return False

        self.personal_config = {
            "root_path": root_path,
            "scout_name": scout_name,
            "scout_number": scout_number,
        }
        with PERSONAL_CONFIG_PATH.open("w", encoding="utf-8") as config_file:
            json.dump(self.personal_config, config_file, indent=4)
        self.status.configure(text="Personal settings saved")
        return True

    def _start(self) -> None:
        if not self._save_personal_settings():
            return
        self.handler = self._make_handler()
        self.handler.start_match(predicted_winner=self.predicted_winner.get())
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.configure(text="Match running - listening")
        self.listener = threading.Thread(target=self._listen, daemon=True)
        self.listener.start()

    def _listen(self) -> None:
        try:
            self.recorder = create_recorder(self.handler.process_chunk)
            while self.handler.snapshot()["active"]:
                self.recorder.text(self.recorder._speechscout_bridge.process_final_text)
        except Exception as error:
            self.root.after(0, lambda: self.status.configure(text=f"Microphone error: {error}"))
        finally:
            if self.recorder is not None:
                self.recorder.shutdown()
                self.recorder = None

    def _stop(self) -> None:
        self.handler.stop_match()
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status.configure(text="Match stopped")

    def _refresh(self) -> None:
        state = self.handler.snapshot()
        self.total.configure(text=f"{state['total_points']} pts")
        self.last_heard.configure(text=f"Last heard: {state['last_transcript'] or '...'}")
        self.events.delete(0, tk.END)
        for event in reversed(state["events"]):
            self.events.insert(
                tk.END,
                f"{event.timestamp:5.1f}s   {event.name}   {event.points:+d} pts",
            )
        self.notes.delete(0, tk.END)
        for note in reversed(state["notes"]):
            self.notes.insert(tk.END, f"{note.timestamp:5.1f}s   {note.text}")
        self.root.after(250, self._refresh)

    def _close(self) -> None:
        self.handler.stop_match()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MatchApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
