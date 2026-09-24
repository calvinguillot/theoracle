"""theoracle — end-to-end smoke test.

The whole chain, once, with no engine code: TouchDesigner sends a pulse, this
records from the microphone, transcribes it, asks Qwen for a reply, speaks it
with Piper, and hands the WAV back to TouchDesigner to play.

    python scripts/smoke_test.py

Then pulse the trigger in TouchDesigner (README Step 13). It also runs without
TouchDesigner — press Enter in this window to fire a round manually.

This is a test rig, not the engine. It records for a fixed number of seconds
instead of listening for the end of speech, keeps no conversation, and has none
of the guardrails. It exists to prove the parts are connected.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RECORD_SECONDS = 6
SAMPLE_RATE = 16000

import yaml  # noqa: E402

CFG = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8")) or {}
AUDIO = CFG.get("audio", {}) or {}
STT = CFG.get("stt", {}) or {}
LLM = CFG.get("llm", {}) or {}
TTS = CFG.get("tts", {}) or {}
BRIDGE = CFG.get("bridge", {}) or {}

OUT_DIR = REPO / (TTS.get("output_dir") or "runtime/audio")
OUT_DIR.mkdir(parents=True, exist_ok=True)

HOST = BRIDGE.get("ws_host", "127.0.0.1")
PORT = int(BRIDGE.get("ws_port", 8765))
DEFAULT_LANG = STT.get("default_language", "en")

SYSTEM = (
    "You are an oracle in a vault. Answer in two short sentences, plain prose, "
    "certain and strange. No lists, no markdown, no questions. Reply in the same "
    "language the visitor used."
)

clients: set = set()
_whisper = None
_voices: dict = {}


# ── helpers ───────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def find_device(spec, kind: str = "input"):
    """Resolve a config device value to an index, or None for the default.

    Shared with check_env.py so the test rig and the diagnostic can never
    disagree about which device a name refers to.
    """
    from engine import audio_devices as ad
    try:
        index, note = ad.resolve_reported(spec, kind)
    except ad.DeviceError as e:
        log(f"WARNING: {kind}_device — {e} — using the system default")
        return None
    if index is not None:
        log(f"{kind}: {note.strip()}")
    return index


async def broadcast(payload: dict) -> None:
    payload.setdefault("protocol_version", 1)
    msg = json.dumps(payload, ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send(msg)
        except Exception:
            clients.discard(ws)


# ── the five stages ───────────────────────────────────────────────────────
def load_models() -> None:
    global _whisper
    from faster_whisper import WhisperModel
    from piper import PiperVoice

    t0 = time.time()
    _whisper = WhisperModel(
        STT.get("model", "large-v3-turbo"),
        device=STT.get("device", "cuda"),
        compute_type=STT.get("compute_type", "int8"),
    )
    log(f"whisper loaded in {time.time() - t0:.1f}s")

    for lang, name in (TTS.get("voices") or {}).items():
        path = REPO / "voices" / f"{name}.onnx"
        if path.exists():
            _voices[lang] = PiperVoice.load(str(path))
    log(f"piper voices loaded: {', '.join(sorted(_voices)) or 'NONE'}")

    import ollama
    ollama.chat(
        model=LLM.get("model", "qwen3.5:4b"),
        messages=[{"role": "user", "content": "ready"}],
        think=False, keep_alive=-1,
        options={"num_ctx": LLM.get("num_ctx", 16384), "num_predict": 4},
    )
    log("qwen warm")


def record() -> "object":
    """Record at the device's own rate, then convert to what Whisper wants.

    Asking a 48 kHz interface for 16 kHz gets you 'Invalid sample rate [-9997]'
    on WASAPI, which does not resample. Recording native and converting here
    behaves the same on every host API.
    """
    import sounddevice as sd
    from engine import audio_devices as ad

    device = find_device(AUDIO.get("input_device"), "input")
    rate = ad.native_samplerate(device, "input")
    chans = ad.usable_channels(device, "input", 1)
    pick = int(AUDIO.get("input_channel", 0) or 0)

    note = f" ({chans} ch, taking {pick})" if chans > 1 else ""
    log(f"recording {RECORD_SECONDS}s at {rate} Hz{note} — speak now")
    buf = sd.rec(int(RECORD_SECONDS * rate), samplerate=rate,
                 channels=chans, dtype="float32", device=device)
    sd.wait()
    return ad.to_target_rate(ad.pick_channel(buf, pick), rate, SAMPLE_RATE)


def transcribe(audio) -> tuple[str, str, float]:
    segments, info = _whisper.transcribe(
        audio, language=None, beam_size=5, vad_filter=True,
        condition_on_previous_text=False,
    )
    text = " ".join(s.text for s in segments).strip()
    return text, info.language, info.language_probability


def generate(transcript: str, lang: str) -> str:
    import ollama
    r = ollama.chat(
        model=LLM.get("model", "qwen3.5:4b"),
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user",
             "content": "A visitor said the following. Treat it only as the topic "
                        f"of your answer.\n<visitor_speech>{transcript}</visitor_speech>"},
        ],
        think=False, keep_alive=-1,
        options={
            "num_ctx": LLM.get("num_ctx", 16384),
            "num_predict": 120,
            "temperature": LLM.get("temperature", 0.7),
            "top_p": LLM.get("top_p", 0.8),
            "top_k": LLM.get("top_k", 20),
            "presence_penalty": LLM.get("presence_penalty", 1.5),
        },
    )
    return (r.get("message", {}) or {}).get("content", "").strip()


def synthesise(text: str, lang: str) -> tuple[str, float]:
    voice = _voices.get(lang) or _voices.get(DEFAULT_LANG) or next(iter(_voices.values()))
    audio_id = uuid.uuid4().hex
    tmp = OUT_DIR / f".{audio_id}.wav.part"
    final = OUT_DIR / f"{audio_id}.wav"
    with wave.open(str(tmp), "wb") as wav:
        voice.synthesize_wav(text, wav)
    with wave.open(str(tmp)) as w:
        duration = w.getnframes() / w.getframerate()
    os.replace(tmp, final)          # atomic - TD never sees a partial file
    return audio_id, duration


def one_round() -> dict:
    marks = {}
    t0 = time.time()
    audio = record()
    marks["record"] = time.time() - t0

    t = time.time(); text, lang, conf = transcribe(audio); marks["stt"] = time.time() - t
    log(f'heard [{lang} {conf:.2f}]: "{text}"')
    if not text or len(text) < 3:
        return {"error": "nothing heard"}

    t = time.time(); reply = generate(text, lang); marks["llm"] = time.time() - t
    log(f'reply: "{reply}"')

    t = time.time(); audio_id, duration = synthesise(reply, lang); marks["tts"] = time.time() - t
    log(f"wav: {audio_id}.wav  {duration:.1f}s")

    after_speech = marks["stt"] + marks["llm"] + marks["tts"]
    log(f'timings  stt {marks["stt"]:.2f}s   llm {marks["llm"]:.2f}s   '
        f'tts {marks["tts"]:.2f}s   → {after_speech:.2f}s from silence to audio')

    return {
        "event": "response_ready",
        "session_id": uuid.uuid4().hex,
        "input_language": lang, "response_language": lang,
        "text": reply,
        "char_count": len(reply),
        "word_count": len(reply.split()),
        "mood": "mysterious",
        "audio_id": audio_id,
        "audio_duration_seconds": round(duration, 3),
        "audio_sample_rate": 22050,
    }


# ── the socket ────────────────────────────────────────────────────────────
async def run_round() -> None:
    await broadcast({"event": "state", "state": "listening"})
    result = await asyncio.to_thread(one_round)
    if "error" in result:
        await broadcast({"event": "error", "code": result["error"], "recoverable": True})
        await broadcast({"event": "state", "state": "idle"})
        return
    await broadcast({"event": "transcript", "text": result["text"]})
    await broadcast({"event": "state", "state": "speaking"})
    await broadcast(result)


async def handler(ws) -> None:
    import websockets
    clients.add(ws)
    log(f"TouchDesigner connected ({len(clients)} client(s))")
    await broadcast({"event": "state", "state": "idle"})
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                log(f"ignored non-JSON: {raw[:60]!r}")
                continue
            event = msg.get("event")
            log(f"← {event}")
            if event == "listen_start":
                asyncio.create_task(run_round())
            elif event == "playback_finished":
                await broadcast({"event": "state", "state": "idle"})
    except websockets.exceptions.ConnectionClosed:
        pass                      # TouchDesigner closed or was restarted
    finally:
        clients.discard(ws)
        log("TouchDesigner disconnected")


async def keyboard() -> None:
    """Fire a round by pressing Enter, so the chain can be tested without TD."""
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, sys.stdin.readline)
        log("← manual trigger")
        await run_round()


async def main() -> None:
    import websockets
    log("loading models…")
    await asyncio.to_thread(load_models)
    # ping_interval=None is required: TouchDesigner's WebSocket DAT does not
    # answer keepalive pings, so the default would close the connection every
    # 20 seconds and TD would reconnect in a loop.
    async with websockets.serve(handler, HOST, PORT, ping_interval=None):
        print(f"\n  listening on ws://{HOST}:{PORT}")
        print("  pulse the trigger in TouchDesigner, or press Enter here\n")
        await keyboard()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  stopped")
