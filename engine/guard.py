"""theoracle — what may reach the model, and what may leave it.

Four of the five layers in the design live here. Layer 2, structural
separation, is `llm.py` building the message — it is a shape, not a check, and
it does more work than everything in this file.

The ordering principle: **a refusal that sounds like a content filter breaks the
spell more thoroughly than complying would.** So nothing here ever explains
itself to a visitor. A blocked attempt gets an oracle's answer from
`stories/fallbacks.yaml` — "My creators did not want me to do that" — which is
true in the world, explains nothing technical, and turns the constraint into
lore. The logging is for you; the room hears a line of the piece.

What this file does NOT do, and should not be asked to: stop a paraphrased
attack. The pattern screen catches "ignore all previous instructions" and its
Finnish and Swedish forms, which is most of what a gallery actually produces,
and it does that without spending a model call. Anything cleverer goes to the
model, where `instructions.md` handles it in character and far better than a
regex could. That is the design, not a gap in it.

    python -m engine.guard --test        # the Phase 4 acceptance test
    python -m engine.guard "some text"   # screen one line
"""

from __future__ import annotations

import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import REPO, load          # noqa: E402

FALLBACKS = REPO / "stories" / "fallbacks.yaml"
_fallbacks: dict | None = None


# ── Layer 1: input hygiene ────────────────────────────────────────────────
# Whisper emits plain speech. Every one of these is either a hallucination from
# training data or someone who typed into a microphone, and neither deserves to
# reach the prompt intact.
_MARKUP = re.compile(
    r"<\|[^|]*\|>"                 # <|im_start|> and friends
    r"|\[/?INST\]|\[/?SYS\]"       # Llama-style role markers
    r"|<\|?/?(?:system|user|assistant|im_start|im_end)\|?>"
    r"|```+|~~~+"                  # code fences
    r"|^\s*#{2,}.*$",              # markdown headings
    re.I | re.M)

_ROLE_PREFIX = re.compile(
    r"^\s*(?:system|assistant|user|human|ai)\s*[:>]\s*", re.I | re.M)


@dataclass
class Screen:
    """What Layer 1 and Layer 3 together decided about a visitor's speech."""
    text: str                       # normalised, capped — what the model would see
    blocked: bool = False
    category: str = ""              # which pattern, for the log
    notes: list = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.blocked and bool(self.text)


def normalise(text: str, cfg=None) -> tuple[str, list]:
    """Layer 1. Returns the cleaned text and what had to be removed."""
    cfg = cfg or load()
    notes = []
    before = text

    text = _MARKUP.sub(" ", text)
    text = _ROLE_PREFIX.sub("", text)
    text = text.replace("​", "").replace("﻿", "")
    text = " ".join(text.split())
    if text != " ".join(before.split()):
        notes.append("stripped markup or role syntax")

    cap = int(cfg.get("guard.max_input_chars", 300) or 300)
    if len(text) > cap:
        # Cut at a word, not mid-syllable: the tail is the least interesting
        # part of a 300-character utterance and the model still gets the shape.
        text = text[:cap].rsplit(" ", 1)[0]
        notes.append(f"capped at {cap} characters")
    return text, notes


