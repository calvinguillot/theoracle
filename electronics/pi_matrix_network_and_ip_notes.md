# Pi Matrix Network and IP Notes

**Status:** Working two-Pi OSC matrix setup

**Last updated:** 2026-09-24

## Current network arrangement

The laptop uses two separate Ethernet adapters. Each adapter connects directly to one Pi and uses a separate subnet.

```text
Laptop adapter for Pi 1: 192.168.50.1/24
Pi 1 Ethernet:           192.168.50.2/24

Laptop adapter for Pi 2: 192.168.60.1/24
Pi 2 Ethernet:           192.168.60.3/24
```

Wi-Fi may remain enabled for internet access, but it should not use either of these two subnets.

Pi 1 Wi-Fi was disabled so Pi 1 uses Ethernet only.

## TouchDesigner OSC destinations

```text
oscoutdat2 → 192.168.50.2:8000 → Pi 1
oscoutdat1 → 192.168.60.3:8000 → Pi 2
```

The OSC address must be:

```text
/pattern
```

The receiver script handles `/pattern`, not `/matrix`.

## TouchDesigner data flow

One complete 12×6 pattern is selected first, then split into two 6×6 halves:

```text
pattern_selector
        ↓
     12×6 source
        ↓
  ┌─────┴─────┐
  ↓           ↓
pattern1    pattern2
  ↓           ↓
null_pattern1  null_pattern2
  ↓              ↓
top2           top3
  ↓              ↓
choptodat2     choptodat3
  └──────┬───────┘
         ↓
       script1
```

`script1` inputs must be wired in this order:

```text
script1 input 0 = choptodat2 = pattern1
script1 input 1 = choptodat3 = pattern2
```

## Working `script1` callback

The callback reads 36 numeric values from each CHOP-to-DAT table, converts them to binary values, and sends each matrix to its own Pi.

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

    # input 0 = choptodat2 = pattern1
    # input 1 = choptodat3 = pattern2
    pattern1 = read_matrix(scriptOp.inputs[0])
    pattern2 = read_matrix(scriptOp.inputs[1])

    if pattern1 is None or pattern2 is None:
        return

    # pattern1 → Pi 1
    op('oscoutdat2').sendOSC('/pattern', pattern1)

    # pattern2 → Pi 2
    op('oscoutdat1').sendOSC('/pattern', pattern2)
```

Important:

- Send `/pattern`, not `/matrix`.
- Send exactly 36 numeric values.
- Do not use `*pattern1`.
- Do not send `None` values from CHOP-to-DAT headers or empty cells.
- One `script1` is sufficient; two scripts are not required.

## Raspberry Pi receiver

Both Pis run the same dependency-free GPIO receiver:

```bash
sudo python3 ~/matrix_osc_receiver_nodeps.py
```

The receiver listens on:

```python
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 8000
```

The receiver accepts `/pattern` and updates the local 6×6 GPIO matrix:

```python
if address == "/pattern":
    set_pattern_from_flat(args)
```

The two Pis can use the same script because each Pi receives OSC packets sent to its own IP address.

## Pi 1: change the IP address

On Pi 1, first list the connection profiles:

```bash
nmcli connection show
```

The wired profile is normally:

```text
Wired connection 1
```

To set Pi 1 to the current address `192.168.50.2/24`:

```bash
sudo nmcli connection modify "Wired connection 1" \
  ipv4.method manual \
  ipv4.addresses 192.168.50.2/24 \
  ipv4.gateway "" \
  ipv4.dns "" \
  connection.autoconnect yes
```

Apply the change:

```bash
sudo nmcli connection down "Wired connection 1"
sudo nmcli connection up "Wired connection 1"
```

Verify:

```bash
nmcli device status
ip -4 -br addr
ip route
```

Expected Pi 1 output:

```text
eth0  UP  192.168.50.2/24
192.168.50.0/24 dev eth0 ... src 192.168.50.2
```

## Pi 2: current IP

Pi 2 is currently working at:

```text
192.168.60.3/24
```

Its profile was shown as:

```text
oracle2 ethernet
```

Do not assign `192.168.60.3` to Pi 1 because that would duplicate Pi 2’s address.

## Changing Pi 1 to another address later

Choose an unused address on the Pi 1 subnet. For example:

```text
Laptop Pi 1 adapter: 192.168.50.1
Pi 1:                192.168.50.4
```

On Pi 1:

```bash
sudo nmcli connection modify "Wired connection 1" \
  ipv4.method manual \
  ipv4.addresses 192.168.50.4/24 \
  ipv4.gateway "" \
  ipv4.dns "" \
  connection.autoconnect yes
```

Apply:

```bash
sudo nmcli connection down "Wired connection 1"
sudo nmcli connection up "Wired connection 1"
```

Then change the TouchDesigner destination:

```text
oscoutdat2 → 192.168.50.4:8000
```

The laptop adapter connected to Pi 1 must remain:

```text
192.168.50.1/24
```

## Configuring the Windows adapter for Pi 1

1. Press `Win + R`.
2. Enter:
   ```text
   ncpa.cpl
   ```
3. Identify the Ethernet adapter physically connected to Pi 1.
4. Right-click it and choose **Properties**.
5. Select **Internet Protocol Version 4 (TCP/IPv4)**.
6. Select **Use the following IP address**.
7. Enter:
   ```text
   IP address:      192.168.50.1
   Subnet mask:     255.255.255.0
   Default gateway: blank
   DNS:             blank
   ```
8. Click **OK**, then **Close**.

If unsure which adapter is Pi 1’s, unplug Pi 1’s cable and observe which adapter becomes disconnected in `ncpa.cpl`. Rename it to something clear, such as:

```text
Ethernet-Pi1
```

## Testing the network

From Windows PowerShell:

```powershell
ping 192.168.50.2
ping 192.168.60.3
```

Both should reply.

To inspect routes:

```powershell
route print
```

To find the route used for a specific Pi:

```powershell
Find-NetRoute -RemoteIPAddress 192.168.50.2
Find-NetRoute -RemoteIPAddress 192.168.60.3
```

## Running and stopping the receiver

Start on each Pi:

```bash
sudo python3 ~/matrix_osc_receiver_nodeps.py
```

Stop with:

```text
Ctrl+C
```

Expected output when a valid packet arrives:

```text
[OSC] pattern updated: [...36 values...]
```

If the receiver prints `/matrix` instead, TouchDesigner is still using the wrong OSC address. If it prints nothing, test the Pi IP with `ping` and verify the corresponding OSC Out DAT address and UDP port `8000`.

## Backup the receiver script

Create a backup:

```bash
cp -p ~/matrix_osc_receiver_nodeps.py \
      ~/matrix_osc_receiver_backup.py
```

Create a readable Markdown note containing the current Python source:

```bash
{
    echo "# Matrix OSC Receiver"
    echo
    echo '```python'
    cat ~/matrix_osc_receiver_nodeps.py
    echo '```'
} > ~/matrix_osc_receiver_note.md
```

Display the note for manual copy/paste:

```bash
cat ~/matrix_osc_receiver_note.md
```

## Standalone LED test

The receiver uses direct GPIO scanning with `RPi.GPIO`; it does not use `rpi_ws281x`.

A standalone matrix blink test should use the same pin lists and `GPIO.BOARD` numbering as the receiver:

```python
COL_PINS = [12, 16, 18, 22, 32, 37]
ROW_PINS = [33, 31, 29, 15, 13, 11]
GPIO.setmode(GPIO.BOARD)
```

Run a test with:

```bash
sudo python3 ~/matrix_blink_test.py
```

The test is independent of TouchDesigner, OSC, and the sensor.
