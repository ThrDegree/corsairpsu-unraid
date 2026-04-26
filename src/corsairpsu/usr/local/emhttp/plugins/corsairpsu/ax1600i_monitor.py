#!/usr/bin/env python3
"""
Corsair AX1600i PSU monitor - reads stats the same way iCUE does.

Protocol: Silicon Labs USB chip with custom Corsair firmware (VID:1b1c PID:1c11).
Commands use Manchester-nibble encoding over USB bulk transfers (EP 0x02 out, 0x82 in).
The device is NOT a standard CP210x UART bridge — do not use pyserial.

Usage:
    python3 ax1600i_monitor.py                        # human-readable one-shot
    python3 ax1600i_monitor.py --json                 # JSON one-shot (for testing)
    python3 ax1600i_monitor.py --daemon               # persistent daemon (normal use)
    python3 ax1600i_monitor.py --daemon --interval 5  # poll every 5 seconds

Daemon mode holds the USB connection open and writes JSON to DAEMON_OUTPUT on
each poll interval. status.php reads that file instead of spawning this script,
which eliminates per-refresh USB resets and syslog noise.
"""

import os
import sys
import json
import time
import signal
import usb.core
import usb.util

# Corsair AX1600i USB identifiers
CORSAIR_VID = 0x1b1c
AX1600I_PID = 0x1c11
EP_OUT = 0x02
EP_IN  = 0x82

# Encode table: nibble → wire byte
ENCODE = [0x55, 0x56, 0x59, 0x5a, 0x65, 0x66, 0x69, 0x6a,
          0x95, 0x96, 0x99, 0x9a, 0xa5, 0xa6, 0xa9, 0xaa]

# Decode table (inverse): wire byte → nibble
DECODE = {}
for _i, _enc in enumerate(ENCODE):
    DECODE[_enc] = _i

# PSU type → number of PCIe channels (total = pcie_channels + 2 for ATX + Peripheral)
PSU_CHANNELS = {
    'AX760i':  6,
    'AX860i':  6,
    'AX1200i': 8,
    'AX1500i': 10,
    'AX1600i': 10,
}

RAIL_PAGES = [
    (0, '+12V'),
    (1, '+5V'),
    (2, '+3.3V'),
    (3, '-12V'),
    (4, '+5Vsb'),
]

REG_PAGE      = 0x00
REG_VOUT      = 0x8b
REG_IOUT      = 0x8c
REG_VIN       = 0x88
REG_IIN       = 0x89
REG_TEMP      = 0x8e
REG_FAN_SPEED = 0x90
REG_FAN_PCT   = 0x3b
REG_FAN_MODE  = 0xf0
REG_POWER_CAL = 0x96
REG_PIN       = 0xee  # measured AC input power (more accurate than VIN*IIN)
REG_12V_PAGE  = 0xe7
REG_12V_IOUT  = 0xe8
REG_12V_POUT  = 0xe9
REG_12V_OCP   = 0xea
REG_PSU_NAME  = 0x9a

DAEMON_OUTPUT   = '/tmp/corsairpsu_ax1600i.json'
DAEMON_PID_FILE = '/tmp/corsairpsu_ax1600i.pid'
DAEMON_INTERVAL = 2  # seconds between polls


def encode_wire(data: bytes) -> bytes:
    """Encode raw command bytes for the USB wire."""
    out = bytearray()
    out.append(ENCODE[0] & 0xfc)  # 0x54: frame start marker
    for b in data:
        out.append(ENCODE[b & 0xf])  # low nibble first
        out.append(ENCODE[b >> 4])   # high nibble second
    out.append(0x00)                 # frame terminator
    return bytes(out)


def decode_wire(raw: bytes) -> bytes:
    """Decode a complete wire response (0xa8...0x00) into payload bytes."""
    out = bytearray()
    i = 0
    while i < len(raw) and raw[i] != 0xa8:
        i += 1
    i += 1  # skip 0xa8
    while i + 1 < len(raw):
        b0, b1 = raw[i], raw[i + 1]
        if b0 == 0x00:
            break
        lo = DECODE.get(b0, 0)
        hi = DECODE.get(b1, 0)
        out.append(lo | (hi << 4))
        i += 2
    return bytes(out)


def linear11(data: bytes) -> float:
    """Convert 2-byte Corsair linear format to float (cpsumon convert_byte_float)."""
    p1 = (data[1] >> 3) & 31
    if p1 > 15:
        p1 -= 32
    p2 = ((data[1] & 7) * 256) + data[0]
    if p2 > 1024:
        p2 = -(65536 - (p2 | 63488))
    return float(p2) * (2.0 ** p1)


class AX1600i:
    def __init__(self, timeout: int = 2000):
        self.timeout = timeout
        self.dev = usb.core.find(idVendor=CORSAIR_VID, idProduct=AX1600I_PID)
        if self.dev is None:
            raise RuntimeError("AX1600i not found (VID:1b1c PID:1c11)")

        # Detach any kernel driver (e.g. cp210x bound by mistake)
        if self.dev.is_kernel_driver_active(0):
            self.dev.detach_kernel_driver(0)

        self.dev.set_configuration()
        usb.util.claim_interface(self.dev, 0)
        self.dev.reset()

        # Vendor control transfer required to initialize the device before bulk I/O
        self.dev.ctrl_transfer(0x40, 0x02, 0x0002, 0, b'')

        self.psu_name = None
        self.pcie_channels = 10

    def close(self):
        usb.util.release_interface(self.dev, 0)
        usb.util.dispose_resources(self.dev)

    def _send(self, data: bytes):
        time.sleep(0.005)  # 5ms before every write (matches Jon0's implementation)
        self.dev.write(EP_OUT, encode_wire(data), timeout=self.timeout)

    def _recv_raw(self, nbytes: int) -> bytes:
        wire_len = nbytes * 2 + 2  # 0xa8 + encoded pairs + 0x00
        return bytes(self.dev.read(EP_IN, wire_len, timeout=self.timeout))

    def _recv(self, nbytes: int) -> bytes:
        return decode_wire(self._recv_raw(nbytes))

    def _discard_ack(self):
        """Discard a bare 2-byte ACK (0xa8 0x00)."""
        self.dev.read(EP_IN, 2, timeout=self.timeout)

    def _discard_ok(self):
        """Discard the 2-decoded-byte OK response to the 0x12 intermediate command."""
        self.dev.read(EP_IN, 64, timeout=self.timeout)

    def _write_register(self, reg: int, value: int):
        cmd = bytes([0x13, 0x01, 0x04, 0x02, reg, value])
        self._send(cmd)
        self._discard_ack()
        self._send(bytes([0x12]))
        self._discard_ok()

    def _read_register(self, reg: int, length: int) -> bytes:
        self._send(bytes([0x13, 0x03, 0x06, 0x01, 0x07, length, reg]))
        self._discard_ack()
        self._send(bytes([0x12]))
        self._discard_ok()
        self._send(bytes([0x08, 0x07, length]))
        return self._recv(length)

    def _read_float(self, reg: int) -> float:
        data = self._read_register(reg, 2)
        if len(data) < 2:
            return 0.0
        return linear11(data[:2])

    def _read_byte(self, reg: int) -> int:
        data = self._read_register(reg, 1)
        return data[0] if data else 0

    def set_main_page(self, page: int):
        self._write_register(REG_PAGE, page)

    def set_12v_channel(self, channel: int):
        self._write_register(REG_12V_PAGE, channel)

    def setup(self):
        """Initialize the USB dongle and identify the PSU."""
        # Dongle setup command: {17, 2, 100, 0, 0, 0, 0}
        self._send(bytes([0x11, 0x02, 0x64, 0x00, 0x00, 0x00, 0x00]))
        self._discard_ack()

        name_bytes = self._read_register(REG_PSU_NAME, 7)
        self.psu_name = name_bytes.rstrip(b'\x00').decode('ascii', errors='replace')
        self.pcie_channels = PSU_CHANNELS.get(self.psu_name, 10)

    def read_input(self) -> dict:
        self.set_main_page(0)
        vin  = self._read_float(REG_VIN)
        iin  = self._read_float(REG_IIN)
        # The device only updates PIN (0xee) correctly after VOUT, IOUT, and PCAL
        # have been read on page 0 — confirmed by --diag showing VOUT→IOUT→PCAL→PIN
        # gives 200W (matching UPS), while skipping PCAL gives wrong values.
        self._read_float(REG_VOUT)
        self._read_float(REG_IOUT)
        self._read_float(REG_POWER_CAL)
        pin  = self._read_float(REG_PIN)
        temp = self._read_float(REG_TEMP)
        fan  = self._read_float(REG_FAN_SPEED)
        fan_pct  = self._read_byte(REG_FAN_PCT)
        fan_mode = self._read_byte(REG_FAN_MODE)
        return {
            'vin_v':    round(vin, 3),
            'iin_a':    round(iin, 3),
            'pin_w':    round(pin, 1),
            'temp_c':   round(temp, 2),
            'fan_rpm':  round(fan, 0),
            'fan_pct':  fan_pct,
            'fan_mode': 'fixed' if fan_mode else 'auto',
        }

    def read_rails(self) -> list:
        rails = []
        for page, name in RAIL_PAGES:
            self.set_main_page(page)
            vout = self._read_float(REG_VOUT)
            iout = self._read_float(REG_IOUT)
            if page in (1, 2):
                pwr_reg = self._read_float(REG_POWER_CAL)
                # PCAL returns 0 on some rails (e.g. +3.3V on AX1600i); fall back to V*I
                pout = (pwr_reg + vout * iout) / 2.0 if pwr_reg > 0 else vout * iout
            else:
                pout = vout * iout
            rails.append({
                'name':   name,
                'vout_v': round(vout, 4),
                'iout_a': round(iout, 4),
                'pout_w': round(pout, 2),
            })
        return rails

    def read_12v_channels(self) -> list:
        channels = []
        total = self.pcie_channels + 2  # +2 for ATX and Peripheral
        self.set_main_page(0)
        for i in range(1, total + 1):
            self.set_12v_channel(i)
            voltage = self._read_float(REG_VOUT)
            current = self._read_float(REG_12V_IOUT)
            power   = self._read_float(REG_12V_POUT)

            if i <= self.pcie_channels:
                label = f'PCIe-{i}'
            elif i == self.pcie_channels + 1:
                label = 'ATX'
            else:
                label = 'Peripheral'

            # Fall back to V*I if power register returns 0
            if power == 0.0 and current > 0.0:
                power = voltage * current
            channels.append({
                'label':  label,
                'vout_v': round(voltage, 3),
                'iout_a': round(current, 4),
                'pout_w': round(power, 2),
            })
        return channels

    def read_all(self) -> dict:
        inp      = self.read_input()
        rails    = self.read_rails()
        channels = self.read_12v_channels()
        pout_total = sum(r['pout_w'] for r in rails)
        pin = inp['pin_w'] or 1.0
        efficiency = round((pout_total / pin) * 100.0, 1)
        return {
            'psu_model':      self.psu_name,
            'input':          inp,
            'pout_total_w':   round(pout_total, 1),
            'efficiency_pct': round(efficiency, 1),
            'rails':          rails,
            'channels_12v':   channels,
        }


