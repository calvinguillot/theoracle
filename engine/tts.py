"""theoracle — the Oracle's answer as a WAV TouchDesigner can play.

Piper synthesises; this module decides which voice, writes the file so that
TouchDesigner can never see it half-written, and keeps the folder from filling
up over a three-week run.

Two things here are less obvious than they look.

**The origin story is a recording, not a synthesis.** The first time a visitor
asks what the Oracle is, they hear a narration recorded for the piece rather
than Piper reading `origin.md`. Those files are copied into the same runtime
folder under a fresh id, so TouchDesigner's playback path stays exactly one
thing: `runtime/audio/<id>.wav`. Nothing downstream needs to know which answers
were recorded and which were spoken.

**Finnish has two recordings and the choice is fixed per visit.** Picking at
random each time a visitor asks a follow-up would make the Oracle sound like
two people arguing. Seeded on the session id, it is one voice for one visit and
a different one for the next.

    python -m engine.tts "Sanasi ovat suolaa."          # one line, default language
    python -m engine.tts --lang sv "Du kom tillbaka."
    python -m engine.tts --audition stories/sample.txt  # every voice, for Phase 2
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config import REPO, load       # noqa: E402

_voices: dict = {}


@dataclass
class Speech:
    audio_id: str
    path: Path
    duration: float
    sample_rate: int
    language: str
    voice: str
    elapsed: float
    prerendered: bool = False

    def as_event(self, **extra) -> dict:
        """The `response_ready` payload TouchDesigner is waiting for."""
        payload = {
            "event": "response_ready",
            "audio_id": self.audio_id,
            "audio_duration_seconds": round(self.duration, 3),
            "audio_sample_rate": self.sample_rate,
            "response_language": self.language,
            "prerendered": self.prerendered,
        }
        payload.update(extra)
        return payload


# ── voices ────────────────────────────────────────────────────────────────
def voice_name(language: str, cfg=None) -> str | None:
    return (cfg or load()).section("tts").get("voices", {}).get(language)


def load_voice(language: str, cfg=None):
    """Load a voice the first time it is needed, and keep it.

    Loading all ten at startup costs about 600 MB of RAM for voices most
    visitors will never hear. Loading on demand costs one visitor a few hundred
    milliseconds, once, on a language nobody has spoken yet.
    """
    cfg = cfg or load()
    if language in _voices:
        return _voices[language]

    name = voice_name(language, cfg)
    if not name:
        return None
    path = REPO / "voices" / f"{name}.onnx"
    if not path.exists():
        return None

    from piper import PiperVoice
    _voices[language] = PiperVoice.load(str(path))
    return _voices[language]


def resolve_voice(language: str, cfg=None) -> tuple[str, object] | tuple[None, None]:
    """The voice for this language, or the default language's, or any at all."""
    cfg = cfg or load()
    for candidate in (language, cfg.get("stt.default_language", "fi")):
        voice = load_voice(candidate, cfg)
        if voice is not None:
            return candidate, voice
    for candidate in cfg.section("tts").get("voices", {}):
        voice = load_voice(candidate, cfg)
        if voice is not None:
            return candidate, voice
    return None, None


# ── writing the file ──────────────────────────────────────────────────────
def output_dir(cfg=None) -> Path:
    d = (cfg or load()).path_to("tts.output_dir", "runtime/audio")
    d.mkdir(parents=True, exist_ok=True)
    return d


def synthesize(text: str, language: str, cfg=None) -> Speech:
    """Speak `text`, and return it only once the file is complete on disk."""
    cfg = cfg or load()
    if not text.strip():
        raise ValueError("nothing to speak")

    used, voice = resolve_voice(language, cfg)
    if voice is None:
        raise RuntimeError("no Piper voice is installed — see README Step 9")

    t0 = time.time()
    out = output_dir(cfg)
    audio_id = uuid.uuid4().hex
    tmp = out / f".{audio_id}.wav.part"
    final = out / f"{audio_id}.wav"

    # The dotted temporary name matters as much as the atomic rename: a Folder
    # DAT or a stray glob in TouchDesigner will not match it, so a partial file
    # is invisible even to something that goes looking.
    with wave.open(str(tmp), "wb") as wav:
        voice.synthesize_wav(text, wav)
    with wave.open(str(tmp)) as w:
        duration = w.getnframes() / w.getframerate()
        rate = w.getframerate()
    os.replace(tmp, final)          # atomic on Windows and POSIX alike

    cap = float(cfg.get("tts.max_duration_s", 45) or 45)
    if duration > cap:
        print(f"  warning: {duration:.0f}s of speech exceeds tts.max_duration_s ({cap:.0f}s)",
              file=sys.stderr)

    housekeep(cfg)
    return Speech(audio_id=audio_id, path=final, duration=duration, sample_rate=rate,
                  language=used, voice=voice_name(used, cfg) or "?",
                  elapsed=time.time() - t0)


