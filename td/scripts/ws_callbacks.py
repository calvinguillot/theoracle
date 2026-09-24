# theoracle — TouchDesigner side of the bridge.
#
# Put this in a Text DAT named 'ws_callbacks' and set it as the WebSocket DAT's
# callbacks DAT. See README Step 13.
#
# Operators it uses, as siblings of this DAT:
#   oracle_ws      WebSocket DAT       required - the connection to the engine
#   oracle_state   Table DAT           optional - every field of the last message
#   oracle_text    Text DAT            optional - the spoken text on its own
#   oracle_audio   Audio File In CHOP  optional - plays the WAV
#
# Anything optional that is missing is reported once in the textport and skipped,
# so a half-built patch still tells you what is arriving.

import json
import re

# Absolute path to your repository's runtime/audio folder, forward slashes.
AUDIO_DIR = 'C:/Users/user/Documents/Projects/theoracle/runtime/audio'

# Leave empty unless you set bridge.ws_token in config.yaml to something other
# than 'change-me'. If you do set one, it must match exactly, and this file is
# then a secret: do not commit it with the token filled in.
WS_TOKEN = ''

AUDIO_ID_RE = re.compile(r'^[0-9a-f]{32}$')     # the engine's id format, nothing else
STATES = ['idle', 'listening', 'transcribing', 'generating',
          'validating', 'synthesizing', 'ready', 'speaking', 'cooldown', 'error']


# ── small helpers ─────────────────────────────────────────────────────────
def _op(name, required=False):
    """Fetch a sibling operator, or None with one clear message."""
    o = op(name)
    if o is None:
        debug("oracle: operator '{}' not found{}".format(
            name, ' (REQUIRED)' if required else ' - skipping'))
    return o


def _set(table, key, value):
    """Set table[key, 1], creating the row if it does not exist yet."""
    if table.numCols < 2:
        table.setSize(table.numRows, 2)
    if table.row(key) is None:
        table.appendRow([key, value])
    else:
        table[key, 1] = value


# ── sending ───────────────────────────────────────────────────────────────
def send(event, **fields):
    ws = _op('oracle_ws', required=True)
    if ws is None:
        return
    payload = {'protocol_version': 1, 'event': event}
    payload.update(fields)
    ws.sendText(json.dumps(payload))


def onConnect(dat):
    """Say hello, so a token-protected engine will accept us.

    Harmless when no token is set — the engine ignores an unexpected hello.
    """
    if WS_TOKEN:
        send('hello', token=WS_TOKEN)
    return


def presence(here):
    """The sensor's level, not an event.

    The HC-SR04 tells you someone is *there*, continuously; it does not tell
    you someone arrived. So both edges send the same message with a different
    value. High opens a visit and keeps it going turn after turn without the
    sensor firing again; low starts a departure clock, and the engine waits
    conversation.end_on_sensor_clear_s before deciding they are really gone —
    so a visitor who leans back to think does not lose their visit.
    """
    send('presence', present=bool(here))


def listen_start():
    """One listening window. For testing without the sensor."""
    send('listen_start')


def listen_stop():
    """Stop recording now and process what was captured."""
    send('listen_stop')


def cancel():
    """Abandon the visit. Anything still in flight is discarded, not played.

    Wire this to the sensor's falling edge once the HC-SR04 is in: a visitor
    who walks away should not be followed down the corridor by their own
    prophecy.
    """
    send('cancel')


def mute():
    """The kill switch. Have a button for this before opening night."""
    send('mute')


def playback_finished():
    """Called automatically when the audio should have finished.

    If you ever see the engine print "no playback_finished from
    TouchDesigner", this is the call that did not happen — look in the
    textport for an error from the delayed run() in play().
    """
    send('playback_finished')


# ── receiving ─────────────────────────────────────────────────────────────
def onReceiveText(dat, rowIndex, message, *args, **kwargs):
    # *args absorbs the trailing 'bytes' argument that newer TouchDesigner
    # builds pass and older ones do not. Do not tighten this signature.
    try:
        msg = json.loads(message)
    except Exception:
        debug('oracle: ignored non-JSON message')
        return

    event = msg.get('event')

    # Everything lands in one table; the rest of the patch reads from there,
    # so parsing lives in exactly one place.
    table = _op('oracle_state')
    if table is not None:
        for key, value in msg.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            _set(table, key, value)
        if event == 'state':
            state = msg.get('state', 'idle')
            _set(table, 'state_index',
                 STATES.index(state) if state in STATES else 0)

    if event in ('transcript', 'response_ready'):
        text_dat = _op('oracle_text')
        if text_dat is not None:
            text_dat.text = msg.get('text', '')

    if event == 'response_ready':
        play(msg)
    elif event == 'error':
        debug('oracle error:', msg.get('code'))

    return


# ── playback ──────────────────────────────────────────────────────────────
def play(msg):
    """Load and cue the WAV the engine just finished writing."""
    audio_id = msg.get('audio_id', '')
    if not AUDIO_ID_RE.match(audio_id):
        debug('oracle: refused a bad audio_id:', str(audio_id)[:40])
        return

    audio = _op('oracle_audio')
    if audio is None:
        return

    # The only variable part of the path is the validated id. Nothing from the
    # Oracle's text ever reaches the filesystem.
    audio.par.file = '{}/{}.wav'.format(AUDIO_DIR, audio_id)
    audio.par.play = 1
    audio.par.cuepulse.pulse()

    send('playback_started', audio_id=audio_id)

    # Tell the engine when it is over. A delayed call rather than a Timer CHOP:
    # one less operator to wire, and the duration is already known.
    #
    # The absolute path matters. run() executes its string later, in whatever
    # operator context happens to be current then, so a bare op('ws_callbacks')
    # resolves to None and the delayed call dies silently in the textport —
    # leaving the engine waiting in 'speaking' for ever. me.path is whatever
    # this DAT is really called, wherever you put it.
    duration = float(msg.get('audio_duration_seconds', 0) or 0)
    if duration > 0:
        run("mod(op('{}')).playback_finished()".format(me.path),
            delayMilliSeconds=int(duration * 1000) + 250, delayRef=me)


# ── the sensor ────────────────────────────────────────────────────────────
# In the sensor's CHOP Execute DAT, with BOTH 'Off to On' and 'On to Off'
# enabled:
#
#     def onOffToOn(channel, sampleIndex, val, prev):
#         mod(op('ws_callbacks')).presence(True)
#         return
#
#     def onOnToOff(channel, sampleIndex, val, prev):
#         mod(op('ws_callbacks')).presence(False)
#         return
#
# Both edges, one function, different value. Do NOT send cancel() on the
# falling edge: cancel throws the visit away at once, and the sensor drops
# every time somebody leans back or turns to a friend. The engine holds the
# grace period for exactly that.
#
# For a first test, a Constant CHOP you set to 1 and back to 0 by hand behaves
# the same as the sensor does. The HC-SR04 replaces it later: Serial DAT ->
# median of 5 -> threshold with hysteresis (arm below 80 cm, disarm above
# 120 cm) -> this same pair of calls.