def linear11_steps(data: bytes) -> tuple:
    """Return (p1, p2, value) showing decode steps for diagnostics."""
    p1 = (data[1] >> 3) & 31
    if p1 > 15:
        p1 -= 32
    p2 = ((data[1] & 7) * 256) + data[0]
    if p2 > 1024:
        p2 = -(65536 - (p2 | 63488))
    return p1, p2, float(p2) * (2.0 ** p1)


def run_diag(psu: 'AX1600i'):
    print(f"\n=== AX1600i Diagnostic ({psu.psu_name}) ===\n")

    regs = [
        (REG_VOUT,      '0x8b', 'VOUT'),
        (REG_IOUT,      '0x8c', 'IOUT'),
        (REG_POWER_CAL, '0x96', 'PCAL'),
        (REG_PIN,       '0xee', 'PIN '),
    ]

    for page, name in RAIL_PAGES:
        psu.set_main_page(page)
        print(f"Page {page} ({name}):")
        for reg, addr, label in regs:
            raw = psu._read_register(reg, 2)
            if len(raw) >= 2:
                p1, p2, val = linear11_steps(raw)
                print(f"  {label} ({addr}): {raw.hex()}  p1={p1:+d}  p2={p2:6}  → {val:.4f}")
            else:
                print(f"  {label} ({addr}): short read ({raw.hex()})")
        print()


