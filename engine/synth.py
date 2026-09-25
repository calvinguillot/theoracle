"""theoracle — the soundscape: served, fed and opened by the engine.

The soundscape is a web page, `synth/index.html`, that makes its sound in
Chrome with WebAudio. The engine serves it on the port it already uses, tells
it the state of the piece the moment it changes, and opens it at startup, so
`python -m engine` is still the only thing anyone has to start.

**A synth page is a listener, like the dashboard.** It hears the state and a
handful of live numbers (mic level, speech, presence). It can write its own
settings file and nothing else. It never receives what a visitor said or what
the Oracle answered — the soundscape has no use for it, so it never has it.

**One page plays, never two.** An engine restart while Chrome is still open
would otherwise open a second window and play the room twice, slightly out of
step, which sounds like a fault rather than a choice. The launcher waits for
an old page to reconnect before opening a new one, and the bridge silences
any second page that connects anyway.

**Chrome is watched.** If the page's window closes or crashes during the
exhibition, it is reopened. A soundscape that dies at 14:00 on a Tuesday and
stays dead until someone notices is the failure this exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from engine.config import REPO

SYNTH_DIR = REPO / "synth"
PROFILE_DIR = REPO / "runtime" / "chrome-synth"

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
GRAIN_RE = re.compile(r"^/synth/grains/([A-Za-z0-9_-]{1,64})\.wav$")
MAX_SETTINGS_BYTES = 512 * 1024


# ── what the engine serves ────────────────────────────────────────────────
def grain_names() -> list[str]:
    folder = SYNTH_DIR / "grains"
    if not folder.is_dir():
        return []
    names = sorted(p.stem for p in folder.iterdir() if p.suffix.lower() == ".wav")
    return [n for n in names if NAME_RE.match(n)]


def _file(path: Path, content_type: str):
    try:
        return 200, content_type, path.read_bytes()
    except OSError:
        return 404, "text/plain; charset=utf-8", b"not found"


def route(path: str):
    """(status, content type, body) for a /synth path, or None if it is not one.

    Nothing from the request reaches the filesystem except a recording name
    that has passed NAME_RE.
    """
    path = path.split("?", 1)[0]
    if path in ("/synth", "/synth/", "/synth/index.html"):
        return _file(SYNTH_DIR / "index.html", "text/html; charset=utf-8")
    if path == "/synth/settings.json":
        return _file(SYNTH_DIR / "settings.json", "application/json")
    if path == "/synth/grains.json":
        return 200, "application/json", json.dumps(grain_names()).encode()
    m = GRAIN_RE.match(path)
    if m:
        return _file(SYNTH_DIR / "grains" / f"{m.group(1)}.wav", "audio/wav")
    return None


def save_settings(settings) -> str | None:
    """Write synth/settings.json from the page. None on success, else why not.

    Atomic, with one step of undo in settings.previous.json. The page checks
    every value on load, so this only has to make sure it is a settings object
    of a sane size.
    """
    if not isinstance(settings, dict) or not isinstance(settings.get("scenes"), dict):
        return "not a settings object"
    text = json.dumps(settings, indent=2, sort_keys=True, ensure_ascii=False)
    if len(text.encode()) > MAX_SETTINGS_BYTES:
        return "too large"
    path = SYNTH_DIR / "settings.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        SYNTH_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        if path.exists():
            os.replace(path, SYNTH_DIR / "settings.previous.json")
        os.replace(tmp, path)
    except OSError as e:
        return str(e)[:120]
    return None


# ── what the engine tells it ──────────────────────────────────────────────
async def feed(bridge, engine, hz: int = 15) -> None:
    """The live numbers the page can route: mic level, speech, presence, visit.

    Separate from the state, which arrives the moment it changes. These are
    continuous and it does not matter if one is late.
    """
    period = 1 / hz
    while True:
        if bridge.synths:
            mic = engine.mic
            await bridge.send_synths({"event": "synth_inputs", "inputs": {
                "mic_level": float(getattr(mic, "level", 0.0) or 0.0) if mic else 0.0,
                "speech_active": 1.0 if mic is not None and mic.speech_active else 0.0,
                "present": 1.0 if engine.present else 0.0,
                "visit": 1.0 if engine.visit is not None else 0.0,
            }})
        await asyncio.sleep(period)


# ── opening it ────────────────────────────────────────────────────────────
class Launcher:
    """Opens the page in its own Chrome window and keeps it open."""

    WAIT_FOR_OLD_PAGE_S = 7.0     # a page left open reconnects within 5 s
    CHECK_EVERY_S = 5.0
    MIN_RELAUNCH_GAP_S = 30.0

    def __init__(self, cfg, bridge, log=print):
        self.cfg, self.bridge, self.log = cfg, bridge, log
        s = cfg.section("synth")
        self.enabled = bool(s.get("enabled", True))
        self.output = str(s.get("output", "") or "")
        self.minimized = str(s.get("window", "normal")).lower() == "minimized"
        self.browser = self._find_browser(s.get("browser"))
        self.proc: subprocess.Popen | None = None
        self._launched_at = 0.0

    @staticmethod
    def _find_browser(configured) -> str | None:
        if configured:
            return str(configured) if Path(configured).exists() else None
        env = os.environ.get
        candidates = [
            Path(env("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(env("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(env("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            # Edge is Chromium and can pick an output device too; better than silence.
            Path(env("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        for c in candidates:
            if c.is_file():
                return str(c)
        for name in ("chrome", "google-chrome", "chromium", "msedge"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def url(self) -> str:
        q = []
        if self.output:
            q.append("sink=" + quote(self.output))
        if self.bridge.token:
            q.append("token=" + quote(self.bridge.token))
        return f"http://{self.bridge.host}:{self.bridge.port}/synth/" + ("?" + "&".join(q) if q else "")

    def start(self) -> None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        # Each flag is here because the soundscape failed without it:
        #   a separate profile — otherwise an open Chrome swallows every other flag
        #   autoplay — otherwise the page is silent until someone clicks it
        #   fake media UI — lets the page read device names to find the output
        #   the three background flags — otherwise Chrome slows a hidden window's timers
        args = [self.browser,
                f"--user-data-dir={PROFILE_DIR}",
                f"--app={self.url()}",
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble"]
        if self.minimized:
            args.append("--start-minimized")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, creationflags=flags)
        self._launched_at = time.time()
        self.log(f"soundscape: opened {Path(self.browser).name}"
                 + (f", output matching '{self.output}'" if self.output else ", system default output"))

    async def run(self) -> None:
        if not self.enabled:
            self.log("soundscape: off (synth.enabled is false)")
            return
        if not (SYNTH_DIR / "index.html").exists():
            self.log(f"soundscape: no page at {SYNTH_DIR / 'index.html'}", "warn")
            return
        if self.browser is None:
            self.log("soundscape: Chrome not found — set synth.browser in config.yaml, "
                     f"or open {self.url()} yourself", "warn")
            return

        await asyncio.sleep(self.WAIT_FOR_OLD_PAGE_S)
        if self.bridge.synths:
            self.log("soundscape: a page from before is still open — using it")
        else:
            self.start()

        # Keep it open. A page that is connected is fine however it got there.
        while True:
            await asyncio.sleep(self.CHECK_EVERY_S)
            if self.bridge.synths:
                continue
            gone = self.proc is None or self.proc.poll() is not None
            if gone and time.time() - self._launched_at > self.MIN_RELAUNCH_GAP_S:
                self.log("soundscape: the page is gone — opening it again", "warn")
                self.start()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
