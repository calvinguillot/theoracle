"""theoracle — the microphone, and deciding when someone has stopped talking.

Two ideas carry this file.

**The stream never closes.** A microphone opened when the sensor fires is a
microphone that misses the first word, because people begin speaking as they
step up, not after. So capture runs continuously into a two-second ring buffer
from the moment the engine starts, and a visit gets those two seconds prepended
to whatever it records. This is the single most common cause of "it cut off my
first word" in installations like this, and the fix costs one deque.

**Recording stops on silence, not on a timer.** A fixed window either rushes
someone who pauses to think or leaves everyone else standing in front of a
machine that has obviously stopped listening. So: wait for speech, then end the
turn after `silence_ms` of quiet, with a hard cap for the hot-mic case.

The voice-activity detector is `webrtcvad` when it is installed and an adaptive
energy gate when it is not. The fallback is honest rather than good: it learns
the room's noise floor while nobody is speaking and triggers above it. With the
CQ's own gate set properly it has an easy job, because it is deciding where
speech ends rather than fighting room tone.

    python -m engine.audio_in          # watch the level meter and the VAD flag
    python -m engine.audio_in --listen # one endpointed capture, then the numbers
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import audio_devices as ad      # noqa: E402
from engine.config import load              # noqa: E402

FRAME_MS = 20            # webrtcvad accepts 10, 20 or 30; 20 is the usual compromise
SILENCE_FLOOR = 1e-5     # below this it is digital silence, not a quiet room


@dataclass
class Capture:
    """One recording, already at 16 kHz and ready for Whisper."""
    audio: object                  # mono float32 at ad.TARGET_RATE
    seconds: float                 # total, including the pre-roll
    speech_seconds: float
    peak_level: float
    reason: str                    # silence | max_length | no_speech | cancelled
    preroll_used: float = 0.0

    @property
    def usable(self) -> bool:
        return self.reason in ("silence", "max_length")


# ── voice activity ────────────────────────────────────────────────────────
class _WebRTC:
    """The real thing, when it is installed."""
    name = "webrtcvad"

    def __init__(self, rate: int, aggressiveness: int = 2):
        import webrtcvad
        self.vad = webrtcvad.Vad(aggressiveness)
        self.rate = rate

    def is_speech(self, frame_f32) -> bool:
        import numpy as np
        pcm = (np.clip(frame_f32, -1, 1) * 32767).astype("<i2").tobytes()
        try:
            return self.vad.is_speech(pcm, self.rate)
        except Exception:
            return False


class _Energy:
    """The fallback: learn the room's floor continuously, trigger above it.

    The obvious version of this — only update the floor while nobody is
    speaking — deadlocks, and did. A laptop microphone array in an ordinary
    room sits above any fixed starting threshold, so the gate decides speech is
    happening, never updates the floor because it thinks someone is talking,
    and never comes down: every recording runs to the hard cap.

    So the floor is a low percentile of *everything* heard recently, speech
    included. Over a five-second window the quietest tenth is the gaps between
    words and the room underneath them, which is exactly the level we want.
    It costs a moment to settle at startup and then it cannot get stuck.

    The margin is 2.0 — speech sits well above room tone, but "well above"
    means about 6 dB on a laptop array, not the 10 dB a tidier number would
    assume. Swept against a quiet room, a noisy array, near-digital silence
    and a loud gallery, everything from 1.6 to 2.0 detects all the speech with
    no false triggers; 3.0 starts missing speech in a loud room entirely.

    It is still the fallback. `webrtcvad` is trained on speech; this is
    arithmetic on loudness. Install the real one.
    """
    name = "energy"

    def __init__(self, rate: int, margin: float = 2.0):
        self.margin = margin
        self.floor = 0.0
        self.threshold = 0.0
        self._recent = deque(maxlen=250)     # five seconds at 20 ms

    def is_speech(self, frame_f32) -> bool:
        import numpy as np
        rms = float(np.sqrt(np.mean(np.square(frame_f32))) or 0.0)
        self._recent.append(rms)

        if len(self._recent) < 25:
            return False                     # half a second of listening first
        self.floor = float(np.percentile(self._recent, 10))
        # The additive term matters in a near-silent room, where the floor is
        # tiny and a multiplier alone would trigger on the noise of the ADC.
        self.threshold = max(self.floor * self.margin,
                             self.floor + 0.004, SILENCE_FLOOR * 20)
        return rms > self.threshold


def make_vad(rate: int, cfg=None):
    cfg = cfg or load()
    try:
        return _WebRTC(rate, int(cfg.get("audio.vad_aggressiveness", 2) or 2))
    except Exception:
        return _Energy(rate, float(cfg.get("audio.vad_margin", 2.0) or 2.0))


# ── the microphone ────────────────────────────────────────────────────────
@dataclass
class Microphone:
    """Continuous capture. Start it once, at startup, and leave it running."""

    cfg: object = None
    device: int | None = None
    rate: int = 48000
    channels: int = 1
    channel: int = 0
    level: float = 0.0             # 0–1, for OSC; updated every frame
    speech_active: bool = False    # the VAD flag, for OSC
    _stream: object = None
    _ring: deque = field(default_factory=deque)
    _live: deque = field(default_factory=deque)
    _armed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.cfg = self.cfg or load()
        audio = self.cfg.section("audio")
        self.device = ad.resolve(audio.get("input_device"), "input")
        self.rate = ad.native_samplerate(self.device, "input")
        self.channels = ad.usable_channels(self.device, "input", 1)
        self.channel = int(audio.get("input_channel", 0) or 0)
        self.blocksize = int(self.rate * FRAME_MS / 1000)
        preroll = float(audio.get("preroll_s", 2.0) or 2.0)
        self._ring = deque(maxlen=max(1, int(preroll * 1000 / FRAME_MS)))
        self._vad = make_vad(self.rate, self.cfg)

    # ── the PortAudio thread ──────────────────────────────────────────────
    def _callback(self, indata, frames, time_info, status):
        import numpy as np
        mono = ad.pick_channel(indata, self.channel)

        rms = float(np.sqrt(np.mean(np.square(mono))) or 0.0)
        # -60 dBFS to 0 dBFS across the 0–1 range: linear RMS spends most of
        # its travel in the bottom tenth and makes a dull control signal.
        db = 20 * np.log10(max(rms, 1e-9))
        self.level = float(min(1.0, max(0.0, (db + 60) / 60)))
        self.speech_active = self._vad.is_speech(mono)

        frame = (mono.copy(), self.speech_active, rms)
        with self._lock:
            self._ring.append(frame)
            if self._armed:
                self._live.append(frame)

    def start(self) -> None:
        import sounddevice as sd
        if self._stream is not None:
            return
        self._stream = sd.InputStream(
            device=self.device, samplerate=self.rate, channels=self.channels,
            dtype="float32", blocksize=self.blocksize, callback=self._callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def describe(self) -> str:
        return (f"{ad.describe(self.device).strip()}  "
                f"{self.channels}ch, channel {self.channel}, VAD: {self._vad.name}")

    # ── one turn ──────────────────────────────────────────────────────────
    def listen(self, cancel: threading.Event | None = None) -> Capture:
        """Block until the visitor stops speaking. Runs off the event loop."""
        import numpy as np
        audio = self.cfg.section("audio")
        silence_s = float(audio.get("silence_ms", 800) or 800) / 1000
        min_speech = float(audio.get("min_speech_s", 0.5) or 0.5)
        max_s = float(audio.get("max_utterance_s", 20) or 20)
        wait_s = float(audio.get("speech_start_timeout_s", 8) or 8)

        with self._lock:
            preroll = list(self._ring)       # the two seconds before the sensor fired
            self._live.clear()
            self._armed = True

        collected = list(preroll)
        preroll_s = len(preroll) * FRAME_MS / 1000
        speech_frames = sum(1 for _, s, _ in preroll if s)
        silent_run = 0.0
        blips = 0
        started = time.time()
        peak = 0.0
        reason = "no_speech"

        try:
            while True:
                if cancel is not None and cancel.is_set():
                    reason = "cancelled"
                    break

                with self._lock:
                    new = list(self._live)
                    self._live.clear()

                if not new:
                    time.sleep(FRAME_MS / 2000)
                else:
                    for frame, speaking, rms in new:
                        collected.append((frame, speaking, rms))
                        peak = max(peak, rms)
                        if speaking:
                            speech_frames += 1
                            blips += 1
                            # One noisy 20 ms frame — a chair, a cough across
                            # the room, a consonant's own burst — should not
                            # restart the silence clock. Two in a row means
                            # someone is actually talking again. Without this,
                            # a visitor who has finished is held listening
                            # until the hard cap, which is what happened.
                            if blips >= 2:
                                silent_run = 0.0
                        else:
                            blips = 0
                            silent_run += FRAME_MS / 1000

                elapsed = time.time() - started
                speech_s = speech_frames * FRAME_MS / 1000

                if speech_s >= min_speech and silent_run >= silence_s:
                    reason = "silence"
                    break
                if elapsed >= max_s:
                    reason = "max_length"
                    break
                if speech_s < min_speech and elapsed >= wait_s:
                    reason = "no_speech"
                    break
        finally:
            with self._lock:
                self._armed = False
                self._live.clear()

        if reason == "max_length":
            # Not a detail: it means endpointing never fired, and every visitor
            # waited the full cap. Worth seeing in the log the first time.
            pass

        raw = (np.concatenate([f for f, _, _ in collected]) if collected
               else np.zeros(1, dtype="float32"))
        return Capture(
            audio=ad.to_target_rate(raw, self.rate, ad.TARGET_RATE),
            seconds=len(raw) / self.rate,
            speech_seconds=speech_frames * FRAME_MS / 1000,
            peak_level=peak, reason=reason, preroll_used=preroll_s)


# ── looking at it ─────────────────────────────────────────────────────────
def main() -> None:
    cfg = load()
    mic = Microphone(cfg=cfg)
    mic.start()
    print(f"  {mic.describe()}")

    if "--listen" in sys.argv:
        print("  listening — speak, then stop\n")
        c = mic.listen()
        print(f"  reason        {c.reason}")
        print(f"  captured      {c.seconds:.1f}s (including {c.preroll_used:.1f}s of pre-roll)")
        print(f"  speech        {c.speech_seconds:.1f}s")
        print(f"  peak          {c.peak_level:.3f}")
        print(f"  at 16 kHz     {len(c.audio)} samples")
        mic.stop()
        return

    print("  level meter — Ctrl-C to stop")
    print("  if SPEECH never goes out while you are silent, the gate is stuck:")
    print("  raise audio.vad_margin, or install webrtcvad\n")
    try:
        while True:
            bar = "█" * int(mic.level * 40)
            flag = "SPEECH" if mic.speech_active else "      "
            extra = ""
            if hasattr(mic._vad, "threshold"):
                extra = f"  floor {mic._vad.floor:.4f}  gate {mic._vad.threshold:.4f}"
            print(f"\r  {flag} |{bar:<40}| {mic.level:.2f}{extra}", end="", flush=True)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()
    finally:
        mic.stop()


if __name__ == "__main__":
    main()
