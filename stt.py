from __future__ import annotations

from typing import Callable

MODEL = "tiny.en"
CHUNK_INTERVAL_SECONDS = 0.5


class RealtimeChunkBridge:
    """Converts RealtimeSTT's cumulative updates into new text chunks."""

    def __init__(self, on_chunk: Callable[[str, bool], object]):
        self.on_chunk = on_chunk
        self._last_realtime_text = ""

    def process_realtime_text(self, text: str) -> None:
        text = text.strip()
        if not text or text == self._last_realtime_text:
            return
        self.on_chunk(text, False)

    def process_final_text(self, text: str) -> None:
        self._last_realtime_text = ""
        if text.strip():
            self.on_chunk(text.strip(), True)


    def _new_text_since_last_update(self, text: str) -> str:
        if self._last_realtime_text and text.startswith(self._last_realtime_text):
            return text[len(self._last_realtime_text) :].strip()
        return text


def create_recorder(on_chunk: Callable[[str, bool], object]):
    from RealtimeSTT import AudioToTextRecorder

    bridge = RealtimeChunkBridge(on_chunk)
    recorder = AudioToTextRecorder(
        model=MODEL,
        realtime_model_type=MODEL,
        enable_realtime_transcription=True,
        init_realtime_after_seconds=0.4,
        realtime_processing_pause=CHUNK_INTERVAL_SECONDS,
        on_realtime_transcription_update=bridge.process_realtime_text,
    )
    recorder._speechscout_bridge = bridge
    return recorder


def run_microphone(on_chunk: Callable[[str, bool], object]) -> None:
    recorder = create_recorder(on_chunk)
    try:
        while True:
            recorder.text(recorder._speechscout_bridge.process_final_text)
    finally:
        recorder.shutdown()
