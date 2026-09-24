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
