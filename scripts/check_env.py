"""theoracle — environment diagnostic.

Run it whenever something behaves oddly. It checks each piece in turn and keeps
going when one fails, so you get the whole picture rather than the first error.

    python scripts/check_env.py                 # everything except web search
    python scripts/check_env.py --web           # also spend one web search
    python scripts/check_env.py --skip-whisper  # faster; skips the GPU model load

The row that matters most is FREE VRAM AFTER — run this with your real
TouchDesigner scene open, and check what is left for it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OK, WARN, BAD = "  ok  ", " warn ", " FAIL "
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def free_vram_mb() -> int | None:
    """Free VRAM via nvidia-smi. None if unavailable."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return int(out.stdout.strip().splitlines()[0])
    except Exception:
        pass
    return None


def load_config() -> dict:
    try:
        import yaml
        path = REPO / "config.yaml"
        if not path.exists():
            record(WARN, "config.yaml", "not found — using defaults for the checks below")
            return {}
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        record(OK, "config.yaml", f"{len(cfg)} sections")
        return cfg
    except Exception as e:
        record(BAD, "config.yaml", f"{type(e).__name__}: {e}")
        return {}


# ──────────────────────────────────────────────────────────────────────────
def check_python() -> None:
    section("Python")
    v = sys.version.split()[0]
    prefix = Path(sys.prefix)
    record(OK, "version", v)
    in_venv = ".venv" in prefix.parts
    record(OK if in_venv else BAD, "environment", str(prefix))
    if not in_venv:
        print("       Not running inside .venv — activate it first:")
        print("       source .venv/Scripts/activate   (Git Bash)")
    if not v.startswith("3.11"):
        print("       Note: this must match TouchDesigner's Python version.")


def check_gpu(cfg: dict) -> int | None:
    section("GPU")
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,temperature.gpu",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            record(BAD, "nvidia-smi", "failed — is the NVIDIA driver installed?")
            return None
        name, total, free, temp = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]
        record(OK, "device", name)
        record(OK, "memory", f"{free} free of {total}   (temp {temp})")
    except FileNotFoundError:
        record(BAD, "nvidia-smi", "not found — no NVIDIA driver on PATH")
        return None
    except Exception as e:
        record(BAD, "nvidia-smi", f"{type(e).__name__}: {e}")
        return None

    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        record(OK if n else BAD, "ctranslate2 CUDA", f"{n} device(s) visible")
    except Exception as e:
        record(BAD, "ctranslate2", f"{type(e).__name__}: {e}")
    return free_vram_mb()


def check_whisper(cfg: dict) -> None:
    section("Speech recognition")
    stt = cfg.get("stt", {})
    model = stt.get("model", "large-v3-turbo")
    device = stt.get("device", "cuda")
    ctype = stt.get("compute_type", "int8")
    before = free_vram_mb()
    try:
        from faster_whisper import WhisperModel
        t0 = time.time()
        WhisperModel(model, device=device, compute_type=ctype)
        took = time.time() - t0
        after = free_vram_mb()
        used = f", used ~{before - after} MB" if (before and after) else ""
        record(OK, f"{model} [{device}/{ctype}]", f"loaded in {took:.1f}s{used}")
    except Exception as e:
        record(BAD, "whisper load", f"{type(e).__name__}: {e}")
        msg = str(e).lower()
        if "cudnn" in msg or "cublas" in msg:
            print("       CUDA libraries not on PATH — see Step 8 in the README.")