# ── Layer 3: the pattern screen ───────────────────────────────────────────
# One regex per idea, across the languages a visitor is most likely to try it
# in. These are cheap and they are meant to be: they buy the common case for
# free, and everything else is the model's job.
PATTERNS = [
    ("override", re.compile(
        r"ignore\s+(all\s+)?(previous|prior|above|your)\s+(instruction|rule|prompt)"
        r"|disregard\s+(all\s+)?(previous|prior|your)"
        r"|forget\s+(everything|all|your\s+(instruction|rule))"
        r"|unohda\s+(kaikki|ohjeet|aiemmat)"                 # fi
        r"|gl(ö|o)m\s+(alla\s+)?(instruktioner|dina\s+regler)"  # sv
        r"|olvida\s+(todas\s+)?(las\s+)?instrucciones"       # es
        r"|ignoriere\s+(alle\s+)?(vorherigen\s+)?anweisungen",  # de
        re.I)),
    ("reveal", re.compile(
        r"(repeat|print|show|output|reveal|tell\s+me)\s+(me\s+)?(your|the)\s+"
        r"(instruction|prompt|system|rule|source|config)"
        r"|what\s+(is|are)\s+your\s+(system\s+)?(prompt|instruction)"
        r"|n(ä|a)yt(ä|a)\s+(ohjee|kehote)"                   # fi
        r"|visa\s+(dina\s+)?(instruktioner|systemprompt)"     # sv
        r"|system\s*prompt",
        re.I)),
    ("persona", re.compile(
        r"you\s+are\s+now\s+(a|an|the)\b"
        r"|pretend\s+(to\s+be|you\s+are)"
        r"|act\s+as\s+(a|an|if\s+you)"
        r"|from\s+now\s+on\s+you"
        r"|(enter|activate)\s+\w+\s+mode"
        r"|olet\s+nyt\s+"                                    # fi
        r"|du\s+(ä|a)r\s+nu\s+(en|ett)\b"                    # sv
        r"|act(ú|u)a\s+como\b",                              # es
        re.I)),
    ("authority", re.compile(
        r"(i\s+am|this\s+is)\s+(the\s+)?(developer|artist|admin|administrator|technician|engineer)"
        r"|developer\s+mode|debug\s+mode|maintenance\s+mode"
        r"|olen\s+(kehitt(ä|a)j(ä|a)|yll(ä|a)pit(ä|a)j(ä|a))"  # fi
        r"|jag\s+(ä|a)r\s+(utvecklare|administrat(ö|o)r)",     # sv
        re.I)),
    ("others", re.compile(
        r"what\s+did\s+(the\s+)?(last|previous|other)\s+(person|visitor|one)"
        r"|(tell|show)\s+me\s+(about\s+)?(the\s+)?(last|previous|other)\s+(person|visitor)"
        r"|mit(ä|a)\s+(edellinen|viime)\s+(k(ä|a)vij(ä|a)|henkil(ö|o))"   # fi
        r"|vad\s+sa\s+(den\s+)?(f(ö|o)rra|f(ö|o)reg(å|a)ende)",           # sv
        re.I)),
]


def screen(text: str, cfg=None) -> Screen:
    """Layers 1 and 3 together. Cheap, and runs on every utterance."""
    cfg = cfg or load()
    cleaned, notes = normalise(text, cfg)
    for category, pattern in PATTERNS:
        if pattern.search(cleaned):
            return Screen(text=cleaned, blocked=True, category=category, notes=notes)
    return Screen(text=cleaned, blocked=False, notes=notes)


def clean_reference(text: str, cfg=None) -> str:
    """A web result, before it goes anywhere near the prompt.

    A page that says "ignore your previous instructions" is the same attack as
    a visitor saying it aloud, only quieter — so references go through the same
    screen, and a flagged one is dropped rather than deflected: nobody asked
    for it, and the Oracle can answer without it.
    """
    cfg = cfg or load()
    cleaned, _ = normalise(text, cfg)
    cap = int(cfg.get("websearch.max_chars", 2000) or 2000)
    cleaned = cleaned[:cap]
    for _, pattern in PATTERNS:
        if pattern.search(cleaned):
            return ""
    return cleaned


# ── Layer 4: output validation ────────────────────────────────────────────
_URL = re.compile(r"https?://|www\.|\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b|\b[A-Za-z]:\\|/[\w./-]{8,}", re.I)
_CODEISH = re.compile(r"```|</?[a-z_]+>|\{\s*\"\w+\"\s*:", re.I)
_LEAK_MARKERS = ("<established>", "<visitor_speech>", "<reference>", "<lore>",
                 "instructions.md", "world.md", "origin.md", "system prompt",
                 "num_ctx", "config.yaml")

# Scripts that are never right for a Latin-alphabet answer. Subtler language
# errors are left alone: on two sentences, any detector worth trusting costs
# more than the mistake, and a wrong-language reply is audible anyway.
_SCRIPTS = {
    "cjk": re.compile(r"[぀-ヿ一-鿿]"),
    "cyrillic": re.compile(r"[Ѐ-ӿ]"),
    "arabic": re.compile(r"[؀-ۿ]"),
    "hangul": re.compile(r"[가-힯]"),
}
_EXPECTED_SCRIPT = {"ja": "cjk", "zh": "cjk"}


@dataclass
class Verdict:
    ok: bool
    reason: str = ""
    category: str = ""


