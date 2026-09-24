"""theoracle — reading and writing WAV files the standard library refuses.

Python's `wave` module handles exactly one encoding, integer PCM, and raises
`unknown format: 3` on anything else. Format 3 is IEEE float — what Audacity,
ffmpeg and most TTS exports produce when you ask for 32-bit. The file is
perfectly good; `wave` is just narrow.

This reads both, and writes the one thing the piece should contain: 16-bit PCM
at the voice sample rate, so every WAV in the repository — Piper's output and
the pre-rendered narrations alike — is byte-for-byte the same kind of file.

    python scripts/wav_io.py voices/prerendered/*.wav        # report
    python scripts/wav_io.py --convert voices/prerendered    # rewrite in place
"""

from __future__ import annotations

import struct
import sys
import wave
from pathlib import Path

FORMATS = {1: "PCM", 3: "IEEE float", 6: "A-law", 7: "mu-law", 0xFFFE: "extensible"}


def probe(path) -> dict:
    """Header facts, without decoding the audio: format, rate, channels, bits."""
    with open(path, "rb") as f:
        riff, _, wave_id = struct.unpack("<4sI4s", f.read(12))
        if riff != b"RIFF" or wave_id != b"WAVE":
            raise ValueError("not a RIFF/WAVE file")
        info = {}
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            cid, size = struct.unpack("<4sI", head)
            body = f.read(size)
            if size % 2:                     # RIFF chunks are word-aligned
                f.read(1)
            if cid == b"fmt ":
                tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]
                info.update(format=tag, channels=ch, rate=rate, bits=bits)
            elif cid == b"data":
                info["data_bytes"] = size
        if "format" not in info:
            raise ValueError("no fmt chunk")
        frame = max(1, info["channels"] * info["bits"] // 8)
        info["seconds"] = info.get("data_bytes", 0) / frame / info["rate"]
        info["format_name"] = FORMATS.get(info["format"], f"unknown ({info['format']})")
        return info


def read(path):
    """(mono float32 samples, sample rate). Handles PCM 8/16/24/32 and float."""
    import numpy as np
    meta = probe(path)

    with open(path, "rb") as f:
        f.seek(12)
        raw = b""
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            cid, size = struct.unpack("<4sI", head)
            if cid == b"data":
                raw = f.read(size)
                break
            f.seek(size + (size % 2), 1)

    bits, fmt = meta["bits"], meta["format"]
    if fmt == 3:
        data = np.frombuffer(raw, dtype="<f4" if bits == 32 else "<f8").astype(np.float32)
    elif bits == 16:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif bits == 32:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif bits == 8:
        data = (np.frombuffer(raw, dtype="u1").astype(np.float32) - 128) / 128.0
    elif bits == 24:
        b = np.frombuffer(raw[: len(raw) // 3 * 3], dtype="u1").reshape(-1, 3)
        packed = (b[:, 0].astype(np.int32)
                  | b[:, 1].astype(np.int32) << 8
                  | b[:, 2].astype(np.int32) << 16)
        packed = np.where(packed & 0x800000, packed - 0x1000000, packed)
        data = packed.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"cannot decode {meta['format_name']} at {bits} bits")

    ch = meta["channels"]
    if ch > 1:
        data = data[: len(data) // ch * ch].reshape(-1, ch).mean(axis=1)
    return data, meta["rate"]


def write_pcm16(path, samples, rate: int) -> None:
    """Write mono 16-bit PCM — the one format everything here agrees on."""
    import numpy as np
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes((clipped * 32767).astype("<i2").tobytes())


def convert(path, rate: int | None = None) -> str:
    """Rewrite a file as mono 16-bit PCM, optionally at a new rate."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import audio_devices as ad

    samples, sr = read(path)
    if rate and rate != sr:
        samples = ad.to_target_rate(samples, sr, rate)
        sr = rate
    tmp = Path(path).with_suffix(".converting")
    write_pcm16(tmp, samples, sr)
    tmp.replace(path)
    return f"{len(samples) / sr:.0f}s @ {sr} Hz, mono 16-bit PCM"


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--convert"]
    doing = "--convert" in sys.argv
    rate = None

    paths: list[Path] = []
    for a in args:
        p = Path(a)
        paths.extend(sorted(p.glob("*.wav")) if p.is_dir() else [p])
    if not paths:
        print(__doc__)
        return

    if doing:
        try:
            import yaml
            cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                                  / "config.yaml").read_text(encoding="utf-8")) or {}
            rate = (cfg.get("tts", {}) or {}).get("sample_rate", 22050)
        except Exception:
            rate = 22050

    for p in paths:
        try:
            meta = probe(p)
            line = (f"{meta['format_name']}, {meta['bits']}-bit, "
                    f"{meta['channels']} ch, {meta['rate']} Hz, {meta['seconds']:.0f}s")
            if doing:
                print(f"  {p.name}\n      was: {line}\n      now: {convert(p, rate)}")
            else:
                print(f"  {p.name:28s} {line}")
        except Exception as e:
            print(f"  {p.name:28s} FAILED: {e}")


if __name__ == "__main__":
    main()
