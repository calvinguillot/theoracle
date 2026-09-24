"""theoracle — what the engine and TouchDesigner say to each other.

WebSocket carries the session: commands in, states and the finished response
out. OSC carries the three numbers that need to arrive every frame and do not
matter if one is lost — the level meter, the speech flag, and the state as an
integer for CHOP maths.

Three details here were learned the hard way and should not be tidied away.

`ping_interval=None`. TouchDesigner's WebSocket DAT does not answer keepalive
pings, so the library's default closes the connection every twenty seconds and
TD reconnects in a loop that looks exactly like a network fault.

`127.0.0.1`, never `0.0.0.0`. This socket opens a microphone in a public room.
A gallery's wifi is not somewhere to expose that, and the difference is one
string.

**A state snapshot on connect, never a replay.** TouchDesigner restarts more
often than the engine does. When it comes back it needs to know where things
stand, not to receive the commands it missed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import dashboard           # noqa: E402
from engine.config import load          # noqa: E402

PROTOCOL_VERSION = 1

STATES = ("idle", "listening", "transcribing", "generating", "validating",
          "synthesizing", "ready", "speaking", "cooldown", "error")

# Anything else is rejected rather than ignored, so a typo in the TD patch
# shows up as a message in the textport instead of as silence.
FROM_TD = ("hello", "presence", "listen_start", "listen_stop", "cancel",
           "playback_started", "playback_finished", "playback_error", "mute")

MAX_PAYLOAD = 4096          # nothing TD sends is near this


@dataclass
class Bridge:
    cfg: object = None
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = ""
    state: str = "idle"
    clients: set = field(default_factory=set)     # TouchDesigner
    monitors: set = field(default_factory=set)    # dashboard pages
    _authed: set = field(default_factory=set)
    _osc: object = None
    on_event: object = None            # async callable(event: str, msg: dict)
    on_monitor: object = None          # async callable(ws) — send a page its first snapshot
    log: object = print

    def __post_init__(self):
        self.cfg = self.cfg or load()
        b = self.cfg.section("bridge")
        self.host = b.get("ws_host", "127.0.0.1")
        self.port = int(b.get("ws_port", 8765) or 8765)
        raw_token = str(b.get("ws_token", "") or "")
        # "change-me" is the shipped placeholder. Treating it as a real secret
        # would lock out a TD patch that has never been told about tokens, and
        # pretending it is one would be worse than saying so out loud.
        self.token = "" if raw_token in ("", "change-me") else raw_token

        try:
            from pythonosc.udp_client import SimpleUDPClient
            self._osc = SimpleUDPClient(self.host, int(b.get("osc_td_port", 9001) or 9001))
        except Exception as e:
            self.log(f"  OSC unavailable ({type(e).__name__}); the visuals lose mic_level")

    # ── outgoing ──────────────────────────────────────────────────────────
    async def send(self, event: str, **fields) -> None:
        payload = {"protocol_version": PROTOCOL_VERSION, "event": event}
        payload.update(fields)
        message = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.clients) + list(self.monitors):
            try:
                await ws.send(message)
            except Exception:
                self.clients.discard(ws)
                self.monitors.discard(ws)
                self._authed.discard(ws)

    async def set_state(self, state: str, **fields) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state '{state}'")
        self.state = state
        self.osc("state_index", STATES.index(state))
        await self.send("state", state=state, **fields)

    def osc(self, channel: str, value) -> None:
        """Fire and forget. A dropped meter frame is not worth an exception."""
        if self._osc is None:
            return
        try:
            self._osc.send_message(f"/{channel}", float(value))
        except Exception:
            pass

    # ── incoming ──────────────────────────────────────────────────────────
    async def _handler(self, ws) -> None:
        import websockets
        self.clients.add(ws)
        self.log(f"  a client connected ({len(self.clients)} waiting to identify)")
        if not self.token:
            self._authed.add(ws)
        await ws.send(json.dumps({"protocol_version": PROTOCOL_VERSION,
                                  "event": "state", "state": self.state}))
        try:
            async for raw in ws:
                if len(raw) > MAX_PAYLOAD:
                    await self._reject(ws, "oversized", f"{len(raw)} bytes")
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    await self._reject(ws, "not_json", str(raw)[:60])
                    continue
                if not isinstance(msg, dict):
                    await self._reject(ws, "not_an_object", "")
                    continue

                event = msg.get("event", "")

                if self.token and ws not in self._authed:
                    if event == "hello" and msg.get("token") == self.token:
                        self._authed.add(ws)
                        await ws.send(json.dumps(
                            {"protocol_version": PROTOCOL_VERSION, "event": "ack",
                             "of": "hello"}))
                    else:
                        await self._reject(ws, "unauthenticated", event)
                    continue

                if event == "hello":
                    # A dashboard page, not TouchDesigner. It listens and is
                    # never allowed to drive the piece: a browser tab left open
                    # in a back room must not be able to open a microphone.
                    if msg.get("role") == "monitor":
                        self.clients.discard(ws)
                        self.monitors.add(ws)
                        self.log(f"  dashboard connected ({len(self.monitors)} open)")
                        await self._send_to(ws, {"event": "state", "state": self.state})
                        if self.on_monitor is not None:
                            await self.on_monitor(ws)
                    continue

                if ws in self.monitors:
                    await self._reject(ws, "monitors_are_read_only", event)
                    continue
                if event not in FROM_TD:
                    await self._reject(ws, "unknown_event", event)
                    continue

                if ws not in self.clients:
                    self.clients.add(ws)
                    self.log("  TouchDesigner connected")
                self.log(f"  ← {event}")
                if self.on_event is not None:
                    await self.on_event(event, msg)
        except websockets.exceptions.ConnectionClosed:
            pass                       # TouchDesigner closed or was restarted
        finally:
            was_td = ws in self.clients
            self.clients.discard(ws)
            self.monitors.discard(ws)
            self._authed.discard(ws)
            self.log("  TouchDesigner disconnected" if was_td else "  dashboard closed")

    async def _send_to(self, ws, payload: dict) -> None:
        payload.setdefault("protocol_version", PROTOCOL_VERSION)
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:
            self.clients.discard(ws)
            self.monitors.discard(ws)

    async def _reject(self, ws, code: str, detail: str) -> None:
        self.log(f"  rejected: {code} {detail}")
        try:
            await ws.send(json.dumps({"protocol_version": PROTOCOL_VERSION,
                                      "event": "rejected", "code": code,
                                      "detail": detail[:120]}))
        except Exception:
            pass

    # ── running ───────────────────────────────────────────────────────────
    def serve(self):
        """An async context manager holding the server open."""
        import websockets
        if self.token:
            self.log(f"  websocket on ws://{self.host}:{self.port} (token required)")
        else:
            self.log(f"  websocket on ws://{self.host}:{self.port} "
                     f"(no token — set bridge.ws_token to require one)")
        self.log(f"  dashboard at http://{self.host}:{self.port}")
        # ping_interval=None: TD does not answer pings. See the module docstring.
        # process_request turns a plain GET on the same port into the dashboard,
        # so there is one port to remember and one thing to start.
        return websockets.serve(self._handler, self.host, self.port,
                                ping_interval=None,
                                process_request=self._http)

    async def _http(self, *args):
        """Serve the dashboard to a browser; let WebSocket upgrades through.

        Two signatures, because the `websockets` API changed and which one you
        get depends on the version in the venv: newer builds pass
        (connection, request), older ones (path, request_headers). Guessing
        wrong means a browser either sees nothing or sees the page source.

        `connection.respond()` is not used: it labels the body text/plain, and
        the browser then shows the HTML as text. The Content-Type has to be set
        here.
        """
        try:
            if len(args) == 2 and hasattr(args[1], "get"):        # legacy
                _path, headers_in = args
                if headers_in.get("Upgrade", "").lower() == "websocket":
                    return None
                import http
                body = dashboard.page_bytes()
                return (http.HTTPStatus.OK,
                        [("Content-Type", "text/html; charset=utf-8"),
                         ("Content-Length", str(len(body))),
                         ("Cache-Control", "no-store")],
                        body)

            connection, request = args[0], args[1]
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return None
            from websockets.datastructures import Headers
            from websockets.http11 import Response
            body = dashboard.page_bytes()
            return Response(200, "OK", Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ]), body)
        except Exception as e:                                    # noqa: BLE001
            self.log(f"  dashboard could not be served: {type(e).__name__}: {e}")
            return None


async def meter(bridge: Bridge, mic, hz: int = 30) -> None:
    """Push mic_level and speech_active while the engine runs.

    A separate task rather than something the capture loop does, because the
    visuals want these numbers whether or not anyone is speaking — the piece
    should be able to breathe with the room.
    """
    period = 1 / hz
    while True:
        bridge.osc("mic_level", mic.level)
        bridge.osc("speech_active", 1.0 if mic.speech_active else 0.0)
        await asyncio.sleep(period)


class Stage:
    """Time one stage of a turn. `with stage('stt'): ...`"""

    def __init__(self, marks: dict, name: str):
        self.marks, self.name = marks, name

    def __enter__(self):
        self._t0 = time.time()
        return self

    def __exit__(self, *exc):
        self.marks[self.name] = round(time.time() - self._t0, 3)
        return False
