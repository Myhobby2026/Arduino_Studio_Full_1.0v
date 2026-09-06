"""Bootloader / programmer service for blank chips and raw firmware flashing.

AVR targets are handled the supported way - through ``arduino-cli
burn-bootloader`` - plus two extras that the CLI does not offer:

* *Read chip information* (signature, fuses, lock bits) through ``avrdude``, and
* *Flash firmware* (``.bin``) for ESP8266/ESP32 through the ``esptool`` that the
  installed core already ships.

Fuse values shown in the confirmation dialog are read from the installed core
(``fuses.xml``) whenever available, falling back to a small built-in table.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import process as proc
from .arduino_cli import ArduinoCLI
from .boards import esptool_chip_for_fqbn, family_for_fqbn, is_esp_fqbn, parse_fqbn
from .utils import ensure_dir, get_logger, human_bytes, natural_key

__all__ = [
    "BootloaderService",
    "BootloaderError",
    "Programmer",
    "ChipInfo",
    "FuseSet",
    "FlashPlan",
    "KNOWN_PROGRAMMERS",
    "FUSE_PRESETS",
    "AVR_PART_FOR_CHIP",
    "SIGNATURE_NAMES",
    "ESP_FLASH_OFFSETS",
    "parse_programmers_txt",
    "parse_boards_txt",
    "read_fuses_xml",
]

MAX_LOG_SNIPPET = 900


class BootloaderError(RuntimeError):
    """Raised when a flashing operation cannot be prepared or executed."""


@dataclass
class Programmer:
    """An ISP programmer as declared by a core (``programmers.txt``)."""

    id: str
    name: str
    protocol: str = ""
    speed: int = 0
    description: str = ""
    needs_port: bool = True
    source: str = ""
    pins: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.speed:
            return f"{self.name} ({self.protocol}, {self.speed} baud)"
        return f"{self.name} ({self.protocol})" if self.protocol else self.name

    @property
    def is_serial_programmer(self) -> bool:
        return (self.protocol or "").lower() in {"arduino", "gpio", "wiring", "zigbee_ffx6"}

    @property
    def wiring_help(self) -> str:
        """Short wiring note shown next to the selection."""
        return WIRING_HELP.get(self.protocol or self.id, "")


#: Programmers every AVR user should be able to pick, even before a core scan.
KNOWN_PROGRAMMERS: tuple[Programmer, ...] = (
    Programmer(id="arduino", name="Arduino as ISP", protocol="arduino", speed=19200,
               description="Another Arduino running the ArduinoISP sketch (COM port required).",
               source="built-in"),
    Programmer(id="arduinoisp", name="Arduino as ISP (ATmega32U4 board)", protocol="arduino", speed=57600,
               description="For Leonardo/Micro based programmers (different reset timing).", source="built-in"),
    Programmer(id="usbasp", name="USBasp", protocol="usb", speed=0, needs_port=False,
               description="Cheap AVR ISP dongle (libusb driver on Windows).", source="built-in"),
    Programmer(id="avrispv2", name="Atmel-ICE / AVRISP mkII", protocol="ice", speed=0, needs_port=False,
               description="Official Atmel programmer - use avrispmkII for the classic mkII stick.", source="built-in"),
    Programmer(id="avrispmkII", name="AVRISP mkII (USB)", protocol="mkII", speed=0, needs_port=False,
               description="AVRISP mkII in ISP mode.", source="built-in"),
    Programmer(id="usbtiny", name="USBTinyISP", protocol="usb", speed=0, needs_port=False,
               description="TinyUSB based programmer (needs the libusb filter driver).", source="built-in"),
    Programmer(id="usbtiny", name="USBtiny (alternate id)", protocol="usb", speed=0, needs_port=False,
               description="Alias used by some clones/cores.", source="built-in"),
    Programmer(id="pickit2", name="PICkit 2", protocol="pickit2", speed=0, needs_port=False,
               description="Microchip PICkit 2 in AVR mode.", source="built-in"),
    Programmer(id="ft245r", name="FT232R bitbang", protocol="ft245r", speed=0, needs_port=True,
               description="Bit-banged ISP over an FTDI cable.", source="built-in"),
    Programmer(id="linuxgpio", name="Linux GPIO (bitbang)", protocol="gpio", speed=0, needs_port=False,
               description="Bit-banged ISP from a Raspberry Pi GPIO header.", source="built-in"),
    Programmer(id="custom", name="Custom programmer (advanced)", protocol="", speed=0,
               needs_port=True, description="Type an avrdude programmer id, e.g. usbasp, stk500v2, dt006mini.",
               source="app"),
)

WIRING_HELP: dict[str, str] = {
    "arduino": (
        "Arduino as ISP wiring (6-pin ICSP, target <- programmer):\n"
        "  MOSI  -> pin 11 (Uno/Nano)      MISO -> pin 12\n"
        "  SCK   -> pin 13                  RESET -> pin 10\n"
        "  5V    -> VCC of the target       GND  -> GND\n"
        "Burn the 'Arduino as ISP' sketch into the programmer board first (NOT 'ISP').\n"
        "For a 3.3 V target do not feed 5 V - power the chip at its own voltage."
    ),
    "usb": (
        "USBasp / USBtiny -> 6-pin ICSP header (keyed corner = pin 1):\n"
        "  pin 1 MISO, pin 2 VCC, pin 3 SCK, pin 4 MOSI, pin 5 RESET, pin 6 GND\n"
        "Keep wires shorter than 15 cm and add a 100 nF capacitor on RESET to GND if the chip is flaky."
    ),
    "mkII": (
        "AVRISP mkII / Atmel-ICE uses the standard 2x3 ISP connector (AVR 6-pin, ISP-10 for some).\n"
        "Pin 1 = MISO, pin 2 = VCC, pin 4 = MOSI, pin 10 = GND; supply target voltage 1.8-5.5 V."
    ),
    "ice": (
        "Atmel-ICE / PICKIT: connect the 6-pin ISP ribbon (pin 1 marked with the triangle) and power the "
        "target from the programmer, not from USB, when fuses change the clock."
    ),
    "gpio": "Bit-bang ISP: 100-470 ohm resistors in series on MOSI/MISO/SCK/RESET, 10 k pulldown on MOSI.",
}

#: avrdude part numbers for the chips the manager exposes.
AVR_PART_FOR_CHIP: dict[str, str] = {
    "ATmega328P": "m328p",
    "ATmega328PB": "m328pb",
    "ATmega328": "m328",
    "ATmega168": "m168",
    "ATmega88": "m88",
    "ATmega644P": "m644p",
    "ATmega1284P": "m1284p",
    "ATmega1280": "m1280",
    "ATmega2560": "m2560",
    "ATmega32U4": "m32u4",
    "ATmega16U2": "m16u2",
    "ATtiny24": "t24",
    "ATtiny44": "t44",
    "ATtiny84": "t84",
    "ATtiny84A": "t84a",
    "ATtiny25": "t25",
    "ATtiny45": "t45",
    "ATtiny85": "t85",
    "ATtiny167": "t167",
}

#: Known device signatures (avrdude -C ... -p ... -U signature:r).
SIGNATURE_NAMES: dict[str, str] = {
    "1E950F": "ATmega328P",
    "1E9513": "ATmega328PB",
    "1E9514": "ATmega328PA?",
    "1E940B": "ATmega48PB",
    "1E9406": "ATmega168",
    "1E9307": "ATmega8A/88 family check",
    "1E9602": "ATmega644P/644PA",
    "1E9705": "ATmega1284P",
    "1E9703": "ATmega1280",
    "1E9801": "ATmega2560/2561",
    "1E9587": "ATmega32U4",
    "1E9488": "ATmega16U2",
    "1E930B": "ATtiny25/45/85 family",
    "1E9207": "ATtiny44",
    "1E930C": "ATtiny84",
    "1E9406 ": "ATmega168",
    "1E9108": "ATtiny25",
    "1E9206": "ATtiny45",
    "1E95F1": "ATtiny816 (UPDI)",
}

#: Clock -> fuse bytes.  Values follow the classic Arduino bootloader configs and
#: are only used when the installed core does not provide ``fuses.xml``.
FUSE_PRESETS: dict[str, list[dict[str, Any]]] = {
    "ATmega328P": [
        {"clock": "16 MHz external crystal", "low": "0xFF", "high": "0xDE", "extended": "0x05",
         "lock": "0x0F", "bootloader": "optiboot_atmega328.hex", "boot_size": "0x800",
         "note": "Arduino Uno / Nano behaviour (2048 byte bootloader, reset enabled)."},
        {"clock": "16 MHz external crystal", "low": "0xFF", "high": "0xDA", "extended": "0x05",
         "lock": "0x0F", "bootloader": "ATmegaBOOT_168_atmega328.hex", "boot_size": "0xE00",
         "note": "Old (pre-OptiBoot) Diecimila/Nano bootloader with 512 word boot section."},
        {"clock": "8 MHz internal oscillator", "low": "0xE2", "high": "0xD9", "extended": "0xFF",
         "lock": "0xFF", "bootloader": "none", "boot_size": "0x0",
         "note": "Factory-like 8 MHz internal clock, no bootloader: upload with a programmer."},
        {"clock": "1 MHz internal oscillator", "low": "0x62", "high": "0xD9", "extended": "0xFF",
         "lock": "0xFF", "bootloader": "none", "boot_size": "0x0",
         "note": "Slow internal RC - handy to wake up a mis-fused chip; F=1 MHz, UART unusable."},
    ],
    "ATmega328PB": [
        {"clock": "16 MHz external crystal", "low": "0xFF", "high": "0xDE", "extended": "0x05",
         "lock": "0x0F", "bootloader": "optiboot_atmega328.hex", "boot_size": "0x800",
         "note": "Same fuse layout as 328P; needs a core that knows the 328PB signature."},
        {"clock": "8 MHz internal oscillator", "low": "0xE2", "high": "0xD9", "extended": "0xFD",
         "lock": "0xFF", "bootloader": "none", "boot_size": "0x0", "note": "No bootloader, PSCRB may need setting."},
    ],
    "ATmega2560": [
        {"clock": "16 MHz external crystal", "low": "0xFF", "high": "0xD8", "extended": "0xFD",
         "lock": "0x0F", "bootloader": "atmega2560_std_fw.hex", "boot_size": "0x1000",
         "note": "Arduino Mega 2560 reference values (stkbd/stk500v2 bootloader)."},
    ],
    "ATmega32U4": [
        {"clock": "16 MHz external crystal", "low": "0xFF", "high": "0xD8", "extended": "0xCB",
         "lock": "0xEF", "bootloader": "Caterina-Arduino Micro.hex", "boot_size": "0x1000",
         "note": "Leonardo/Micro: USB bootloader; HB = 0xD8 with 4 kB boot section."},
    ],
    "ATtiny85": [
        {"clock": "1 MHz internal oscillator", "low": "0x62", "high": "0xFF", "extended": "0xFF",
         "lock": "0x3F", "bootloader": "none", "boot_size": "0x0",
         "note": "Digispark-style default (no bootloader, program via ISP or USB)."},
        {"clock": "8 MHz internal oscillator", "low": "0xE2", "high": "0xDF", "extended": "0xFF",
         "lock": "0x3F", "bootloader": "none", "boot_size": "0x0",
         "note": "8 MHz internal, BOD 2.7 V; slow down SCK if powered at 3.3 V."},
        {"clock": "16.5 MHz internal PLL", "low": "0xF1", "high": "0xDF", "extended": "0xFF",
         "lock": "0x3F", "bootloader": "none", "boot_size": "0x0",
         "note": "PLL clock: 4 MHz for serial/soft-serial timing; only works above ~2.7 V."},
    ],
    "ATtiny84": [
        {"clock": "8 MHz internal oscillator", "low": "0xE2", "high": "0xDF", "extended": "0xFF",
         "lock": "0x3F", "bootloader": "none", "boot_size": "0x0", "note": "ATTinyCore default for ATtiny84."},
    ],
    "ATtiny44": [
        {"clock": "8 MHz internal oscillator", "low": "0xE2", "high": "0xDF", "extended": "0xFF",
         "lock": "0x3F", "bootloader": "none", "boot_size": "0x0", "note": "ATTinyCore default for ATtiny44."},
    ],
}

#: Flash addresses offered for ESP boards.
ESP_FLASH_OFFSETS: dict[str, tuple[tuple[str, str], ...]] = {
    "esp32": (
        ("0x1000", "Bootloader (recommended app start for Arduino core: 0x0 -> combined bin)"),
        ("0x0", "Whole image (bootloader + partition table + app merged .bin)"),
        ("0x8000", "Partition table"),
        ("0xE000", "NVS (settings) - only for expert use"),
        ("0x10000", "Application (standalone .ino binary)"),
    ),
    "esp8266": (
        ("0x0", "Flash start (4 MB modules - normal for NodeMCU v3/lua)"),
        ("0x1000", "Bootloader offset (old 512 kB / 1 MB modules)"),
        ("0x10000", "Sketch (sdk-based images)"),
        ("0x3FC000", "SDK parameters (rare)"),
    ),
}

ESP_FLASH_MODES: tuple[str, ...] = ("dio", "qio", "dout", "qout")
ESP_FLASH_SIZES: tuple[str, ...] = ("detect", "512KB", "1MB", "2MB", "4MB", "8MB", "16MB")
ESP_FLASH_FREQUENCIES: tuple[str, ...] = ("40m", "26m", "20m", "80m")
ESP_BAUD_RATES: tuple[int, ...] = (115200, 230400, 460800, 921600, 1500000, 2000000, 3000000, 4608000)

_FUSE_ATTR_KEYS = {
    "low": ("low", "lfuse", "low_fuse"),
    "high": ("high", "hfuse", "high_fuse"),
    "extended": ("extended", "efuse", "ext", "extended_fuse", "fuses"),
    "lock": ("lock", "lockbits", "lockbits?", "lockbit", "lock_bits"),
    "bootloader": ("bootloaderfile", "bootloader", "bootloader_file"),
    "size": ("bootloadersize", "bootloader_size", "size"),
}


@dataclass
class FuseSet:
    """Fuse bytes for one chip/clock combination (with provenance)."""

    chip: str
    clock: str
    #: empty means "unknown" on purpose: a default of ``0x00`` would look like a
    #: real profile and could be programmed into a chip that then stops running.
    low: str = ""
    high: str = ""
    extended: str = ""
    lock: str = ""
    bootloader: str = ""
    boot_size: str = ""
    note: str = ""
    source: str = "built-in table"
    raw: dict[str, str] = field(default_factory=dict)
    matched: bool = True

    @property
    def complete(self) -> bool:
        """True when the profile actually carries fuse bytes (not just placeholders)."""
        pattern = re.compile(r"^(0x)?[0-9a-fA-F]{2}$")
        return bool(pattern.match((self.low or "").strip())
                    and pattern.match((self.high or "").strip()))

    @property
    def summary(self) -> str:
        def show(value: str) -> str:
            value = (value or "").strip()
            return value if value else "unknown"

        return (f"Low {show(self.low)}  High {show(self.high)} "
                f"Extended {show(self.extended)}  Lock {show(self.lock)}")

    @property
    def has_bootloader(self) -> bool:
        return bool(self.bootloader) and self.bootloader.lower() not in {"none", "no", "-"}

    def warnings(self) -> list[str]:
        """Warnings shown in the confirmation dialog."""
        out: list[str] = []
        if not self.complete:
            out.append("No verified fuse values for this chip/profile - install or update the AVR core "
                       "(it ships fuses.xml) or type the bytes yourself instead of programming defaults.")
        lock = self.lock.lower().replace("0x", "")
        try:
            value = int(lock, 16) if lock else 0
        except ValueError:
            value = 0
        if value and (value & 0x03) != 0x03:
            out.append("Lock bits restrict memory read/write - double check before programming them.")
        if "internal" in self.clock.lower() and "16" not in self.clock:
            out.append("Internal RC clock: the actual speed may differ up to ~10 % from the value shown.")
        if not self.has_bootloader:
            out.append("No bootloader in this profile: Serial uploads will not work afterwards, only ISP.")
        return out


@dataclass
class ChipInfo:
    """What ``avrdude`` reported about the connected target."""

    ok: bool = False
    part_code: str = ""
    signature: str = ""
    signature_name: str = ""
    fuses: dict[str, str] = field(default_factory=dict)
    lock: str = ""
    calibration: str = ""
    output: str = ""
    error: str = ""
    device: str = ""
    avr_fuses: dict[str, str] = field(default_factory=dict)

    @property
    def chip_name(self) -> str:
        return self.signature_name or self.device or "unknown device"

    def formatted(self) -> str:
        lines = []
        if self.device:
            lines.append(f"Device id     : {self.device}")
        lines.append(f"Signature     : {('0x' + self.signature) if self.signature else 'not read'}"
                     + (f"  ({self.signature_name})" if self.signature_name else ""))
        for key in ("lfuse", "hfuse", "efuse"):
            if self.fuses.get(key):
                lines.append(f"{key.ljust(13)}: {self.fuses[key]}")
        if self.lock:
            lines.append(f"{'lock bits'.ljust(13)}: {self.lock}")
        if self.calibration:
            lines.append(f"{'calibration'.ljust(13)}: {self.calibration}")
        return "\n".join(lines)


@dataclass
class FlashPlan:
    """A prepared command line that the UI can show before running it."""

    kind: str                  # burn-bootloader | read-chip | backup | flash | erase | verify-flash
    argv: list[str]
    human: str
    target: str
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_file: str = ""

    @property
    def is_dangerous(self) -> bool:
        return self.kind in {"burn-bootloader", "flash", "erase", "backup"}


def parse_programmers_txt(path: os.PathLike[str] | str) -> list[Programmer]:
    """Parse an Arduino core ``programmers.txt`` file into :class:`Programmer`s."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        parts = key.split(".")
        if len(parts) < 2 or not parts[0]:
            continue
        pid = parts[0]
        entry = found.setdefault(pid, {"id": pid})
        attr = ".".join(parts[1:]).lower()
        if attr == "name":
            entry["name"] = value
        elif attr == "protocol":
            entry["protocol"] = value
        elif attr in {"speed", "programming.speed"}:
            entry["speed"] = value
        elif attr in {"description", "communication", "auth"}:
            entry.setdefault("extra", "")
            entry["extra"] = f"{entry['extra']} {attr}={value}".strip()
        elif attr.startswith("programming.pin"):
            pins = entry.setdefault("pins", "")
            pin_name = attr.replace("programming.pin.", "").replace("programming.pin", "").strip()
            entry[pin_name] = value
            if pins is not None and isinstance(pins, str) and not pin_name:
                entry["pins"] = value
    out: list[Programmer] = []
    for pid, entry in found.items():
        pins = {k: v for k, v in entry.items() if k.startswith(("MOSI", "MISO", "SCK", "RESET", "reset", "sck"))}
        out.append(Programmer(
            id=pid,
            name=entry.get("name") or pid,
            protocol=entry.get("protocol", ""),
            speed=_to_int(entry.get("speed", "")),
            description=entry.get("extra", ""),
            source=str(target),
            pins=pins,
        ))
    out.sort(key=lambda p: natural_key(p.name.lower()))
    return out


