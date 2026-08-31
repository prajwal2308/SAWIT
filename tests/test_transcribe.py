"""Local whisper is the memory in this service, so only one may run at a time."""

from __future__ import annotations

import threading
from pathlib import Path

from app import transcribe


def test_local_whisper_runs_one_at_a_time(monkeypatch):
    """Two reels shared at once both land in the threadpool. Each would build
    its own WhisperModel, and the pair is what OOMs a small box."""
    overlap = []
    live = 0
    guard = threading.Lock()

    class FakeModel:
        def transcribe(self, _path, **_kw):
            nonlocal live
            with guard:
                live += 1
                overlap.append(live)
            # Long enough that a second thread would be inside this window if
            # nothing were serialising them.
            threading.Event().wait(0.05)
            with guard:
                live -= 1
            return iter([]), None

    monkeypatch.setattr(transcribe, "_load_local_model", lambda _size: FakeModel())

    threads = [
        threading.Thread(
            target=transcribe.transcribe,
            args=(Path("/tmp/a.wav"),),
            kwargs={"backend": "faster-whisper", "model_size": "small"},
        )
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(overlap) == 4, "every call should have run"
    assert max(overlap) == 1, f"two transcriptions overlapped: {overlap}"