def check_ollama(cfg: dict) -> None:
    section("Language model")
    llm = cfg.get("llm", {})
    model = llm.get("model", "qwen3.5:4b")
    num_ctx = llm.get("num_ctx", 16384)
    try:
        import ollama
    except Exception as e:
        record(BAD, "ollama package", f"{type(e).__name__}: {e}")
        return

    try:
        tags = ollama.list()
        names = [m.get("model") or m.get("name") for m in tags.get("models", [])]
        record(OK, "server", f"{len(names)} model(s) installed")
        record(OK if model in names else BAD, f"model {model}",
               "present" if model in names else f"NOT installed — run: ollama pull {model}")
        if model not in names:
            return
    except Exception as e:
        record(BAD, "server", f"{type(e).__name__}: {e} — is Ollama running?")
        return

    try:
        t0 = time.time()
        r = ollama.chat(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: ready"}],
            think=False,
            keep_alive=-1,
            options={"num_ctx": num_ctx, "num_predict": 24, "temperature": 0.0},
        )
        took = time.time() - t0
        text = (r.get("message", {}) or {}).get("content", "")
        n = r.get("eval_count") or 0
        dur = (r.get("eval_duration") or 0) / 1e9
        rate = f", {n / dur:.0f} tok/s" if dur else ""
        record(OK, f"generation (num_ctx {num_ctx})", f"{took:.1f}s{rate}")

        thinking = (r.get("message", {}) or {}).get("thinking")
        if thinking or "<think" in text.lower():
            record(BAD, "thinking mode", "reasoning came back — think=False is not taking effect")
        else:
            record(OK, "thinking mode", "off")
    except Exception as e:
        record(BAD, "generation", f"{type(e).__name__}: {e}")


def check_websearch(enabled: bool) -> None:
    section("Web search")
    try:
        from dotenv import load_dotenv  # must come before importing ollama
        load_dotenv(REPO / ".env")
    except Exception as e:
        record(BAD, "python-dotenv", f"{type(e).__name__}: {e}")
        return

    import os
    if not os.getenv("OLLAMA_API_KEY"):
        record(BAD, "OLLAMA_API_KEY", "not set — check .env at the repository root")
        return
    record(OK, "OLLAMA_API_KEY", "present")

    if not enabled:
        record(WARN, "live query", "skipped — pass --web to spend one search")
        return
    try:
        import ollama
        t0 = time.time()
        res = ollama.web_search("Helsinki weather today")
        record(OK, "live query", f"{len(res.results)} results in {time.time() - t0:.1f}s")
    except Exception as e:
        record(BAD, "live query", f"{type(e).__name__}: {e}")
        print("       If this is an auth error, check load_dotenv() runs BEFORE importing ollama.")


def check_voices(cfg: dict) -> None:
    section("Voices")
    vdir = REPO / "voices"
    if not vdir.exists():
        record(BAD, "voices/", "missing")
        return
    wanted = (cfg.get("tts", {}) or {}).get("voices", {}) or {}
    found = sorted(p.stem for p in vdir.glob("*.onnx"))
    record(OK, "installed", f"{len(found)} voice(s)")
    for lang, name in sorted(wanted.items()):
        onnx, meta = vdir / f"{name}.onnx", vdir / f"{name}.onnx.json"
        if onnx.exists() and meta.exists():
            try:
                sr = json.loads(meta.read_text(encoding="utf-8"))["audio"]["sample_rate"]
            except Exception:
                sr = "?"
            record(OK, f"  {lang}", f"{name} @ {sr} Hz")
        else:
            missing = "model" if not onnx.exists() else "config"
            record(BAD, f"  {lang}", f"{name} — {missing} file missing")
    for extra in found:
        if extra not in wanted.values():
            record(WARN, "  unused", f"{extra} is installed but not in config tts.voices")

    allowed = set((cfg.get("stt", {}) or {}).get("allowed_languages", []))
    if allowed and allowed != set(wanted):
        record(BAD, "language sets",
               f"allowed_languages {sorted(allowed)} != tts.voices {sorted(wanted)}")
    elif allowed:
        record(OK, "language sets", "allowed_languages matches tts.voices")