def housekeep(cfg=None) -> int:
    """Keep the newest `tts.keep_last` files. Returns how many were removed.

    A three-week run at one visit every two minutes is thousands of files. None
    of them is wanted after it has played, and every one of them is a visitor's
    answer sitting on a disk in a public room.
    """
    cfg = cfg or load()
    keep = int(cfg.get("tts.keep_last", 20) or 20)
    files = sorted(output_dir(cfg).glob("*.wav"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            pass                    # TouchDesigner may still hold the newest; try again later
    return removed


# ── the recorded origin story ─────────────────────────────────────────────
def origin_choices(language: str, cfg=None) -> list:
    return list((cfg or load()).get(f"prerendered.origin.{language}", []) or [])


def has_origin(language: str, cfg=None) -> bool:
    cfg = cfg or load()
    folder = cfg.path_to("prerendered.dir", "voices/prerendered")
    return any((folder / f).exists() for f in origin_choices(language, cfg))


def origin(language: str, session_id: str = "", cfg=None) -> Speech | None:
    """The recorded narration for this language, staged for playback.

    None means there is no recording in their language, and the caller should
    generate the answer live from `origin.md` instead — which is what
    `prerendered.fallback: generate` asks for.
    """
    cfg = cfg or load()
    folder = cfg.path_to("prerendered.dir", "voices/prerendered")
    available = [folder / f for f in origin_choices(language, cfg) if (folder / f).exists()]
    if not available:
        return None

    # Seeded on the session: one voice per visit, a different one next time.
    seed = int(hashlib.sha256((session_id or uuid.uuid4().hex).encode()).hexdigest()[:8], 16)
    source = random.Random(seed).choice(available)

    from engine import wav_io
    meta = wav_io.probe(source)

    t0 = time.time()
    out = output_dir(cfg)
    audio_id = uuid.uuid4().hex
    tmp = out / f".{audio_id}.wav.part"
    final = out / f"{audio_id}.wav"
    tmp.write_bytes(source.read_bytes())
    os.replace(tmp, final)

    return Speech(audio_id=audio_id, path=final, duration=meta["seconds"],
                  sample_rate=meta["rate"], language=language, voice=source.name,
                  elapsed=time.time() - t0, prerendered=True)


# ── the acceptance test, and the Phase 2 audition ─────────────────────────
def _audition(source: str | None, cfg) -> None:
    """Render the same text in every installed voice, for listening to.

    This is the decision Phase 2 exists for: whether the Finnish voice is good
    enough to carry the piece. Read the files, do not read the waveform.
    """
    languages = list(cfg.section("tts").get("voices", {}))
    text = (Path(source).read_text(encoding="utf-8").strip() if source
            else "Et ole täällä. Synnyit sen jälkeen kun valo sammui.")

    print(f"  auditioning {len(languages)} voice(s) on {len(text)} characters\n")
    for lang in languages:
        if load_voice(lang, cfg) is None:
            print(f"  {lang:3s} — no voice installed")
            continue
        try:
            s = synthesize(text, lang, cfg)
            ratio = s.duration / max(s.elapsed, 1e-6)
            print(f"  {lang:3s} {s.voice:26s} {s.duration:6.1f}s audio  "
                  f"{s.elapsed:5.2f}s to render  ({ratio:.0f}× realtime)")
            print(f"       {s.path}")
        except Exception as e:
            print(f"  {lang:3s} — failed: {type(e).__name__}: {e}")

    print("\n  Listen to them. The Finnish one decides whether Finnish can be the")
    print("  primary language, and that is a listening decision, not a measurement.")


def main() -> None:
    args = sys.argv[1:]
    cfg = load()

    if "--audition" in args:
        rest = [a for a in args if not a.startswith("--")]
        _audition(rest[0] if rest else None, cfg)
        return

    lang = cfg.get("stt.default_language", "fi")
    if "--lang" in args:
        i = args.index("--lang")
        lang = args[i + 1]
        args = args[:i] + args[i + 2:]

    if "--origin" in args:
        s = origin(lang, cfg=cfg)
        if s is None:
            print(f"  no recording for '{lang}' — the engine would answer live from origin.md")
            return
        print(f"  {s.path}  {s.duration:.0f}s, from {s.voice}")
        return

    text = " ".join(a for a in args if not a.startswith("--"))
    if not text:
        print(__doc__)
        return

    s = synthesize(text, lang, cfg)
    print(f"  [{s.language}] {s.voice}")
    print(f"  {s.path}")
    print(f"  {s.duration:.1f}s of audio in {s.elapsed:.2f}s "
          f"({s.duration / max(s.elapsed, 1e-6):.0f}× realtime), {s.sample_rate} Hz")


if __name__ == "__main__":
    main()
