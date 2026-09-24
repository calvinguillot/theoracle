"""theoracle — configuration, loaded once and checked.

`config.yaml` holds the settings; `.env` holds the one secret. Both are read
here and nowhere else, so there is a single answer to "what is this set to".

Some settings are not preferences. `third_party_search: false` is what stops
the piece looking up a person who is not in the room; `sanitize_on_end: true`
is what stops visitors' names accumulating on a laptop in a public gallery;
`think: false` is what stops the model's reasoning being read aloud. Those are
checked at startup and refuse to load when they have drifted, because each one
fails silently and none of them fails in rehearsal — they fail in front of an
audience, or quietly onto a disk, weeks later.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Something in config.yaml would make the piece behave wrongly."""


# Settings that exist to protect someone. (path, required value, why).
INVARIANTS = [
    ("websearch.third_party_search", False,
     "the engine would look up people who are not in the room and cannot consent"),
    ("websearch.self_search.require_consent", True,
     "visitors would be searched for without being asked"),
    ("websearch.self_search.cache", False,
     "one visitor's search results could be spoken to the next"),
    ("logging.sanitize_on_end", True,
     "names and raw speech would stay on disk after each visit"),
    ("logging.keep_raw_sessions", False,
     "raw session directories would accumulate in a public space"),
    ("llm.think", False,
     "the model's reasoning would be spoken aloud as the Oracle's answer"),
]


class Config:
    """Dotted read-only access to config.yaml. `cfg.get('stt.model')`."""

    def __init__(self, data: dict, path: Path):
        self._data = data
        self.path = path

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict:
        return self.get(name, {}) or {}

    def require(self, dotted: str) -> Any:
        value = self.get(dotted, _MISSING)
        if value is _MISSING:
            raise ConfigError(f"{self.path.name} is missing '{dotted}'")
        return value

    def path_to(self, dotted: str, default: str) -> Path:
        """A config value read as a path relative to the repository root."""
        return REPO / (self.get(dotted) or default)

    def check(self) -> None:
        problems = []
        for dotted, required, why in INVARIANTS:
            actual = self.get(dotted, required)       # absent means the default holds
            if actual != required:
                problems.append(f"  {dotted}: {actual!r} — must be {required!r}, or {why}")

        num_ctx = int(self.get("llm.num_ctx", 0) or 0)
        if num_ctx < 8192:
            problems.append(
                f"  llm.num_ctx: {num_ctx} — the always-on prompt alone is about 7,000 tokens, "
                "so anything under 8192 silently cuts the Oracle's instructions")

        voices = set(self.section("tts").get("voices", {}) or {})
        allowed = set(self.get("stt.allowed_languages", []) or [])
        missing = allowed - voices
        if missing:
            problems.append(
                f"  tts.voices has no voice for {sorted(missing)} — "
                "a visitor speaking one would be answered in another language")

        if problems:
            raise ConfigError(
                f"{self.path} has settings that change who the piece is safe for:\n"
                + "\n".join(problems))


_MISSING = object()
_cached: Config | None = None


def load(strict: bool = True) -> Config:
    """Read config.yaml and .env. Cached — the file is read once per process."""
    global _cached
    if _cached is not None:
        return _cached

    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass                       # only the web search needs it

    import yaml
    path = REPO / "config.yaml"
    if not path.exists():
        raise ConfigError(f"no config.yaml at {path}")
    cfg = Config(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)
    if strict:
        cfg.check()
    _cached = cfg
    return cfg


def api_key() -> str | None:
    """The Ollama web search key. Never from config.yaml, which is committed."""
    return os.environ.get("OLLAMA_API_KEY") or None
