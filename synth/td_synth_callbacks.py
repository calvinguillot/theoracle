# theoracle — TouchDesigner side of the soundscape.
#
# Put this in a Text DAT named 'synth_callbacks' and set it as the callbacks
# DAT of a Web Server DAT named 'synth_server' (Port 9980, Active on).
#
# That one operator:
#   - serves synth/index.html, the recordings in synth/grains, and the page's
#     saved settings (synth/settings.json) to Chrome over HTTP
#   - holds the WebSocket Chrome connects back on
#   - writes synth/settings.json when you press Save on the page
#
# TouchDesigner's job is to say what is happening. What that does to the sound
# is decided on the page, under Routes, and saved with everything else.
#
# Anywhere in the patch:
#     mod(op('synth_callbacks')).input('distance', 0.42)
#     mod(op('synth_callbacks')).inputs({'distance': 0.42, 'level': 0.1})
#     mod(op('synth_callbacks')).scene('speaking')         # the Oracle's state

import json
import os
import re

# Absolute path to your repository's synth folder, forward slashes.
SYNTH_DIR = 'C:/Users/user/Documents/Projects/theoracle/synth'

NAME_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
INPUT_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,64}$')
GRAIN_URI_RE = re.compile(r'^/grains/([A-Za-z0-9_-]{1,64})\.wav$')
MAX_SETTINGS_BYTES = 512 * 1024


# ── clients ───────────────────────────────────────────────────────────────
def _server():
    return op('synth_server')


def _clients():
    return _server().fetch('clients', [], storeDefault=True)


def _broadcast(obj):
    server = _server()
    text = json.dumps(obj)
    for client in list(_clients()):
        server.webSocketSendText(client, text)


# ── the API the rest of the patch uses ────────────────────────────────────
def input(name, value):
    """One named number. It appears on the page under From TouchDesigner."""
    if INPUT_RE.match(name):
        _broadcast({'address': '/in/' + name, 'value': float(value)})


def inputs(values):
    """Several named numbers in one message; use this for anything per-frame."""
    clean = {k: float(v) for k, v in values.items() if INPUT_RE.match(k)}
    if clean:
        _broadcast({'inputs': clean})


def scene(state, glide=None):
    """The Oracle's state. The page decides which scene plays for it."""
    _server().store('state', state)
    msg = {'address': '/scene', 'value': state}
    if glide is not None:
        msg['glide'] = glide
    _broadcast(msg)


def send(address, value, glide=None):
    """Set a parameter directly, bypassing scenes. Mostly for testing."""
    msg = {'address': address, 'value': value}
    if glide is not None:
        msg['glide'] = glide
    _broadcast(msg)


# ── HTTP ──────────────────────────────────────────────────────────────────
def _respond(response, status, reason, data, content_type='text/plain; charset=utf-8'):
    response['statusCode'] = status
    response['statusReason'] = reason
    response['data'] = data
    response['content-type'] = content_type
    response['cache-control'] = 'no-store'
    return response


def _file(response, path, content_type):
    try:
        with open(path, 'rb') as f:
            return _respond(response, 200, 'OK', f.read(), content_type)
    except OSError:
        return _respond(response, 404, 'Not Found', 'not found')


def _grain_names():
    folder = SYNTH_DIR + '/grains'
    if not os.path.isdir(folder):
        return []
    names = [f[:-4] for f in sorted(os.listdir(folder)) if f.lower().endswith('.wav')]
    return [n for n in names if NAME_RE.match(n)]


def onHTTPRequest(webServerDAT, request, response):
    uri = request.get('uri', '/').split('?')[0]
    if uri in ('/', '/index.html'):
        return _file(response, SYNTH_DIR + '/index.html', 'text/html; charset=utf-8')
    if uri == '/settings.json':
        return _file(response, SYNTH_DIR + '/settings.json', 'application/json')
    if uri == '/grains.json':
        return _respond(response, 200, 'OK', json.dumps(_grain_names()), 'application/json')
    m = GRAIN_URI_RE.match(uri)
    if m:
        # The only variable part of the path is a name that passed the regex.
        return _file(response, '{}/grains/{}.wav'.format(SYNTH_DIR, m.group(1)), 'audio/wav')
    return _respond(response, 404, 'Not Found', 'not found')


