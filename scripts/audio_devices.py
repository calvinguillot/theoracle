"""theoracle — resolving an audio device name to something PortAudio can open.

Windows shows every sound card several times over, once per host API: MME,
DirectSound, WASAPI and WDM-KS all list "CQ 1&2 (CQ12T)" under the same name.
A name on its own is therefore never enough to identify a device, which is why
a plain name match reports three hits and refuses to guess.

Config accepts three forms, in order of how much they pin down:

    input_device: 27                     # a PortAudio index — exact, but it moves
    input_device: "CQ 1&2 (CQ12T)"       # a name fragment, resolved by preference
    input_device: {name: "CQ 1&2", host_api: "WASAPI"}   # a name and a host API

The middle form is the one to use. When several host APIs offer the same
device, HOST_API_PREFERENCE decides, and the check prints which one it picked so
nothing is silently different from what you expected.

Indices are exact but they are not stable: they shift when a USB device is
replugged, when Bluetooth connects, or when Windows updates a driver. Use one
only to prove a theory, then write the name down instead.
"""

from __future__ import annotations

# Best first. WASAPI is the modern Windows path and has the lowest latency of
# the shared-mode APIs; DirectSound and MME are older and add buffering; WDM-KS
# talks to the driver directly and takes exclusive control, which is why it is
# last — it can lock the mixer away from TouchDesigner.
HOST_API_PREFERENCE = [
    "Windows WASAPI",
    "Windows DirectSound",
    "MME",
    "Windows WDM-KS",
    "ASIO",
    "Core Audio",        # macOS
    "ALSA", "JACK",      # Linux
]


class DeviceError(Exception):
    """Raised with a sentence you can act on, not a stack trace."""


def _host_api_names() -> list[str]:
    import sounddevice as sd
    return [a["name"] for a in sd.query_hostapis()]


def describe(index: int) -> str:
    """'27  CQ 1&2 (CQ12T)  [Windows WASAPI]  in:2' — for printing."""
    import sounddevice as sd
    d = sd.query_devices(index)
    apis = _host_api_names()
    api = apis[d["hostapi"]] if d["hostapi"] < len(apis) else "?"
    kind = []
    if d["max_input_channels"]:
        kind.append(f"in:{d['max_input_channels']}")
    if d["max_output_channels"]:
        kind.append(f"out:{d['max_output_channels']}")
    return f"{index:3d}  {d['name'][:44]:44s} [{api:<18}] {','.join(kind)}"


def candidates(spec, kind: str) -> list[int]:
    """Every device index matching spec, filtered to 'input' or 'output'."""
    import sounddevice as sd

    if isinstance(spec, dict):
        fragment = spec.get("name", "")
        want_api = spec.get("host_api")
    else:
        fragment, want_api = str(spec), None

    apis = _host_api_names()
    channels = "max_input_channels" if kind == "input" else "max_output_channels"

    out = []
    for i, d in enumerate(sd.query_devices()):
        if not d[channels]:
            continue                                   # wrong direction
        if fragment.lower() not in d["name"].lower():
            continue
        if want_api:
            api = apis[d["hostapi"]] if d["hostapi"] < len(apis) else ""
            if want_api.lower() not in api.lower():
                continue
        out.append(i)
    return out


def resolve(spec, kind: str) -> int | None:
    """Config value → a PortAudio index. None means 'use the system default'.

    Raises DeviceError with an explanation when it cannot choose confidently.
    """
    import sounddevice as sd

    if spec is None or spec == "":
        return None

    # An explicit index: trust it, but check it points the right way.
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        index = int(spec)
        try:
            d = sd.query_devices(index)
        except Exception:
            raise DeviceError(f"index {index} does not exist on this machine")
        channels = "max_input_channels" if kind == "input" else "max_output_channels"
        if not d[channels]:
            raise DeviceError(f"index {index} ({d['name']}) has no {kind} channels")
        return index

    hits = candidates(spec, kind)
    if not hits:
        name = spec.get("name", spec) if isinstance(spec, dict) else spec
        raise DeviceError(f'"{name}" matches no {kind} device — see the list above')
    if len(hits) == 1:
        return hits[0]

    # Several host APIs offer the same device. Prefer, rather than refuse: the
    # alternative is asking someone to type a host API name they have no reason
    # to know. The caller prints what was chosen.
    apis = _host_api_names()
    for wanted in HOST_API_PREFERENCE:
        for i in hits:
            api = apis[sd.query_devices(i)["hostapi"]]
            if api == wanted:
                return i
    return hits[0]


def resolve_reported(spec, kind: str) -> tuple[int | None, str]:
    """resolve(), plus a one-line account of what happened and why."""
    if spec is None or spec == "":
        return None, "not set — using the system default"
    index = resolve(spec, kind)
    others = len(candidates(spec, kind)) - 1
    note = describe(index)
    if others > 0:
        note += f"   (chosen from {others + 1} host APIs offering this device)"
    return index, note


