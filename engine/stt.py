"""theoracle — speech to text, and deciding which language a visit is in.

Two jobs, and the second is the harder one. Whisper will happily tell you that
"joo" is English, that a cough is Welsh, and that a visitor who has spoken
Finnish for four turns has just switched to Estonian. Acting on every one of
those would make the Oracle seem to lose the thread, which is the one thing it
must never do.

So the language of a visit is a decision, not an observation. It is set once,
early, from something long enough to be worth trusting, and after that it takes
a much higher bar to change than it took to set: `switch_confidence` above
`min_confidence`, and a real sentence rather than a word. A visitor who truly
changes language says more than "joo" when they do.

    python -m engine.stt                # record and transcribe, once
    python -m engine.stt --loop         # the Phase 1 acceptance test
    python -m engine.stt file.wav       # transcribe a file instead
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import audio_devices as ad      # noqa: E402
from engine.config import load              # noqa: E402

_model = None


# ── what a transcription is ───────────────────────────────────────────────
@dataclass
class Transcript:
    text: str
    language: str                  # the visit's language, after the policy
    detected: str                  # what Whisper actually said it was
    confidence: float
    words: int
    speech_seconds: float          # speech only, silence excluded by the VAD
    elapsed: float                 # wall clock, for the latency budget
    switched: bool = False         # the policy changed the visit's language
    reason: str = ""               # why it did or did not

    @property
    def usable(self) -> bool:
        return bool(self.text) and self.words > 0


# ── the language policy ───────────────────────────────────────────────────
@dataclass
class LanguageState:
    """One visit's language, and the rules for changing it.

    Held by the visit, not by the model: it is conversation state, and it dies
    with the visit like everything else the Oracle knows about a person.
    """
    mode: str = "sticky"                  # sticky | per_turn | fixed
    default: str = "fi"
    allowed: tuple = ()
    min_confidence: float = 0.5
    switch_confidence: float = 0.75
    switch_min_words: int = 3
    switch_min_speech_s: float = 1.5
    current: str | None = None
    history: list = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg=None) -> "LanguageState":
        cfg = cfg or load()
        stt = cfg.section("stt")
        return cls(
            mode=stt.get("language_lock", "sticky"),
            default=stt.get("default_language", "fi"),
            allowed=tuple(stt.get("allowed_languages", []) or []),
            min_confidence=float(stt.get("min_confidence", 0.5)),
            switch_confidence=float(stt.get("switch_confidence", 0.75)),
            switch_min_words=int(stt.get("switch_min_words", 3)),
            switch_min_speech_s=float(stt.get("switch_min_speech_s", 1.5)),
        )

    def decide(self, detected: str, confidence: float, words: int,
               speech_s: float) -> tuple[str, bool, str]:
        """(language to answer in, did it change, why)."""
        if self.mode == "fixed":
            self.current = self.default
            return self.current, False, "language_lock is fixed"

        known = detected in self.allowed

        # First turn: set it, if there is anything to set it from.
        if self.current is None:
            if known and confidence >= self.min_confidence:
                self.current = detected
                self.history.append(detected)
                return detected, False, f"first turn, {confidence:.2f} confident"
            self.current = self.default
            self.history.append(self.default)
            why = (f"'{detected}' is not in allowed_languages" if not known
                   else f"only {confidence:.2f} confident")
            return self.current, False, f"{why}, defaulting to {self.default}"

        if not known:
            return self.current, False, f"'{detected}' is not a language we can speak"
        if detected == self.current:
            return self.current, False, "same language"

        if self.mode == "per_turn":
            if confidence >= self.min_confidence:
                return self._switch(detected, f"per_turn, {confidence:.2f} confident")
            return self.current, False, f"per_turn but only {confidence:.2f} confident"

        # Sticky: a real sentence, confidently, or nothing changes. Each of
        # these three has caught a different false positive in testing — the
        # confident single word, the long mumble, the crisp half-second "okay".
        if confidence < self.switch_confidence:
            return self.current, False, (
                f"would switch to {detected}, but {confidence:.2f} < {self.switch_confidence}")
        if words < self.switch_min_words:
            return self.current, False, (
                f"would switch to {detected}, but {words} word(s) is too short")
        if speech_s < self.switch_min_speech_s:
            return self.current, False, (
                f"would switch to {detected}, but {speech_s:.1f}s of speech is too short")
        return self._switch(detected, f"{words} words at {confidence:.2f}")

    def _switch(self, language: str, why: str) -> tuple[str, bool, str]:
        previous = self.current
        self.current = language
        self.history.append(language)
        return language, True, f"switched {previous} → {language}: {why}"


# ── the model ─────────────────────────────────────────────────────────────
def load_model(cfg=None):
    """Load Whisper once per process. Loading it costs seconds and VRAM."""
    global _model
    if _model is not None:
        return _model
    cfg = cfg or load()
    from faster_whisper import WhisperModel
    stt = cfg.section("stt")
    _model = WhisperModel(
        stt.get("model", "large-v3-turbo"),
        device=stt.get("device", "cuda"),
        compute_type=stt.get("compute_type", "int8"),
    )
    return _model


def unload() -> None:
    """Release the VRAM. Plan B, if TouchDesigner's scene needs it back."""
    global _model
    _model = None
    import gc
    gc.collect()