def parse_boards_txt(path: os.PathLike[str] | str, board_id: str = "") -> dict[str, Any]:
    """Parse ``boards.txt`` for one board id (menu options + a few properties)."""
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    menus: dict[str, dict[str, Any]] = {}
    props: dict[str, str] = {}
    menu_labels: dict[str, str] = {}
    prefix = (board_id + ".") if board_id else ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        parts = key.split(".")
        if parts[0] == "menu" and len(parts) == 2:      # global menu label: "menu.cpu=Processor"
            menu_labels[parts[1]] = value
            continue
        if board_id:
            if not key.startswith(prefix):
                continue
            parts = parts[1:]
        if len(parts) >= 3 and parts[0] == "menu":
            menu_id, option = parts[1], parts[2]
            attribute = ".".join(parts[3:])
            record = menus.setdefault(menu_id, {"label": menu_labels.get(menu_id, menu_id), "options": {}})
            entry = record["options"].setdefault(option, {"id": option})
            if attribute:
                entry[attribute] = value
            else:
                entry["label"] = value
        elif parts:
            props[".".join(parts)] = value
    ordered: list[dict[str, Any]] = []
    for menu_id, record in menus.items():
        options = sorted(record["options"].items(), key=lambda kv: kv[0])
        ordered.append({
            "id": menu_id,
            "label": record.get("label") or menu_id,
            "options": [
                {"id": oid, "label": opt.get("label", oid), **{k: v for k, v in opt.items() if k not in {"id", "label"}}}
                for oid, opt in options
            ],
        })
    return {"menus": ordered, "props": props, "board": board_id, "file": str(target)}


