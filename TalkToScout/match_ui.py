from __future__ import annotations

import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from config import Match
from match_handler import MatchHandler
from stt import create_recorder
from tba_schedule import ScheduleError, download_schedule, load_schedule, teams_for_match


# Keep application data beside this module instead of depending on the
# directory from which Python (or an IDE) launches the program.
APP_DIRECTORY = Path(__file__).resolve().parent
GAME_CONFIG_PATH = APP_DIRECTORY / "game.json"
PERSONAL_CONFIG_PATH = APP_DIRECTORY / "personal.json"
BACKGROUND = "#10131a"
SURFACE = "#191e29"
SURFACE_ALT = "#252d3d"
TEXT = "#f4f7fb"
MUTED = "#9aa7bb"
ACCENT = "#52d4a2"
RED = "#ff6978"
WARNING = "#ffbf69"


class ConfigurationError(ValueError):
    """The local SpeechScout configuration cannot be loaded."""


def load_game_config() -> dict:
    try:
        with GAME_CONFIG_PATH.open(encoding="utf-8") as config_file:
            game_config = json.load(config_file)
    except FileNotFoundError as error:
        raise ConfigurationError(
            f"Could not find game.json at:\n{GAME_CONFIG_PATH}\n\n"
            "Keep game.json beside match_ui.py."
        ) from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(
            f"game.json is not valid JSON:\n{GAME_CONFIG_PATH}\n\n{error.msg}"
        ) from error

    if not isinstance(game_config, dict):
        raise ConfigurationError("game.json must contain a JSON object.")
    return game_config


def load_personal_config(game_config: dict) -> dict:
    defaults = {
        "root_path": str(Path(game_config.get("root_path", "matches")) / "matches"),
        "scout_name": "",
        "scout_number": None,
        "tba_api_key": "",
    }
    if not PERSONAL_CONFIG_PATH.exists():
        with PERSONAL_CONFIG_PATH.open("w", encoding="utf-8") as config_file:
            json.dump(defaults, config_file, indent=4)
        return defaults

    with PERSONAL_CONFIG_PATH.open(encoding="utf-8") as config_file:
        personal_config = json.load(config_file)
    if not isinstance(personal_config, dict):
        raise ValueError("personal.json must contain a JSON object.")
    return defaults | personal_config


class MatchApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SpeechScout")
        self.root.minsize(760, 620)
        self.root.configure(background=BACKGROUND)
        self._configure_style()

        self.game_config = load_game_config()
        self.personal_config = load_personal_config(self.game_config)
        self.competition_name = self.game_config.get("competition_name", "Practice Match")
        self.schedule_path = Path(self.game_config.get("schedule_path", "data/match_schedule.json"))
        if not self.schedule_path.is_absolute():
            self.schedule_path = GAME_CONFIG_PATH.parent / self.schedule_path
        try:
            self.schedule = load_schedule(self.schedule_path)
        except ScheduleError:
            self.schedule = {"matches": []}
        self.match = Match(match_number=1, competition_name=self.competition_name, teams=[])
        self.handler = self._make_handler()
        self.recorder = None
        self.listener: threading.Thread | None = None
        self._closing = False
        self._recording_view = False
        self._microphone_status_lock = threading.Lock()
        self._microphone_status = ("starting", "Microphone • Starting…")
        # This flag is written by the microphone thread and consumed by the
        # Tk main loop.  The microphone thread must never touch Tk widgets.
        self._display_dirty = True
        self._show_setup()
        self._refresh()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.listener = threading.Thread(target=self._listen, daemon=True)
        self.listener.start()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("Title.TLabel", background=BACKGROUND, foreground=TEXT, font=("TkDefaultFont", 26, "bold"))
        style.configure("Subtitle.TLabel", background=BACKGROUND, foreground=MUTED, font=("TkDefaultFont", 11))
        style.configure("CardText.TLabel", background=SURFACE, foreground=MUTED)
        style.configure("Metric.TLabel", background=SURFACE, foreground=TEXT, font=("TkDefaultFont", 36, "bold"))
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
        style.configure("TEntry", fieldbackground=SURFACE_ALT, foreground=TEXT, insertcolor=TEXT, padding=8)
        style.configure("TSpinbox", fieldbackground=SURFACE_ALT, foreground=TEXT, padding=8)
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE_ALT,
            background=SURFACE_ALT,
            foreground=TEXT,
            arrowcolor=TEXT,
            padding=8,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", SURFACE_ALT)],
            foreground=[("readonly", TEXT)],
            selectbackground=[("readonly", SURFACE_ALT)],
            selectforeground=[("readonly", TEXT)],
        )
        self.root.option_add("*TCombobox*Listbox.background", SURFACE_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#06251b")
        style.configure("Primary.TButton", background=ACCENT, foreground="#06251b", borderwidth=0, padding=(18, 11), font=("TkDefaultFont", 11, "bold"))
        style.map("Primary.TButton", background=[("active", "#74e5bb")])
        style.configure("Danger.TButton", background=RED, foreground="#34070d", borderwidth=0, padding=(18, 11), font=("TkDefaultFont", 11, "bold"))
        style.map("Danger.TButton", background=[("active", "#ff8995")])
        style.configure("TRadiobutton", background=SURFACE, foreground=TEXT)
        style.map("TRadiobutton", background=[("active", SURFACE)])
        style.configure("Toggle.TCheckbutton", background=BACKGROUND, foreground=MUTED)
        style.map("Toggle.TCheckbutton", background=[("active", BACKGROUND)])

    def _make_handler(self) -> MatchHandler:
        root_path = Path(self.personal_config["root_path"]).expanduser()
        if not root_path.is_absolute():
            root_path = PERSONAL_CONFIG_PATH.parent / root_path
        root_path = root_path / "matches"
        return MatchHandler(self.game_config, self.personal_config | {"root_path": str(root_path)}, self.match)

    def _show_setup(self) -> None:
        self._recording_view = False
        if hasattr(self, "page"):
            self.page.destroy()
        self.page = ttk.Frame(self.root, style="App.TFrame", padding=32)
        self.page.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.page.columnconfigure(0, weight=1)

        scout_name = self.personal_config.get("scout_name", "").strip()
        ttk.Label(self.page, text=f"Hello {scout_name}" if scout_name else "Hello", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 24)
        )
        self.microphone_status_label = ttk.Label(self.page, style="Subtitle.TLabel")
        self.microphone_status_label.grid(row=1, column=0, sticky="w", pady=(0, 16))
        self._render_microphone_status()

        card = ttk.Frame(self.page, style="Card.TFrame", padding=24)
        card.grid(row=2, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)

        self.match_number_var = tk.StringVar(value="")
        self.team_number_var = tk.StringVar(value="")
        saved_scout = self.personal_config.get("scout_number")
        self.scout_number_var = tk.StringVar(value="" if saved_scout is None else str(saved_scout))
        self.predicted_winner = tk.StringVar(value="red")
        self.match_number_var.trace_add("write", self._match_selection_changed)
        self.team_number_var.trace_add("write", self._update_existing_warning)
        self.scout_number_var.trace_add("write", self._match_selection_changed)

        ttk.Label(card, text="Match number", style="CardText.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(card, textvariable=self.match_number_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(card, text="Sync schedule", command=self._sync_schedule).grid(row=0, column=2, sticky="e", padx=(10, 0))
        ttk.Label(card, text="Your team number", style="CardText.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.team_picker = ttk.Combobox(card, textvariable=self.team_number_var, state="normal")
        self.team_picker.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Label(card, text="Scout number", style="CardText.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(card, from_=1, to=6, textvariable=self.scout_number_var, width=6).grid(row=2, column=1, columnspan=2, sticky="ew", pady=(12, 0))

        ttk.Label(card, text="Predicted winner", style="CardText.TLabel").grid(row=3, column=0, sticky="w", pady=(18, 0))
        alliance = ttk.Frame(card, style="Card.TFrame")
        alliance.grid(row=3, column=1, columnspan=2, sticky="w", pady=(18, 0))
        ttk.Radiobutton(alliance, text="Red", variable=self.predicted_winner, value="red").grid(row=0, column=0, padx=(0, 14))
        ttk.Radiobutton(alliance, text="Blue", variable=self.predicted_winner, value="blue").grid(row=0, column=1)

        self.existing_var = tk.StringVar(value="")
        ttk.Label(card, textvariable=self.existing_var, style="CardText.TLabel", foreground="#ffbf69").grid(row=4, column=0, columnspan=3, sticky="w", pady=(16, 0))
        ttk.Button(card, text="Sync with match timing", style="Primary.TButton", command=self._start_match).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(20, 0))

    def _match_path(self, match_number: int, team_number: int) -> Path:
        root_path = Path(self.personal_config["root_path"]).expanduser()
        if not root_path.is_absolute():
            root_path = PERSONAL_CONFIG_PATH.parent / root_path
        root_path = root_path / "matches"
        filename = f"{self.match.competition_name}_{team_number}_{match_number}.json"
        return root_path / filename

    def _match_selection_changed(self, *_: object) -> None:
        self._select_scout_team()
        self._update_existing_warning()

    def _select_scout_team(self) -> None:
        """Populate the editable team picker from the scout's schedule position."""
        try:
            match_number = int(self.match_number_var.get())
            scout_number = int(self.scout_number_var.get())
        except ValueError:
            self.team_picker.configure(values=())
            return
        teams = teams_for_match(self.schedule, match_number)
        self.team_picker.configure(values=tuple(str(team) for team in teams))
        if 1 <= scout_number <= len(teams):
            self.team_number_var.set(str(teams[scout_number - 1]))

    def _sync_schedule(self) -> None:
        try:
            self.schedule = download_schedule(
                self.game_config.get("tba_event_key", ""),
                self.personal_config.get("tba_api_key", ""),
                self.schedule_path,
            )
        except ScheduleError as error:
            messagebox.showerror("Schedule sync", str(error))
            return
        self._select_scout_team()
        messagebox.showinfo("Schedule sync", f"Saved {len(self.schedule['matches'])} qualification matches.")

    def _update_existing_warning(self, *_: object) -> None:
        try:
            path = self._match_path(int(self.match_number_var.get()), int(self.team_number_var.get()))
        except (AttributeError, ValueError):
            return
        self.existing_var.set("A saved file already exists for this team and match." if path.exists() else "")

    def _start_match(self) -> None:
        try:
            match_number = int(self.match_number_var.get())
            team_number = int(self.team_number_var.get())
            scout_number = int(self.scout_number_var.get())
        except ValueError:
            messagebox.showerror("Match setup", "Match, team, and scout numbers must be whole numbers.")
            return
        if match_number < 1 or team_number < 1 or not 1 <= scout_number <= 6:
            messagebox.showerror("Match setup", "Use a positive match and team number, and a scout number from 1 to 6.")
            return

        path = self._match_path(match_number, team_number)
        if path.exists() and not messagebox.askyesno("Existing match", f"{path.name} already exists. Replace it when this match ends?"):
            return

        self.personal_config["scout_number"] = scout_number
        with PERSONAL_CONFIG_PATH.open("w", encoding="utf-8") as config_file:
            json.dump(self.personal_config, config_file, indent=4)
        self.match = Match(match_number=match_number, competition_name=self.competition_name, teams=[])
        self.handler = self._make_handler()
        self.handler.start_match(predicted_winner=self.predicted_winner.get(), path=str(path))
        self._show_recording(team_number)

    def _show_recording(self, team_number: int) -> None:
        self._recording_view = False
        self.page.destroy()
        self.page = ttk.Frame(self.root, style="App.TFrame", padding=28)
        self.page.grid(sticky="nsew")
        self.page.columnconfigure(0, weight=1)
        self.page.rowconfigure(5, weight=1)
        self.page.rowconfigure(7, weight=1)
        ttk.Label(self.page, text="Recording", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self.page, text=f"Match {self.match.match_number}  •  Team {team_number}  •  Scout {self.personal_config['scout_number']}", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 18))

        score_card = ttk.Frame(self.page, style="Card.TFrame", padding=20)
        score_card.grid(row=2, column=0, sticky="ew")
        score_card.columnconfigure(0, weight=1)
        self.total = ttk.Label(score_card, text="0 pts", style="Metric.TLabel")
        self.total.grid(row=0, column=0, sticky="w")
        self.microphone_status_label = ttk.Label(score_card, style="CardText.TLabel")
        self.microphone_status_label.grid(row=1, column=0, sticky="w")
        self._render_microphone_status()
        ttk.Button(score_card, text="End match", style="Danger.TButton", command=self._stop_match).grid(row=0, column=1, rowspan=2, sticky="e")

        self.show_events_var = tk.BooleanVar(value=True)
        self.show_notes_var = tk.BooleanVar(value=True)
        visibility_controls = ttk.Frame(self.page, style="App.TFrame")
        visibility_controls.grid(row=3, column=0, sticky="w", pady=(16, 0))
        ttk.Checkbutton(
            visibility_controls,
            text="Show scoring events",
            variable=self.show_events_var,
            command=self._update_panel_visibility,
            style="Toggle.TCheckbutton",
        ).grid(row=0, column=0, padx=(0, 16))
        ttk.Checkbutton(
            visibility_controls,
            text="Show notes",
            variable=self.show_notes_var,
            command=self._update_panel_visibility,
            style="Toggle.TCheckbutton",
        ).grid(row=0, column=1)

        self.events_heading = ttk.Label(self.page, text="Scoring events", style="Subtitle.TLabel")
        self.events_heading.grid(row=4, column=0, sticky="w", pady=(10, 6))
        self.events = self._listbox(self.page)
        self.events.grid(row=5, column=0, sticky="nsew")
        self.notes_heading = ttk.Label(self.page, text="Notes", style="Subtitle.TLabel")
        self.notes_heading.grid(row=6, column=0, sticky="w", pady=(16, 6))
        self.notes = self._listbox(self.page)
        self.notes.grid(row=7, column=0, sticky="nsew")
        self._recording_view = True
        self._display_dirty = True
        self._render_snapshot()

    def _update_panel_visibility(self) -> None:
        """Show or hide the two live log panels without affecting capture."""
        event_widgets = (self.events_heading, self.events)
        note_widgets = (self.notes_heading, self.notes)
        if self.show_events_var.get():
            self.events_heading.grid()
            self.events.grid()
            self.page.rowconfigure(5, weight=1)
        else:
            for widget in event_widgets:
                widget.grid_remove()
            self.page.rowconfigure(5, weight=0)

        if self.show_notes_var.get():
            self.notes_heading.grid()
            self.notes.grid()
            self.page.rowconfigure(7, weight=1)
        else:
            for widget in note_widgets:
                widget.grid_remove()
            self.page.rowconfigure(7, weight=0)

    def _listbox(self, parent: ttk.Frame) -> tk.Listbox:
        return tk.Listbox(
            parent,
            activestyle="none",
            bg=SURFACE,
            fg=TEXT,
            selectbackground=SURFACE_ALT,
            highlightthickness=0,
            borderwidth=0,
            exportselection=False,
            font=("TkDefaultFont", 11),
            height=9,
        )

    def _process_chunk(self, transcript: str, is_final: bool) -> object:
        result = self.handler.process_chunk(transcript, is_final)
        self._display_dirty = True
        return result

    def _listen(self) -> None:
        try:
            self._set_microphone_status("starting", "Microphone • Starting…")
            self.recorder = create_recorder(self._process_chunk)
            self._set_microphone_status("live", "Microphone • Live • synced with match timing")
            while not self._closing:
                self.recorder.text(self.recorder._speechscout_bridge.process_final_text)
        except Exception as error:
            self._set_microphone_status("error", f"Microphone unavailable • {error}")
            print(f"Microphone error: {error}")
        finally:
            if self.recorder is not None:
                self.recorder.shutdown()
                self.recorder = None

    def _set_microphone_status(self, state: str, text: str) -> None:
        """Receive a status update from the microphone thread without touching Tk."""
        with self._microphone_status_lock:
            self._microphone_status = (state, text)

    def _render_microphone_status(self) -> None:
        """Render the most recent microphone state from Tk's main thread."""
        label = getattr(self, "microphone_status_label", None)
        if label is None or not label.winfo_exists():
            return
        with self._microphone_status_lock:
            state, text = self._microphone_status
        color = {
            "starting": WARNING,
            "live": ACCENT,
            "error": RED,
        }.get(state, MUTED)
        label.configure(text=text, foreground=color)

    def _stop_match(self) -> None:
        self.handler.stop_match()
        self._show_setup()

    def _refresh(self) -> None:
        if self._closing:
            return
        self.root.after(250, self._refresh)
        self._render_microphone_status()
        if self._recording_view and self._display_dirty:
            self._render_snapshot()

    def _render_snapshot(self) -> None:
        """Render the handler's current state from Tk's main thread."""
        if not self._recording_view:
            return
        widgets = (self.total, self.events, self.notes)
        if not all(widget.winfo_exists() for widget in widgets):
            return

        state = self.handler.snapshot()
        events = tuple(state["events"])
        notes = tuple(state["notes"])
        self.total.configure(text=f"{state['total_points']} pts")
        self.events.delete(0, tk.END)
        for event in reversed(events):
            self.events.insert(tk.END, f"{event.timestamp:5.1f}s   {event.name}   {event.points:+d} pts")
        self.notes.delete(0, tk.END)
        for note in reversed(notes):
            self.notes.insert(tk.END, f"{note.timestamp:5.1f}s   {note.text}")
        self._display_dirty = False

    def _close(self) -> None:
        self._closing = True
        self.handler.stop_match()
        if self.recorder is not None:
            self.recorder.shutdown()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        MatchApp(root)
    except ConfigurationError as error:
        root.withdraw()
        messagebox.showerror("SpeechScout configuration", str(error), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
