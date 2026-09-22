# Two-Pi TouchDesigner OSC Setup — Current Working State

_Last verified: Monday, September 21, 2026_

## Working result

Both Raspberry Pis receive the same 6×6 pattern from TouchDesigner and display it on their LED matrices.

The sensor is **not required** for the current test setup.

## Network topology

The Mac uses USB-C-to-Ethernet adapters. The two Pis are on separate direct Ethernet subnets to avoid the Mac’s Wi-Fi and wired interfaces sharing the same subnet.

| Device | Interface | IPv4 address | UDP port |
|---|---|---:|---:|
| Mac / Pi 1 Ethernet path | USB Ethernet | `10.20.12.1/24` on the relevant adapter | — |
| Pi 1 | `eth0` | `10.20.12.247/24` | `8000` |
| Mac / Pi 2 Ethernet path | USB Ethernet, interface previously identified as `en6` | `192.168.50.1/24` | — |
| Pi 2 | `eth0` | `192.168.50.2/24` | `8000` |

Important: Do not configure Wi-Fi and a USB Ethernet interface on the same subnet. The Mac’s Wi-Fi was using `10.20.12.106`, while the original direct Ethernet setup also used `10.20.12.x`, causing routing/ARP confusion. Moving Pi 2 to `192.168.50.0/24` fixed the connection.

## Mac adapter configuration

For the Pi 2 Ethernet service, the working manual configuration is:

```text
IP address: 192.168.50.1
Subnet mask: 255.255.255.0
Router: blank
```

The service previously identified as:

```text
USB 10/100/1G/2.5G LAN
```

The active interface was previously identified as:

```text
en6
```

Check current adapter/interface names rather than assuming they remain unchanged:

```bash
networksetup -listallnetworkservices
ifconfig
```

Pi 1’s Ethernet path remains on the `10.20.12.0/24` subnet.

## Pi network verification

Pi 1 should show approximately:

```bash
ip -4 -br addr
```

```text
eth0 UP 10.20.12.247/24
```

Pi 2 should show:

```bash
ip -4 -br addr
```

```text
eth0 UP 192.168.50.2/24
```

Also verify the physical link on each Pi:

```bash
cat /sys/class/net/eth0/carrier
```

Expected:

```text
1
```

The following should also appear in `ip link show eth0`:

```text
LOWER_UP
```

## Ping tests from Mac

Run both before troubleshooting OSC:

```bash
ping -c 3 10.20.12.247
ping -c 3 192.168.50.2
```

Both should reply.

Check route selection if needed:

```bash
route -n get 10.20.12.247
route -n get 192.168.50.2
```

Each destination should use the appropriate USB Ethernet interface.

## Pi receiver

Run the receiver on each Pi before sending from TouchDesigner.

Pi 1:

```bash
sudo python3 ~/matrix_osc_receiver_nodeps.py
```

Pi 2:

```bash
sudo python3 ~/matrix_osc_receiver_nodeps.py
```

Expected startup message:

```text
OSC listener bound on UDP port 8000.
```

Verify the listener if necessary:

```bash
sudo ss -lunp | grep ':8000'
```

Expected binding:

```text
0.0.0.0:8000
```

The receiver currently has a shutdown cleanup issue: after `Ctrl+C`, it may print an `_enter_buffered_busy` fatal error involving the background `_scan_loop` thread. This occurs during shutdown after the receiver has already printed `Shutting down.` It does not prevent normal startup or OSC reception.

## TouchDesigner OSC Out DATs

Two OSC Out DATs are used.

### Pi 1 output

```text
Operator: /project1/oscoutdat1
Network Address: 10.20.12.247
Network Port: 8000
```

### Pi 2 output

```text
Operator: /project1/oscoutdat2
Network Address: 192.168.50.2
Network Port: 8000
```

The operator path is only the TouchDesigner object reference. The actual destination is controlled by each OSC Out DAT’s Network Address and Network Port parameters.

OSC address:

```text
/pattern
```

## Current sensor-free test flow

```text
6×6 pattern source
        ↓
      script1
        ↓
  oscoutdat1 → Pi 1 10.20.12.247:8000
  oscoutdat2 → Pi 2 192.168.50.2:8000
```

The sensor, `serial1`, `serial1_chop`, distance threshold, and sensor state machine are disabled/not required for this test.

## Working callback pattern

The callback reads a 6×6 grid from the input DAT connected to `script1`, applies hysteresis thresholding, flips the grid vertically, flattens it row-major, and sends the same pattern to both Pis.

