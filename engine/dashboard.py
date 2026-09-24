"""theoracle — the window into a running engine.

A gallery piece fails while nobody is looking at a terminal. This serves one
page on the same port the protocol already uses, so opening
http://127.0.0.1:8765 shows what the engine is doing, what it has done today,
and where it stopped.

**What it deliberately does not do.** It keeps nothing. Counters live in
memory and reset when the engine restarts; the live exchange is cleared the
moment a visit ends; the page writes nothing to disk and nothing to browser
storage. The whole point of `journal.py` deleting the raw transcript is
undermined by a dashboard that quietly keeps a copy, and a screen in a back
room showing what strangers just said about their marriages is the same
problem as a file on disk, only easier to photograph.

`show_speech: false` in the config turns off the transcript panel entirely, for
when the screen is somewhere a visitor can see it.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

PAGE = Path(__file__).resolve().parent / "dashboard.html"


@dataclass
class Stats:
    """Everything the page shows, held for as long as the process runs."""

    started: float = field(default_factory=time.time)
    visits: int = 0
    turns: int = 0
    blocked: int = 0
    rejected: int = 0
    errors: int = 0
    languages: dict = field(default_factory=dict)
    readings: dict = field(default_factory=dict)
    latencies: deque = field(default_factory=lambda: deque(maxlen=50))
    log: deque = field(default_factory=lambda: deque(maxlen=200))
    health: dict = field(default_factory=dict)
    last_error: str = ""

    def note_log(self, line: str, kind: str = "info") -> dict:
        entry = {"at": time.time(), "line": line, "kind": kind}
        self.log.append(entry)
        if kind == "error":
            self.errors += 1
            self.last_error = line
        return entry

    def note_turn(self, language: str, reading: str, marks: dict) -> None:
        self.turns += 1
        self.languages[language] = self.languages.get(language, 0) + 1
        self.readings[reading] = self.readings.get(reading, 0) + 1
        after = sum(marks.get(k, 0) for k in ("stt", "llm", "tts"))
        self.latencies.append({"at": time.time(), "after_speech": round(after, 3),
                               **{k: marks.get(k, 0) for k in ("stt", "llm", "tts")}})

    def snapshot(self, **extra) -> dict:
        lat = list(self.latencies)
        recent = [x["after_speech"] for x in lat[-10:]]
        return {
            "uptime": round(time.time() - self.started),
            "visits": self.visits,
            "turns": self.turns,
            "blocked": self.blocked,
            "rejected": self.rejected,
            "errors": self.errors,
            "last_error": self.last_error,
            "languages": self.languages,
            "readings": self.readings,
            "latencies": lat,
            "median_after_speech": round(sorted(recent)[len(recent) // 2], 2) if recent else None,
            "health": self.health,
            "log": list(self.log)[-80:],
            **extra,
        }


def page_bytes() -> bytes:
    if PAGE.exists():
        return PAGE.read_bytes()
    return b"<!doctype html><title>theoracle</title><p>dashboard.html is missing."