def read_fuses_xml(path: os.PathLike[str] | str, chip_hint: str = "") -> list[FuseSet]:
    """Read the AVR core's ``fuses.xml`` (best-effort attribute matching).

    The file lists one ``<fuse/>`` element per board/variant with the fuse bytes
    the official bootloader was built for.  Attribute spelling has drifted over
    core versions, so we look for candidates instead of a fixed schema.
    """
    target = Path(path)
    try:
        root = ET.fromstring(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ET.ParseError):
        return []
    out: list[FuseSet] = []
    chips = _fuse_chip_names(chip_hint)
    for element in root.iter():
        if not element.tag.lower().startswith("fuse") and "board" not in element.tag.lower():
            continue
        attrs = {k.lower().replace(" ", "").replace("-", ""): v for k, v in element.attrib.items()}
        if not attrs:
            continue
        name = attrs.get("name") or attrs.get("board") or attrs.get("description") or ""
        low = _first_attr(attrs, _FUSE_ATTR_KEYS["low"])
        high = _first_attr(attrs, _FUSE_ATTR_KEYS["high"])
        extended = _first_attr(attrs, _FUSE_ATTR_KEYS["extended"])
        if not (low or high or extended):
            continue
        bootloader = _first_attr(attrs, _FUSE_ATTR_KEYS["bootloader"])
        size = _first_attr(attrs, _FUSE_ATTR_KEYS["size"])
        lock = _first_attr(attrs, _FUSE_ATTR_KEYS["lock"]) or "0x0F"
        out.append(FuseSet(
            chip=chip_hint or name,
            clock=name or attrs.get("clock", "core default"),
            matched=bool(chips) and any(token in (name or "").lower() for token in chips),
            low=_norm_hex(low), high=_norm_hex(high), extended=_norm_hex(extended), lock=_norm_hex(lock),
            bootloader=bootloader, boot_size=size,
            note=f"read from {target.parent.name}/{target.name}",
            source="core fuses.xml",
            raw=dict(element.attrib),
        ))
    return out