# ── saving the page's settings ────────────────────────────────────────────
def _save_settings(settings):
    if not isinstance(settings, dict) or not isinstance(settings.get('scenes'), dict):
        return 'not a settings object'
    text = json.dumps(settings, indent=2, sort_keys=True)
    if len(text) > MAX_SETTINGS_BYTES:
        return 'too large'
    path = SYNTH_DIR + '/settings.json'
    tmp = path + '.tmp'
    try:
        if os.path.exists(path):
            os.replace(path, SYNTH_DIR + '/settings.previous.json')   # one step of undo
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        return str(e)[:120]
    return None


# ── WebSocket ─────────────────────────────────────────────────────────────
def onWebSocketOpen(webServerDAT, client, uri):
    clients = _clients()
    if client not in clients:
        clients.append(client)
    debug('synth: connected', client)
    # Bring a fresh or reloaded page up to date with the Oracle.
    state = webServerDAT.fetch('state', None)
    if state:
        webServerDAT.webSocketSendText(client, json.dumps(
            {'address': '/scene', 'value': state, 'glide': 4}))
    return


def onWebSocketClose(webServerDAT, client):
    clients = _clients()
    if client in clients:
        clients.remove(client)
    debug('synth: disconnected', client)
    return


def onWebSocketReceiveText(webServerDAT, client, data):
    if len(data) > MAX_SETTINGS_BYTES * 2:
        return
    try:
        msg = json.loads(data)
    except Exception:
        return
    event = msg.get('event')
    if event == 'save_settings':
        problem = _save_settings(msg.get('settings'))
        reply = {'event': 'saved'} if problem is None else {'event': 'save_failed', 'detail': problem}
        webServerDAT.webSocketSendText(client, json.dumps(reply))
        debug('synth: settings', 'saved' if problem is None else 'NOT saved: ' + problem)
    elif event == 'error':
        debug('synth error:', msg.get('code'), str(msg.get('detail', ''))[:80])
    elif event in ('hello', 'loaded'):
        debug('synth:', event, {k: v for k, v in msg.items() if k != 'event'})
    return


def onWebSocketReceiveBinary(webServerDAT, client, data):
    return


def onWebSocketReceivePing(webServerDAT, client, data):
    webServerDAT.webSocketSendPong(client, pongData=data)
    return


def onWebSocketReceivePong(webServerDAT, client, data):
    return


def onServerStart(webServerDAT):
    webServerDAT.store('clients', [])
    return


def onServerStop(webServerDAT):
    webServerDAT.store('clients', [])
    return


# ── hooking it to the Oracle ──────────────────────────────────────────────
# In ws_callbacks.py, inside onReceiveText, in the `if event == 'state':` block:
#
#     synth = op('synth_callbacks')
#     if synth is not None:
#         mod(synth).scene(msg.get('state', 'idle'))
#
# ── sending CHOP channels as inputs ───────────────────────────────────────
# Make a Null CHOP named 'synth_inputs' holding whatever you want the page to
# see (sensor distance, voice level, anything). Its channel names become the
# input names. Then in an Execute DAT with Frame End on:
#
#     def onFrameEnd(frame):
#         if frame % 3 == 0:                      # 20 times a second is plenty
#             chop = op('synth_inputs')
#             mod(op('synth_callbacks')).inputs({c.name: c.eval() for c in chop.chans()})
#         return
#
# Put a Lag or Filter CHOP before the Null so a jittery sensor does not
# become a jittery room. The page smooths too, per route.
