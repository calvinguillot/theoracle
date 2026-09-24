"""theoracle — the Oracle's reply, parsed and held to its contract.

`instructions.md` asks for four fields and nothing else:

    {"text": "...", "mood": "mysterious", "needs": "name", "fate": {"love": "..."}}

A 4B model produces that most of the time. The rest of the time it wraps the
object in a code fence, writes "Here is the reply:" above it, leaks a `<think>`
block despite thinking being off, or invents a mood. None of that is worth a
failed visit, so everything recoverable is recovered and only an empty `text`
is fatal — a visitor hearing a slightly wrong `mood` notices nothing, and a
visitor hearing silence notices everything.

The fate sheet is the other half of this file. A visitor who asks three
questions should hear three pages of the same book, and the model cannot be
relied on to remember what it foretold six hundred tokens ago. So every lasting
claim it makes is kept here and handed back to it, as `<established>`, on every
later turn. The consistency is bookkeeping, not memory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

MOODS = ("calm", "mysterious", "tense", "hopeful")
NEEDS = ("name", "detail", "consent", "narrow", "nothing", "end")
FATE_KEYS = ("work", "love", "family", "home", "turning", "death", "before")

# The protocol's `reading` vocabulary, as the design document defines it.
READING_OF = {"work": "work", "love": "love", "family": "family", "death": "death",
              "home": "life", "turning": "decision", "before": "before"}

DEFAULT_MOOD = "mysterious"
DEFAULT_NEEDS = "nothing"

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)
_THINK = re.compile(r"<think>.*?</think>\s*", re.S | re.I)
_MARKDOWN = re.compile(r"[*_`#]+")


class ReplyError(Exception):
    """Nothing usable came back. The caller should retry or fall back."""


@dataclass
class Reply:
    text: str
    mood: str = DEFAULT_MOOD
    needs: str = DEFAULT_NEEDS
    fate: dict = field(default_factory=dict)
    repairs: list = field(default_factory=list)   # what had to be fixed, for the log

    @property
    def reading(self) -> str:
        """The kind of reading this turn gave, for the visuals.

        Taken from what the Oracle declared about their life rather than from
        the words of the question. Classifying the question would mean keyword
        lists in ten languages that go wrong on the first person who phrases it
        oddly; the fate key is the Oracle's own account of what it just did.
        """
        for key in FATE_KEYS:
            if key in self.fate:
                return READING_OF.get(key, key)
        return "other"

    def as_event(self, **extra) -> dict:
        payload = {
            "text": self.text,
            "mood": self.mood,
            "needs": self.needs,
            "reading": self.reading,
            # char_count is code points of the final spoken text, measured here
            # and never taken from anything the model claims about itself.
            "char_count": len(self.text),
            "word_count": len(self.text.split()),
        }
        payload.update(extra)
        return payload


# ── finding the object ────────────────────────────────────────────────────
def _extract(raw: str) -> str:
    """The JSON object inside whatever the model actually sent."""
    raw = _THINK.sub("", raw).strip()

    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    # Braces, counted rather than matched by regex: the text field will contain
    # its own punctuation, and a lazy regex stops at the first '}' inside it.
    start = raw.find("{")
    if start == -1:
        raise ReplyError("no JSON object in the reply")
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(raw[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    raise ReplyError("the JSON object is not closed")


def _clean_text(text: str, repairs: list) -> str:
    """Spoken prose. Piper reads punctuation; it should not read asterisks."""
    before = text
    text = _THINK.sub("", text)
    text = _MARKDOWN.sub("", text)
    text = " ".join(text.split())        # newlines and runs of space collapse
    if text != before.strip():
        repairs.append("cleaned markup or whitespace out of text")
    return text.strip()


def parse(raw: str, fate_max_words: int = 12) -> Reply:
    """Model output in, a Reply out. Raises ReplyError only if nothing is left."""
    repairs: list[str] = []
    try:
        data = json.loads(_extract(raw))
    except ReplyError:
        raise
    except json.JSONDecodeError as e:
        raise ReplyError(f"malformed JSON: {e}") from e
    if not isinstance(data, dict):
        raise ReplyError("the reply is not an object")

    text = _clean_text(str(data.get("text", "")), repairs)
    if not text:
        raise ReplyError("the reply has no text to speak")

    mood = str(data.get("mood", "")).strip().lower()
    if mood not in MOODS:
        if mood:
            repairs.append(f"mood '{mood}' is not one of {MOODS}")
        mood = DEFAULT_MOOD

    needs = str(data.get("needs", "")).strip().lower()
    if needs not in NEEDS:
        if needs:
            repairs.append(f"needs '{needs}' is not one of {NEEDS}")
        needs = DEFAULT_NEEDS

    fate = {}
    raw_fate = data.get("fate") or {}
    if isinstance(raw_fate, dict):
        for key, value in raw_fate.items():
            key = str(key).strip().lower()
            if key not in FATE_KEYS:
                repairs.append(f"dropped fate key '{key}'")
                continue
            value = " ".join(str(value).split())
            if not value:
                continue
            words = value.split()
            if len(words) > fate_max_words:
                value = " ".join(words[:fate_max_words])
                repairs.append(f"trimmed fate.{key} to {fate_max_words} words")
            fate[key] = value
    elif raw_fate:
        repairs.append("fate was not an object; ignored")

    return Reply(text=text, mood=mood, needs=needs, fate=fate, repairs=repairs)


# ── what has been foretold ────────────────────────────────────────────────
@dataclass
class FateSheet:
    """The facts of one foretold life. Dies with the visit, like everything else."""
    max_entries: int = 8
    max_words: int = 12
    entries: dict = field(default_factory=dict)
    name: str | None = None

    @classmethod
    def from_config(cls, cfg=None) -> "FateSheet":
        from engine.config import load
        cfg = cfg or load()
        c = cfg.section("conversation")
        return cls(max_entries=int(c.get("fate_max_entries", 8)),
                   max_words=int(c.get("fate_max_words", 12)))

    def absorb(self, reply: Reply) -> list[str]:
        """Keep what is new. Returns the keys that were added or changed."""
        changed = []
        for key, value in reply.fate.items():
            if self.entries.get(key) == value:
                continue
            # A later declaration refines an earlier one rather than replacing
            # it wholesale: the Oracle said 'a radio repairer', then 'met at
            # thirty-four'. Both are true of the same life.
            if key in self.entries and value not in self.entries[key]:
                merged = f"{self.entries[key]}; {value}"
                if len(merged.split()) <= self.max_words * 2:
                    value = merged
            if key not in self.entries and len(self.entries) >= self.max_entries:
                continue                      # full; the visit is nearly over anyway
            self.entries[key] = value
            changed.append(key)
        return changed

    def block(self) -> str:
        """The `<established>` block, or empty on the first turn."""
        if not self.entries and not self.name:
            return ""
        lines = []
        if self.name:
            lines.append(f"name: {self.name}")
        lines += [f"{k}: {v}" for k, v in self.entries.items()]
        return ("<established>\n"
                "Already true of this visitor. Build on it; never contradict it.\n"
                + "\n".join(lines) + "\n</established>")

    def __len__(self) -> int:
        return len(self.entries)
