# Pi Matrix OSC Setup Notes

**Status:** Working

**Last updated:** 2026-09-22

## System overview

One 12×6 pattern is selected in TouchDesigner, then split into two 6×6 halves:

```text
pattern_selector
        ↓
     12×6 pattern
        ↓
  ┌─────┴─────┐
  ↓           ↓
pattern1   pattern2
  ↓           ↓
null_pattern1  null_pattern2
  ↓           ↓
top2        top3
  ↓           ↓
choptodat2  choptodat3
  └─────┬─────┘
        ↓
      script1
```

- `pattern1` is the first/left 6×6 half.
- `pattern2` is the second/right 6×6 half.
- The pattern selector chooses the complete 12×6 source before it is split.
- The selector currently uses Switch TOP indices; TouchDesigner indices are zero-based.

## TouchDesigner wiring

```text
null_pattern1 → top2 → choptodat2 → script1 input 0
null_pattern2 → top3 → choptodat3 → script1 input 1
```

The `script1` callback reads exactly 36 numeric values from each CHOP-to-DAT table, converts them to binary values, and sends the two matrices separately.

### `script1` callback

```python
def read_matrix(dat):
    values = []

    for row in dat.rows():
        for cell in row:
            try:
                value = float(cell.val)
            except (ValueError, TypeError):
                continue

            values.append(1 if value >= 0.5 else 0)

    if len(values) != 36:
        debug('{}: expected 36 values, got {}'.format(
            dat.name,
            len(values)
        ))
        return None

    return values


def onCook(scriptOp):
    if len(scriptOp.inputs) < 2:
        debug('script1 needs choptodat2 and choptodat3')
        return

    # Input 0 = choptodat2 = pattern1
    # Input 1 = choptodat3 = pattern2
    pattern1 = read_matrix(scriptOp.inputs[0])
    pattern2 = read_matrix(scriptOp.inputs[1])

    if pattern1 is None or pattern2 is None:
        return

    # Pattern 1 → Pi 1
    op('oscoutdat2').sendOSC('/pattern', pattern1)

    # Pattern 2 → Pi 2
    op('oscoutdat1').sendOSC('/pattern', pattern2)
```

Important:

- Use OSC address `/pattern`, not `/matrix`.
- Send a flat list of exactly 36 integer values.
- Do not use `*pattern1`.
- Do not use `[pattern1]`.
- Filter out empty/non-numeric CHOP-to-DAT cells so `None` values are not sent.

## OSC destinations

| TouchDesigner output | Pattern | Destination | Pi |
|---|---|---|---|
| `oscoutdat2` | `pattern1` | `192.168.60.2:8000` | Pi 1 |
| `oscoutdat1` | `pattern2` | `192.168.50.2:8000` | Pi 2 |

The output names are intentionally crossed:

```text
pattern1 → oscoutdat2 → Pi 1
pattern2 → oscoutdat1 → Pi 2
```

## Raspberry Pi networking

### Pi 1

- Wi-Fi profile `calvinmaster` disabled.
- Wi-Fi radio disabled.
- Wired-only operation.
- Ethernet address:
  ```text
  192.168.60.2/24
  ```
- Laptop Ethernet counterpart:
  ```text
  192.168.60.1/24
  ```

### Pi 2

- Wired address:
  ```text
  192.168.50.2/24
  ```
- Laptop Ethernet 2 counterpart:
  ```text
  192.168.50.1/24
  ```

The direct Ethernet links have no default gateway or DNS configured.

## Receiver script

Both Pis run the same receiver:

```bash
sudo python3 ~/matrix_osc_receiver_nodeps.py
```

The script is a dependency-free GPIO-scanned 6×6 matrix receiver. It listens on:

```python
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 8000
```

The same script works on both Pis because each Pi receives traffic sent to its own IP address.

The receiver handles:

```python
if address == "/pattern":
    set_pattern_from_flat(args)
```

It copies the 36 flat values into `matrix_state`, which is read by the GPIO scan thread.

## Receiver GPIO configuration

The working receiver currently contains:

```python
COL_PINS = [12, 16, 18, 22, 32, 37]   # left → right
ROW_PINS = [33, 31, 29, 15, 13, 11]   # top → bottom
```

GPIO numbering mode:

```python
GPIO.setmode(GPIO.BOARD)
```

This means the pin numbers above are physical header pin numbers, not BCM GPIO numbers.

## Receiver matrix mapping

The receiver maps the flat OSC list in row-major order:

```python
matrix_state[r][c] = 1 if flat_values[r * 6 + c] else 0
```

So values are interpreted as:

```text
values 0–5   → row 0
values 6–11  → row 1
values 12–17 → row 2
values 18–23 → row 3
values 24–29 → row 4
values 30–35 → row 5
```

## Troubleshooting notes

- If the Pi prints `/pattern updated`, the OSC address and receiver dispatch are correct.
- If the Pi prints `/matrix` instead, TouchDesigner is using the wrong OSC address.
- A valid packet must contain exactly 36 values and no `None` values.
- If both Pi terminals receive data but only one matrix responds, check the OSC destination IPs.
- If the pattern is correct but rows or columns are reversed, adjust the receiver’s `ROW_PINS`, `COL_PINS`, or matrix indexing—not the OSC routing.
- Do not switch to `rpi_ws281x`; this setup uses direct GPIO scanning through `RPi.GPIO`.
- The previous `led_test.py` using `rpi_ws281x` is unrelated and fails because that module is not installed.

## Known-good data flow

```text
TouchDesigner pattern_selector
→ 12×6 source
→ two 6×6 crops
→ null_pattern1 / null_pattern2
→ top2 / top3
→ choptodat2 / choptodat3
→ script1
→ /pattern OSC messages
→ two wired Ethernet links
→ same receiver script on both Pis
→ matrix_state
→ GPIO row/column scan
→ LEDs
```
