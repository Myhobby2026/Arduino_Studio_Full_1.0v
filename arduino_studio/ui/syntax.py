"""Syntax highlighting engine for Arduino / C++ sources.

A hand written, dependency-free tokenizer built for ``tkinter.Text`` tags.  It
recognises:

* line and block comments (unterminated blocks keep colouring to the end of the
  file, which is what you want while typing ``/*``),
* preprocessor directives including backslash continuations,
* string and character literals with escapes,
* numbers (dec/hex/oct/bin/float with ``U``/``L``/``F`` suffixes),
* C++ keywords, types, Arduino API functions, pin constants,
* function calls (``foo(``), class-ish identifiers (``Servo``), and
* ``TODO`` / ``FIXME`` markers inside comments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

__all__ = [
    "ArduinoKeywords",
    "Token",
    "tokenize",
    "find_matching",
    "strip_comment_and_string_spans",
    "is_open_block_comment",
    "MAX_SCAN_CHARS",
]

C_KEYWORDS: frozenset[str] = frozenset("""
alignas alignof and and_eq asm auto bitand bitor break case catch class compl const consteval
constexpr constinit const_cast continue decltype default delete do dynamic_cast else enum
explicit export extern false for friend goto if inline mutable namespace new noexcept not not_eq
operator or or_eq private protected public register reinterpret_cast return sizeof static
static_assert static_cast struct switch template this thread_local throw true try typedef typeid
typename using virtual volatile while xor xor_eq override final nullptr co_await co_return
co_yield requires concept
""".split())

CPP_TYPES: frozenset[str] = frozenset("""
bool char short int long float double void signed unsigned size_t ssize_t intptr_t uintptr_t
uint8_t uint16_t uint32_t uint64_t int8_t int16_t int32_t int64_t byte word boolean String File
Stream wchar_t char8_t char16_t char32_t ptrdiff_t clock_t time_t va_list int_fast8_t uint_fast8_t
""".split())

ARDUINO_APIS: frozenset[str] = frozenset("""
setup loop pinMode digitalWrite digitalRead analogRead analogWrite analogReference delay
delayMicroseconds millis micros yield shiftIn shiftOut tone noTone noInterrupts interrupts
attachInterrupt detachInterrupt serialEvent Serial Stream Keyboard Mouse Server Client IPAddress
EEPROM SPI Wire Wire1 SD ESP begin end read write print println printf sprintf snprintf flush
peek available availableForWrite parseFloat parseInt setTimeout setClockDivider attach detach
ledcSetup ledcAttachPin ledcWrite ledcRead digitalWriteFast delayMicrosecondsFast
""".split())

ARDUINO_CONSTANTS: frozenset[str] = frozenset("""
HIGH LOW INPUT INPUT_PULLUP INPUT_PULLDOWN OUTPUT OPEN_DRAIN CHANGE RISING FALLING LED_BUILTIN
BUILTIN_LED SS MISO MOSI SCK A0 A1 A2 A3 A4 A5 A6 A7 A8 A9 A10 A11 A12 A13 RX TX RX0 TX0 RX1 TX1
SDA SCL DTR RTS DEC BIN HEX OCT BYTE LSBFIRST MSBFIRST true false NULL nullptr F PGM_P PROGMEM
""".split())

#: Libraries that show up in ``#include`` a lot - coloured like Arduino APIs.
COMMON_LIBS: frozenset[str] = frozenset("""
Servo Stepper LiquidCrystal LiquidCrystal_I2C Adafruit_GFX Adafruit_ILI9341 Adafruit_Sensor
Adafruit_BME280 Adafruit_NeoPixel DHT OneWire DallasTemperature FastLED MFRC522 NRF24
ESPAsyncWebServer AsyncWebServer Blinker Ticker PWM WebServer Update ArduinoJson SPIFFS LittleFS
FreeRTOS BLE WiFi WiFiClient WiFiServer WiFiUDP HTTPClient HTTPUpdate OTA DNSServer ESPmDNS
WiFiClientSecure ESP8266WiFi ESP8266WebServer ESP8266HTTPClient PubSubClient Adafruit_SSD1306
U8g2 ESPAsyncUDP
""".split())

#: Documents longer than this are highlighted window by window (see CodeEditor).
MAX_SCAN_CHARS = 220_000

_TODO_RE = re.compile(r"\b(?:TODO|FIXME|HACK|XXX|NOTE|BUG|DANGER)\b")

#: One master pattern; group order decides precedence at a given position.
_MASTER_RE = re.compile(
    r"""
    (?P<comment_block>   /\*.*?\*/ | /\*(?:(?!\*/).)*\Z )
  | (?P<comment_line>    //[^\n]* )
  | (?P<preproc>         ^[ \t]*\#[ \t]*[A-Za-z_][A-Za-z_0-9]*[^\n]* )
  | (?P<string>          "(?:\\.|[^"\\\n])*"? )
  | (?P<char>            '(?:\\.|[^'\\\n]){1,4}'? )
  | (?P<number>          \b(?:0[xXbB][0-9a-fA-F]+|0[0-7]+|(?:\d+'\d+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?)[uUlLfF]{0,4}\b
                       | \.\d+(?:[eE][-+]?\d+)?[fF]? )
  | (?P<ident>           [A-Za-z_][A-Za-z_0-9]* )
  | (?P<punct>           [-+*/%<>=!&|^~?:]+ | [()\[\]{};,.] )
    """,
    re.S | re.M | re.X,
)

_INCLUDE_TARGET_RE = re.compile(r'^[ \t]*\#[ \t]*include[ \t]*(<[^>\n]*>|"[^"\n]*")')


@dataclass(frozen=True)
class Token:
    """A highlighted span; ``start``/``end`` are character indexes (half open)."""

    start: int
    end: int
    tag: str

    def __len__(self) -> int:  # pragma: no cover - debugging helper
        return max(0, self.end - self.start)


class ArduinoKeywords:
    """Keyword sets used by :func:`tokenize` (overridable per project)."""

    def __init__(
        self,
        keywords: Optional[Sequence[str]] = None,
        types: Optional[Sequence[str]] = None,
        apis: Optional[Sequence[str]] = None,
        constants: Optional[Sequence[str]] = None,
        libs: Optional[Sequence[str]] = None,
    ) -> None:
        self.keywords = frozenset(C_KEYWORDS if keywords is None else keywords)
        self.types = frozenset(CPP_TYPES if types is None else types)
        self.apis = frozenset(ARDUINO_APIS if apis is None else apis)
        self.constants = frozenset(ARDUINO_CONSTANTS if constants is None else constants)
        self.libs = frozenset(COMMON_LIBS if libs is None else libs)

    def classify(self, name: str) -> str:
        """Tag name for an identifier (``""`` = default colour)."""
        if not name:
            return ""
        if name in self.keywords:
            return "keyword"
        if name in self.types:
            return "type"
        if name in self.constants:
            return "constant"
        if name in self.apis or name in self.libs:
            return "arduino_api"
        return ""


def tokenize(
    source: str,
    keywords: Optional[ArduinoKeywords] = None,
    *,
    offset: int = 0,
    todo_tags: bool = True,
) -> list[Token]:
    """Return the highlight spans for *source*.

    ``offset`` is added to every index, which lets the editor highlight only the
    visible window and still insert correct absolute Text indexes.
    """
    keywords = keywords or ArduinoKeywords()
    tokens: list[Token] = []
    append = tokens.append
    length = len(source)
    position = 0
    while position < length:
        match = _MASTER_RE.match(source, position)
        if match is None or match.end() == position:
            position += 1
            continue
        start, end = match.start(), match.end()
        kind = match.lastgroup or ""
        if kind == "ident":
            name = match.group()
            tag = keywords.classify(name)
            if not tag:
                tail = source[end:end + 8].lstrip()
                if tail.startswith("("):
                    tag = "function"
                elif source[max(0, start - 24):start].rstrip().endswith(("class", "struct", "enum", "union", "typename")):
                    tag = "type"
                elif name[:1].isupper() and len(name) > 2 and not name.isupper():
                    tag = "type"
            if tag:
                append(Token(start + offset, end + offset, tag))
        elif kind == "preproc":
            append(Token(start + offset, end + offset, "preprocessor"))
            target = _INCLUDE_TARGET_RE.match(source[start:end + 1])
            if target:
                append(Token(start + offset + target.start(1), start + offset + target.end(1), "macro"))
        elif kind in {"comment_block", "comment_line"}:
            append(Token(start + offset, end + offset, "comment"))
            if todo_tags:
                comment = match.group()
                for todo in _TODO_RE.finditer(comment):
                    append(Token(start + offset + todo.start(), start + offset + todo.end(), "todo"))
        elif kind == "string":
            append(Token(start + offset, end + offset, "string"))
        elif kind == "char":
            append(Token(start + offset, end + offset, "char"))
        elif kind == "number":
            append(Token(start + offset, end + offset, "number"))
        position = end
    return tokens


def is_open_block_comment(source: str, upto: int = -1) -> bool:
    """True when a ``/*`` opened before *upto* is still open at that point."""
    text = source if upto < 0 else source[:max(0, upto)]
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "*":
            close = text.find("*/", index + 2)
            if close == -1:
                return True
            index = close + 2
            continue
        if char == "/" and nxt == "/":
            newline = text.find("\n", index)
            if newline == -1:
                return False
            index = newline + 1
            continue
        if char in "\"'":
            quote = char
            index += 1
            while index < length:
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote or text[index] == "\n":
                    index += 1
                    break
                index += 1
            continue
        index += 1
    return False


def strip_comment_and_string_spans(source: str) -> str:
    """Same length as *source*, with comment/string bodies blanked to spaces.

    Bracket matching and auto-indent use this so a ``{`` inside a string or
    comment cannot influence the editor.
    """
    out = list(source)
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        nxt = source[index + 1] if index + 1 < length else ""
        if char == "/" and nxt == "*":
            close = source.find("*/", index + 2)
            stop = length if close == -1 else close + 2
            for pos in range(index, stop):
                if out[pos] != "\n":
                    out[pos] = " "
            index = stop
            continue
        if char == "/" and nxt == "/":
            stop = source.find("\n", index)
            if stop == -1:
                stop = length
            for pos in range(index, stop):
                out[pos] = " "
            index = stop
            continue
        if char in "\"'":
            quote = char
            pos = index + 1
            while pos < length:
                if source[pos] == "\\":
                    pos += 2
                    continue
                if source[pos] == "\n" and quote == "'":
                    break
                if source[pos] == quote:
                    pos += 1
                    break
                pos += 1
            for fill in range(index, min(pos, length)):
                if out[fill] != "\n":
                    out[fill] = " "
            index = min(pos, length)
            continue
        index += 1
    return "".join(out)


def find_matching(
    source: str,
    index: int,
    pairs: str = "()[]{}",
    *,
    masked: Optional[str] = None,
) -> Optional[int]:
    """Index of the bracket matching the one at *index* (``None`` if unbalanced).

    *source* is the raw buffer; pass ``masked`` (see
    :func:`strip_comment_and_string_spans`) to skip comments and strings.
    """
    text = masked if masked is not None else source
    if index < 0 or index >= len(text) or text[index] not in pairs:
        return None
    opening, closing = pairs[0::2], pairs[1::2]
    forward = text[index] in opening
    depth = 0
    if forward:
        for position in range(index, len(text)):
            char = text[position]
            if char in opening:
                depth += 1
            elif char in closing:
                depth -= 1
                if depth == 0:
                    return position if position != index else None
        return None
    for position in range(index, -1, -1):
        char = text[position]
        if char in closing:
            depth += 1
        elif char in opening:
            depth -= 1
            if depth == 0:
                return position if position != index else None
    return None


def indent_of(line: str, tab_size: int = 4) -> int:
    """Visual width of a line's leading whitespace."""
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += tab_size - (width % max(1, tab_size))
        else:
            break
    return width


def leading_whitespace(line: str) -> str:
    """The whitespace prefix of *line*."""
    stripped = line.lstrip(" \t")
    return line[: len(line) - len(stripped)]


def brace_balance(line: str) -> int:
    """``{`` minus ``}`` in a code-only line (comments/strings already masked)."""
    return line.count("{") - line.count("}")


def iter_regex_tokens(pattern: str, source: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` for every *pattern* hit (helper for tests)."""
    regex = re.compile(pattern, re.S | re.M)
    for match in regex.finditer(source):
        yield match.start(), match.end()
