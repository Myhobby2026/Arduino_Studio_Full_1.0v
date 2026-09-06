"""Serial monitor backend (pyserial wrapper with a background reader thread).

The service never touches Tkinter: it pushes data through callbacks, and the UI
layer marshals those into the mainloop with ``after()``.  It is deliberately
separate from :mod:`arduino_studio.core.terminal_service` - the terminal runs
OS commands, this module talks to the board through a COM port.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .utils import ensure_dir, get_logger

__all__ = ["SerialService", "SerialError", "SerialPortInfo", "list_serial_ports", "LINE_ENDING_BYTES"]

LINE_ENDING_BYTES: dict[str, bytes] = {
    "None": b"",
    "LF": b"\n",
    "CR": b"\r",
    "CRLF": b"\r\n",
}


class SerialError(RuntimeError):
    """Serial port could not be opened/used, with a friendly message."""


@dataclass
class SerialPortInfo:
    """A COM port as reported by the operating system."""

    device: str
    description: str = ""
    manufacturer: str = ""
    hwid: str = ""
    name: str = ""
    serial_number: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None

    @property
    def label(self) -> str:
        parts = [self.device]
        human = self.description or self.manufacturer
        if human:
            parts.append(human)
        if self.vid and self.pid:
            parts.append(f"VID:PID={self.vid:04X}:{self.pid:04X}")
        return " - ".join(parts)

    @property
    def short_label(self) -> str:
        return self.device

    @property
    def looks_arduino(self) -> bool:
        """Guess whether this is an Arduino/ESP based on the VID."""
        # Arduino, Arduino.org,picoboard, WCH ch34x, Silicon Labs CP210x,
        # FTDI, Raspberry Pi, Espressif, Adafruit Feather/Its...
        known = {0x2341, 0x2A03, 0x04D8, 0x1A86, 0x10C4, 0x067B, 0x2E8A, 0x303A, 0x239A, 0x0403, 0x1FC9}
        if self.vid in known:
            return True
        text = f"{self.description} {self.manufacturer} {self.hwid}".lower()
        return any(word in text for word in ("arduino", "ch340", "cp210", "ftdi", "espressif", "usb-jtag", "wemos", "esp"))


def list_serial_ports() -> list[SerialPortInfo]:
    """Enumerate serial ports via pyserial (empty list when pyserial is absent)."""
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - dependency missing
        return []
    out: list[SerialPortInfo] = []
    try:
        for info in list_ports.comports():
            out.append(SerialPortInfo(
                device=str(info.device),
                description=str(info.description or ""),
                manufacturer=str(info.manufacturer or ""),
                hwid=str(info.hwid or ""),
                name=str(getattr(info, "name", "") or ""),
                serial_number=str(getattr(info, "serial_number", "") or ""),
                vid=info.vid,
                pid=info.pid,
            ))
    except Exception:  # pragma: no cover - driver enumeration can fail
        get_logger("serial").exception("port enumeration failed")
    out.sort(key=lambda p: natural_port_key(p.device))
    return out


def natural_port_key(device: str) -> tuple[int, str]:
    """Sort ``COM3`` before ``COM12`` (and ``/dev/ttyUSB*`` numerically)."""
    digits = "".join(ch for ch in device if ch.isdigit())
    return (int(digits) if digits else 0, device)


@dataclass
class SerialConfig:
    """Open parameters for the monitor."""

    port: str
    baud: int = 9600
    timeout: float = 0.2
    write_timeout: float = 2.0
    dtr: bool = True
    rts: bool = True
    rtscts: bool = False
    dsrdtr: bool = False
    encoding: str = "utf-8"
    reset_dtr_pulse: bool = True


class SerialService:
    """Opens a port, reads in a worker thread and hands bytes to callbacks."""

    def __init__(
        self,
        ui_post: Optional[Callable[[Callable[[], Any]], Any]] = None,
        on_data: Optional[Callable[[str, str], Any]] = None,
        on_state: Optional[Callable[[str, str], Any]] = None,
    ) -> None:
        self._log = get_logger("serial")
        self._post = ui_post or (lambda fn: fn())
        self._on_data = on_data
        self._on_state = on_state
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._port: Any = None
        self.config: Optional[SerialConfig] = None
        self.last_error: str = ""
        self.bytes_in = 0
        self.bytes_out = 0
        self.opened_at: Optional[datetime] = None
        self.paused = threading.Event()
        self._pause_buffer: list[tuple[str, bytes]] = []
        self._pause_limit = 4096

    # ------------------------------------------------------------- state
    @property
    def is_open(self) -> bool:
        with self._lock:
            return bool(self._port and getattr(self._port, "is_open", False))

    @property
    def active_port(self) -> str:
        with self._lock:
            return self.config.port if self.is_open and self.config else ""

    def holds_port(self, port: str) -> bool:
        """True when *port* is currently opened by this monitor."""
        with self._lock:
            return bool(self.is_open and self.config and self.config.port.strip().upper() == (port or "").strip().upper())

    # ----------------------------------------------------------- open/close
    def open(self, config: SerialConfig) -> None:
        """Open the port (raises :class:`SerialError` with an actionable message)."""
        try:
            import serial  # type: ignore[import-not-found]
        except Exception as exc:  # pragma: no cover - missing dependency
            raise SerialError(
                "pyserial is not installed. Run 'pip install pyserial' (or pip install -r requirements.txt)."
            ) from exc
        with self._lock:
            if self._port is not None and getattr(self._port, "is_open", False):
                if self.config and self.config.port == config.port and self.config.baud == config.baud:
                    raise SerialError(f"{config.port} is already open in the Serial Monitor.")
                self._close_locked()
            try:
                port = serial.Serial(
                    port=config.port,
                    baudrate=int(config.baud),
                    timeout=config.timeout,
                    write_timeout=config.write_timeout,
                    dtr=config.dtr,
                    rts=config.rts,
                    rtscts=config.rtscts,
                    dsrdtr=config.dsrdtr,
                )
            except Exception as exc:  # serial.SerialException + OSError
                raise SerialError(self._explain(exc, config)) from exc
            self._port = port
            self.config = config
            self.bytes_in = 0
            self.bytes_out = 0
            self.opened_at = datetime.now()
            self.last_error = ""
            self._stop.clear()
            self._pause_buffer.clear()
            self.paused.clear()
        self._emit_state("open", f"{config.port} @ {config.baud} baud")
        self._thread = threading.Thread(target=self._read_loop, name="ardu-serial-reader", daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._close_locked()
        self._emit_state("closed", "")

    def _close_locked(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        port, self._port = self._port, None
        if port is not None:
            try:
                if getattr(port, "is_open", False):
                    port.reset_input_buffer()
                    port.reset_output_buffer()
            except Exception:  # pragma: no cover - device may already be unplugged
                pass
            try:
                port.close()
            except Exception as exc:  # pragma: no cover
                self._log.debug("error closing port: %s", exc)
        self.config = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    def _explain(self, exc: Exception, config: SerialConfig) -> str:
        """Translate pyserial/OS errors into something a user can act on."""
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        hints = {
            "permissionerror": f"Access to {config.port} was denied. Close the Arduino IDE, other Serial Monitors or any app using that port.",
            "access is denied": f"{config.port} is in use by another program (Arduino IDE? another monitor?). Close it and retry.",
            "file is already open": "This monitor already holds the port - close it first.",
            "could not exclusively open": f"{config.port} is exclusively held by another application.",
            "the system cannot find the file": f"{config.port} does not exist. Unplug/replug the board and refresh the port list.",
            "twinkie": "The USB device disappeared while opening the port.",
            "invalid comport": "That port name is not valid on this system.",
        }
        extra = next((hint for key, hint in hints.items() if key in lowered), "")
        return f"Cannot open {config.port} at {config.baud} baud.\n{text}" + (f"\n\n{extra}" if extra else "")

    # ------------------------------------------------------------ reading
    def _read_loop(self) -> None:
        buffer = bytearray()
        idle_rounds = 0
        while not self._stop.is_set():
            port = self._port
            if port is None:
                break
            try:
                if not getattr(port, "is_open", False):
                    break
                pending = int(getattr(port, "in_waiting", 0) or 0)
                chunk = port.read(pending if pending else 1)
            except Exception as exc:  # device unplugged, driver error, ...
                if not self._stop.is_set():
                    message = self._explain(exc, self.config or SerialConfig(port="?"))
                    self.last_error = message
                    self._log.warning("serial read stopped: %s", exc)
                    self._post(lambda: self._on_state and self._on_state("error", message))
                    self._close_locked_quiet()
                break
            if chunk:
                idle_rounds = 0
                buffer.extend(chunk)
                self.bytes_in += len(chunk)
                if len(buffer) > 65536:
                    self._flush_buffer(buffer)
                    buffer = bytearray()
                    continue
                # split on newlines so each UI line keeps its own timestamp
                if b"\n" in buffer or b"\r" in buffer:
                    self._flush_buffer(buffer)
                    buffer = bytearray()
            else:
                idle_rounds += 1
                if buffer and idle_rounds > 6:  # ~1.2 s without a newline: show what we have
                    self._flush_buffer(buffer)
                    buffer = bytearray()
                    idle_rounds = 0
        if buffer:
            self._flush_buffer(buffer)

    def _flush_buffer(self, buffer: bytearray) -> None:
        if not buffer:
            return
        for raw_line in _split_lines(bytes(buffer)):
            if not raw_line:
                continue
            if self.paused.is_set():
                self._pause_buffer.append(("rx", raw_line))
                if len(self._pause_buffer) > self._pause_limit:
                    self._pause_buffer.pop(0)
                continue
            self._emit_data("rx", raw_line)

    def _close_locked_quiet(self) -> None:
        try:
            with self._lock:
                self._close_locked()
        except Exception:  # pragma: no cover
            pass
        self._emit_state("closed", "port closed")

    def _emit_data(self, direction: str, raw: bytes) -> None:
        callback = self._on_data
        if callback is None:
            return
        config = self.config
        encoding = getattr(config, "encoding", "utf-8") if config else "utf-8"
        try:
            text = raw.decode(encoding, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        text = text.rstrip("\r\n")
        self._post(lambda: callback(text, direction))

    def _emit_state(self, state: str, message: str) -> None:
        callback = self._on_state
        if callback is not None:
            self._post(lambda: callback(state, message))

    # ------------------------------------------------------------ writing
    def write(self, text: str, line_ending: str = "None", encoding: str = "utf-8") -> int:
        """Send *text* (+ *line_ending*) to the board; returns byte count."""
        payload = text.encode(encoding, errors="replace") + LINE_ENDING_BYTES.get(line_ending, b"")
        with self._lock:
            port = self._port
            if port is None or not getattr(port, "is_open", False):
                raise SerialError("The serial port is not open.")
            try:
                written = port.write(payload)
                port.flush()
            except Exception as exc:
                raise SerialError(self._explain(exc, self.config or SerialConfig(port="?"))) from exc
            self.bytes_out += len(payload)
        self._emit_data("tx", payload)
        return int(written or len(payload))

    def send_bytes(self, data: bytes) -> None:
        """Raw write (used by the ``SEND`` buttons of the EEPROM example)."""
        with self._lock:
            port = self._port
            if port is None or not getattr(port, "is_open", False):
                raise SerialError("The serial port is not open.")
            port.write(bytes(data))
            port.flush()
            self.bytes_out += len(data)
        self._emit_data("tx", bytes(data))

    def set_flow_control(self, dtr: Optional[bool] = None, rts: Optional[bool] = None) -> None:
        """Toggle DTR/RTS (used to reset an Uno or enter ESP boot mode)."""
        with self._lock:
            port = self._port
            if port is None or not getattr(port, "is_open", False):
                return
            try:
                if dtr is not None:
                    port.dtr = bool(dtr)
                if rts is not None:
                    port.rts = bool(rts)
            except Exception as exc:
                self._log.debug("flow control failed: %s", exc)
                self.last_error = f"Cannot change DTR/RTS: {exc}"

    def pulse_dtr(self, hold: float = 0.25) -> None:
        """Reset the board the same way the Arduino IDE does before an upload."""
        self.set_flow_control(dtr=False)
        time.sleep(max(0.01, hold))
        self.set_flow_control(dtr=True)

    def enter_boot_mode(self) -> None:
        """ESP-style auto-program sequence (DTR/RTS pulse)."""
        self.set_flow_control(dtr=False, rts=True)
        time.sleep(0.2)
        self.set_flow_control(dtr=True, rts=False)
        time.sleep(0.2)
        self.set_flow_control(dtr=False, rts=False)

    # ------------------------------------------------------------- buffering
    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()
        pending, self._pause_buffer = list(self._pause_buffer), []
        for direction, raw in pending:
            self._emit_data(direction, raw)

    @property
    def paused_count(self) -> int:
        return len(self._pause_buffer)

    # -------------------------------------------------------- upload safety
    def close_for_upload(self, port: str = "") -> bool:
        """Close the monitor when it holds *port* (or any port if empty).

        Returns ``True`` when a port was actually released, so the caller can
        offer to reopen it after the upload.
        """
        with self._lock:
            if not (self._port and getattr(self._port, "is_open", False)):
                return False
            if port and self.config and self.config.port.strip().upper() != port.strip().upper():
                return False
            self._close_locked()
        self._emit_state("closed", f"released for upload on {port or 'all ports'}")
        return True

    @staticmethod
    def port_is_free(port: str) -> bool:
        """Try to open/close *port* quickly to see whether another app holds it."""
        if not port:
            return False
        try:
            import serial  # type: ignore[import-not-found]
        except Exception:
            return True  # cannot check without pyserial: assume usable
        try:
            test = serial.Serial(port, 9600, timeout=0.2)
            time.sleep(0.05)
            test.close()
            return True
        except Exception:
            return False

    def status_line(self) -> str:
        if not self.is_open or not self.config:
            return "Serial: closed"
        duration = ""
        if self.opened_at:
            seconds = int((datetime.now() - self.opened_at).total_seconds())
            duration = f" - {seconds // 60:d}m {seconds % 60:02d}s"
        return (f"Serial: {self.config.port} @ {self.config.baud}{duration} - "
                f"RX {self.bytes_in:,} B / TX {self.bytes_out:,} B")


def _split_lines(data: bytes) -> list[bytes]:
    """Split *data* on ``\\n``, ``\\r`` and ``\\r\\n``, keeping terminators attached.

    Keeping the terminator means the monitor can render exactly what the board
    sent (a bare ``\\r`` progress line stays a bare ``\\r`` line).
    """
    out: list[bytes] = []
    current = bytearray()
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte == 0x0A:  # LF
            current.append(byte)
            out.append(bytes(current))
            current = bytearray()
            index += 1
            continue
        if byte == 0x0D:  # CR (possibly followed by LF)
            current.append(byte)
            index += 1
            if index < length and data[index] == 0x0A:
                current.append(data[index])
                index += 1
            out.append(bytes(current))
            current = bytearray()
            continue
        current.append(byte)
        index += 1
    if current:
        out.append(bytes(current))
    return out


def save_log(lines: Iterable[str], path: os.PathLike[str] | str, header: str = "") -> Path:
    """Write monitor lines to *path*; returns the written path."""
    target = ensure_dir(Path(path).parent) / Path(path).name
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        if header:
            handle.write(header.rstrip("\n") + "\n")
            handle.write("-" * 60 + "\n")
        for line in lines:
            handle.write(line.rstrip("\r\n") + "\n")
    return target