def transcribe(audio, language_state: LanguageState | None = None, cfg=None) -> Transcript:
    """Mono float32 at 16 kHz in, a Transcript out.

    `audio_devices.to_target_rate` is what produces the input; this never
    touches a device itself, so it can be tested from a file.
    """
    cfg = cfg or load()
    model = load_model(cfg)
    t0 = time.time()

    segments, info = model.transcribe(
        audio,
        language=None,                      # detect; the policy decides what to do about it
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,   # each turn stands alone; no drift between visits
    )
    segments = list(segments)
    text = " ".join(s.text for s in segments).strip()
    speech_s = sum(s.end - s.start for s in segments)
    words = len(text.split())

    detected = info.language
    confidence = float(info.language_probability)

    if language_state is None:
        language, switched, reason = detected, False, "no language state"
    else:
        language, switched, reason = language_state.decide(
            detected, confidence, words, speech_s)

    return Transcript(text=text, language=language, detected=detected,
                      confidence=confidence, words=words, speech_seconds=speech_s,
                      elapsed=time.time() - t0, switched=switched, reason=reason)


# ── the acceptance test ───────────────────────────────────────────────────
def _record(seconds: float, cfg):
    """Phase 5 replaces this with real endpointing. Here, a fixed window."""
    import sounddevice as sd
    audio = cfg.section("audio")
    index = ad.resolve(audio.get("input_device"), "input")
    rate = ad.native_samplerate(index, "input")
    chans = ad.usable_channels(index, "input", 1)
    pick = int(audio.get("input_channel", 0) or 0)

    print(f"  speak now — {seconds:.0f}s at {rate} Hz", flush=True)
    buf = sd.rec(int(seconds * rate), samplerate=rate, channels=chans,
                 dtype="float32", device=index)
    sd.wait()
    return ad.to_target_rate(ad.pick_channel(buf, pick), rate, ad.TARGET_RATE)


def main() -> None:
    args = sys.argv[1:]
    loop = "--loop" in args
    files = [a for a in args if not a.startswith("--")]
    cfg = load()
    state = LanguageState.from_config(cfg)

    print(f"  loading {cfg.get('stt.model')} on {cfg.get('stt.device')}…", flush=True)
    t0 = time.time()
    load_model(cfg)
    print(f"  ready in {time.time() - t0:.1f}s\n")
    print(f"  policy: {state.mode}, default {state.default}, "
          f"set at {state.min_confidence}, changed at {state.switch_confidence} "
          f"and {state.switch_min_words}+ words\n")

    if files:
        for f in files:
            audio, rate = _read_file(f)
            _report(transcribe(ad.to_target_rate(audio, rate), state, cfg))
        return

    while True:
        _report(transcribe(_record(6, cfg), state, cfg))
        if not loop:
            return
        try:
            input("  Enter to go again, Ctrl-C to stop\n")
        except (EOFError, KeyboardInterrupt):
            print("\n  visit language history:", " → ".join(state.history) or "none")
            return


def _read_file(path):
    from engine import wav_io
    return wav_io.read(path)


def _report(t: Transcript) -> None:
    mark = "→" if not t.switched else "⇒"
    print(f'  {mark} [{t.language}] "{t.text}"')
    print(f"      detected {t.detected} at {t.confidence:.2f}, {t.words} words, "
          f"{t.speech_seconds:.1f}s speech, {t.elapsed:.2f}s to transcribe")
    print(f"      {t.reason}\n")


if __name__ == "__main__":
    main()