def print_summary(data: dict):
    inp = data['input']
    print(f"\n=== {data['psu_model']} ===")
    print(f"  Input:      {inp['vin_v']:.1f}V  {inp['iin_a']:.3f}A  →  {inp['pin_w']:.1f}W")
    print(f"  Output:     {data['pout_total_w']:.1f}W  ({data['efficiency_pct']:.1f}% efficient)")
    print(f"  Temp:       {inp['temp_c']:.1f}°C")
    fan_info = f"{inp['fan_rpm']:.0f} RPM"
    if inp['fan_mode'] == 'fixed':
        fan_info += f" (fixed {inp['fan_pct']}%)"
    else:
        fan_info += " (auto)"
    print(f"  Fan:        {fan_info}")
    print()
    print("  Output Rails:")
    for r in data['rails']:
        print(f"    {r['name']:8s}  {r['vout_v']:7.3f}V  {r['iout_a']:7.4f}A  {r['pout_w']:7.2f}W")
    print()
    loaded = [c for c in data['channels_12v'] if c['iout_a'] > 0.01]
    if loaded:
        print("  12V Channels (active):")
        for c in loaded:
            print(f"    {c['label']:12s}  {c['iout_a']:7.4f}A  {c['pout_w']:7.2f}W")
    print()


def run_daemon(interval: int = DAEMON_INTERVAL, output_file: str = DAEMON_OUTPUT):
    """Hold the USB connection open and write JSON on each poll interval."""

    # Prevent duplicate instances
    if os.path.exists(DAEMON_PID_FILE):
        try:
            existing_pid = int(open(DAEMON_PID_FILE).read().strip())
            os.kill(existing_pid, 0)
            print(f"Daemon already running (PID {existing_pid})", file=sys.stderr)
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # stale PID file

    with open(DAEMON_PID_FILE, 'w') as f:
        f.write(str(os.getpid()))

    def cleanup(signum=None, frame=None):
        try:
            os.unlink(DAEMON_PID_FILE)
        except OSError:
            pass
        try:
            os.unlink(output_file)
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    print(f"AX1600i daemon starting (PID {os.getpid()}, interval {interval}s)", file=sys.stderr)

    psu = None
    while True:
        try:
            if psu is None:
                psu = AX1600i()
                psu.setup()
                print(f"Connected to {psu.psu_name}", file=sys.stderr)

            data = psu.read_all()
            data['timestamp'] = time.time()

            tmp = output_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, output_file)  # atomic write

        except Exception as e:
            print(f"AX1600i daemon error: {e}", file=sys.stderr)
            try:
                with open(output_file, 'w') as f:
                    json.dump({'error': str(e), 'timestamp': time.time()}, f)
            except OSError:
                pass

            if psu is not None:
                try:
                    psu.close()
                except Exception:
                    pass
                psu = None

            time.sleep(10)  # back off before reconnect attempt
            continue

        time.sleep(interval)


def main():
    args = sys.argv[1:]
    as_json     = '--json'   in args
    daemon_mode = '--daemon' in args

    interval    = DAEMON_INTERVAL
    output_file = DAEMON_OUTPUT

    if '--interval' in args:
        idx = args.index('--interval')
        try:
            interval = int(args[idx + 1])
        except (IndexError, ValueError):
            pass

    if '--output' in args:
        idx = args.index('--output')
        try:
            output_file = args[idx + 1]
        except IndexError:
            pass

    if daemon_mode:
        run_daemon(interval=interval, output_file=output_file)
        return

    diag_mode = '--diag' in args

    print("Connecting to AX1600i...", file=sys.stderr)
    psu = AX1600i()
    try:
        psu.setup()
        if diag_mode:
            run_diag(psu)
        elif as_json:
            print(json.dumps(psu.read_all(), indent=2))
        else:
            print_summary(psu.read_all())
    finally:
        psu.close()


if __name__ == '__main__':
    main()
