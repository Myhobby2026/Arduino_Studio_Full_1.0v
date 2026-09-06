"""Static board catalog and board/FQBN helpers.

Arduino Studio works with *FQBNs* (Fully Qualified Board Names) because that is
what ``arduino-cli`` expects.  This module adds friendly labels, the core that
has to be installed for a board, and the family (``avr`` / ``esp8266`` /
``esp32``) which drives bootloader and flashing behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .utils import natural_key

__all__ = [
    "BoardProfile",
    "BUILTIN_BOARDS",
    "SUPPORTED_CORES",
    "board_for_fqbn",
    "display_name_for_fqbn",
    "core_for_fqbn",
    "family_for_fqbn",
    "is_avr_fqbn",
    "is_esp_fqbn",
    "is_esp32_fqbn",
    "is_esp8266_fqbn",
    "vendor_for_fqbn",
    "boards_by_family",
    "core_install_name",
    "CORE_INSTALL_HINTS",
    "FLASH_TOOL",
    "ESPTOOL_CHIP_FOR_FQBN",
]


@dataclass(frozen=True)
class BoardProfile:
    """A board we know something useful about (label, core, MCU, flash tool)."""

    name: str
    fqbn: str
    core: str                      # arduino-cli core, e.g. "arduino:avr"
    family: str                    # "avr" | "esp8266" | "esp32" | "other"
    mcu: str = ""                  # ATmega328P, ESP32-D0WDQ6, ...
    flash_bytes: int = 0           # usable flash for sketches
    sram_bytes: int = 0
    upload_tool: str = ""          # "avrdude" | "esptool"
    default_baud: int = 115200
    bootloader_supported: bool = True
    programmer_default: str = "arduino"   # default avrdude programmer for ISP
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def short_label(self) -> str:
        """``"Arduino Uno"`` style label used in menus and the toolbar."""
        return self.name

    @property
    def flash_kb(self) -> float:
        return round(self.flash_bytes / 1024.0, 1) if self.flash_bytes else 0.0


#: Boards offered in the toolbar out of the box (no core installation required
#: to *list* them, but :attr:`BoardProfile.core` must be installed to compile).
BUILTIN_BOARDS: tuple[BoardProfile, ...] = (
    BoardProfile(
        name="Arduino Uno",
        fqbn="arduino:avr:uno",
        core="arduino:avr",
        family="avr",
        mcu="ATmega328P",
        flash_bytes=32256,
        sram_bytes=2048,
        upload_tool="avrdude",
        default_baud=115200,
        programmer_default="arduino",
        notes="Classic DIP/TQFP ATmega328P @16 MHz, OptiBoot bootloader.",
        aliases=("uno",),
    ),
    BoardProfile(
        name="Arduino Nano",
        fqbn="arduino:avr:nano",
        core="arduino:avr",
        family="avr",
        mcu="ATmega328P",
        flash_bytes=30720,
        sram_bytes=2048,
        upload_tool="avrdude",
        default_baud=57600,
        programmer_default="arduino",
        notes="Old bootloaders use 57600 baud; Nano Every/ESP32 variants need their own FQBN.",
        aliases=("nano",),
    ),
    BoardProfile(
        name="Arduino Mega 2560",
        fqbn="arduino:avr:mega",
        core="arduino:avr",
        family="avr",
        mcu="ATmega2560",
        flash_bytes=253952,
        sram_bytes=8192,
        upload_tool="avrdude",
        default_baud=115200,
        programmer_default="arduino",
        notes="Requires -c wiring on some clones; use Mega/CADGENIE/Boards=... menu if needed.",
        aliases=("mega", "mega2560"),
    ),
    BoardProfile(
        name="Arduino Pro or Pro Mini (ATmega328P, 5V, 16 MHz)",
        fqbn="arduino:avr:pro",
        core="arduino:avr",
        family="avr",
        mcu="ATmega328P",
        flash_bytes=30720,
        sram_bytes=2048,
        upload_tool="avrdude",
        default_baud=57600,
        programmer_default="arduino",
        notes="Board menu selects 3.3V/8 MHz vs 5V/16 MHz - important for fuse writing.",
        aliases=("pro mini", "promini"),
    ),
    BoardProfile(
        name="Arduino Micro",
        fqbn="arduino:avr:micro",
        core="arduino:avr",
        family="avr",
        mcu="ATmega32U4",
        flash_bytes=28672,
        sram_bytes=2560,
        upload_tool="avrdude",
        default_baud=57600,
        bootloader_supported=True,
        programmer_default="arduino",
        notes="ATmega32U4 - flashing works via Caterina (DFU) bootloader, ISP needs avr109/caterina.",
        aliases=("micro", "leonardo"),
    ),
    BoardProfile(
        name="ESP8266 NodeMCU / LoLin",
        fqbn="esp8266:esp8266:nodemcuv2",
        core="esp8266:esp8266",
        family="esp8266",
        mcu="ESP8266EX",
        flash_bytes=1044464,
        sram_bytes=81920,
        upload_tool="esptool",
        default_baud=115200,
        bootloader_supported=False,
        notes="No ICSP: flash .bin files at 0x00000 (or 0x1000 for old 1 MB modules).",
        aliases=("esp8266", "nodemcu"),
    ),
    BoardProfile(
        name="ESP8266 Generic Module",
        fqbn="esp8266:esp8266:generic",
        core="esp8266:esp8266",
        family="esp8266",
        mcu="ESP8266EX",
        flash_bytes=4194304,
        sram_bytes=81920,
        upload_tool="esptool",
        default_baud=115200,
        bootloader_supported=False,
        notes="Configure flash size/mode in the board menu (FQBN + :menu.xxx options).",
        aliases=("esp8266 generic",),
    ),
    BoardProfile(
        name="ESP32 Dev Module",
        fqbn="esp32:esp32:esp32",
        core="esp32:esp32",
        family="esp32",
        mcu="ESP32-D0WDQ6",
        flash_bytes=1310720,
        sram_bytes=327680,
        upload_tool="esptool",
        default_baud=921600,
        bootloader_supported=False,
        notes="Bootloader lives at 0x1000, partition table 0x8000, app 0x10000.",
        aliases=("esp32", "esp32 dev"),
    ),
    BoardProfile(
        name="ESP32-S3 Dev Module",
        fqbn="esp32:esp32:esp32s3",
        core="esp32:esp32",
        family="esp32",
        mcu="ESP32-S3",
        flash_bytes=1310720,
        sram_bytes=327680,
        upload_tool="esptool",
        default_baud=921600,
        bootloader_supported=False,
        notes="Uses USB-CDC in some board menus; select 'Hardware CDC' for native USB.",
        aliases=("esp32-s3", "s3"),
    ),
    BoardProfile(
        name="ATtiny85 (Digistump/Drazzy style)",
        fqbn="attiny:avr:ATtinyX5",
        core="attiny:avr",
        family="avr",
        mcu="ATtiny85",
        flash_bytes=6144,
        sram_bytes=512,
        upload_tool="avrdude",
        default_baud=9600,
        bootloader_supported=True,
        programmer_default="usbasp",
        notes="Needs a 3rd party core (e.g. Drazzy ATTinyCore) and an external programmer.",
        aliases=("attiny", "attiny85"),
    ),
)

#: Cores that can be installed straight from the *First run* / *Settings* screen.
CORE_INSTALL_HINTS: tuple[tuple[str, str, str], ...] = (
    ("arduino:avr", "Arduino AVR boards", "Uno, Nano, Mega, Pro Mini, Micro, Leonardo"),
    ("esp8266:esp8266", "ESP8266 Sdk Library", "NodeMCU, Wemos D1 mini (requires board manager URL)"),
    ("esp32:esp32", "esp32 by Espressif", "ESP32 / ESP32-S2 / ESP32-S3 (requires board manager URL)"),
    ("attiny:avr", "ATTinyCore (Drazzy)", "ATtiny25/45/85, ATtiny84/167 (requires board manager URL)"),
)

SUPPORTED_CORES: dict[str, str] = {
    "arduino:avr": "Arduino AVR Boards",
    "arduino:esp32": "Arduino ESP32 Boards (deprecated, use esp32:esp32)",
    "esp8266:esp8266": "ESP8266 Sdk Library",
    "esp32:esp32": "esp32 by Espressif Systems",
    "attiny:avr": "ATTinyCore",
    "megaavr:avr": "Arduino MegaAVR Boards",
}

#: Board manager JSON URLs needed by the cores above.
BOARD_MANAGER_URLS: dict[str, str] = {
    "esp8266:esp8266": "https://arduino.esp8266.com/stable/package_esp8266com_index.json",
    "esp32:esp32": "https://espressif.github.io/arduino-esp32/package_esp32_index.json",
    "attiny:avr": "http://drazzy.com/package_drazzy.com_index.json",
}

#: Flash tool used for each family (also used by the Bootloader Manager).
FLASH_TOOL: dict[str, str] = {
    "avr": "avrdude",
    "esp8266": "esptool",
    "esp32": "esptool",
}

#: ``esptool --chip`` argument per family.
ESPTOOL_CHIP_FOR_FQBN: dict[str, str] = {
    "esp8266": "esp8266",
    "esp32": "esp32",
    "esp32s3": "esp32s3",
    "esp32s2": "esp32s2",
    "esp32c3": "esp32c3",
    "avr": "auto",
}

_FQBN_RE = re.compile(r"^(?P<vendor>[^:]+):(?P<arch>[^:]+):(?P<board>[^:]+)(?::(?P<menu>.*))?$")


# --------------------------------------------------------------------------------- lookups
def parse_fqbn(fqbn: str) -> dict[str, str]:
    """Split ``vendor:arch:board:menu.value`` into its parts.

    Unknown/short FQBNs return the parts that are present (missing ones are
    empty strings) so callers can be lax with user input.
    """
    match = _FQBN_RE.match((fqbn or "").strip())
    if not match:
        text = (fqbn or "").strip()
        parts = text.split(":")
        return {
            "vendor": parts[0] if len(parts) > 0 else "",
            "arch": parts[1] if len(parts) > 1 else "",
            "board": parts[2] if len(parts) > 2 else "",
            "menu": ":".join(parts[3:]) if len(parts) > 3 else "",
            "valid": bool(text) and len(parts) >= 3,
        }
    data = match.groupdict()
    data["valid"] = True
    return {k: (v or "") for k, v in data.items()}


def board_for_fqbn(fqbn: str) -> Optional[BoardProfile]:
    """Return the catalog entry matching *fqbn* (ignoring ``:menu`` options)."""
    if not fqbn:
        return None
    parsed = parse_fqbn(fqbn)
    base = ":".join(p for p in (parsed["vendor"], parsed["arch"], parsed["board"]) if p)
    for board in BUILTIN_BOARDS:
        if board.fqbn.lower() == base.lower():
            return board
    # second pass: match on the board id only (e.g. custom clones of a mega)
    for board in BUILTIN_BOARDS:
        if parsed["board"] and board.fqbn.split(":")[-1].lower() == parsed["board"].lower():
            if board.core.startswith(parsed["vendor"] + ":") or not parsed["vendor"]:
                return board
    return None


def display_name_for_fqbn(fqbn: str) -> str:
    """Human readable name for *fqbn*, falling back to the FQBN itself."""
    board = board_for_fqbn(fqbn)
    if board is not None:
        parsed = parse_fqbn(fqbn)
        if parsed["menu"]:
            return f"{board.name} ({parsed['menu']})"
        return board.name
    return fqbn or "No board selected"


def core_for_fqbn(fqbn: str) -> str:
    """The ``arduino-cli`` core name required by *fqbn* (``vendor:arch``)."""
    board = board_for_fqbn(fqbn)
    if board:
        return board.core
    parsed = parse_fqbn(fqbn)
    if parsed["vendor"] and parsed["arch"]:
        return f"{parsed['vendor']}:{parsed['arch']}"
    return "arduino:avr"


def family_for_fqbn(fqbn: str) -> str:
    """Board family: ``avr``, ``esp8266``, ``esp32`` or ``other``."""
    board = board_for_fqbn(fqbn)
    if board:
        return board.family
    parsed = parse_fqbn(fqbn)
    arch = parsed["arch"].lower()
    vendor = parsed["vendor"].lower()
    if "esp32" in arch or "esp32" in vendor:
        return "esp32"
    if "esp8266" in arch or "esp8266" in vendor:
        return "esp8266"
    if arch in {"avr", "megaavr", "mbed"} or vendor in {"arduino", "attiny", "digispark"}:
        return "avr"
    return "other"


def is_avr_fqbn(fqbn: str) -> bool:
    return family_for_fqbn(fqbn) == "avr"


def is_esp_fqbn(fqbn: str) -> bool:
    return family_for_fqbn(fqbn) in {"esp8266", "esp32"}


def is_esp32_fqbn(fqbn: str) -> bool:
    return family_for_fqbn(fqbn) == "esp32"


def is_esp8266_fqbn(fqbn: str) -> bool:
    return family_for_fqbn(fqbn) == "esp8266"


def vendor_for_fqbn(fqbn: str) -> str:
    """Package vendor (``arduino``, ``esp32``, ...)."""
    return parse_fqbn(fqbn)["vendor"]


def boards_by_family(family: str) -> list[BoardProfile]:
    """All catalog boards of one family (``"avr"``, ``"esp32"``, ...)."""
    return [board for board in BUILTIN_BOARDS if board.family == family]


def esptool_chip_for_fqbn(fqbn: str) -> str:
    """``--chip`` value for esptool derived from the FQBN board id."""
    parsed = parse_fqbn(fqbn)
    board = parsed["board"].lower()
    for key, chip in ESPTOOL_CHIP_FOR_FQBN.items():
        if key != "avr" and key in board:
            return chip
    return ESPTOOL_CHIP_FOR_FQBN.get(family_for_fqbn(fqbn), "auto")


def core_install_name(core_or_fqbn: str) -> str:
    """Argument used with ``arduino-cli core install`` (a *platform* id).

    Accepts either form on purpose: ``esp32:esp32`` stays as is, while a full
    board FQBN such as ``esp32:esp32:esp32`` is reduced to ``esp32:esp32`` -
    ``arduino-cli core install`` only knows platforms, so feeding it a board id
    would fail with "platform not found".
    """
    text = (core_or_fqbn or "").strip()
    if not text:
        return ""
    parts = text.split(":")
    if len(parts) >= 3:
        return ":".join(parts[:2])
    return text


def sort_board_labels(labels: Iterable[str]) -> list[str]:
    """Natural sort for board labels coming from ``board listall``."""
    return sorted({label for label in labels if label}, key=natural_key)


def baud_for_fqbn(fqbn: str) -> int:
    """Recommended serial-monitor baud rate for a board."""
    board = board_for_fqbn(fqbn)
    return board.default_baud if board else 115200


def sketch_memory_report(fqbn: str) -> str:
    """``"32256 bytes flash / 2048 bytes RAM"`` for tooltips and info labels."""
    board = board_for_fqbn(fqbn)
    if not board:
        return "Unknown memory layout"
    parts = []
    if board.flash_bytes:
        parts.append(f"{board.flash_bytes:,} bytes flash")
    if board.sram_bytes:
        parts.append(f"{board.sram_bytes:,} bytes SRAM")
    return " / ".join(parts) or "Unknown memory layout"