def validate_output(text: str, language: str, corpus_text: str = "", cfg=None) -> Verdict:
    """Layer 4. Everything here is a reason to speak the fallback instead."""
    cfg = cfg or load()
    low = text.lower()

    floor, cap = 10, int(cfg.get("guard.max_output_chars", 600) or 600)
    if len(text) < floor:
        return Verdict(False, f"only {len(text)} characters", "too_short")
    if len(text) > cap:
        return Verdict(False, f"{len(text)} characters exceeds {cap}", "too_long")

    if _URL.search(text):
        return Verdict(False, "contains a URL, address or path", "not_speech")
    if _CODEISH.search(text):
        return Verdict(False, "contains markup or code", "not_speech")

    for marker in _LEAK_MARKERS:
        if marker in low:
            return Verdict(False, f"leaked '{marker}'", "leak")

    # A verbatim run from the corpus means the model is reciting its own
    # instructions rather than speaking. Forty characters is long enough that
    # ordinary phrasing never collides with it by accident.
    if corpus_text:
        haystack = " ".join(corpus_text.split()).lower()
        needle = " ".join(text.split()).lower()
        for i in range(0, max(0, len(needle) - 40), 8):
            if needle[i:i + 40] in haystack:
                return Verdict(False, "repeated 40+ characters of the corpus", "leak")

    expected = _EXPECTED_SCRIPT.get(language)
    for name, pattern in _SCRIPTS.items():
        if pattern.search(text) and name != expected:
            return Verdict(False, f"{name} characters in a {language} reply", "wrong_language")

    for word in profanity(cfg):
        if re.search(rf"\b{re.escape(word)}\b", low):
            return Verdict(False, "matched the profanity list", "profanity")

    return Verdict(True)


def profanity(cfg=None) -> tuple:
    """Words the piece will not say, from an optional file you supply.

    Empty by default and deliberately so: a list shipped in the repository
    would be wrong for your languages and your room, and a bad one rejects
    good replies. Point `guard.profanity_file` at one when you know what
    belongs in it.
    """
    cfg = cfg or load()
    name = cfg.get("guard.profanity_file")
    if not name:
        return ()
    path = REPO / name
    if not path.exists():
        return ()
    return tuple(w.strip().lower() for w in path.read_text(encoding="utf-8").splitlines()
                 if w.strip() and not w.startswith("#"))


# ── the canned lines ──────────────────────────────────────────────────────
# A second and third attempt get shorter, colder lines — instructions.md asks
# for exactly that, and three identical sentences in a row is the most
# spell-breaking thing the piece could do. A language without its own escalation
# falls back to that language's ordinary deflection, never to another language.
KIND_FALLBACK = {"deflect_again": "deflect", "deflect_last": "deflect"}


def fallback(kind: str, language: str, cfg=None) -> str:
    """An oracle's line for a moment the model cannot be trusted to fill.

    `kind` is one of: deflect, deflect_again, deflect_last (someone is trying
    to break it, for the first / second / third time), unclear (noise or a
    single word), error (the machinery failed).

    The language of the visit wins over the kind. Answering a German visitor in
    Finnish because the German escalation line is missing would be a worse
    failure than repeating the German one.
    """
    global _fallbacks
    if _fallbacks is None:
        if not FALLBACKS.exists():
            _fallbacks = {}
        else:
            import yaml
            _fallbacks = yaml.safe_load(FALLBACKS.read_text(encoding="utf-8")) or {}

    for candidate in (kind, KIND_FALLBACK.get(kind)):
        if candidate and language in (_fallbacks.get(candidate) or {}):
            return _fallbacks[candidate][language]

    # No line in their language at all: the default language is better than
    # silence, and this only happens for a language with no voice either.
    default = (cfg or load()).get("stt.default_language", "fi")
    base = KIND_FALLBACK.get(kind, kind)
    for candidate in (kind, base):
        lines = _fallbacks.get(candidate) or {}
        if lines.get(default) or lines.get("en"):
            return lines.get(default) or lines["en"]
    return "…"