# ── sample rates ──────────────────────────────────────────────────────────
# The CQ runs at 48 kHz and Whisper wants 16 kHz. Windows' own mixer would
# convert between them, but PortAudio's WASAPI backend opens the device with the
# exact format you ask for and fails with "Invalid sample rate [-9997]" if it
# cannot. MME and DirectSound hide this by resampling behind your back, at a
# quality you do not choose.
#
# So: record at whatever rate the device actually runs at, and convert here,
# where the filtering is visible and the same on every host API.

TARGET_RATE = 16000          # what faster-whisper expects, always


def native_samplerate(index: int | None, kind: str = "input") -> int:
    """The rate this device really runs at. 48000 for most interfaces."""
    import sounddevice as sd
    if index is None:
        index = sd.default.device[0 if kind == "input" else 1]
    return int(round(sd.query_devices(index)["default_samplerate"]))


def _lowpass(cutoff_norm: float, taps: int = 121):
    """A windowed-sinc FIR. cutoff_norm is a fraction of the sample rate."""
    import numpy as np
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff_norm * np.sinc(2 * cutoff_norm * n) * np.hamming(taps)
    return h / h.sum()


def to_target_rate(audio, sr_in: int, sr_out: int = TARGET_RATE):
    """Resample mono float32 audio. Uses soxr when it is installed."""
    import numpy as np
    if sr_in == sr_out:
        return np.asarray(audio, dtype=np.float32)

    try:
        import soxr
        return soxr.resample(np.asarray(audio, dtype=np.float32),
                             sr_in, sr_out).astype(np.float32)
    except ImportError:
        pass

    audio = np.asarray(audio, dtype=np.float64).ravel()
    if sr_out < sr_in:
        # Anti-alias first, or every frequency above the new Nyquist folds back
        # into the speech band as noise Whisper will happily transcribe.
        audio = np.convolve(audio, _lowpass(0.45 * sr_out / sr_in), mode="same")
    n_out = int(round(len(audio) * sr_out / sr_in))
    x_out = np.linspace(0, len(audio) - 1, n_out)
    return np.interp(x_out, np.arange(len(audio)), audio).astype(np.float32)


def usable_channels(index: int | None, kind: str = "input", want: int = 1) -> int:
    """How many channels this device will actually open.

    A laptop microphone array reports four channels and, on WASAPI, refuses to
    open as one: shared mode hands you the device's own format or nothing. Ask
    for what you want, and fall back to everything it has.
    """
    import sounddevice as sd
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    rate = native_samplerate(index, kind)
    for n in (want, _max_channels(index, kind)):
        if not n:
            continue
        try:
            check(device=index, channels=n, samplerate=rate)
            return n
        except Exception:
            continue
    return want          # let the real open() raise something informative


def _max_channels(index: int | None, kind: str) -> int:
    import sounddevice as sd
    if index is None:
        index = sd.default.device[0 if kind == "input" else 1]
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    return int(sd.query_devices(index)[key])


def pick_channel(buf, channel: int = 0):
    """One column out of a multi-channel recording, as mono float32.

    Channel 0, not a downmix: the elements of a microphone array are spaced
    centimetres apart, so averaging them comb-filters the speech — quietly, at
    exactly the frequencies that carry consonants.
    """
    import numpy as np
    buf = np.asarray(buf, dtype=np.float32)
    if buf.ndim == 1:
        return buf
    if channel >= buf.shape[1]:
        channel = 0
    return np.ascontiguousarray(buf[:, channel])


# PortAudio's codes, spelled out. Getting these wrong sends someone looking for
# a driver conflict when the real answer is one number in a config file.
_PA_ERRORS = {
    -9998: ("paInvalidChannelCount",
            "the device will not open with that many channels — "
            "audio_devices.usable_channels finds a count it accepts"),
    -9997: ("paInvalidSampleRate",
            "the device does not run at that rate — record at its native rate "
            "and resample (audio_devices.to_target_rate does this)"),
    -9996: ("paInvalidDevice", "that device index does not exist"),
    -9985: ("paDeviceUnavailable",
            "something else holds the device exclusively, "
            "usually an ASIO client or a WDM-KS stream"),
    -9999: ("paNotInitialized", "PortAudio failed to start — reinstall sounddevice"),
}


def explain_open_error(e: Exception) -> str:
    """PortAudio's error codes are terse. Say what to actually do."""
    args = getattr(e, "args", ())
    code = args[1] if len(args) > 1 else None
    if code in _PA_ERRORS:
        return _PA_ERRORS[code][1]
    return "check the device is connected and not in use"
