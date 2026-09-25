"""theoracle — the engine.

    python -m engine            # the piece
    python -m engine --keyboard # the same loop, typing instead of speaking
    python -m engine --no-synth # without opening the soundscape

Ten states, and the whole design is in which transitions do not exist.

    idle → listening → transcribing → generating → validating
         → synthesizing → ready → speaking → cooldown → idle

A trigger arriving in any state but `idle` is refused. That is the barge-in
decision from the design document, made in code: with one visitor, letting a
new trigger cancel the current answer feels responsive; with a crowd around a
stand it is chaos, and someone always ends up hearing half of a stranger's
prophecy. Refusing is boring and it is right.

A visit spans several turns. It ends when the Oracle says so (`needs: end`),
when the turn cap is reached, when three attempts to break it have been
refused, when TouchDesigner cancels, or when nobody speaks for
`max_silence_s`. Whichever way it ends, the raw transcript is deleted and only
a summary survives.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import corpus as corpus_mod      # noqa: E402
from engine import stt, tts                  # noqa: E402
from engine.audio_in import Microphone       # noqa: E402
from engine.bridge import Bridge, Stage, meter   # noqa: E402
from engine.config import REPO, load         # noqa: E402
from engine.corpus import Corpus             # noqa: E402
from engine.dashboard import Stats           # noqa: E402
from engine.guard import Limits, fallback    # noqa: E402
from engine.journal import Journal, sweep    # noqa: E402
from engine.llm import OracleError, Turn, Visit, warm   # noqa: E402
from engine.schema import Reply              # noqa: E402
from engine import websearch                 # noqa: E402
from engine import synth as soundscape        # noqa: E402


STATS = Stats()
_ENGINE = None


def log(msg: str = "", kind: str = "info") -> None:
    """One line, to the terminal and to every dashboard page.

    The terminal stays exactly as it was — this is the same text, mirrored, so
    there is nothing extra to read and no second format to keep in step.
    """
    print(f"  {msg}", flush=True)
    entry = STATS.note_log(msg, kind)
    if _ENGINE is not None:
        _ENGINE.push_log(entry)


class Engine:
    _last_reading = "other"
    playback: dict = None

    def __init__(self, cfg=None, keyboard: bool = False):
        self.cfg = cfg or load()
        self.keyboard = keyboard
        self.corpus = Corpus(self.cfg)
        self.bridge = Bridge(cfg=self.cfg, log=log)
        self.bridge.on_event = self.on_event
        self.bridge.on_monitor = self.send_snapshot
        self.stats = STATS
        self.search = websearch.Search(self.cfg)
        self.searches_this_visit = 0
        self.show_speech = bool(self.cfg.get("dashboard.show_speech", True))

        global _ENGINE
        _ENGINE = self
        self.limits = Limits.from_config(self.cfg)
        self.mic: Microphone | None = None

        self.visit: Visit | None = None
        self.journal: Journal | None = None
        self.language = self.cfg.get("stt.default_language", "fi")
        self.lang_state = stt.LanguageState.from_config(self.cfg)

        self.playback = {"state": "idle"}
        self.busy = False                       # a turn is in flight
        self.cancel = threading.Event()
        self._last_turn_at = 0.0

        # The sensor is a presence signal, not a doorbell: high means someone
        # is standing at the stand and the conversation continues, low means
        # they have gone. See on_event('presence').
        self.present = False
        self._left_at = 0.0
        self._watchdog: asyncio.Task | None = None
        self._playback: asyncio.Task | None = None

    # ── startup ───────────────────────────────────────────────────────────
    async def prepare(self) -> None:
        swept = sweep(self.cfg)
        if swept:
            log(f"removed {swept} session director{'y' if swept == 1 else 'ies'} "
                f"a previous run left behind")

        for problem in self.corpus.check():
            log(f"WARNING: {problem}")
        b = self.corpus.budget()
        log(f"corpus ~{b['always_on']} tokens always on, "
            f"~{b['for_conversation']} left for the visit")

        log("loading Whisper…")
        t0 = time.time()
        await asyncio.to_thread(stt.load_model, self.cfg)
        log(f"Whisper ready in {time.time() - t0:.1f}s")

        log("warming the model…")
        log(f"model ready in {await asyncio.to_thread(warm, self.cfg):.1f}s")

        voices = sum(1 for lang in self.cfg.section("tts").get("voices", {})
                     if tts.load_voice(lang, self.cfg) is not None)
        log(f"{voices} voice(s) available")

        # Say out loud which narrations will actually play. "It worked in
        # English" should not be something anyone has to discover in the room.
        for lang, states in tts.origin_report(self.cfg).items():
            broken = [x for x in states if "ok" not in x]
            log(f"origin {lang}: {', '.join(states)}", "warn" if broken else "info")

        log(f"web search: {'ready' if self.search.enabled else 'off — ' + self.search.reason}",
            "info" if self.search.enabled else "warn")

        if not self.keyboard:
            self.mic = Microphone(cfg=self.cfg)
            self.mic.start()
            log(self.mic.describe())

    # ── the dashboard ─────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        visit = self.visit
        self.stats.health = {
            "TouchDesigner": bool(self.bridge.clients),
            "soundscape": bool(self.bridge.synths),
            "microphone": self.mic.describe().split("  ")[-1] if self.mic else False,
            "model": self.cfg.get("llm.model", "?"),
            "voices": f"{len(self.cfg.section('tts').get('voices', {}))}",
        }
        return self.stats.snapshot(
            event="monitor",
            state=self.bridge.state,
            search=self.search.status(),
            playback=self.playback,
            origins=tts.origin_report(self.cfg),
            present=self.present,
            session=visit.session_id if visit else None,
            turn=visit.turn_count if visit else None,
            max_turns=int(self.cfg.get("conversation.max_turns", 8) or 8),
            show_speech=self.show_speech,
        )

    async def send_snapshot(self, ws=None) -> None:
        if not self.bridge.monitors:
            return
        payload = self.snapshot()
        if ws is not None:
            await self.bridge._send_to(ws, payload)
        else:
            await self.bridge.send(**{k: v for k, v in payload.items() if k != "event"},
                                   event="monitor")

    def push_log(self, entry: dict) -> None:
        """Called from log(), which is synchronous and often off the loop."""
        if not self.bridge.monitors:
            return
        try:
            asyncio.get_running_loop().create_task(self.send_snapshot())
        except RuntimeError:
            pass                       # logged before the loop started; the page
                                       # will pick it up in its first snapshot

    # ── the visit ─────────────────────────────────────────────────────────
    def _open_visit(self) -> Visit:
        if self.visit is not None and not self.visit.over:
            return self.visit
        self._close_visit()
        session = uuid.uuid4().hex
        self.visit = Visit(session_id=session, language=self.language, cfg=self.cfg,
                           corpus=self.corpus)
        self.journal = Journal(session, self.cfg)
        self.lang_state = stt.LanguageState.from_config(self.cfg)
        self.stats.visits += 1
        self.searches_this_visit = 0
        log(f"— visit {session[:8]} begins", "visit")
        return self.visit

    def _close_visit(self) -> None:
        if self.visit is None:
            return
        if self.journal is not None:
            self.journal.fate(self.visit.fate)
            kept = self.journal.close()
            log(f"— visit {self.visit.session_id[:8]} ends: "
                f"{kept['turns']} turn(s), {kept['seconds']}s, "
                f"languages {kept['languages']}", "visit")
            if self.bridge.monitors:
                asyncio.create_task(self.bridge.send("visit_end"))
        self.visit = None
        self.journal = None

    # ── events from TouchDesigner ─────────────────────────────────────────
    async def on_event(self, event: str, msg: dict) -> None:
        if event == "presence":
            await self.presence(bool(msg.get("present", True)))

        elif event == "listen_start":
            if self.busy:
                log(f"ignored: busy in '{self.bridge.state}'")
                return
            allowed, why = self.limits.may_start()
            if not allowed:
                log(f"ignored: {why}")
                return
            self.limits.note_start()
            asyncio.create_task(self.turn())

        elif event == "listen_stop":
            self.cancel.clear()
            log("listen_stop is not yet wired to the capture loop")

        elif event == "cancel":
            self.cancel.set()
            self._close_visit()
            await self.bridge.set_state("idle")

        elif event == "mute":
            self.cancel.set()
            self._close_visit()
            await self.bridge.set_state("idle")
            log("muted — the kill switch fired")

        elif event == "playback_started":
            # TouchDesigner confirming the audio really began. Shown on the
            # dashboard separately from "we handed it over", because the gap
            # between those two is exactly where the first real failure lived.
            self.playback = {**self.playback, "state": "playing",
                             "confirmed": True, "started": time.time()}

        elif event == "playback_finished":
            self.playback = {**self.playback, "state": "finished"}
            if self._playback is not None:
                self._playback.cancel()
                self._playback = None
            self.limits.note_playback_end()
            await self.bridge.set_state("cooldown")
            await asyncio.sleep(self.limits.cooldown_s)
            await self._settle()

        elif event == "playback_error":
            self.playback = {**self.playback, "state": "failed"}
            log(f"TouchDesigner could not play {msg.get('audio_id', '?')}", "error")
            await self._settle()

    def _watch_playback(self, duration: float) -> None:
        """Never wait on TouchDesigner for permission to carry on.

        `playback_finished` is the tidy path and TD sends it when the patch is
        wired correctly. But a missing callback, a Text DAT renamed, an Audio
        File In CHOP that failed to load — any of those leaves the engine in
        `speaking` for ever, which is what a visitor experiences as the piece
        dying after one answer. So the engine also knows how long the audio is,
        because it measured the WAV, and settles on its own if nothing arrives.
        """
        if self._playback is not None:
            self._playback.cancel()

        async def wait():
            await asyncio.sleep(duration + 2.0)
            if self.bridge.state == "speaking":
                log("no playback_finished from TouchDesigner — settling anyway")
                log("  check the WebSocket DAT's callbacks and AUDIO_DIR in ws_callbacks")
                self.limits.note_playback_end()
                await self._settle()

        self._playback = asyncio.create_task(wait())

    async def presence(self, present: bool) -> None:
        """The sensor, as the sensor actually behaves.

        An ultrasonic rangefinder does not produce an event when someone
        arrives; it produces a level that is high while they are there. So a
        rising edge opens a visit and a falling edge starts a departure clock,
        rather than each edge being a separate command.

        The clock matters. People lean away to think, turn to a friend, step
        back from a thing that just told them how they die. Ending the visit on
        the first frame the sensor drops would cut half of those conversations
        in two, so `conversation.end_on_sensor_clear_s` has to pass with the
        sensor still clear before anything is discarded.
        """
        if present == self.present:
            return
        self.present = present

        if present:
            self._left_at = 0.0
            log("— someone is here")
            allowed, why = self.limits.may_start()
            if not allowed:
                log(f"not starting: {why}")
                return
            if not self.busy and self.bridge.state == "idle":
                self.limits.note_start()
                asyncio.create_task(self.turn())
        else:
            self._left_at = time.time()
            grace = float(self.cfg.get("conversation.end_on_sensor_clear_s", 5) or 5)
            log(f"— the sensor is clear; ending the visit in {grace:.0f}s unless they return")

    async def _abandoned(self, stage: str) -> bool:
        """Has this turn been cancelled out from under us?

        Checked after every stage that can block. The design is explicit: a
        cancel invalidates the session immediately, and any transcription,
        generation or audio arriving afterwards is discarded rather than
        played. A visitor who walked away must not be followed down the
        corridor by their own prophecy.
        """
        if not self.cancel.is_set():
            return False
        log(f"cancelled during {stage} — discarding the result")
        await self.bridge.set_state("idle")
        return True

    def _arm_watchdog(self, duration: float) -> None:
        """Settle on our own if TouchDesigner never says the audio ended.

        It happened on the first real run: `playback_started` arrived, nothing
        followed, and the engine sat in `speaking` until it was killed. The
        duration is measured from the WAV, so we already know when the audio
        must be over — waiting to be told is a courtesy, not a dependency.
        Anything that waits forever on another process is a piece that stops
        mid-exhibition with nobody there to restart it.
        """
        self._disarm_watchdog()
        grace = float(self.cfg.get("bridge.playback_grace_s", 3.0) or 3.0)

        async def wait() -> None:
            try:
                await asyncio.sleep(duration + grace)
            except asyncio.CancelledError:
                return
            if self.bridge.state == "speaking":
                self.playback = {**self.playback, "state": "assumed_finished"}
                log(f"TouchDesigner never reported the end of playback "
                    f"(waited {duration + grace:.1f}s) — continuing anyway", "warn")
                self.limits.note_playback_end()
                await self._settle()

        self._watchdog = asyncio.create_task(wait())

    def _disarm_watchdog(self) -> None:
        if self._watchdog is not None and not self._watchdog.done():
            self._watchdog.cancel()
        self._watchdog = None

    async def _settle(self) -> None:
        """Back to idle, ending the visit if it has run its course.

        If the visitor is still standing there and the visit has turns left,
        listen again straight away — the sensor does not have to fire twice for
        a conversation to continue.
        """
        self._disarm_watchdog()
        if self.visit is not None and self.visit.over:
            self._close_visit()
        await self.bridge.set_state("idle")

        if self.present and not self.busy and self.visit is not None:
            await asyncio.sleep(0.1)
            asyncio.create_task(self.turn())

    # ── one turn ──────────────────────────────────────────────────────────
    async def turn(self, typed: str | None = None) -> None:
        self.busy = True
        self.cancel.clear()
        marks: dict = {}
        visit = self._open_visit()

        try:
            # ── listen ────────────────────────────────────────────────────
            await self.bridge.set_state("listening")
            with Stage(marks, "listen"):
                if typed is not None:
                    heard, capture = typed, None
                else:
                    capture = await asyncio.to_thread(self.mic.listen, self.cancel)
                    heard = None

            if await self._abandoned("listening"):
                return
            if capture is not None and not capture.usable:
                log(f"nothing to work with: {capture.reason}")
                if capture.reason == "no_speech":
                    await self.speak(Reply(text=fallback("unclear", self.language, self.cfg),
                                           mood="mysterious", needs="nothing"), marks)
                    return
                await self._settle()
                return

            # ── transcribe ────────────────────────────────────────────────
            if capture is not None:
                await self.bridge.set_state("transcribing")
                with Stage(marks, "stt"):
                    t = await asyncio.to_thread(stt.transcribe, capture.audio,
                                                self.lang_state, self.cfg)
                if await self._abandoned("transcription"):
                    return
                heard = t.text
                self.language = visit.language = t.language
                log(f'heard [{t.language} {t.confidence:.2f}] "{heard}"', "heard")
                if t.switched:
                    log(f"  {t.reason}")
                if not t.usable:
                    log(f"discarded: {t.rejected or 'nothing usable in it'}", "warn")
                    if self.journal:
                        self.journal.note("discarded", t.rejected)
                    await self.speak(Reply(text=fallback("unclear", self.language, self.cfg),
                                           mood="mysterious", needs="nothing"), marks)
                    return
                await self.bridge.send("transcript", session_id=visit.session_id,
                                       text=heard, language=t.language,
                                       confidence=round(t.confidence, 3))

            # ── the recorded origin story ─────────────────────────────────
            asks_origin = corpus_mod.wants_origin(heard)
            if asks_origin and not visit.origin_told:
                if await self.play_origin(visit, heard, marks):
                    return
                # No recording in their language: answer live from origin.md.

            reference = await self.look_up(visit, heard, marks)

            # ── generate ──────────────────────────────────────────────────
            await self.bridge.set_state("generating")
            with Stage(marks, "llm"):
                reply = await asyncio.to_thread(visit.ask, heard, asks_origin, reference)
            if asks_origin:
                visit.origin_told = True
            if await self._abandoned("generation"):
                return
            # The guard ran inside ask(); this is the state TD sees for it.
            await self.bridge.set_state("validating")
            log(f'reply [{reply.mood}/{reply.needs}] "{reply.text}"')
            if reply.repairs:
                log(f"  {'; '.join(reply.repairs)}")
                for category, why, _ in visit.rejected[-1:]:
                    self.journal.note("rejected", f"{category}: {why}")
            if visit.turns and visit.turns[-1].blocked:
                self.journal.note("blocked", visit.turns[-1].blocked)
                self.stats.blocked += 1
                log(f"blocked: {visit.turns[-1].blocked}", "warn")
            if visit.rejected:
                self.stats.rejected = len(visit.rejected)

            self._last_reading = reply.reading
            await self.speak(reply, marks)

        except OracleError as e:
            log(f"model failed: {e}", "error")
            if self.journal:
                self.journal.note("error", str(e))
            await self.speak(Reply(text=fallback("error", self.language, self.cfg),
                                   mood="tense", needs="nothing"), marks)
        except Exception as e:                       # noqa: BLE001
            log(f"unexpected {type(e).__name__}: {e}", "error")
            if self.journal:
                self.journal.note("error", f"{type(e).__name__}: {e}")
            await self.bridge.send("error", code="engine_failure", recoverable=True)
            await self._settle()
        finally:
            self.busy = False
            self._last_turn_at = time.time()

    async def look_up(self, visit: Visit, heard: str, marks: dict) -> str:
        """Whether this turn gets a `<reference>` block, and what is in it.

        Two things can put one there, and only two. A world topic the engine
        recognised — never the visitor's sentence, which is theirs and is not a
        query. Or a visitor who was offered a look at their own record last
        turn and said yes in this one.
        """
        if not self.search.enabled:
            return ""
        budget = int(self.cfg.get("websearch.max_per_visit", 2) or 2)
        if self.searches_this_visit >= budget:
            return ""

        waiting = visit.turns and visit.turns[-1].reply.needs == "consent"
        if waiting and visit.fate.name:
            answer = websearch.consented(heard)
            if answer is True:
                log(f"consented — looking for {visit.fate.name}", "search")
                with Stage(marks, "search"):
                    r = await self._search(self.search.about_self, visit.fate.name)
                self.searches_this_visit += 1
                await self.report_search(r)
                return r.text
            # Silence, a question, "I guess" — all of it is a no, and the
            # Oracle is told never to offer twice.
            log(f"not a clear yes — letting it go", "search")
            return ""

        topic = websearch.topic_of(heard, self.cfg)
        if not topic:
            return ""
        log(f"looking up the world: {topic}", "search")
        with Stage(marks, "search"):
            r = await self._search(self.search.world, topic, websearch.place_in(heard))
        self.searches_this_visit += 1
        await self.report_search(r)
        return r.text

    async def _search(self, fn, *args):
        """Under a hard timeout. An oracle does not pause to look things up."""
        limit = float(self.cfg.get("websearch.timeout_s", 3) or 3)
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=limit)
        except asyncio.TimeoutError:
            log(f"search gave up after {limit:.0f}s", "warn")
            self.search.failures += 1
            return websearch.Result(kind="world", query=" ".join(map(str, args)),
                                    error=f"timed out after {limit:.0f}s")

    async def report_search(self, r) -> None:
        if r.usable:
            log(f"found {len(r.sources)} source(s) in {r.elapsed:.1f}s", "search")
        else:
            log(f"nothing used: {r.error or 'no results'}", "warn")
        await self.send_snapshot()

    async def play_origin(self, visit: Visit, heard: str, marks: dict) -> bool:
        """Hand TouchDesigner the recorded narration. False if there is none."""
        await self.bridge.set_state("synthesizing")
        with Stage(marks, "tts"):
            speech = await asyncio.to_thread(tts.origin, self.language,
                                             visit.session_id, self.cfg)
        if await self._abandoned("the origin recording"):
            return True
        if speech is None:
            log(f"no origin recording for '{self.language}' — answering live")
            return False

        # The model must see this as something it has already said, or it will
        # tell the story again the moment they ask a follow-up.
        told = Reply(text="[the origin story, as recorded]", mood="calm", needs="nothing")
        visit.turns.append(Turn(visitor=heard, reply=told, elapsed=0.0))
        visit.origin_told = True

        self._last_reading = "before"
        self.playback = {"state": "playing", "source": "recording",
                         "file": speech.voice, "seconds": round(speech.duration, 1),
                         "language": self.language, "confirmed": False,
                         "started": time.time()}
        log(f"playing the recording: {speech.voice} ({speech.duration:.0f}s)")
        await self.bridge.set_state("ready")
        await self.bridge.send(
            "response_ready", session_id=visit.session_id,
            input_language=self.language, response_language=self.language,
            text="", char_count=0, word_count=0, mood="calm",
            reading="before", prerendered=True,
            audio_id=speech.audio_id,
            audio_duration_seconds=round(speech.duration, 3),
            audio_sample_rate=speech.sample_rate)
        await self.bridge.set_state("speaking")
        self._watch_playback(speech.duration)
        self._report(marks, speech.duration)
        return True

    async def speak(self, reply: Reply, marks: dict) -> None:
        visit = self.visit
        if await self._abandoned("synthesis"):
            return
        await self.bridge.set_state("synthesizing")
        try:
            with Stage(marks, "tts"):
                speech = await asyncio.to_thread(tts.synthesize, reply.text,
                                                 self.language, self.cfg)
        except Exception as e:                       # noqa: BLE001
            log(f"synthesis failed: {e}", "error")
            await self.bridge.send("error", code="tts_failed", recoverable=True)
            await self._settle()
            return

        # Last gate. Everything after this line is audible in the room.
        if await self._abandoned("playback"):
            return
        if visit is not None and self.journal is not None:
            self.journal.turn(visit.turns[-1].visitor if visit.turns else "",
                              reply, self.language, marks)

        self.playback = {"state": "playing", "source": "piper",
                         "file": speech.audio_id[:8] + ".wav", "voice": speech.voice,
                         "seconds": round(speech.duration, 1),
                         "language": speech.language, "confirmed": False,
                         "started": time.time()}
        await self.bridge.set_state("ready")
        await self.bridge.send(
            "response_ready",
            session_id=visit.session_id if visit else "",
            input_language=self.language, response_language=speech.language,
            **reply.as_event(),
            audio_id=speech.audio_id,
            audio_duration_seconds=round(speech.duration, 3),
            audio_sample_rate=speech.sample_rate)
        await self.bridge.set_state("speaking")
        self._watch_playback(speech.duration)
        self._report(marks, speech.duration)

        # Without TouchDesigner there is nobody to tell us the audio ended.
        if not self.bridge.clients:
            await asyncio.sleep(0.05)
            self.limits.note_playback_end()
            await self._settle()

    def _report(self, marks: dict, duration: float) -> None:
        """Per-stage latency, on screen and on disk.

        `after_speech` is the number that matters: everything between the
        visitor falling silent and audio being ready. The listening time is
        theirs, not the machine's.
        """
        after = sum(marks.get(k, 0) for k in ("stt", "llm", "tts"))
        self.stats.note_turn(self.language, self._last_reading, marks)
        log("timings  " + "  ".join(f"{k} {v:.2f}s" for k, v in marks.items())
            + f"  → {after:.2f}s from silence to audio, {duration:.1f}s of speech")
        try:
            path = REPO / "runtime" / "latency.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.time(), **marks,
                                    "after_speech": round(after, 3),
                                    "audio_seconds": round(duration, 3)}) + "\n")
        except OSError:
            pass

    # ── idle ──────────────────────────────────────────────────────────────
    async def watch_for_departure(self) -> None:
        """Two ways a visit ends without anyone saying so.

        The sensor going clear and staying clear is the real one. The silence
        timer is the backstop for a sensor that is stuck high — someone's coat
        on the stand, a reflection off a pillar — which otherwise holds a visit
        open forever and lets the next person inherit it.
        """
        gap = float(self.cfg.get("conversation.max_silence_s", 12) or 12)
        grace = float(self.cfg.get("conversation.end_on_sensor_clear_s", 5) or 5)
        while True:
            await asyncio.sleep(0.5)
            now = time.time()

            if (self._left_at and not self.present
                    and now - self._left_at > grace and self.visit is not None):
                log("— they left")
                self._left_at = 0.0
                if self.busy:
                    self.cancel.set()
                self._close_visit()
                await self.bridge.set_state("idle")
                continue

            if (self.visit is not None and not self.busy and not self.present
                    and self._last_turn_at and now - self._last_turn_at > gap
                    and self.bridge.state in ("idle", "cooldown")):
                log("— nobody spoke; the visit lapsed")
                self._close_visit()

    async def keyboard_loop(self) -> None:
        loop = asyncio.get_running_loop()
        log("type as the visitor; Ctrl-C to stop\n")
        while True:
            line = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not line:
                continue
            if self.busy:
                log("ignored: still working on the last one")
                continue
            await self.turn(typed=line)


async def run(keyboard: bool = False, synth: bool = True) -> None:
    cfg = load()
    engine = Engine(cfg, keyboard=keyboard)
    await engine.prepare()
    launcher = soundscape.Launcher(cfg, engine.bridge, log) if synth else None

    async with engine.bridge.serve():
        await engine.bridge.set_state("idle")
        tasks = [asyncio.create_task(engine.watch_for_departure()),
                 asyncio.create_task(_tick(engine)),
                 asyncio.create_task(soundscape.feed(engine.bridge, engine))]
        if launcher is not None:
            tasks.append(asyncio.create_task(launcher.run()))
        if engine.mic is not None:
            tasks.append(asyncio.create_task(meter(engine.bridge, engine.mic)))
        if keyboard:
            tasks.append(asyncio.create_task(engine.keyboard_loop()))
        else:
            log("waiting for the sensor — or press Enter here to trigger a turn\n")
            tasks.append(asyncio.create_task(_enter_triggers(engine)))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            engine._close_visit()
            if engine.mic is not None:
                engine.mic.stop()
            if launcher is not None:
                launcher.stop()


async def _tick(engine: Engine) -> None:
    """Refresh the dashboard once a second so uptime and health stay true."""
    while True:
        await asyncio.sleep(1.0)
        await engine.send_snapshot()


async def _enter_triggers(engine: Engine) -> None:
    """Enter does what the sensor does, so the loop can be run without hardware."""
    loop = asyncio.get_running_loop()
    while True:
        await loop.run_in_executor(None, sys.stdin.readline)
        await engine.on_event("listen_start", {})


def main() -> None:
    try:
        asyncio.run(run(keyboard="--keyboard" in sys.argv,
                        synth="--no-synth" not in sys.argv))
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