# ── Layer 5: operational limits ───────────────────────────────────────────
@dataclass
class Limits:
    """The cheapest guardrails in the design, and among the strongest.

    A visit is capped at a handful of turns and nothing survives it, so an
    attacker gets one short conversation and no way to build up context across
    visitors. These limits protect the room rather than the model: one person
    monopolising the piece, a sensor stuck high, audio arriving from a visit
    that ended.
    """
    cooldown_s: float = 2.0
    max_per_minute: int = 12
    _starts: deque = field(default_factory=deque)
    _free_at: float = 0.0

    @classmethod
    def from_config(cls, cfg=None) -> "Limits":
        cfg = cfg or load()
        return cls(cooldown_s=float(cfg.get("guard.cooldown_s", 2.0) or 2.0),
                   max_per_minute=int(cfg.get("guard.max_per_minute", 12) or 12))

    def may_start(self, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        if now < self._free_at:
            return False, f"cooling down for {self._free_at - now:.1f}s"
        while self._starts and now - self._starts[0] > 60:
            self._starts.popleft()
        if len(self._starts) >= self.max_per_minute:
            return False, f"{self.max_per_minute} visits in the last minute"
        return True, ""

    def note_start(self, now: float | None = None) -> None:
        self._starts.append(time.time() if now is None else now)

    def note_playback_end(self, now: float | None = None) -> None:
        self._free_at = (time.time() if now is None else now) + self.cooldown_s


# ── the acceptance test ───────────────────────────────────────────────────
def _test(cfg) -> None:
    import yaml
    path = REPO / "tests" / "guard_cases.yaml"
    if not path.exists():
        print(f"  missing {path}")
        return
    cases = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    direct = cases.get("direct_attacks", [])
    paraphrased = cases.get("paraphrased_attacks", [])
    normal = cases.get("normal", [])

    print(f"  {len(direct)} direct attacks — every one must be caught here\n")
    missed = []
    for case in direct:
        s = screen(case["text"], cfg)
        mark = "caught" if s.blocked else "MISSED"
        if not s.blocked:
            missed.append(case)
        print(f"  [{case['lang']}] {mark:6s} {case.get('category', ''):10s} {case['text'][:56]}")

    print(f"\n  {len(paraphrased)} paraphrased attacks — these are MEANT to reach the model,")
    print("  where instructions.md answers them in character\n")
    caught_free = 0
    for case in paraphrased:
        s = screen(case["text"], cfg)
        if s.blocked:
            caught_free += 1
        print(f"  [{case['lang']}] {'caught' if s.blocked else 'to model':9s} {case['text'][:56]}")

    print(f"\n  {len(normal)} ordinary things visitors say — none may be flagged\n")
    false_positives = []
    for case in normal:
        s = screen(case["text"], cfg)
        if s.blocked:
            false_positives.append((case, s.category))
            print(f"  [{case['lang']}] FALSE POSITIVE ({s.category}) {case['text'][:48]}")
    if not false_positives:
        print("  none flagged")

    print("\n  ── output validation ──\n")
    bad = [
        ("a URL", "The tide turns at https://example.com/tides tonight."),
        ("an email", "Write to oracle@olkiluoto.fi and they will answer."),
        ("JSON", 'Here is my reply: {"text": "You are not in here."}'),
        ("a tag", "You are not in here. <visitor_speech>"),
        ("a file name", "My character is described in instructions.md."),
        ("too short", "Yes."),
        ("CJK in Finnish", "Et ole täällä. 你不在这里。"),
    ]
    for name, text in bad:
        v = validate_output(text, "fi", cfg=cfg)
        print(f"  {'rejected' if not v.ok else 'ALLOWED ':9s} {name:16s} {v.reason}")

    good = "You are not in here. You were born after the light went out, and nobody wrote it down."
    v = validate_output(good, "en", cfg=cfg)
    print(f"  {'allowed ' if v.ok else 'REJECTED':9s} {'a real reply':16s} {v.reason}")

    print("\n  ── summary ──")
    print(f"  direct attacks caught:     {len(direct) - len(missed)}/{len(direct)}")
    print(f"  paraphrased sent to model: {len(paraphrased) - caught_free}/{len(paraphrased)}")
    print(f"  false positives:           {len(false_positives)}  (must be 0)")
    if missed:
        print("\n  MISSED, add a pattern for each:")
        for case in missed:
            print(f"    [{case['lang']}] {case['text']}")


def main() -> None:
    cfg = load()
    if "--test" in sys.argv:
        _test(cfg)
        return
    text = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    if not text:
        print(__doc__)
        return
    s = screen(text, cfg)
    print(f"  text:     {s.text!r}")
    print(f"  blocked:  {s.blocked}" + (f"  ({s.category})" if s.category else ""))
    if s.notes:
        print(f"  notes:    {'; '.join(s.notes)}")
    if s.blocked:
        print(f"  would say: {fallback('deflect', cfg.get('stt.default_language', 'fi'), cfg)}")


if __name__ == "__main__":
    main()