def check_prerendered(cfg: dict) -> None:
    section("Origin recordings")
    pre = cfg.get("prerendered", {}) or {}
    pdir = REPO / (pre.get("dir") or "voices/prerendered")
    if not pdir.exists():
        record(WARN, str(pdir.relative_to(REPO)), "missing — the Oracle will answer live instead")
        return
    for lang, files in (pre.get("origin", {}) or {}).items():
        for fname in files:
            f = pdir / fname
            if not f.exists():
                record(BAD, f"  {lang}", f"{fname} missing")
                continue
            try:
                with wave.open(str(f)) as w:
                    secs = w.getnframes() / w.getframerate()
                record(OK, f"  {lang}", f"{fname} — {secs:.0f}s @ {w.getframerate()} Hz")
            except Exception as e:
                record(BAD, f"  {lang}", f"{fname} — unreadable: {e}")


def check_audio(cfg: dict) -> None:
    section("Audio devices")
    try:
        import sounddevice as sd
    except Exception as e:
        record(BAD, "sounddevice", f"{type(e).__name__}: {e}")
        return
    try:
        devices = sd.query_devices()
    except Exception as e:
        record(BAD, "query_devices", f"{type(e).__name__}: {e}")
        return

    print("       Copy a distinctive fragment of a name into config.yaml audio.*_device\n")
    for i, d in enumerate(devices):
        kind = []
        if d["max_input_channels"]:
            kind.append(f"in:{d['max_input_channels']}")
        if d["max_output_channels"]:
            kind.append(f"out:{d['max_output_channels']}")
        print(f"       {i:3d}  {d['name'][:52]:52s} {','.join(kind)}")

    audio = cfg.get("audio", {}) or {}
    names = [d["name"] for d in devices]
    for key in ("input_device", "output_device"):
        want = audio.get(key)
        if not want:
            continue
        hits = [n for n in names if want.lower() in n.lower()]
        if len(hits) == 1:
            record(OK, key, f'"{want}" → {hits[0]}')
        elif not hits:
            record(BAD, key, f'"{want}" matches nothing above')
        else:
            record(BAD, key, f'"{want}" matches {len(hits)} devices — make it more specific')


def check_stories() -> None:
    section("Story material")
    approx = 0
    for name in ("instructions.md", "world.md"):
        f = REPO / "stories" / name
        if not f.exists():
            record(BAD, name, "missing")
            continue
        words = len(f.read_text(encoding="utf-8").split())
        approx += int(words * 1.35)
        record(OK, name, f"~{int(words * 1.35)} tokens")
    if approx:
        status = OK if approx < 9000 else WARN
        record(status, "always-on prompt", f"~{approx} tokens (num_ctx must be well above this)")
    for name in ("origin.md", "origin_piper.md"):
        f = REPO / "stories" / name
        record(OK if f.exists() else WARN, name, "present" if f.exists() else "missing")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", action="store_true", help="also run one live web search")
    ap.add_argument("--skip-whisper", action="store_true", help="skip the GPU model load")
    args = ap.parse_args()

    print(f"theoracle environment check — {REPO}")
    cfg = load_config()
    check_python()
    before = check_gpu(cfg)
    if not args.skip_whisper:
        check_whisper(cfg)
    check_ollama(cfg)
    check_websearch(args.web)
    check_voices(cfg)
    check_prerendered(cfg)
    check_audio(cfg)
    check_stories()

    section("Summary")
    after = free_vram_mb()
    if before is not None and after is not None:
        print(f"       FREE VRAM BEFORE loading: {before} MB")
        print(f"       FREE VRAM AFTER  loading: {after} MB")
        print("       Run this with TouchDesigner open — AFTER is what is left for your scene.")
    bad = [r for r in results if r[0] == BAD]
    warn = [r for r in results if r[0] == WARN]
    print(f"\n       {len(results) - len(bad) - len(warn)} ok, {len(warn)} warnings, {len(bad)} failures")
    for _, name, detail in bad:
        print(f"       FAIL  {name} — {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