def _preset_rows(chip: str) -> list[dict[str, Any]]:
    """Look up :data:`FUSE_PRESETS` with forgiving name matching.

    ``avr_part_for_board()`` yields avrdude part codes (``m328p``) while the table is
    keyed by marketing names (``ATmega328P``), so an exact dict lookup alone would
    silently return nothing - which is how a chip ends up being shown "unknown"
    fuses instead of the right ones.
    """
    wanted = (chip or "").strip()
    if not wanted:
        return []
    if wanted in FUSE_PRESETS:
        return FUSE_PRESETS[wanted]
    lowered = wanted.lower()
    # avrdude part codes ("m328p", "t85") are the marketing name without its prefix
    aliases = [lowered]
    if re.match(r"^m\d", lowered):
        aliases.append("atmega" + lowered[1:])
    elif re.match(r"^t\d", lowered):
        aliases.append("attiny" + lowered[1:])

    def matches(name: str) -> bool:
        return name.lower() in aliases or name.lower().replace(" ", "") in aliases

    for name, rows in FUSE_PRESETS.items():
        if matches(name):
            return rows
    tokens = set(_fuse_chip_names(wanted))
    if tokens:
        best: tuple[int, list[dict[str, Any]]] = (0, [])
        for name, rows in FUSE_PRESETS.items():
            overlap = tokens & set(_fuse_chip_names(name))
            if overlap and len(overlap) > best[0]:
                best = (len(overlap), rows)
        if best[1]:
            return best[1]
    return []


def _fuse_chip_names(hint: str) -> list[str]:
    """Match tokens for a chip name (``ATmega328P`` -> ``atmega328p``, ``328p`` ...)."""
    text = (hint or "").strip().lower()
    if not text:
        return []
    tokens = {text, text.replace("-", "")}
    match = re.match(r"^(?:atmega|attiny|atxmega|at90can|at90pwm)?(\w+)$", text)
    if match:
        tokens.add(match.group(1))
    for prefix in ("atmega328p", "atmega328pb", "atmega328", "atmega2560", "atmega32u4",
                   "atmega1284p", "atmega168", "attiny85", "attiny84", "attiny44", "attiny25"):
        if prefix in text:
            tokens.add(prefix)
            tokens.add(prefix.replace("atmega", "").replace("attiny", ""))
    return [t for t in tokens if len(t) >= 3]


def _first_attr(attrs: dict[str, str], keys: Sequence[str]) -> str:
    for key in keys:
        value = attrs.get(key)
        if value:
            return value
    for name, value in attrs.items():
        for key in keys:
            if key in name and value and re.match(r"^(0x)?[0-9a-fA-F]{2}$", value.strip()):
                return value
    return ""


