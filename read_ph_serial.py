#!/usr/bin/env python3
"""
Simple helper to read a PH monitor sketch output over serial.

Usage:
  python read_ph_serial.py /dev/ttyACM0
Optional env:
  PORT=/dev/ttyUSB0 python read_ph_serial.py
"""

import sys
import time
from typing import Optional

try:
    import serial  # type: ignore
except ImportError as exc:  # pragma: no cover
    print(
        "pyserial manquant. Installez-le via 'pip install pyserial'.", file=sys.stderr
    )
    raise


DEFAULT_BAUD = 9600


def read_loop(port: str, baudrate: int = DEFAULT_BAUD) -> None:
    try:
        ser = serial.Serial(port, baudrate, timeout=1)
    except serial.SerialException as exc:
        print(f"Impossible d'ouvrir {port}: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"Lecture sur {port} @ {baudrate} bauds (Ctrl+C pour quitter)")
    try:
        while True:
            line: bytes = ser.readline()
            if not line:
                continue
            text = line.decode(errors="ignore").strip()
            if text:
                print(f"[{time.strftime('%H:%M:%S')}] {text}")
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
    finally:
        ser.close()


def main(args: list[str]) -> None:
    port: Optional[str] = None
    if len(args) >= 2:
        port = args[1]
    else:
        # fallback: /dev/ttyACM0 si présent
        from pathlib import Path

        candidates = sorted(Path("/dev").glob("ttyACM*")) + sorted(
            Path("/dev").glob("ttyUSB*")
        )
        if len(candidates) == 1:
            port = str(candidates[0])
    if not port:
        print("Usage: python read_ph_serial.py /dev/ttyACMn", file=sys.stderr)
        sys.exit(1)
    read_loop(port)


if __name__ == "__main__":
    main(sys.argv)