```python
last_pattern = None
last_send_time = 0.0

SEND_INTERVAL_SECONDS = 0.10
ON_THRESHOLD = 0.60
OFF_THRESHOLD = 0.40

OSC_OUT_PATHS = [
    '/project1/oscoutdat1',
    '/project1/oscoutdat2',
]


def onCook(scriptOp):
    global last_pattern
    global last_send_time

    dat = scriptOp.inputs[0]

    if dat is None:
        print(
            '[OSC] script1 has no input DAT',
            flush=True
        )
        return

    if dat.numRows < 6 or dat.numCols < 6:
        print(
            f'[OSC] expected at least 6x6 values, '
            f'got {dat.numRows}x{dat.numCols}',
            flush=True
        )
        return

    previous = (
        list(last_pattern)
        if last_pattern is not None
        else None
    )

    pattern = []

    for row in range(6):
        for col in range(6):
            try:
                value = float(dat[row, col].val)
            except Exception as exc:
                print(
                    f'[OSC] invalid value at '
                    f'row={row}, col={col}: {exc}',
                    flush=True
                )
                return

            index = row * 6 + col
            old_value = (
                previous[index]
                if previous is not None
                else 0
            )

            if old_value == 1:
                pixel = (
                    0
                    if value <= OFF_THRESHOLD
                    else 1
                )
            else:
                pixel = (
                    1
                    if value >= ON_THRESHOLD
                    else 0
                )

            pattern.append(pixel)

    pattern = tuple(pattern)
    now = absTime.seconds

    if pattern == last_pattern:
        return

    if now - last_send_time < SEND_INTERVAL_SECONDS:
        return

    sent_count = 0

    for output_path in OSC_OUT_PATHS:
        osc_out = op(output_path)

        if osc_out is None:
            print(
                f'[OSC] missing operator: {output_path}',
                flush=True
            )
            continue

        try:
            osc_out.sendOSC(
                '/pattern',
                list(pattern)
            )
            sent_count += 1

            print(
                f'[OSC] sent /pattern to {output_path}',
                flush=True
            )

        except Exception as exc:
            print(
                f'[OSC] send failed for '
                f'{output_path}: {exc}',
                flush=True
            )

    if sent_count == 0:
        return

    last_pattern = pattern
    last_send_time = now

    print(
        f'[OSC] sent /pattern to '
        f'{sent_count}/{len(OSC_OUT_PATHS)} outputs',
        flush=True
    )
```

## Simpler single-pattern test callback

For a separate small file with one fixed pattern, without the sensor or state machine, use the simpler callback below. It expects `topto1` to provide six channels with six samples each.

```python
def onCook(scriptOp):
    chop = op('topto1')

    if chop is None:
        print('Missing TOP to CHOP: topto1')
        return

    if len(chop.chans()) != 6:
        print(
            'Expected 6 row-channels, got '
            + str(len(chop.chans()))
        )
        return

    bottom_up_grid = []

    for row_chan in chop.chans():
        if len(row_chan) != 6:
            print(
                'Expected 6 samples per row, got '
                + str(len(row_chan))
            )
            return

        row_values = []

        for c in range(6):
            if row_chan[c] > 0.5:
                row_values.append(1)
            else:
                row_values.append(0)

        bottom_up_grid.append(row_values)

    grid = bottom_up_grid[::-1]

    flat = []

    for r in range(6):
        for c in range(6):
            flat.append(grid[r][c])

    op('oscoutdat1').sendOSC('/pattern', flat)

    print('[TEST] sent:', flat)

    return
```

For the two-Pi version, use both OSC Out DATs instead of only `oscoutdat1`.

## GPIO-only test

To verify a Pi’s matrix hardware without OSC or TouchDesigner:

```bash
sudo python3 ~/blink_gpio.py
```

Expected output:

```text
GPIO matrix blink test running
All LEDs on and off every 1 second
Press Ctrl+C to stop
ON
OFF
```

Pi 2 passed this test, proving its matrix GPIO output and LED hardware are working.

## Troubleshooting checklist for tomorrow

1. Confirm Pi 1 is `10.20.12.247` and Pi 2 is `192.168.50.2`.
2. Confirm both Pis have `eth0` and `carrier = 1`.
3. Ping both addresses from the Mac.
4. Start the receiver on both Pis.
5. Check both OSC Out DAT destinations.
6. Ensure the sensor callback/state machine is disabled for the sensor-free test.
7. Change the 6×6 source so `script1` cooks and sends.
8. Confirm both Pis print `[OSC] pattern updated: [...]`.
9. If only one Pi fails, test its network independently before changing the callback.

## Current success message

The working state was confirmed when both Pis received the pattern and displayed it successfully.