def _norm_hex(value: str) -> str:
    text = (value or "").strip().lower().replace("0x", "")
    if not text:
        return "0x00"
    if not re.fullmatch(r"[0-9a-f]{1,8}", text):
        return value.strip()
    if len(text) == 1:
        text = "0" + text
    return "0x" + text.upper()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class BootloaderService:
    """Prepares and runs bootloader / ISP / esptool operations."""

    def __init__(self, cli: ArduinoCLI, cli_path: str = "", data_dir: os.PathLike[str] | str | None = None) -> None:
        self._log = get_logger("boot")
        self.cli = cli
        self._preferred_cli_path = cli_path
        self.data_dir = Path(data_dir).expanduser() if data_dir else None
        self._tools_cache: dict[str, str] = {}

    # --------------------------------------------------------- tool discovery
    @property
    def packages_root(self) -> Path:
        """``<data>/packages`` where arduino-cli installs cores and tools."""
        base = self.data_dir or self.cli.data_dir
        return Path(base) / "packages"

    def _first_existing(self, patterns: Sequence[str], root: Optional[Path] = None) -> str:
        """First file matching any of *patterns* (newest version wins)."""
        base = root or self.packages_root
        if not base.is_dir():
            return ""
        for pattern in patterns:
            try:
                matches = [c for c in base.glob(pattern) if c.is_file()]
            except (OSError, IndexError):
                continue
            if matches:
                matches.sort(key=lambda c: natural_key(str(c)), reverse=True)
                return str(matches[0])
        return ""

    def find_avrdude(self) -> str:
        """Locate ``avrdude`` (core tool folder first, then PATH)."""
        if "avrdude" in self._tools_cache:
            return self._tools_cache["avrdude"]
        found = self._first_existing([
            "arduino/tools/avrdude/*/avrdude.exe",
            "arduino/tools/avrdude/*/bin/avrdude.exe",
            "arduino/tools/avrdude/*/avrdude",
            "arduino/tools/avrdude/*/bin/avrdude",
            "*/tools/avrdude*/*/avrdude.exe",
            "*/tools/avrdude*/*/avrdude",
            "*/tools/avrdude*/avrdude.exe",
            "*/tools/avrdude*/avrdude",
        ])
        if not found:
            found = proc.find_executable("avrdude")
        self._tools_cache["avrdude"] = found
        return found

    def find_avrdude_conf(self) -> str:
        """Locate ``avrdude.conf`` matching the discovered binary."""
        if "avrdude.conf" in self._tools_cache:
            return self._tools_cache["avrdude.conf"]
        exe = self.find_avrdude()
        candidates: list[Path] = []
        if exe:
            base = Path(exe).parent
            candidates += [base / "etc" / "avrdude.conf", base.parent / "etc" / "avrdude.conf",
                           base / "avrdude.conf", base.parent / "avrdude.conf"]
        candidates += [self.packages_root / "arduino" / "tools" / "avrdude" / "*" / "etc" / "avrdude.conf"]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    self._tools_cache["avrdude.conf"] = str(candidate)
                    return str(candidate)
                if candidate.parent.is_dir():  # glob path
                    matches = sorted(candidate.parent.glob("*/etc/avrdude.conf"), reverse=True)
                    if matches:
                        self._tools_cache["avrdude.conf"] = str(matches[0])
                        return str(matches[0])
            except OSError:
                continue
        self._tools_cache["avrdude.conf"] = ""
        return ""

    def find_esptool(self, family: str = "esp32") -> str:
        """Locate ``esptool`` from the installed ESP core (or PATH)."""
        key = f"esptool:{family}"
        if key in self._tools_cache:
            return self._tools_cache[key]
        vendor = "esp32" if family == "esp32" else "esp8266"
        patterns = [
            f"{vendor}/tools/esptool*/*/esptool.exe",
            f"{vendor}/tools/esptool*/*/esptool.py",
            f"{vendor}/tools/esptool*/*/esptool",
            f"{vendor}/tools/*esptool*/*/esptool.exe",
            f"{vendor}/tools/*esptool*/*/esptool",
            f"{vendor}/tools/*esptool*/*/esptool.py",
            f"{vendor}/tools/*esptool*/esptool.py",
            f"*/tools/*esptool*/*/esptool.exe",
            f"*/tools/*esptool*/*/esptool",
            f"*/tools/*esptool*/*/esptool.py",
        ]
        found = self._first_existing(patterns)
        if not found:
            found = self._first_existing(["*/tools/*/esptool.exe", "*/tools/*/esptool"], root=self.packages_root)
        if not found:
            found = proc.find_executable("esptool")
        self._tools_cache[key] = found
        return found

    def find_python_for_esptool(self, esptool_path: str = "") -> str:
        """If esptool is a ``.py`` script we need a python interpreter."""
        path = Path(esptool_path) if esptool_path else None
        if path is not None and path.suffix.lower() == ".py":
            import sys

            return sys.executable
        return ""

    # --------------------------------------------------------------- chips
    def programmers(self, include_custom: bool = True) -> list[Programmer]:
        """Known + core declared programmers (``programmers.txt`` of each AVR core)."""
        out: dict[str, Programmer] = {}
        for program in KNOWN_PROGRAMMERS:
            out[program.id] = program
        for file in self._programmer_files():
            for entry in parse_programmers_txt(file):
                existing = out.get(entry.id)
                if existing is None:
                    out[entry.id] = entry
                elif existing.source == "built-in":
                    # core data wins for name/protocol/speed, we keep our help text
                    out[entry.id] = Programmer(
                        id=entry.id,
                        name=entry.name or existing.name,
                        protocol=entry.protocol or existing.protocol,
                        speed=entry.speed or existing.speed,
                        description=existing.description,
                        needs_port=existing.needs_port,
                        source=entry.source,
                    )
        ordered = sorted(out.values(), key=lambda p: (p.id == "custom", natural_key(p.name.lower())))
        if not include_custom:
            ordered = [p for p in ordered if p.id != "custom"]
        return ordered

    def _programmer_files(self) -> list[Path]:
        roots = [self.packages_root]
        if self.data_dir:
            roots.append(Path(self.data_dir))
        sketchbook = None
        try:
            sketchbook = self.cli.sketchbook_dir / "hardware"
        except Exception:  # pragma: no cover - cli may be unconfigured
            sketchbook = None
        if sketchbook is not None:
            roots.append(sketchbook)
        found: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for candidate in root.rglob("programmers.txt"):
                    if candidate.is_file() and candidate not in found:
                        found.append(candidate)
                    if len(found) > 30:
                        break
            except OSError:
                continue
        return found

    def supported_chips(self) -> list[str]:
        """Chips with fuse presets (used to populate the target dropdown)."""
        chips = sorted(FUSE_PRESETS.keys(), key=natural_key)
        return chips

    def fuse_presets(self, chip: str) -> list[FuseSet]:
        """Built-in fuse presets for *chip* (``atmega328p``/``m328p``/``ATmega328P``)."""
        rows = _preset_rows(chip)
        return [FuseSet(
            chip=chip, clock=str(row.get("clock", "")), low=str(row.get("low", "0x00")),
            high=str(row.get("high", "0x00")), extended=str(row.get("extended", "0xFF")),
            lock=str(row.get("lock", "0xFF")), bootloader=str(row.get("bootloader", "")),
            boot_size=str(row.get("boot_size", "")), note=str(row.get("note", "")),
            source="built-in table", raw=dict(row),
        ) for row in rows]

    def fuses_from_core(self, fqbn: str = "", chip: str = "") -> list[FuseSet]:
        """Fuse sets declared by the installed core (``fuses.xml`` next to boards.txt)."""
        parsed = parse_fqbn(fqbn or "")
        vendor, arch, board = parsed["vendor"] or "arduino", parsed["arch"] or "avr", parsed["board"] or ""
        results: list[FuseSet] = []
        for base in (self.packages_root / vendor / "hardware" / arch, self.cli.sketchbook_dir / "hardware" / vendor):
            if not base.is_dir():
                continue
            for version_dir in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
                fuses_file = version_dir / "fuses.xml"
                if fuses_file.is_file():
                    results = read_fuses_xml(fuses_file, chip_hint=chip or board)
                    if results:
                        return results
                boards_file = version_dir / "boards.txt"
                if boards_file.is_file() and board:
                    data = parse_boards_txt(boards_file, board)
                    for menu in data.get("menus", []):
                        if "fuse" in str(menu.get("id", "")).lower() or "fuses" in str(menu.get("label", "")).lower():
                            for option in menu.get("options", []):
                                config = option.get("config") or option.get("fuses") or ""
                                parsed_config = _parse_fuse_config(config)
                                if parsed_config:
                                    results.append(FuseSet(
                                        chip=chip or board, clock=str(option.get("label") or option.get("id")),
                                        low=parsed_config.get("low", "0xFF"), high=parsed_config.get("high", "0xDE"),
                                        extended=parsed_config.get("extended", "0x05"),
                                        lock=parsed_config.get("lock", "0x3F"),
                                        bootloader=parsed_config.get("bootloader", ""),
                                        note=f"boards.txt menu {menu.get('id')}:{option.get('id')}",
                                        source="core boards.txt",
                                    ))
                    if results:
                        return results
        return results

    def best_fuses(self, chip: str = "", fqbn: str = "", clock: str = "") -> FuseSet:
        """Best available fuse set: core first, then the built-in table."""
        core_rows = self.fuses_from_core(fqbn=fqbn, chip=chip)
        rows = core_rows or self.fuse_presets(chip)
        if not rows:
            return FuseSet(chip=chip or "unknown", clock=clock or "unknown", note=(
                "No fuse data available for this chip - use the values from your core's fuses.xml "
                "or enter them manually."), source="none", matched=False)
        for row in rows:
            if clock and clock.lower() in row.clock.lower():
                return row
        return rows[0]

    def board_menu_options(self, fqbn: str) -> dict[str, Any]:
        """Board menu definitions (processor variant, clock, ...) from boards.txt."""
        parsed = parse_fqbn(fqbn)
        if not parsed["board"]:
            return {}
        vendor, arch, board = parsed["vendor"], parsed["arch"], parsed["board"]
        for base in (self.packages_root / vendor / "hardware" / arch,):
            if not base.is_dir():
                continue
            for version_dir in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
                boards_file = version_dir / "boards.txt"
                if boards_file.is_file():
                    return parse_boards_txt(boards_file, board)
        return {}

    # -------------------------------------------------------------- commands
    def plan_burn_bootloader(self, fqbn: str, programmer: str, port: str, fuse: Optional[FuseSet] = None,
                             sketch_dir: os.PathLike[str] | str | None = None) -> FlashPlan:
        """Build the ``arduino-cli burn-bootloader`` command + its confirmation text."""
        if not fqbn:
            raise BootloaderError("Select a target board (FQBN) first.")
        if is_esp_fqbn(fqbn):
            raise BootloaderError(
                "ESP boards have no AVR bootloader. Use 'Flash Firmware (.bin)' instead - it writes "
                "bootloader + sketch with esptool."
            )
        if not programmer:
            raise BootloaderError("Select a programmer (USBasp, Arduino as ISP, ...).")
        program = self.programmer_by_id(programmer)
        if program is not None and program.needs_port and not port:
            raise BootloaderError(f"'{program.name}' is connected through a COM port - select one.")
        chip = fuse.chip if fuse else ""
        warnings = [
            "Incorrect fuse values can make the chip inaccessible (wrong clock, locked SPI, disabled reset).",
            "Power the target from a stable 5 V / 3.3 V source; disconnect it from USB serial adapters.",
        ]
        if fuse is not None:
            warnings.extend(fuse.warnings())
        notes = [
            "burn-bootloader compiles the bootloader of the selected board, then programs flash + fuses + lock bits.",
            "Add --preserve-fuses in Settings -> CLI extra args if you only want to rewrite the flash part.",
        ]
        argv = [self.cli.require(), *self.cli.global_args(), "burn-bootloader", "--fqbn", fqbn]
        if programmer:
            argv += ["--programmer", programmer]
        if port:
            argv += ["--port", port]
        argv += ["-v"]
        if sketch_dir:
            argv.append(str(sketch_dir))
        human = (
            f"Burn bootloader\n  Board      : {fqbn}\n  Programmer : {programmer}\n"
            f"  Port       : {port or '(usb programmer)'}\n"
            + (f"  Fuses      : {fuse.summary}\n  Clock      : {fuse.clock}\n  Source     : {fuse.source}\n" if fuse else "")
        )
        return FlashPlan(kind="burn-bootloader", argv=argv, human=human, target=fqbn,
                         warnings=warnings, notes=notes)

    def programmer_by_id(self, programmer_id: str) -> Optional[Programmer]:
        for candidate in self.programmers():
            if candidate.id.lower() == (programmer_id or "").lower():
                return candidate
        return None

    def plan_read_chip(self, avr_part: str, programmer: str, port: str) -> Optional[FlashPlan]:
        """``avrdude`` read of signature, fuses and lock bits (needs a real avrdude)."""
        exe = self.find_avrdude()
        if not exe:
            return None
        conf = self.find_avrdude_conf()
        argv = [exe, "-v"]
        if conf:
            argv += ["-C", conf]
        argv += ["-c", programmer or "arduino", "-p", avr_part or "m328p"]
        if port:
            argv += ["-P", port]
        argv += ["-u", "-V"]
        for item in ("signature", "lfuse", "hfuse", "efuse", "lock"):
            argv += ["-U", f"{item}:r:-:h"]
        return FlashPlan(
            kind="read-chip", argv=argv,
            human=f"Read chip information for {avr_part} via {programmer}" + (f" on {port}" if port else ""),
            target=avr_part, notes=["read only - this does not modify the chip"],
        )

    def plan_backup(self, avr_part: str, programmer: str, port: str, out_file: os.PathLike[str] | str) -> FlashPlan:
        """Read the flash memory of an AVR chip into an Intel-Hex file."""
        exe = self.find_avrdude()
        if not exe:
            raise BootloaderError(
                "avrdude was not found in the Arduino AVR core folder, so a read-back is not possible.\n"
                "Install the 'Arduino AVR boards' core (Settings -> Cores) and retry."
            )
        target = Path(out_file)
        ensure_dir(target.parent)
        argv = [exe, "-v"]
        conf = self.find_avrdude_conf()
        if conf:
            argv += ["-C", conf]
        argv += ["-c", programmer or "arduino", "-p", avr_part or "m328p"]
        if port:
            argv += ["-P", port]
        argv += ["-u", "-V", "-U", f"flash:r:{target}:i"]
        warnings = [
            "This is a binary image of the FLASH contents only. It can be written back to an identical chip, "
            "but it does NOT recover the original Arduino source code - .hex cannot be decompiled into a sketch.",
            "Fuse/lock bytes are not included; read them with 'Read chip information' and save them separately.",
        ]
        return FlashPlan(kind="backup", argv=argv, human=f"Backup flash of {avr_part} to {target.name}",
                         target=avr_part, warnings=warnings, output_file=str(target))

    def plan_esptool(
        self,
        *,
        family: str,
        port: str,
        baud: int = 921600,
        address: str = "0x0",
        binary: os.PathLike[str] | str | None = None,
        erase_first: bool = False,
        flash_mode: str = "dio",
        flash_size: str = "detect",
        flash_freq: str = "40m",
        action: str = "write",
        read_size: int = 0,
        out_file: os.PathLike[str] | str | None = None,
    ) -> FlashPlan:
        """Build an ``esptool`` command line (write / erase / read / verify / id)."""
        exe = self.find_esptool(family)
        if not exe:
            raise BootloaderError(
                f"esptool for {family} was not found. Install the '{'esp32:esp32' if family == 'esp32' else 'esp8266:esp8266'}' "
                "core in Settings -> Cores first (it ships esptool)."
            )
        chip = "esp32" if family == "esp32" else "esp8266"
        args: list[str] = [exe]
        python = self.find_python_for_esptool(exe)
        if python:
            args = [python, exe]
        args += ["--chip", chip, "--port", port or "", "--baud", str(int(baud or 115200))]
        args += ["--before", "default_reset", "--after", "hard_reset"]
        if action == "id":
            args.append("flash_id")
        elif action == "erase":
            args.append("erase_flash")
        elif action == "read":
            if not out_file:
                raise BootloaderError("Choose a file name for the firmware backup first.")
            size = int(read_size or 0)
            if size <= 0:
                raise BootloaderError("Enter how many bytes to read (e.g. 1048576 for 1 MB).")
            args += ["read_flash", "0", str(size), str(out_file)]
        elif action == "verify":
            if not binary:
                raise BootloaderError("Select the .bin file to compare against.")
            args += ["verify_flash", str(address), str(binary)]
        else:
            if not binary:
                raise BootloaderError("Select a firmware .bin file first.")
            if erase_first:
                args += ["erase_flash"]
            args += ["write_flash", "--flash_mode", flash_mode or "dio", "--flash_size", flash_size or "detect",
                     "--flash_freq", flash_freq or "40m", str(address or "0x0"), str(binary)]
        warnings: list[str] = []
        if action in {"write", "erase"}:
            warnings.append("Writing at the wrong address (or erasing) replaces the bootloader - the board may need "
                            "a full re-flash of bootloader + partition table + app to come back.")
        if erase_first:
            warnings.append("'Erase before write' removes the whole flash, including WiFi calibration and NVS settings.")
        warnings.append("Put the module in download mode (hold BOOT/GPIO0 while pressing RESET) if it is not detected.")
        notes = [
            "esptool is invoked with --before default_reset/--after hard_reset, so auto-program circuits are used "
            "when present.",
            "Use 'Verify' after flashing to compare the written image with the .bin file.",
        ]
        human = "esptool " + " ".join(part for part in args[1:] if part)
        return FlashPlan(
            kind={"id": "read-chip", "read": "backup"}.get(action, "flash" if action in {"write", ""} else action),
            argv=args, human=human, target=f"{chip} @ {address or '-'}", warnings=warnings, notes=notes,
            output_file=str(out_file) if out_file else "",
        )

    def plan_flash_from_project(self, family: str, port: str, build_dir: os.PathLike[str] | str,
                                baud: int = 921600) -> FlashPlan:
        """Flash the *compiled* Arduino sketch (build dir) with esptool.

        The ESP cores ship a per-sketch ``flash_args`` file inside the build
        folder that lists every ``.bin`` and its address.  If it exists we use
        it, otherwise we fall back to the merged/``.ino.bin`` at 0x0/0x10000.
        """
        build = Path(build_dir)
        if not build.is_dir():
            raise BootloaderError(f"Build folder {build} does not exist - run Verify (compile) first.")
        flash_args = build / "flash_args"
        if flash_args.is_file():
            try:
                lines = [l.strip() for l in flash_args.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
            except OSError:
                lines = []
            payload = " ".join(lines)
            exe = self.find_esptool(family)
            if not exe:
                raise BootloaderError("esptool not found - install the ESP core first.")
            args = [exe]
            python = self.find_python_for_esptool(exe)
            if python:
                args = [python, exe]
            args += ["--chip", "esp32" if family == "esp32" else "esp8266", "--port", port, "--baud", str(baud),
                     "--before", "default_reset", "--after", "hard_reset", "write_flash"]
            args += payload.split()
            return FlashPlan(
                kind="flash", argv=args,
                human="esptool " + " ".join(args[1:]),
                target=f"{family} (from {flash_args.name})",
                warnings=["This writes every file listed in flash_args, overwriting the existing firmware."],
                notes=["Command built from the core generated flash_args file, so addresses are always correct."],
            )
        candidates = sorted(build.glob("*.bin"), key=lambda p: natural_key(p.name))
        if not candidates:
            raise BootloaderError("No .bin files found in the build folder - compile the project first.")
        main = next((c for c in candidates if c.name.endswith(".ino.bin")), candidates[0])
        address = "0x10000" if family == "esp32" else "0x0"
        plan = self.plan_esptool(family=family, port=port, baud=baud, address=address, binary=main, action="write")
        plan.notes.append(f"flash_args was missing, so '{main.name}' was written at {address} only.")
        return plan

    # -------------------------------------------------------------- execution
    def run(
        self,
        plan: FlashPlan,
        on_line: Optional[Callable[[str], Any]] = None,
        context: Any = None,
        progress: Optional[Callable[[Optional[float], str], Any]] = None,
        timeout: Optional[float] = None,
    ) -> proc.CommandResult:
        """Run a prepared plan, streaming output and parsing progress."""

        def _line(text: str) -> None:
            if on_line is not None:
                on_line(text)
            if progress is not None:
                fraction, message = self._progress(text)
                if fraction is not None or message:
                    progress(fraction, message or text[:60])

        try:
            result = proc.run_streaming(plan.argv, on_line=_line if (on_line or progress) else None,
                                        context=context, timeout=timeout)
        except proc.CommandError as exc:
            raise BootloaderError(str(exc)) from exc
        if result.returncode not in (0, None) or result.cancelled:
            advice = self.explain_failure(result.output, plan.kind)
            if on_line is not None and advice:
                on_line("hint: " + advice)
        return result

    @staticmethod
    def _progress(text: str) -> tuple[Optional[float], str]:
        """avrdude/esptool draw ASCII progress bars - turn them into a fraction."""
        match = re.search(r"([0-9]{1,3})\s*%", text)
        if match and ("Writing" in text or "Reading" in text or "|" in text or "Erasing" in text or "Compressed" in text):
            return min(0.99, max(0.0, _to_int(match.group(1)) / 100.0)), text.strip()[:70]
        lowered = text.lower()
        for marker, message in (
            ("connecting", "connecting to chip"),
            ("chip detection", "detecting chip"),
            ("erasing flash", "erasing flash"),
            ("writing at", "writing flash"),
            ("verifying", "verifying"),
            ("leaving", "finishing"),
            ("hard resetting", "resetting board"),
        ):
            if marker in lowered:
                return None, message
        return None, ""

    @staticmethod
    def explain_failure(output: str, kind: str = "") -> str:
        """Translate avrdude/esptool failures into advice."""
        text = (output or "").strip()
        lowered = text.lower()
        table = [
            ("cannot open port", "The COM port could not be opened: close the Serial Monitor / other apps, "
                                 "check the selected port and driver."),
            ("can't open device", "The port could not be opened: another program holds it (Serial Monitor, "
                                   "PuTTY), or the driver for this board is missing (CH340, CP210x)."),
            ("stk500_getsync", "The bootloader did not answer. Check the selected board and port, try a "
                               "data-capable USB cable, and if the board is bare use a programmer; on ESP boards "
                               "hold BOOT while resetting. Uploading at 115200 baud also helps on clone boards."),
            ("invalid device signature", "The chip answered with an unexpected signature - wrong part, wrong "
                                          "core, or miswired ISP cables (MOSI/MISO swapped)."),
            ("the system cannot find the file specified", "That COM port does not exist. Refresh the port list."),
            ("access is denied", "Windows denied access to the port. Close other programs using it."),
            ("no device found on", "esptool saw no chip in download mode. Hold BOOT (GPIO0) while pressing RESET, "
                                    "then retry."),
            ("failed to connect to esp", "Same as above: force download mode and lower the baud rate (115200)."),
            ("verification mismatch", "Verification failed - re-check the wiring/voltage and flash again at a lower "
                                       "baud rate."),
            ("chip type does not match", "The selected part does not match the chip. Fix the device or the board profile."),
            ("expected signature", "The signature does not match: wrong chip, wrong core, or a clone with a "
                                    "different die."),
            ("device is locked", "Lock bits prevent access. The chip cannot be reprogrammed over ISP without a "
                                   "high-voltage programmer."),
            ("sorry, your fuses are set to use", "The clock fuses are set to a source the chip cannot run with "
                                                   "(e.g. external crystal missing). Reset fuses via ISP."),
            ("power supply caution", "avrdude measured an out-of-range VCC - check the target supply."),
            ("ispsignaturecheck failed", "Fuse bytes could not be read; check wiring (MOSI/MISO swapped?) and speed."),
            ("not responding", "The programmer is not responding: verify wiring and that the programmer sketch is "
                                "flashed; for 'Arduino as ISP' use 19200 baud."),
            ("permission denied", "The USB device is busy or needs a different driver (libusb for USBasp/USBtiny)."),
            ("timed out", "The tool timed out. Lower the programmer/flash speed and retry."),
        ]
        hits = [hint for needle, hint in table if needle in lowered]
        if kind in {"flash", "erase"} and "esptool" not in lowered and not hits:
            hits.append("Try --baud 115200, check TX/RX swap, and add a 10 uF capacitor on EN/RESET.")
        if not hits:
            return ""
        return " ".join(dict.fromkeys(hits))[:MAX_LOG_SNIPPET]

    def parse_chip_info(self, output: str, expected_bytes: int = 3) -> ChipInfo:
        """Parse ``avrdude`` read output into :class:`ChipInfo`.

        avrdude prints the values in slightly different shapes depending on
        version and flags, so we try (a) explicit ``key = 0xNN`` lines, then
        (b) "key on one line, value on the next", then (c) bare hex tokens in
        the order of the ``-U`` arguments.
        """
        info = ChipInfo(output=output or "")
        if not (output or "").strip():
            info.error = "avrdude produced no output"
            return info
        lowered_lines = output.splitlines()
        keys = ("signature", "lfuse", "hfuse", "efuse", "lock", "calibration")
        values: dict[str, list[str]] = {key: [] for key in keys}
        pending: Optional[str] = None
        for raw_line in lowered_lines:
            line = raw_line.strip()
            if not line:
                continue
            found = re.findall(r"0x([0-9A-Fa-f]{2})", line)
            hit_key = next((key for key in keys if key in line.lower()), "")
            if hit_key:
                pending = hit_key
                values[hit_key].extend(v.upper() for v in found)
                if found:
                    pending = None
                continue
            if pending and found:
                values[pending].extend(v.upper() for v in found)
                pending = None
        signature = values["signature"]
        if not signature:
            match = re.search(r"signature[^0-9a-f]{0,20}((?:0x[0-9A-Fa-f]{2}\s*){1,4})", output, re.I)
            if match:
                signature = [h.upper() for h in re.findall(r"0x([0-9A-Fa-f]{2})", match.group(1))]
        if signature:
            info.signature = "".join(signature[:3])
            info.signature_name = SIGNATURE_NAMES.get(info.signature, "")
        for key in ("lfuse", "hfuse", "efuse"):
            if values[key]:
                info.fuses[key] = "0x" + values[key][0]
        if values["lock"]:
            info.lock = "0x" + values["lock"][0]
        if values["calibration"]:
            info.calibration = "0x" + values["calibration"][0]
        # fallback: bare hex bytes in order (signature, lfuse, hfuse, efuse, lock)
        if not info.signature and not info.fuses:
            bare = re.findall(r"^\s*0x([0-9A-Fa-f]{2})\s*$", output, re.M)
            order = ["signature", "lfuse", "hfuse", "efuse", "lock"]
            index = 0
            for name in order:
                if index >= len(bare):
                    break
                if name == "signature":
                    info.signature = "".join(b.upper() for b in bare[index:index + 3])
                    info.signature_name = SIGNATURE_NAMES.get(info.signature, "")
                    index += 3
                else:
                    value = "0x" + bare[index].upper()
                    index += 1
                    if name == "lock":
                        info.lock = value
                    elif name == "calibration":
                        info.calibration = value
                    else:
                        info.fuses[name] = value
        info.device = _detect_part(output)
        info.avr_fuses = dict(info.fuses)
        if info.lock:
            info.avr_fuses["lock"] = info.lock
        if info.signature:
            info.avr_fuses["signature"] = "0x" + info.signature
        if "avrdude done" in output.lower() or info.signature or info.fuses:
            info.ok = True
        if "error" in output.lower() and not info.ok:
            info.error = self.explain_failure(output, "read") or "avrdude reported an error"
        elif "error" in output.lower():
            info.error = self.explain_failure(output, "read")
        return info

    # ---------------------------------------------------------------- helpers
    def avr_part_for_board(self, fqbn: str, chip: str = "") -> str:
        """avrdude part code for the current board / chip selection."""
        if chip and chip in AVR_PART_FOR_CHIP:
            return AVR_PART_FOR_CHIP[chip]
        parsed = parse_fqbn(fqbn)
        boards = self.board_menu_options(fqbn) if parsed["board"] else {}
        prop = str((boards or {}).get("props", {}).get("build.mcu", ""))
        mapping = {
            "atmega328p": "m328p", "atmega328pb": "m328pb", "atmega168": "m168", "atmega88": "m88",
            "atmega2560": "m2560", "atmega1280": "m1280", "atmega32u4": "m32u4", "atmega644p": "m644p",
            "atmega1284p": "m1284p", "attiny85": "t85", "attiny45": "t45", "attiny25": "t25",
            "attiny84": "t84", "attiny84a": "t84a", "attiny44": "t44", "attiny167": "t167",
        }
        if prop and prop.lower() in mapping:
            return mapping[prop.lower()]
        for name, code in AVR_PART_FOR_CHIP.items():
            if name.lower() in (fqbn or "").lower():
                return code
        return "m328p"

    def chip_for_board(self, fqbn: str) -> str:
        """Human chip name for a board (empty when unknown)."""
        boards = self.board_menu_options(fqbn)
        mcu = str((boards or {}).get("props", {}).get("build.mcu", ""))
        if mcu:
            return mcu.upper().replace("ATMEGA", "ATmega").replace("ATTINY", "ATtiny")
        for name in AVR_PART_FOR_CHIP:
            if name.lower() in (fqbn or "").lower():
                return name
        from .boards import board_for_fqbn

        profile = board_for_fqbn(fqbn)
        return profile.mcu if profile else ""

    def supports_bootloader(self, fqbn: str) -> tuple[bool, str]:
        """Whether burning a bootloader makes sense for *fqbn*."""
        if not fqbn:
            return False, "No board selected."
        family = family_for_fqbn(fqbn)
        if family == "esp32" or family == "esp8266":
            return False, f"{family.upper()} boards are flashed with esptool, not with an AVR bootloader."
        if family == "avr":
            return True, "AVR board - bootloader burning is supported when a programmer is attached."
        return False, "This platform does not expose a burn-bootloader command."

    @staticmethod
    def flash_size_text(path: os.PathLike[str] | str) -> str:
        try:
            return human_bytes(Path(path).stat().st_size)
        except OSError:
            return "unknown size"

    @staticmethod
    def default_backup_path(folder: os.PathLike[str] | str, label: str, suffix: str = "hex") -> Path:
        base = Path(folder)
        safe = re.sub(r"[^\w.\-]", "_", label or "backup")
        stamp = time_stamp()
        ensure_dir(base)
        return base / f"{safe}-{stamp}.{suffix}"

    def cleanup_backup_dir(self, folder: os.PathLike[str] | str, keep: int = 10) -> int:
        """Delete old backups, keeping the newest *keep* files."""
        base = Path(folder)
        if not base.is_dir():
            return 0
        files = sorted(
            (p for p in base.glob("backup-*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for old in files[max(0, keep):]:
            try:
                old.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                continue
        return removed


def time_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _parse_fuse_config(config: str) -> dict[str, str]:
    """Parse a ``menu.*.fuses=...`` style string (``low,high,ext,lock``)."""
    out: dict[str, str] = {}
    tokens = [t.strip() for t in re.split(r"[,\s]+", config or "") if t.strip()]
    names = ("low", "high", "extended", "lock")
    for index, token in enumerate(tokens[:4]):
        if re.match(r"^(0x)?[0-9A-Fa-f]{2}$", token):
            out[names[index]] = _norm_hex(token)
    return out


def _detect_part(output: str) -> str:
    """Grab ``Avrdude done`` context: the part string from the command output."""
    match = re.search(r"(?:device|part|avrdude.*?for)\s+'?([a-z]{1,2}\d{2,4}[a-zA-Z]?)'?", output, re.I)
    return match.group(1) if match else ""


def build_fqbn_with_menus(base_fqbn: str, selections: dict[str, str]) -> str:
    """Append ``menu.option`` pairs to a base FQBN (``arduino:avr:pro:cpu.16MHz``)."""
    parts = [base_fqbn.rstrip(":")]
    for menu, option in selections.items():
        if menu and option:
            parts.append(f"{menu}.{option}")
    return ":".join(parts)


def find_avr_fuses_file(cli: ArduinoCLI) -> str:
    """Path of the installed AVR core's ``fuses.xml`` (empty when not installed)."""
    roots = [cli.data_dir / "packages", cli.sketchbook_dir / "hardware"]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for candidate in root.rglob("fuses.xml"):
                if candidate.is_file():
                    return str(candidate)
        except OSError:  # pragma: no cover
            continue
    return ""
