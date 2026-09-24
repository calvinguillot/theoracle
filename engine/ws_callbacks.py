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


def listen_start():
    """Call this from the sensor's CHOP Execute DAT."""
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
    """Called automatically when the audio should have finished."""
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
    duration = float(msg.get('audio_duration_seconds', 0) or 0)
    if duration > 0:
        run("mod(op('ws_callbacks')).playback_finished()",
            delayMilliSeconds=int(duration * 1000) + 250)


# ── the sensor ────────────────────────────────────────────────────────────
# In the sensor's CHOP Execute DAT, with 'Off to On' enabled:
#
#     def onOffToOn(channel, sampleIndex, val, prev):
#         mod(op('ws_callbacks')).listen_start()
#         return
#
#     def onOnToOff(channel, sampleIndex, val, prev):
#         mod(op('ws_callbacks')).cancel()
#         return
#
# mod() is required. op('ws_callbacks') is the DAT operator, which has no
# listen_start on it; mod() imports the text inside it as a module, which does.
# Without it TouchDesigner raises:
#     'td.textDAT' object has no attribute 'listen_start'
#
# For the first test, a Constant CHOP you set to 1 by hand is enough. The
# HC-SR04 replaces it later: Serial DAT -> median filter -> threshold with
# hysteresis -> this same call.
