"""theoracle — what is written down, and how little of it survives the visit.

`config.py` refuses to start unless `sanitize_on_end` is true and
`keep_raw_sessions` is false, so this module has to exist for those promises to
mean anything.

During a visit the raw transcript is written to that visit's own directory,
because when something goes wrong in a gallery you need to see what was
actually said. When the visit ends, the directory is deleted and a summary
without names or speech goes to `history/`. Retention is measured in minutes.

The reason is not squeamishness. A laptop standing in a public room, recording
what strangers say about their marriages and their dead, is a data protection
problem with an art piece attached. The summary keeps what is worth keeping —
how long visits ran, which languages, where it failed — and none of what is not.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import REPO, load      # noqa: E402


class Journal:
    """One visit's writing. Opened when they arrive, closed when they leave."""

    def __init__(self, session_id: str, cfg=None):
        self.cfg = cfg or load()
        self.session_id = session_id
        self.started = time.time()
        self.enabled = bool(self.cfg.get("logging.store_transcripts", True))
        self.dir = REPO / "runtime" / "sessions" / session_id
        self.summary = {"turns": 0, "languages": [], "blocked": 0,
                        "rejected": 0, "errors": []}
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)

    def _append(self, name: str, record: dict) -> None:
        if not self.enabled:
            return
        try:
            with open(self.dir / name, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass          # a full disk must not stop the piece from speaking

    def turn(self, visitor: str, reply, language: str, marks: dict) -> None:
        self.summary["turns"] += 1
        if language not in self.summary["languages"]:
            self.summary["languages"].append(language)
        self._append("transcript.jsonl", {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "language": language,
            "visitor": visitor,
            "oracle": reply.text,
            "mood": reply.mood,
            "needs": reply.needs,
            "timings": marks,
        })

    def note(self, kind: str, detail: str = "") -> None:
        if kind == "blocked":
            self.summary["blocked"] += 1
        elif kind == "rejected":
            self.summary["rejected"] += 1
        elif kind == "error":
            self.summary["errors"].append(detail[:120])
        self._append("events.jsonl", {"at": time.time(), "kind": kind,
                                      "detail": detail[:200]})

    def fate(self, sheet) -> None:
        if not self.enabled:
            return
        try:
            (self.dir / "fate.json").write_text(
                json.dumps(sheet.entries, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def close(self) -> dict:
        """Write the summary, delete the raw. Returns what was kept."""
        record = {
            "session": self.session_id,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seconds": round(time.time() - self.started, 1),
            **self.summary,
        }

        history = REPO / (self.cfg.get("logging.history_dir") or "history")
        history.mkdir(parents=True, exist_ok=True)
        try:
            with open(history / "visits.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # No name, no speech, no fate — nothing here belongs to a person.
        if self.cfg.get("logging.sanitize_on_end", True) and self.dir.exists():
            if not self.cfg.get("logging.keep_raw_sessions", False):
                shutil.rmtree(self.dir, ignore_errors=True)
        return record


def sweep(cfg=None) -> int:
    """Delete session directories a crash left behind. Call at startup.

    The engine dying mid-visit is exactly when the sanitisation does not run,
    which is exactly when it matters most.
    """
    cfg = cfg or load()
    root = REPO / "runtime" / "sessions"
    if not root.exists() or cfg.get("logging.keep_raw_sessions", False):
        return 0
    removed = 0
    for d in root.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed
