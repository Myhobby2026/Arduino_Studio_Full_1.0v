"""Built-in example sketches (``File -> New Example``).

Each example is a full project template: a mapping of project-relative file
names to their content.  Generating an example therefore produces a real,
compilable Arduino Studio project (main ``.ino`` + ``include/`` + ``src/``),
not just a sketch file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = ["ExampleDef", "EXAMPLES", "get_example", "EEPROM_STRING_STORAGE"]

# ---------------------------------------------------------------------------------------
# EEPROM String Storage
# ---------------------------------------------------------------------------------------
EEPROM_STRING_STORAGE = r'''/***************************************************************************************
 * EEPROM String Storage
 * ---------------------
 * Stores one text line in EEPROM and reads it back over the serial port.
 *
 * Serial commands (115200 baud, end of line = LF or CR):
 *
 *   SAVE:<text>    write <text> to EEPROM        -> replies "OK: Saved (n bytes)"
 *   READ           read the stored text back     -> replies "DATA:<text>"
 *   CLEAR          erase the stored record       -> replies "OK: Cleared"
 *   STATUS         show capacity / validity       -> replies "STATUS: ..."
 *   HELP           list the commands
 *
 * Robustness:
 *   - a magic byte plus a checksum detects corrupted / never-written EEPROM,
 *   - the stored length is validated against the record area,
 *   - identical bytes are not rewritten (EEPROM endurance),
 *   - ESP8266 / ESP32 use their buffered EEPROM API, so commit() is called
 *     through a compile-time guard (AVR has no commit() and no begin()).
 *
 * Board notes:
 *   ATmega328P (Uno/Nano)  1024 byte EEPROM  -> max string length  96 bytes
 *   ATmega2560 (Mega)     4096 byte EEPROM  -> max string length 512 bytes
 *   ESP32 / ESP8266       emulated in flash  -> max string length 512 bytes
 ****************************************************************************************/

#include <Arduino.h>
#include <EEPROM.h>

/* --------------------------------------------------------------------------------------
 * Layout of the record (byte addresses inside the EEPROM)
 *   0        magic byte 0xA5
 *   1..2     payload length, big endian uint16
 *   3..N     payload bytes
 *   N+1      checksum of the payload
 * ------------------------------------------------------------------------------------ */

static const uint8_t RECORD_MAGIC = 0xA5;
static const int ADDR_MAGIC = 0;
static const int ADDR_LEN_HI = 1;
static const int ADDR_LEN_LO = 2;
static const int ADDR_PAYLOAD = 3;

#if defined(ESP32) || defined(ESP8266)
  /* ESP cores emulate EEPROM in a RAM buffer that must be sized and committed. */
  #define ARDUINO_STUDIO_EEPROM_BUFFERED 1
  static const size_t EEPROM_SIZE = 1024;
  static const size_t MAX_STRING_LENGTH = 512;
#elif defined(__AVR_ATmega2560__)
  #define ARDUINO_STUDIO_EEPROM_BUFFERED 0
  static const size_t EEPROM_SIZE = 4096;
  static const size_t MAX_STRING_LENGTH = 512;
#else
  /* ATmega328P (Uno, Nano, Pro Mini) and friends: 1024 byte EEPROM. */
  #define ARDUINO_STUDIO_EEPROM_BUFFERED 0
  static const size_t EEPROM_SIZE = 1024;
  static const size_t MAX_STRING_LENGTH = 96;
#endif

static const size_t RECORD_BYTES = ADDR_PAYLOAD + MAX_STRING_LENGTH + 1;

static const unsigned long SERIAL_BAUD = 115200;
static const int SERIAL_WAIT_MS = 3000;      /* wait for native-USB serial ports */

String lineBuffer;                          /* incoming serial line            */
uint32_t saveCounter = 0;                   /* number of successful writes     */

/* --------------------------------------------------------------------------------------
 * low level EEPROM helpers
 * ------------------------------------------------------------------------------------ */

static inline uint8_t eepromRead(int address) {
  return EEPROM.read(address);
}

/** Write one byte, skipping it when the EEPROM already holds that value. */
static inline void eepromWriteDedup(int address, uint8_t value) {
  if (eepromRead(address) != value) {
    EEPROM.write(address, value);
  }
}

/** Flush the buffered ESP implementation; a no-op on AVR. */
static void eepromCommit() {
#if ARDUINO_STUDIO_EEPROM_BUFFERED
  EEPROM.commit();
#endif
}

static uint8_t checksumOf(const String &text) {
  uint8_t sum = 0;
  for (size_t i = 0; i < text.length(); ++i) {
    sum = (uint8_t)(sum + (uint8_t)text[i]);
  }
  return sum;
}

/* --------------------------------------------------------------------------------------
 * record handling
 * ------------------------------------------------------------------------------------ */

/**
 * Read the stored string.
 * @param out    filled with the payload when the record is valid
 * @param reason human readable problem when the return value is false
 * @return true when a valid, uncorrupted record was found
 */
bool readStoredString(String &out, String &reason) {
  out = "";
  const uint8_t magic = eepromRead(ADDR_MAGIC);
  if (magic != RECORD_MAGIC) {
    reason = (magic == 0xFF || magic == 0x00) ? "EEPROM is empty" : "bad magic byte (corrupted record)";
    return false;
  }
  const uint16_t length = ((uint16_t)eepromRead(ADDR_LEN_HI) << 8) | (uint16_t)eepromRead(ADDR_LEN_LO);
  if (length == 0xFFFF || length == 0x0000) {
    reason = "stored length is invalid";
    return false;
  }
  if ((size_t)length > MAX_STRING_LENGTH) {
    reason = "stored length exceeds the record size";
    return false;
  }
  if ((size_t)ADDR_PAYLOAD + length >= EEPROM_SIZE) {
    reason = "stored length does not fit this board";
    return false;
  }
  String payload;
  payload.reserve(length + 1);
  uint8_t sum = 0;
  for (uint16_t i = 0; i < length; ++i) {
    uint8_t byte = eepromRead(ADDR_PAYLOAD + i);
    if (byte == 0xFF || byte == 0x00) {
      // 0xFF is the erased state, 0x00 never appears in our saved payloads
      reason = "payload contains erased or zero bytes (corrupted)";
      return false;
    }
    payload += (char)byte;
    sum = (uint8_t)(sum + byte);
  }
  const uint8_t stored = eepromRead(ADDR_PAYLOAD + length);
  if (stored != sum) {
    reason = "checksum mismatch (data was damaged)";
    return false;
  }
  out = payload;
  reason = "ok";
  return true;
}

/**
 * Write a string (and its metadata) into the EEPROM.
 * @return false when the value is empty or too long
 */
bool writeStoredString(const String &value, String &reason) {
  if (value.length() == 0) {
    reason = "nothing to save (empty text)";
    return false;
  }
  if (value.length() > MAX_STRING_LENGTH) {
    reason = "text is too long for this board";
    return false;
  }
  const uint16_t length = (uint16_t)value.length();
  eepromWriteDedup(ADDR_MAGIC, RECORD_MAGIC);
  eepromWriteDedup(ADDR_LEN_HI, (uint8_t)((length >> 8) & 0xFF));
  eepromWriteDedup(ADDR_LEN_LO, (uint8_t)(length & 0xFF));
  for (uint16_t i = 0; i < length; ++i) {
    eepromWriteDedup(ADDR_PAYLOAD + i, (uint8_t)value[i]);
  }
  eepromWriteDedup(ADDR_PAYLOAD + length, checksumOf(value));
  eepromCommit();
  reason = "ok";
  return true;
}

/** Erase the record so that READ reports "empty" again. */
void eraseStoredString() {
  for (size_t address = ADDR_MAGIC; address < RECORD_BYTES && address < EEPROM_SIZE; ++address) {
    EEPROM.write((int)address, 0xFF);
  }
  eepromCommit();
}

/* --------------------------------------------------------------------------------------
 * serial protocol
 * ------------------------------------------------------------------------------------ */

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  SAVE:<text>   store <text> in EEPROM and answer OK: Saved"));
  Serial.println(F("  READ          answer with DATA:<text>"));
  Serial.println(F("  CLEAR         erase the stored record"));
  Serial.println(F("  STATUS        show capacity and validity of the record"));
  Serial.println(F("Send one command per line (LF or CR)."));
}

void printStatus() {
  String value;
  String reason;
  const bool valid = readStoredString(value, reason);
  Serial.print(F("STATUS: chip="));
#if defined(ESP32)
  Serial.print(F("ESP32"));
#elif defined(ESP8266)
  Serial.print(F("ESP8266"));
#elif defined(__AVR_ATmega2560__)
  Serial.print(F("ATmega2560"));
#elif defined(__AVR_ATmega328P__)
  Serial.print(F("ATmega328P"));
#else
  Serial.print(F("AVR"));
#endif
  Serial.print(F(", eepromBytes="));
  Serial.print((unsigned long)EEPROM_SIZE);
  Serial.print(F(", maxString="));
  Serial.print((unsigned long)MAX_STRING_LENGTH);
  Serial.print(F(", commitApi="));
#if ARDUINO_STUDIO_EEPROM_BUFFERED
  Serial.print(F("ESP (needs commit)"));
#else
  Serial.print(F("AVR (immediate)"));
#endif
  Serial.print(F(", saves="));
  Serial.print((unsigned long)saveCounter);
  Serial.println();
  if (valid) {
    Serial.print(F("STATUS: record valid, length="));
    Serial.print(value.length());
    Serial.print(F(" bytes, checksum=0x"));
    Serial.println(checksumOf(value), HEX);
  } else {
    Serial.print(F("STATUS: no valid record ("));
    Serial.print(reason);
    Serial.println(F(")"));
  }
}

void handleCommand(String line) {
  line.trim();
  if (line.length() == 0) {
    return;
  }

  if (line.startsWith("SAVE:") || line.startsWith("save:")) {
    const String payload = line.substring(5);
    payload.trim();
    String reason;
    if (writeStoredString(payload, reason)) {
      saveCounter++;
      Serial.print(F("OK: Saved ("));
      Serial.print(payload.length());
      Serial.println(F(" bytes)"));
    } else {
      Serial.print(F("ERR: "));
      Serial.println(reason);
    }
    return;
  }

  if (line.equalsIgnoreCase("READ")) {
    String value;
    String reason;
    if (readStoredString(value, reason)) {
      Serial.print(F("DATA:"));
      Serial.println(value);
    } else {
      Serial.print(F("ERR: "));
      Serial.println(reason);
    }
    return;
  }

  if (line.equalsIgnoreCase("CLEAR")) {
    eraseStoredString();
    Serial.println(F("OK: Cleared"));
    return;
  }

  if (line.equalsIgnoreCase("STATUS")) {
    printStatus();
    return;
  }

  if (line.equalsIgnoreCase("HELP")) {
    printHelp();
    return;
  }

  Serial.print(F("ERR: unknown command '"));
  Serial.print(line);
  Serial.println(F("' (send HELP)"));
}

/* --------------------------------------------------------------------------------------
 * Arduino entry points
 * ------------------------------------------------------------------------------------ */

void setup() {
  Serial.begin(SERIAL_BAUD);
#if ARDUINO_STUDIO_EEPROM_BUFFERED
  EEPROM.begin(EEPROM_SIZE);
#endif
  // boards with a native USB-CDC port need a moment before the host listens
  unsigned long started = millis();
  while (!Serial && (millis() - started) < SERIAL_WAIT_MS) {
    yield();
  }

  delay(200);
  Serial.println();
  Serial.println(F("EEPROM String Storage ready"));
  printHelp();

  String value;
  String reason;
  if (readStoredString(value, reason)) {
    Serial.print(F("INFO: stored record found: "));
    Serial.println(value);
  } else {
    Serial.print(F("INFO: "));
    Serial.print(reason);
    Serial.println(F(" - send SAVE:<text> to store something"));
  }
}

void loop() {
  while (Serial.available() > 0) {
    const char received = (char)Serial.read();
    if (received == '\n' || received == '\r') {
      if (lineBuffer.length() > 0) {
        handleCommand(lineBuffer);
        lineBuffer = "";
      }
    } else if (lineBuffer.length() < 640) {
      lineBuffer += received;
    } else {
      // runaway input: drop the line instead of exhausting the RAM
      Serial.println(F("ERR: line too long, discarded"));
      lineBuffer = "";
    }
  }
}
'''

BLINK_STANDARDE = r'''/***************************************************************************************
 * Blink (with a serial heartbeat)
 * --------------------------------
 * The classic "hello hardware" sketch, extended so you can see in the Serial
 * Monitor that the loop is still alive - useful when a board seems frozen.
 ****************************************************************************************/

const uint8_t LED_PIN = LED_BUILTIN;
const unsigned long BLINK_MS = 1000;

unsigned long lastToggle = 0;
uint8_t blinkCount = 0;

void setup() {
  pinMode(LED_PIN, OUTPUT);
  Serial.begin(115200);
  Serial.println(F("Blink started"));
}

void loop() {
  const unsigned long now = millis();
  if (now - lastToggle >= BLINK_MS) {
    lastToggle = now;
    blinkCount++;
    digitalWrite(LED_PIN, blinkCount % 2);
    Serial.print(F("blink "));
    Serial.println(blinkCount);
  }
}
'''

ANALOG_LOGGER = r'''/***************************************************************************************
 * Analog CSV Logger
 * ------------------
 * Samples an analog pin, prints CSV to the Serial port and keeps a small ring
 * buffer so the newest samples can be replayed with the commands below.
 *
 * Commands: START | STOP | DUMP | RATE:<ms> | THRESHOLD:<value>
 ****************************************************************************************/

#include <Arduino.h>

const uint8_t SENSOR_PIN = A0;
const size_t HISTORY_SIZE = 64;

unsigned long intervalMs = 250;
unsigned long lastSample = 0;
bool logging = true;
int threshold = 512;

uint16_t history[HISTORY_SIZE];
size_t historyFill = 0;
size_t historyIndex = 0;

String inputLine;

void pushSample(uint16_t value) {
  history[historyIndex] = value;
  historyIndex = (historyIndex + 1) % HISTORY_SIZE;
  if (historyFill < HISTORY_SIZE) {
    historyFill++;
  }
}

void dumpHistory() {
  Serial.println(F("TIME_MS,VALUE,ABOVE"));
  const unsigned long now = millis();
  for (size_t i = 0; i < historyFill; ++i) {
    const size_t position = (historyIndex + HISTORY_SIZE - historyFill + i) % HISTORY_SIZE;
    const uint16_t value = history[position];
    Serial.print(now - (unsigned long)(historyFill - i) * intervalMs);
    Serial.print(',');
    Serial.print(value);
    Serial.print(',');
    Serial.println(value > threshold ? 1 : 0);
  }
}

void handleCommand(String line) {
  line.trim();
  if (line.length() == 0) {
    return;
  }
  if (line.equalsIgnoreCase("START")) {
    logging = true;
    Serial.println(F("OK: logging on"));
  } else if (line.equalsIgnoreCase("STOP")) {
    logging = false;
    Serial.println(F("OK: logging off"));
  } else if (line.equalsIgnoreCase("DUMP")) {
    dumpHistory();
  } else if (line.startsWith("RATE:") || line.startsWith("rate:")) {
    const long value = line.substring(5).toInt();
    intervalMs = constrain(value, 10, 60000);
    Serial.print(F("OK: rate "));
    Serial.print(intervalMs);
    Serial.println(F(" ms"));
  } else if (line.startsWith("THRESHOLD:") || line.startsWith("threshold:")) {
    threshold = constrain(line.substring(10).toInt(), 0, 1023);
    Serial.print(F("OK: threshold "));
    Serial.println(threshold);
  } else {
    Serial.println(F("ERR: unknown command (START STOP DUMP RATE:<ms> THRESHOLD:<v>)"));
  }
}

void setup() {
  Serial.begin(115200);
  analogReference(DEFAULT);
  Serial.println(F("ms,value"));
  Serial.println(F("Commands: START STOP DUMP RATE:<ms> THRESHOLD:<0-1023>"));
}

void loop() {
  while (Serial.available() > 0) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      handleCommand(inputLine);
      inputLine = "";
    } else if (inputLine.length() < 64) {
      inputLine += c;
    }
  }

  const unsigned long now = millis();
  if (logging && now - lastSample >= intervalMs) {
    lastSample = now;
    const uint16_t value = (uint16_t)analogRead(SENSOR_PIN);
    pushSample(value);
    Serial.print(now);
    Serial.print(',');
    Serial.print(value);
    Serial.print(',');
    Serial.println(value > threshold ? 1 : 0);
  }
}
'''

SERVO_SCANNER = r'''/***************************************************************************************
 * Servo Scanner (with software UART-safe setup)
 * ---------------------------------------------
 * Sweeps a servo from 0 to 180 degrees and reports the position over Serial.
 * Attach the servo signal wire to pin 9 and power it from an external 5 V supply.
 ****************************************************************************************/

#include <Arduino.h>
#include <Servo.h>

const uint8_t SERVO_PIN = 9;
const int STEP_DEGREES = 5;
const unsigned long DWELL_MS = 20;

Servo servo;
int position = 0;
int direction = STEP_DEGREES;
unsigned long lastStep = 0;

void setup() {
  Serial.begin(115200);
  servo.attach(SERVO_PIN);
  servo.write(0);
  Serial.println(F("Servo scanner running - send 0..180 to move it, 'STOP' or 'RUN'"));
}

void loop() {
  while (Serial.available() > 0) {
    static String buffer;
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      buffer.trim();
      if (buffer.equalsIgnoreCase("STOP")) {
        Serial.println(F("OK: stopped"));
        while (true) {
          yield();
        }
      } else if (buffer.equalsIgnoreCase("RUN")) {
        lastStep = millis();
        Serial.println(F("OK: running"));
      } else {
        const long angle = buffer.toInt();
        if (angle >= 0 && angle <= 180) {
          servo.write((int)angle);
          position = (int)angle;
          Serial.print(F("OK: positioned at "));
          Serial.print(position);
          Serial.println(F(" deg"));
        } else {
          Serial.println(F("ERR: send 0..180, STOP or RUN"));
        }
      }
      buffer = "";
    } else if (buffer.length() < 16) {
      buffer += c;
    }
  }

  if (millis() - lastStep < DWELL_MS) {
    return;
  }
  lastStep = millis();
  position += direction;
  if (position >= 180) {
    position = 180;
    direction = -STEP_DEGREES;
  } else if (position <= 0) {
    position = 0;
    direction = STEP_DEGREES;
  }
  servo.write(position);
  Serial.print(F("pos="));
  Serial.println(position);
}
'''


@dataclass(frozen=True)
class ExampleDef:
    """A ready-to-create project template."""

    id: str
    title: str
    summary: str
    files: dict[str, str]
    suggested_name: str = ""
    fqbn_hint: str = "arduino:avr:uno"
    serial_baud: int = 115200
    tags: tuple[str, ...] = field(default_factory=tuple)
    readme: str = ""


EEPROM_README = """# EEPROM String Storage

Stores one text line in EEPROM and reads it back through the serial port.

## Wiring / setup
1. Upload the sketch (Uno/Nano: **arduino:avr:uno**, ESP32: **esp32:esp32:esp32**).
2. Open the Serial Monitor at **115200 baud**, line ending *None* (the sketch ends
   its own lines).

## Protocol
| send                | answer                                   |
|---------------------|--------------------------------------------|
| `SAVE:hello world`  | `OK: Saved (11 bytes)`                     |
| `READ`              | `DATA:hello world`                         |
| `CLEAR`             | `OK: Cleared`                              |
| `STATUS`            | chip, EEPROM size, max length, validity    |

Corrupted data is detected: a magic byte (0xA5), the length field (max 96 bytes on
ATmega328P, 512 on ATmega2560/ESP32/ESP8266) and a payload checksum must all agree,
otherwise `READ` answers `ERR: ...`.

## Portability
`EEPROM.commit()` and `EEPROM.begin(size)` only exist on the ESP cores, so they are
used behind `#if defined(ESP32) || defined(ESP8266)` guards. On AVR every byte is
compared before writing, which protects the ~100 000 cycle EEPROM endurance.

Try it from the Arduino Studio terminal:

    arduino-cli compile --fqbn arduino:avr:uno .
    arduino-cli upload --port COM5 --fqbn arduino:avr:uno .
"""

BLINK_README = """# Blink

Blinks `LED_BUILTIN` and prints a heartbeat line every second, so you can tell a
frozen sketch from a working one.
"""

ANALOG_README = """# Analog CSV Logger

Prints `ms,value,above` lines, buffers the last 64 samples and can dump them with
the `DUMP` command. Change speed with `RATE:<ms>` and the trigger level with
`THRESHOLD:<0-1023>`.
"""

SERVO_README = """# Servo Scanner

Sweeps a servo 0-180 degrees on pin 9. Use an external 5 V supply for the servo -
the board's 5 V pin cannot drive most servos reliably.
"""

EXAMPLES: tuple[ExampleDef, ...] = (
    ExampleDef(
        id="eeprom-string",
        title="EEPROM String Storage",
        summary=(
            "Serial protocol (SAVE:/READ/CLEAR/STATUS) that stores a text line in EEPROM. "
            "Magic byte + length + checksum validation, EEPROM endurance protection and "
            "conditional commit support for ESP32/ESP8266."
        ),
        files={"{project}.ino": EEPROM_STRING_STORAGE, "README.md": EEPROM_README},
        suggested_name="EepromStringStorage",
        fqbn_hint="arduino:avr:uno",
        serial_baud=115200,
        tags=("EEPROM", "Serial", "ESP32", "ESP8266", "Uno", "Nano"),
    ),
    ExampleDef(
        id="blink",
        title="Blink with Serial Heartbeat",
        summary="The classic blink sketch plus a heartbeat line in the Serial Monitor.",
        files={"{project}.ino": BLINK_STANDARDE, "README.md": BLINK_README},
        suggested_name="Blink",
        tags=("beginner", "GPIO"),
    ),
    ExampleDef(
        id="analog-logger",
        title="Analog CSV Logger",
        summary="Samples an analog pin as CSV, with a ring buffer and runtime commands.",
        files={"{project}.ino": ANALOG_LOGGER, "README.md": ANALOG_README},
        tags=("analog", "serial", "logging"),
    ),
    ExampleDef(
        id="servo-scanner",
        title="Servo Scanner",
        summary="Sweeps a servo and accepts position/stop/run commands over Serial.",
        files={"{project}.ino": SERVO_SCANNER, "README.md": SERVO_README},
        tags=("servo", "actuator"),
        fqbn_hint="arduino:avr:uno",
    ),
)


def example_project_name(example: ExampleDef) -> str:
    """Folder / sketch name to use when creating *example*."""
    return example.suggested_name or "".join(
        part.capitalize() for part in example.title.replace("(", " ").replace(")", " ").split()
        if part.isalpha() or part[0].isalpha()
    ).replace(" ", "") or "ExampleSketch"


def get_example(example_id: str) -> Optional[ExampleDef]:
    """Look up an example by id."""
    for example in EXAMPLES:
        if example.id == example_id:
            return example
    return None


def render_files(example: ExampleDef, project_name: str = "") -> dict[str, str]:
    """Replace the ``{project}`` placeholder in file names/paths."""
    name = project_name or example_project_name(example)
    out: dict[str, str] = {}
    for relative, content in example.files.items():
        key = relative.replace("{project}", name)
        value = content.replace("{project}", name)
        out[key] = value
    return out


def example_menu_label(example: ExampleDef) -> str:
    """Label used in the *File -> New Example* menu."""
    return f"{example.title}"
