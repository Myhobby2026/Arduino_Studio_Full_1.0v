# Arduino Studio 1.0

A professional, standalone Arduino IDE for Windows written in Python + Tkinter
(CustomTkinter). It drives **`arduino-cli`** through `subprocess`, so the official
Arduino IDE never has to be installed or running — Studio *is* the editor, the
board manager front-end, the serial monitor, the bootloader tool and the terminal.

Everything runs in a single window: project explorer on the left, tabbed code
editor in the middle, and a bottom panel with **Console / Serial Monitor /
Terminal** tabs. Long operations (compile, upload, install, burn) run on
worker threads, so the interface never freezes, and every result is reported
with plain-language advice rather than a raw traceback.

---

## Contents

1. [What you get](#what-you-get)
2. [Requirements](#requirements)
3. [Install Arduino Studio](#install-arduino-studio)
4. [Install and configure arduino-cli](#install-and-configure-arduino-cli)
5. [First run](#first-run)
6. [Boards, ports and cores](#boards-ports-and-cores)
7. [Verify and upload](#verify-and-upload)
8. [Projects on disk](#projects-on-disk)
9. [Library manager](#library-manager)
10. [Serial monitor](#serial-monitor)
11. [Integrated terminal](#integrated-terminal)
12. [Bootloader manager](#bootloader-manager)
13. [Bundled example: EEPROM String Storage](#bundled-example-eeprom-string-storage)
14. [Menus and keyboard shortcuts](#menus-and-keyboard-shortcuts)
15. [Settings reference](#settings-reference)
16. [Where Studio keeps its files](#where-studio-keeps-its-files)
17. [Troubleshooting](#troubleshooting)
18. [Running the tests and the self-check](#running-the-tests-and-the-self-check)
19. [Building a standalone .exe](#building-a-standalone-exe)
20. [How the code is organised](#how-the-code-is-organised)

---

## What you get

| Area | What Arduino Studio does |
| --- | --- |
| **Projects** | Create, open, rename, duplicate, delete; per-project `project.json`; explorer tree with the files Arduino cares about (`.ino`, `.h`, `.hpp`, `.cpp`, `.c`, `.txt`, `.json`, `.md`); closable tabs; unsaved-dot indicators. |
| **Editor** | Arduino/C++ syntax highlighting (own tokenizer, no third-party widget), line numbers, current-line highlight, auto-indent, bracket auto-close + match highlighting, auto-close quotes, block indent/outdent, toggle comment, duplicate/delete line, go to line, go to matching bracket, find / find-next / find-previous / replace / find-all, full undo–redo, Ctrl+S per file, dark or light theme, zoom, word wrap, autosave timer. |
| **Boards & ports** | `board listall` + `upload-port list` in the background, one-click refresh (F5), Uno/Nano/Mega 2560/ESP8266/ESP32 presets built in, custom FQBN support, board and port selectors in the toolbar *and* the menus. |
| **Build & flash** | Verify (Ctrl+R), clean rebuild (Ctrl+Shift+R), Upload (Ctrl+U), Upload Using Programmer, export compiled binary, open the build folder, clean the build cache. Console shows progress, elapsed time, flash/RAM usage and clickable error lines. Upload is refused until a port is selected. |
| **Libraries** | Search the Library Manager index, see all available versions, install/remove/update, install from a ZIP file or a Git URL, keep libraries inside the project's `libraries/` folder, and an "install the missing library?" prompt when the compiler cannot find an `#include`. |
| **Serial monitor** | Open/close, baud presets + custom value, send with None/LF/CR/CRLF line endings, timestamps, RX/TX echo, hex view, DTR/RTS, filter box, pause, clear, autoscroll, save log, auto-release of the port before an upload and an offer to reopen afterwards. |
| **Terminal** | PowerShell (default) or `cmd`, plus `bash` where present; starts in the project folder; command history with ↑/↓; quick buttons for the common project commands; copy/paste/clear/restart; prints the active directory and each command's exit status; destructive commands need a confirmation. |
| **Bootloader manager** | Burn bootloaders to blank AVRs with a programmer, choose chip/fuses, read chip information, back up firmware, and a separate ESP32/ESP8266 "flash .bin" workflow — with the safety confirmations the operation deserves. |
| **Robustness** | Every subprocess call is bounded by a timeout and cancellable; failures are translated into hints; unknown keys in `settings.json` are preserved; a crash-logging file is always on; `--check` prints a headless support report. |

There are no stub or "TODO" functions: each feature above is implemented in the
source tree in this repository (see [How the code is organised](#how-the-code-is-organised)).

---

## Requirements

* **Python 3.9 or newer** (3.11/3.12 recommended). On Windows use the installer
  from python.org — it includes Tkinter. On Debian/Ubuntu install it separately:
  `sudo apt install python3-tk`.
* **`arduino-cli` 0.32.0 or newer** — the only external tool. Studio will tell
  you which version it found and what is wrong if it cannot use it.
* **Windows 10/11** is the target platform. Linux and macOS work too (the
  terminal falls back to `bash`, and the Recycle Bin option is skipped), but
  COM-port naming and driver help are Windows-oriented.
* Python packages: `customtkinter` (UI) and `pyserial` (serial monitor) — see
  [`requirements.txt`](requirements.txt).

---

## Install Arduino Studio

1. Unzip / clone this repository somewhere permanent, e.g.
   `C:\Tools\arduino-studio`.

2. Create and activate a virtual environment (recommended, keeps the system
   Python clean):

   ```bat
   cd C:\Tools\arduino-studio
   py -3.12 -m venv .venv
   .venv\Scripts\activate
   ```

3. Install the dependencies:

   ```bat
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

   Optional extras: `send2trash` (send deleted projects to the Recycle Bin),
   and `requirements-dev.txt` for PyInstaller/ruff/mypy/pytest.

4. *(Optional)* install it as a package so you get an `arduino-studio` command:

   ```bat
   pip install -e .
   arduino-studio
   ```

5. Start it in any of these ways:

   | Command | What happens |
   | --- | --- |
   | `python run.py` | Launch from the source folder (no install needed). |
   | `python -m arduino_studio` | Same, via the package entry point. |
   | `arduino-studio` | The console script created by `pip install -e .`. |
   | `python run.py C:\Users\me\Documents\Blink` | Open a project folder or a `.ino` file on start-up. |
   | `python run.py --new-project Sensor` | Create `Sensor` in your sketchbook and open it. |
   | `python run.py --setup` | Force the first-run setup screen again. |
   | `python run.py --no-setup` | Skip the setup screen (use it from a script). |
   | `python run.py --config-dir D:\as-profile` | Keep settings/logs in another folder (handy for a portable install). |
   | `python run.py --check` | Print a headless support report and exit — no window. |
   | `python run.py --log-level DEBUG` | More detail in the log file. |

---

## Install and configure arduino-cli

Arduino Studio does not bundle the compiler toolchain; it calls `arduino-cli`,
which downloads cores, libraries and the avrdude/esptool binaries it needs.

### Windows

```powershell
winget install Arduino.ArduinoCLI
```

or, if you use Chocolatey:

```powershell
choco install arduino-cli
```

or download the `.zip` from
<https://arduino.github.io/arduino-cli/latest/installer/>, extract
`arduino-cli.exe` into a folder of your choice (for example
`C:\Tools\arduino-cli\`) and either add that folder to `PATH` or point Studio at
the file directly in the setup screen.

### macOS / Linux

```bash
brew install arduino-cli        # macOS
# or
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
```

### Check it works

```bat
arduino-cli version
arduino-cli config init
```

`config init` creates `arduino-cli.yaml` (on Windows in
`%LOCALAPPDATA%\arduino-cli\`) and prints its path. Studio runs the same index
commands for you, but doing this once makes the first start faster.

Typical things you may want to put in that file:

```yaml
board_manager:
  additional_urls:
    - https://arduino.espressif.com/package_esp32_index.json
directories:
  data: C:\Users\me\.arduino15
  user: C:\Users\me\Documents\Arduino
library:
  enable_unsafe_install: true   # required for "Install from ZIP / Git" (see below)
```

You never have to edit it by hand: Studio's setup screen and
**File ▸ Settings ▸ Arduino CLI** do the same thing, and any extra arguments you
want on every call can go in the *Extra CLI arguments* field (`cli_extra_args`).

---

## First run

Start Studio. If it has not been configured yet you get a four-page setup
window (`_STEPS = Welcome · arduino-cli · Folders · Board & port`) that asks
only for what it cannot safely guess:

1. **Welcome** — what the app needs and why, with the CLI download link.
2. **arduino-cli** — the path to the executable, with *Detect* (searches `PATH`,
   the usual install folders and next to Studio) and a live version check
   against the supported minimum (0.32.0); browsing always works, and "Arduino
   CLI not verified" warns instead of blocking if you want to continue anyway.
3. **Folders** — the CLI's `directories.data` (cores/libraries) and
   `directories.user` (sketchbook); leave them empty to keep arduino-cli's own
   defaults. Underneath are three checkboxes that add a Board Manager URL for
   `esp8266:esp8266`, `esp32:esp32` and `attiny:avr` (the Drazzy megaTinyCore
   index, which is what ATtiny bootloader burning needs).
4. **Board & port** — a board picker (with *From CLI* to pull `board listall`),
   a port picker with *Rescan*, an optional custom FQBN that overrides the
   picker, and "create a blank sketch for this board on start-up".

On **Finish** Studio rebuilds its CLI wrapper, refreshes boards and ports,
probes the version again in the background and — if no project is open — offers
the starter sketch so Verify does something immediately. `first_run_completed`
is then written and the wizard never appears on its own again; **Help ▸ Run Setup
Wizard Again** (or `--setup`) brings it back, `--no-setup` suppresses it.

If `arduino-cli` goes missing later, a dialog offers *Locate it*, *Install it*
and *Open Settings* instead of failing on the next compile.

---

## Boards, ports and cores

**Boards.** Tools ▸ Board lists every board the CLI reports, sorted naturally,
plus the built-in presets (Uno, Nano, Mega 2560, ESP8266 NodeMCU, ESP32 Dev
Module …) so the menu is useful even before an index update. Choosing a board
updates the toolbar, the status bar and `project.json` of the open project.

**Custom FQBN.** Settings ▸ *Use a custom FQBN* + the FQBN field lets you use
any board the core knows about, e.g. `arduino:avr:nano:menu.cpu=atmega328` or
`Megawifi:megaavr:megaWiFi`. Everything else (compile, upload, libraries)
follows that FQBN.

**Cores.** If a sketch targets a board whose platform is not installed, Studio
offers **Tools ▸ Install Core for Current Board** and runs
`arduino-cli core install <platform>` for you (progress in the Console).
`install_missing_cores` (on by default) makes the offer appear automatically
when a compile fails with "platform not found".

**Ports.** Tools ▸ Port is refreshed at start-up, on F5, on window focus and
whenever a build needs a port. Each entry shows the friendly name and the COM
number (or `/dev/ttyUSB*`) that `upload-port list` reported. If the list is
empty Studio says so instead of letting `avrdude` time out.

Handy from a terminal (Studio runs the same commands under the hood):

```bat
arduino-cli core update-index
arduino-cli core install arduino:avr
arduino-cli core install esp32:esp32 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp8266:esp8266 --additional-urls https://arduino.esp8266.com/stable/package_esp8266com_index.json
arduino-cli core install esp8266:esp8266
arduino-cli board listall
arduino-cli board list
arduino-cli upload -p COM5 --fqbn arduino:avr:uno --input-dir build
```

---

## Verify and upload

* **Verify (Ctrl+R)** compiles the whole project folder — the `.ino` plus
  everything in `src/` and `libraries/` — with `arduino-cli compile`.
* **Verify (Clean Build) (Ctrl+Shift+R)** adds `--clean`.
* **Upload (Ctrl+U)** compiles first (unless the build is up to date), then
  uploads with `--input-dir` pointing at the build folder, always with `--verify`
  so a bad flash fails loudly instead of looking fine; *Upload Using Programmer*
  adds `--programmer` instead of the bootloader reset.
* No port selected? Studio prompts for one instead of failing inside `avrdude`.
* The Console gets the raw CLI output plus a summary line built from it
  (`Sketch uses 924 bytes (2%) of program storage space. Maximum is 32256
  bytes.` → "flash 2.9%, RAM 2.5% in 3.4 s"), and the status bar shows the
  compact `flash 2.9%  ram 2.5%` after every successful build.
* Error lines are clickable: `Broken.ino:2:3: error: 'notAFunction' was not
  declared in this scope` jumps to that file and line. Missing `#include`s are
  detected and turned into a *"install Servo?"* offer (see below).
* Builds are cancellable: **Cancel task** in the toolbar (or the command
  palette) stops the running job, and the Console's progress bar relays the
  CLI's own status lines ("Compiling sketch...", "Linking...") with a percentage
  when the output allows it.

By default the compiled artefacts land in `<project>/build/Release/`
(`build_output_mode` = `project`); switch to `temp` if you prefer a throw-away
directory per build. **Sketch ▸ Show Build Folder** opens whatever you chose.

---

## Projects on disk

**File ▸ New Project (Ctrl+Shift+N)** asks for a name and a location and creates
exactly this structure (the `.ino` is named after the folder, which is what
`arduino-cli` requires):

```
BlinkDetector/
├── BlinkDetector.ino      the sketch: setup() / loop() + your code
├── include/
│   └── example.h          header template with an include guard
├── src/
│   └── example.cpp        source template that includes its header
├── libraries/             project-specific libraries live here
│   └── README.md          explains the folder and how to use it
└── project.json           name, main file, FQBN, port, build options, files
```

* Other files are created from templates too: a new `.h`/`.hpp` gets a guard
  named after the project, a new `.cpp` gets the matching include.
* **project.json** is one flat `ProjectManifest`: `schema_version`, `name`,
  `main_file`, `board_fqbn`, `port`, `description`, `author`, `created`,
  `modified`, `build_properties` (`key=value` pairs forwarded to the compiler as
  `--build-property`), `extra_flags`, `libraries` (name/version/scope entries
  for the `libraries/` folder), `open_files` (so the tab layout comes back) and
  `notes`. Unknown keys are ignored on load and preserved nowhere else, so keep
  project-specific data in `notes`. Studio rewrites the file when the project
  state changes; you can also edit it in a tab and press **Sketch ▸ Reload Files
  from Disk**-style refresh or reopen the project.
* Opening a plain folder that only contains `Foo.ino` works too: Studio adopts
  the folder and writes the missing `project.json` (never overwriting an
  existing one). This is how you open projects made with the Arduino IDE.
* **Rename Project** renames the folder, the primary `.ino`, the manifest's
  `name`/`main_file` and — if the folder name was taken — reverts the whole
  operation instead of leaving a half-renamed project behind. **Duplicate
  Project** copies everything except `build/` and `.git/`, using the name you
  typed (a `-copy` suffix only when that folder already exists). **Delete**
  sends the folder to the Recycle Bin when `send2trash` is installed, otherwise
  it is zipped next to the project first — nothing is silently unlinked.
* Names are validated against Windows' rules, so `con`, `nul.txt`, `a/b` or a
  name starting with a digit are refused with an explanation instead of an
  `OSError` from `arduino-cli`.

The explorer is a tree of the project files with a type glyph per extension
(`ino`, `h`, `cpp`, `json`, …) and a right-click menu: *New file…*,
*New folder…*, *Add existing file…*, *Rename (F2)*, *Duplicate*,
*Delete (Del)*, *Copy full path*, *Copy relative path*, *Show in Explorer*,
*Open with Windows editor*, plus (on the project root) *Show project in
Explorer*, *Open project folder in terminal* and *Project properties…*.
Double-click opens a file in a tab.

Tabs show a dot when unsaved and close with the ✕, **Ctrl+W** or a middle-click;
the strip scrolls with the mouse wheel, and the right-click menu offers
*Save <name> · Save all · Close · Close others · Close all · Revert to saved ·
Copy full path · Copy file name · Show in Explorer*. **Alt+←/Alt+→** (or
Ctrl+Tab) cycle tabs.

---

## Library manager

**Tools ▸ Manage Libraries (Ctrl+Shift+L)** opens a panel with two tabs:

* **Library Manager** — searches the `lib search` index as you type (with a
  short debounce), showing name, author, version, category and the headers each
  library provides. Selecting a row lets you install a *specific version* from
  the "available versions" list, or `Name@version` directly.
* **Installed** — what you already have, including the cores' built-in
  libraries and your project's own `libraries/` folder, with an
  **Update available** marker when `lib list --updatable` disagrees with the
  installed version.

Buttons: **Install**, **Remove**, **Update selected**, **Update all**,
**Install from ZIP…**, **Install from Git URL…**, **Update Index**, and a
**Install into project's libraries/** checkbox (remembered in `lib_use_project_dir`).

* ZIP and Git installs are the "unsafe" ones in arduino-cli, so they need
  `library.enable_unsafe_install: true` in its config. When the CLI refuses,
  Studio says so and offers **Enable & retry**, which runs
  `arduino-cli config set library.enable_unsafe_install true` (through the CLI,
  so the right config file is touched) and repeats your install. If the CLI is
  too old to know `--zip-path` at all, Studio falls back to `--zip_path`, then to
  unpacking the archive into `libraries/` itself - and says in the console that
  it did it by extraction. A Git URL with a branch (`…git#dev`) is passed as
  `--git-url <url>#dev`, and retried without the fragment if the CLI rejects it.
* Git installs accept `https://github.com/you/Adafruit_Foo.git#branch` and pass
  the branch through `--git-url`.
* Missing include detection: when a compile fails with
  `fatal error: Adafruit_SGP30.h: No such file or directory`, Studio looks the
  header up in the index and pops a dialog: *"Install Adafruit SGP30 Sensor from
  the Library Manager?"* — Install runs `lib install` in the background and then
  re-verifies the sketch.
* Project-local libraries are handed to the compiler with
  `--libraries <project>/libraries` and `--libraries <project>/src`, so a copy
  inside the project always wins over a global one.

From the command line, for reference:

```bat
arduino-cli lib search SGP30
arduino-cli lib install "Adafruit SGP30 Sensor@2.0.0"
arduino-cli lib install --zip-path C:\downloads\MyLib.zip
arduino-cli lib install --git-url https://github.com/adafruit/Adafruit_NeoPixel.git
arduino-cli lib list --updatable
```

---

## Serial monitor

**Tools ▸ Serial Monitor (Ctrl+M)** opens the bottom-panel tab.

| Control | Behaviour |
| --- | --- |
| Port / baud | Any port Studio knows about + *Refresh*; the usual 300…2000000 presets plus a free-text field. Choosing a board sets a sensible default baud (`baud_for_fqbn`). |
| Open / Close | Closes the handle cleanly; the read thread is asked to stop first so no bytes are lost on close. |
| Line ending | None / LF / CR / CRLF, applied to what you *send* (received data is split on CR/LF either way). |
| Timestamps | `HH:MM:SS.mmm` per received line, on/off. |
| Show RX / TX | Toggle whether incoming and/or your own lines appear. |
| Hex view | Dump raw bytes — useful for I²C/one-wire debugging. |
| Filter | Only lines containing this text are shown (the buffer keeps everything, so clearing the filter reveals the rest). |
| Pause | Freeze the view while the reader keeps buffering. |
| Clear / Save log | Save writes the whole buffer with a `# port @ baud` header into `serial_log_dir` (default `Documents\ArduinoStudioLogs`). |
| Autoscroll | Off means the view stays where you scrolled. |
| DTR / RTS | Check boxes applied when opening — uncheck both to reset-probe a board that hangs in the bootloader, or to talk to a module that must not be reset. |

Typing in the send field never steals focus from the editor, and ↑ recalls the
last command you sent (history is kept per session).

**Port ownership is coordinated with uploads.** Before flashing, Studio closes
the monitor and tells you; if `serial_auto_reopen_after_upload` is on (the
default) it reopens the same port after a successful upload so you see the
boot banner. If something else owns the port you get the actual OS error plus
the usual suspects listed in the dialog.

---

## Integrated terminal

**Tools ▸ Integrated Terminal (Ctrl+`)** opens a real shell as a tab next to the
Console — separate from the serial monitor, so `arduino-cli monitor` and your own
`platformio`/`idf.py` runs are possible in the same window.

* **Shell:** PowerShell (default), `cmd`, or `bash` — whatever exists on this
  machine; the picker shows the full path it will use.
* **Start-up folder:** the open project's folder (`terminal_follow_project_dir`);
  the header line shows the active directory and it updates when the shell
  `cd`s.
* **Commands are queued into the live shell** by the Quick buttons — *board
  list*, *board listall*, *compile*, *upload*, *lib list*, *lib search*, *lib
  install*, *cores*, *update index*, *cache clean*, *version*, *list files* and
  *to project* — each one is inserted into the input line with the CLI path and
  your project folder already filled in, so you can still edit it before
  pressing Enter and see exactly what the tool printed. (The menu items *Open
  Terminal Here* and *Open Serial Port in Terminal* start a shell with the right
  `cd`/`mode`/`screen` command.)
* **History** with ↑/↓ and Ctrl+R-style reverse search, **Clear**, **Copy**,
  **Paste**, and **Restart** to respawn the shell (also after a `exit`).
* **Exit status** is printed after each command (`[exit 1]`), so a failed
  `arduino-cli` call cannot be missed.
* Output streams asynchronously; a 4000-line ring buffer keeps memory flat even
  for `core install` progress bars (ANSI control codes are stripped for display).
* **Destructive commands** (`rm -rf`, `del /q`, `format`, `diskpart`,
  `shutdown`, `icacls`, `> /dev/…`, `Remove-Item -Recurse`, `rd /s`, …) open a
  confirmation showing the exact command line first; `terminal_confirm_destructive`
  turns that guard off if you really do not want it.

---

## Bootloader manager

**Tools ▸ Bootloader Manager… (Ctrl+Shift+I)** — a dedicated dialog with four
tabs. Every AVR action runs `arduino-cli burn-bootloader` (which calls the
`avrdude` bundled with the AVR core), and *nothing* is sent to hardware until
you confirm the summary.

1. **Burn Bootloader** — target board (or bare chip) → programmer → port →
   options. The chip list is what the cores actually support: ATmega32U4,
   ATmega328P, ATmega328PB, ATmega2560 and ATtiny44/84/85; selecting an ESP32
   or ESP8266 board disables the AVR controls and points you at the flash tab.
   *Programmers:* Arduino as ISP, Arduino as ISP (ATmega32U4 board), Atmel-ICE /
   AVRISP mkII, AVRISP mkII (USB), USBasp, USBtinyISP, FT232R bit-bang, Linux
   GPIO, and *Custom* (free-text `-c` config). Each one shows its wiring help —
   the 2×3 ICSP pinout with MOSI/MISO/SCK/RESET/5 V/GND labelled for master and
   slave orientation, the Arduino-as-ISP pin mapping (10 = RESET, 11/12/13 =
   MOSI/MISO/SCK) and the ATtiny notes.
   **Confirm before burning** (on by default) opens a summary: chip, fuse
   profile and every fuse byte, programmer + protocol + speed, port, and the
   warning that the fuse values will overwrite whatever is on the target — plus
   the explicit note that the original bootloader/firmware of a board cannot be
   restored by Studio. You must tick *I understand…* before **Burn** becomes
   enabled (`ask_plan(require_ack=True)`).
2. **Read Chip Information** — signature bytes, fuse/lock bytes, calibration
   and flash size, parsed into a table with the expected signature for the chip
   so `0x1E950F` is immediately recognisable as an ATmega328P. Read-only, so it
   is the right first step on an unknown board.
3. **Backup Firmware** — `avrdude -U flash:r:<file>.hex:i` (plus EEPROM) into
   `bootloader_backup_dir`. The tab states plainly that this is a snapshot of
   what is *currently* on the chip: it does not recover the original vendor
   bootloader or factory-calibrated data if you have already overwritten them.
4. **Flash ESP Firmware (.bin)** — for ESP32/ESP8266, where there is no AVR
   bootloader at all. Pick the `.bin`, the flash address (0x0 default, 0x1000 for
   ESP8266, 0x0/0x1000/0x8000/0xE000 for ESP32 images), baud, flash mode/size/
   frequency, and *erase first*. Uses `esptool.py` from the installed core
   (Studio finds it and can run it through the core's Python), and offers
   *Flash the build output of the open project* so you can skip the file picker.
   A separate **Read** action dumps a region to a file for backup.

The Console shows every `avrdude`/`esptool` line; on failure Studio explains the
usual ones (`ser_open()` → wrong/busy port, `stk500_getsync()` → wrong
bootloader/programmer/baud, `invalid device signature` → wiring or a chip in
reset). A `Custom fuse bytes…` field lets advanced users override `-U lfuse:w:…`
etc., and is flagged in the confirmation as a manual override.

**Safety:** the AVR tabs are disabled until a *programmer*, a *port* (for
serial/`arduino` protocols) and a valid target are selected; the burn buttons
are disabled while any build/upload/burn task is running; and `Ctrl+Shift+I`
never burns — it only opens the dialog.

---

## Bundled example: EEPROM String Storage

**File ▸ New Example ▸ EEPROM String Storage** (Ctrl+Shift+E opens the picker,
which also offers *Blink*, *Analog Logger* and *Servo Scanner*).

The generated project is a complete serial-controlled EEPROM key/value store:

| You send | The sketch replies |
| --- | --- |
| `SAVE:Hello world` | `OK: Saved (11 bytes)` |
| `READ` | `DATA:Hello world` |
| `CLEAR` | `OK: Cleared` |
| `STATUS` | used bytes, magic byte, length, checksum, whether the data is valid |
| `HELP` | the command list |
| anything else | `ERR: unknown command 'X' (send HELP)` |

* Layout: byte 0 = magic `0xA5`, byte 1 = length, bytes 2… = payload, then a
  checksum; a corrupt or uninitialised EEPROM is reported instead of printed.
* `MAX_STRING_LENGTH` adapts to the target — 512 on ESP32/ESP8266, 96 on AVR
  (`EEPROM.length` is checked at run time too, and over-long input is refused
  with a message, not truncated).
* AVR writes go straight to EEPROM; on ESP32/ESP8266 the sketch uses the emulated
  EEPROM and calls `EEPROM.commit()` — guarded by compile-time
  `#if defined(...)` blocks, so the same file compiles for Uno, Mega, ESP32 and
  NodeMCU. Writes are skipped when the value already matches (`eepromWriteDedup`),
  which is what saves the erase cycles.
* 9600 baud, `dtr/rts`-friendly, `setup()` prints a short banner and `loop()` is
  a single `readLineUntil('\n')` state machine.

The example's `README.md` (also generated) contains the wiring/usage notes, and
the project's `board_fqbn` is pre-filled so Verify works immediately.

---

## Menus and keyboard shortcuts

The toolbar is icon-based with a tooltip on every button:
**New project · Open · Save | Verify · Upload | Board ▾ · Port ▾ · Refresh |
Serial Monitor · Libraries · Bootloader · Settings**, and **Cancel task** beside
them, which only does something while a build/flash/install is running. The
status bar shows project · board · port · memory after the last build · current
operation · clock, so you can read the state of the toolchain without opening a
menu.

Every accelerator below is bound in the running app, and a smoke test
(`test_menu_accelerators_are_actually_bound`) walks the whole menu tree to make
sure no menu item advertises a shortcut that nothing binds.

| App-wide | Bottom panel / views | Editor (while the code area has focus) | Editor (while the code area has focus) |
| --- | --- | --- |
| Ctrl+Shift+N new project | Ctrl+B toggle explorer | Tab / Shift+Tab indent / outdent |
| Ctrl+O open project | Ctrl+J toggle bottom panel | Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z undo / redo |
| Ctrl+S save, Ctrl+Shift+S save all | Ctrl+M serial monitor | Ctrl+X / Ctrl+C / Ctrl+V / Ctrl+A cut, copy, paste, select all |
| Ctrl+W close tab | Ctrl+\` integrated terminal | Ctrl+D duplicate line · Ctrl+Shift+K delete line · Ctrl+K cut line |
| Ctrl+R verify, Ctrl+Shift+R clean verify | Ctrl+Shift+L manage libraries | Alt+↑ / Alt+↓ move line · Shift+Alt+↑ / ↓ duplicate line |
| Ctrl+U upload, Ctrl+Shift+U upload using programmer | Ctrl+Shift+I bootloader manager | Ctrl+/ toggle comment |
| Ctrl+G go to line | Ctrl+Shift+E new example | Ctrl+[ or Ctrl+] go to matching bracket |
| Ctrl+F find, Ctrl+H replace | Ctrl+P command palette | Ctrl+L select line · Ctrl+Space completions |
| F3 / Shift+F3 find next / previous | F5 refresh boards and ports | Ctrl+Home / Ctrl+End document start / end |
| F2 / Shift+F2 next / previous build error | Alt+→ / Alt+← / Ctrl+Tab / Ctrl+Shift+Tab cycle tabs | Ctrl+= or Ctrl++ zoom in · Ctrl+- zoom out · Ctrl+0 reset zoom · the numpad `KP_Add` / `KP_Subtract` keys work in the editor and console |
| Escape close the find bar | View ▸ Console / Serial Monitor / Terminal | Page Up / Page Down |

The **Command Palette** (Ctrl+P) is a fuzzy list of every action
— the fastest way to find *Backup Firmware*, *Open Settings Folder*, *Clean
Build Cache*, *New Project* and the rest without hunting menus.

Full menu map:

* **File** — New Project, New Example ▸, Open Project, Open Recent ▸, Save, Save
  All, Close Project, Rename/Duplicate Project, Show Project Folder, Project
  Info…, Settings…, Quit.
* **Edit** — Undo/Redo, Find group, Replace, Find All in File, Toggle Comment,
  Duplicate Line, Delete Line, Indent/Outdent, Go to Matching Bracket, Go to Line.
* **Sketch** — Verify, Verify (Clean Build), Upload, Upload Using Programmer,
  Export Compiled Binary, Show Build Folder, Clean Build Cache, Reload Files from Disk.
* **Tools** — Board ▸, Port ▸, Programmer ▸, Bootloader Manager…, Burn
  Bootloader…, Read Chip Information, Backup Firmware…, Flash ESP Binary…,
  Manage Libraries, Update Library Index, Install Core for Current Board,
  Refresh Boards and Ports, Serial Monitor, Integrated Terminal, Open Terminal
  Here, Open Serial Port in Terminal.
* **View** — Toggle Project Explorer, Toggle Bottom Panel, Console, Serial
  Monitor, Terminal, Zoom In/Out/Reset, Toggle Line Numbers, Word Wrap,
  Auto-Complete, Dark/Light.
* **Help** — Run Setup Wizard Again, Board Manager URLs, Open Settings Folder,
  Open Log File, Arduino Studio Readme, About.

---

## Settings reference

Everything lives in one JSON file so you can back it up or sync it. **File ▸
Settings (Ctrl+,)** is the normal way to edit it; the fields below are the whole
set (defaults in brackets). Unknown keys you add by hand are preserved.

**Appearance & window** — `appearance_mode` [Dark, also Light/System],
`accent_color` [`#2f81f7`], `ui_font_family` [`Segoe UI`], `ui_font_size` [10],
`window_width`/`window_height` [1320×820, minimum 900×600], `window_x`,
`window_y` [−1 = let Windows place it], `window_maximized` [false].

**Editor** — `editor_font_family` [`Cascadia Code`], `editor_font_size` [12],
`tab_size` [4], `insert_spaces` [true], `auto_indent`, `auto_close_brackets`,
`auto_close_quotes`, `auto_complete`, `highlight_current_line`,
`show_line_numbers`, `show_minimap_gutter`, `word_wrap` [false],
`max_recent_projects` [10], `autosave_interval_sec` [0 = off; anything below 2 s
is raised to 2 s], `trim_trailing_ws_on_save` [false],
`ensure_final_newline_on_save` [true].

**Arduino CLI** — `arduino_cli_path` [empty = search `PATH` and the usual
install folders], `auto_detect_cli` [true], `arduino_data_dir` (the CLI's
`--data`), `sketchbook_dir` (`--library`/user folder), `cli_config_file`
(`--config-file`), `cli_extra_args` [appended to every call],
`verbose_cli_output` [false].

**Build** — `compile_warnings` [default | none | all | more],
`clean_build` [false], `build_output_mode` [project | temp],
`check_missing_includes` [true] (the "install this library?" prompt),
`install_missing_cores` [true].

**Board & port** — `fqbn` [`arduino:avr:uno`], `board_history` (last few),
`use_custom_fqbn` [false] + `custom_fqbn`, `port` [empty],
`additional_board_urls` [the three common ESP/AVR index URLs, ticked off until
you ask for them].

**Libraries** — `lib_auto_update_index` [false], `lib_index_max_age_days` [7],
`lib_use_project_dir` [true — offer project-local installs by default].

**Serial monitor** — `serial_baud` [9600], `serial_line_ending` [LF],
`serial_timestamps` [true], `serial_autoscroll` [true], `serial_show_rx`
[true], `serial_show_tx` [true], `serial_hex_display` [false],
`serial_toggle_dtr` [true], `serial_toggle_rts` [true], `serial_font_size` [10],
`serial_auto_reopen_after_upload` [true], `serial_log_dir` [Documents\
ArduinoStudioLogs].

**Console & terminal** — `console_font_size` [10], `console_max_lines` [5000],
`console_timestamps` [false], `terminal_shell` [powershell | cmd | bash],
`terminal_follow_project_dir` [true], `terminal_confirm_destructive` [true],
`terminal_font_size` [10].

**Bootloader** — `bootloader_programmer` [`arduino`],
`bootloader_confirm_required` [true], `bootloader_last_chip` [`ATmega328P`],
`bootloader_last_clock` [`16 MHz external`], `bootloader_backup_dir`.

**Session** — `recent_projects`, `open_files`, `active_project`,
`first_run_completed`, `version` [1], `extra` (forward-compatibility bag).

---

## Where Studio keeps its files

| Path | Contents |
| --- | --- |
| `%APPDATA%\ArduinoStudio\settings.json` | the settings above (`~/.config/arduino-studio` on Linux/macOS; `--config-dir` / `ARDUINO_STUDIO_CONFIG_DIR` overrides). |
| `%APPDATA%\ArduinoStudio\logs\arduino_studio.log` | rotating log: 1 MB, three backups. **Help ▸ Open Log File.** |
| `%APPDATA%\ArduinoStudio\data\` | Studio's own caches (library index timestamp and parsed index, download progress). |
| `…\Documents\ArduinoStudioLogs\` | saved serial-monitor logs. |
| `<project>\build\Release\` | compiled `.hex`/`.elf`/`.bin` and the CLI's build artefacts. |
| `<project>\project.json` | per-project board/port/build settings. |
| the arduino-cli data dir | cores, tools, global libraries — Studio never writes here except through the CLI. |

Studio stores no telemetry and needs no network access of its own: every
download is performed by `arduino-cli`.

---

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| *"arduino-cli was not found"* | Install it (see above) or press **Locate it…** and pick `arduino-cli.exe`. `python run.py --check` prints what Studio can and cannot find. |
| *"arduino-cli is too old"* | Studio needs **0.32.0+** (JSON output shapes). `winget upgrade Arduino.ArduinoCLI`. |
| Compile fails with *"platform … not found"* | **Tools ▸ Install Core for Current Board** (or `arduino-cli core install arduino:avr`). For ESP32/ESP8266 add the Board Manager URL first. |
| `fatal error: SomeLib.h: No such file or directory` | Click **Install** in the prompt Studio raises, or install it from the Library Manager; library-level `#include "…"` from `src/` works because both `src/` and `libraries/` are on the include path. |
| ZIP/Git install is refused with *"…is not allowed; set `library.enable_unsafe_install: true`"* | Click **Enable & retry** - Studio runs `arduino-cli config set library.enable_unsafe_install true` and repeats the install. (ZIP installs work even without it: Studio unpacks the archive into `libraries/` itself and says so in the console.) |
| Port list is empty | Windows: install the CH340/CP210x/FTDI driver and check Device Manager; try another cable (charge-only cables are common). Then **F5**. |
| Upload: `avrdude: ser_open(): can't open device` | The port is in use — close the Serial Monitor (Studio closes it itself before uploading, but another program may hold it), or pick the right COM number. |
| Upload: `stk500_getsync(): not in sync: resp=0x00` | Wrong board/FQBN, board not in bootloader, bad cable, or the Uno auto-reset is disabled. Try **Upload Using Programmer** or hold-reset on boards with a button. |
| Burn: `invalid device signature` (0x000000 / 0xffffff) | Wiring: RESET not connected, VCC/GND swapped, or the chip needs decoupling/a slower `-B` speed. The dialog shows the ICSP pinout for the programmer you picked. |
| Burn: *"target is not responding"* on Arduino-as-ISP | Remove `#define RESET 10` style conflicts, use 19200 for the classic ISP sketch, and make sure the ISP sketch (not a normal sketch) is on the programmer board. |
| Serial monitor shows garbage | Wrong baud (check `Serial.begin`), or the board is 3.3 V logic with a 5 V adapter. |
| ESP flash: *"esptool … not found"* | Install the ESP core — esptool ships inside it; Studio then runs it from there. |
| The window opens off-screen | Delete `window_x`/`window_y` from `settings.json` (or set them to `-1`) and restart. |
| Something else is wrong | **Help ▸ Open Log File**, look at the last lines; the same information is in `python run.py --check` for pasting into an issue. |

A failed operation never kills the app: every CLI call is wrapped, its stderr is
captured, and the Console shows the exit code plus a "what probably went wrong"
line. Cancellations are recorded as cancelled, not as errors.

---

## Running the tests and the self-check

```bat
python tests/test_core_flow.py     :: 24 tests: projects, settings, CLI argv shapes,
                                     diagnostics, libraries, bootloader, serial,
                                     terminal, threading, process layer, examples
python tests/test_ui_smoke.py      :: 21 tests: every panel/dialog, driven against a
                                     Tk stub so they run headless; the stub rejects
                                     widget options the real interpreter would reject.
                                     Pass name fragments (python tests/test_ui_smoke.py
                                     explorer serial) to run a subset
python -m arduino_studio --check   :: headless support report - settings/log/data
                                     paths, arduino-cli version + data dir,
                                     pyserial, tkinter (Tk/Tcl versions),
                                     customtkinter, board presets, exit status;
                                     exits 1 when something essential is missing
```

Both suites are plain scripts (they also work under `pytest`, if you installed
`requirements-dev.txt`) and use `tests/fake_arduino_cli.py`, a stand-in for
`arduino-cli` that reproduces its JSON, its success output and its common
failures — so the tests need no toolchain, no hardware and no network. To run the
whole GUI against the fake CLI:

```bat
set PATH=%CD%\tests;%PATH%      :: optional, or point Settings at the script
python run.py --config-dir %TEMP%\as-run
```

---

## Building a standalone .exe

```bat
python -m pip install -r requirements.txt -r requirements-dev.txt
build_exe.bat
```

or manually:

```bat
python -m PyInstaller --noconfirm --clean arduino_studio.spec
```

The result is `dist\Arduino Studio\Arduino Studio.exe` — a **one-folder** build
(Studio's own `resources`, CustomTkinter's themes and the icon sit next to it),
so copy or zip the whole `Arduino Studio` folder, not just the `.exe`. `arduino-cli`
is deliberately *not* bundled: it is 40 MB of toolchains and must stay
updatable, so the exe finds it on `PATH` or via `Settings` (put
`arduino-cli.exe` in the same folder as `Arduino Studio.exe` for a portable set).

The window/taskbar icon is generated, not committed:

```bat
python tools\make_icon.py                 :: -> arduino_studio\resources\arduino.ico
python tools\make_icon.py --png icon.png  :: also dump a 256 px preview
```

`arduino_studio.spec` embeds it when the file exists (and the app falls back to
the stock Tk icon when it does not), so a fresh clone builds fine without
running the generator first.

### If the .exe does not open

The app is built as a *windowed* executable, so a start-up failure has no
console to scream into. Since 1.0.1 it never fails silently either:

1. **A dialog appears** (Win32 message box, so it works even when Tk itself is
   the thing that broke) naming the file it wrote.
2. **`%APPDATA%\ArduinoStudio\logs\startup_error.log`** holds the traceback,
   next to the rotating `arduino_studio.log`.
3. Run it from a terminal to see the exit code and the report::

       cd "dist\Arduino Studio"
       ".\Arduino Studio.exe" --check

   In a windowed build `--check` writes the same report to
   `logs\arduino_studio_check.txt` and shows it in a dialog.

The usual causes, in the order they are worth checking:

| Cause | Fix |
| --- | --- |
| Only `Arduino Studio.exe` was copied; `_internal\` is missing | copy (or zip) the **whole** `Arduino Studio` folder. |
| `build_exe.bat` ran with a Python that has no Tkinter (e.g. the Microsoft Store build) | use the python.org installer's `py -3.12`; `build_exe.bat` refuses a Python where `import tkinter` fails. |
| The bundle missed the lazily-imported UI package (`ModuleNotFoundError: No module named 'arduino_studio.ui.app'`) | rebuild with the current `arduino_studio.spec` — it runs `collect_submodules("arduino_studio")` for exactly this reason. |
| The UI passes a widget option CustomTkinter does not know (e.g. `CTkButton(..., padx=12)`) — it raises `ValueError: ['padx'] are not supported arguments` while the window is being built | run `python tools\check_ctk_kwargs.py`; Tk-only options belong on `.grid()`/`.pack()`, not on the widget. `build_exe.bat` runs this check before PyInstaller starts. |
| A Tk option name is misspelled (e.g. `Treeview.column(min_width=...)` where ttk calls it `minwidth`) — it raises `TclError: unknown option "-min_width"` while the window is being built, so the exe opens and closes again | run `python tools\check_ctk_kwargs.py`; it checks every `grid`/`pack`/`place`, `Treeview.column`/`heading` and `Menu.add_*` option name against `tests/tk_options.py`, and `tests/tkstub.py` rejects the same names at run time. |
| A key binding names a keysym Tk does not know (e.g. `<Control-keypad-plus>`, or `<Control+KP_Add>` with a `+` separator) — it raises `TclError: bad event type or keysym "keypad"` while the window is being built | run `python tools\check_ctk_kwargs.py`; it parses every `bind`/`bind_all`/`tag_bind` pattern in `arduino_studio/` against `tests/tk_events.py`. Tk spells the numpad `KP_Add`/`KP_Subtract` and joins modifiers with `-`, never `+`. |
| Antivirus / SmartScreen deleted or blocked the exe | allow the folder, or build with `pyinstaller --onedir` yourself and sign it. |
| Corrupt profile after an earlier crash | delete `%APPDATA%\ArduinoStudio\settings.json`, or start with `--config-dir` pointing at a scratch folder. |

Two checks wrap the build so a broken bundle never reaches you.
**`tools/check_ctk_kwargs.py`** validates the widget options the UI asks for: every
`ctk.CTk*` constructor and `.configure()` keyword against the CustomTkinter that is
actually installed (its options are *not* Tk's options), plus every
`grid`/`pack`/`place`, `Treeview.column`/`heading` and `Menu.add_*` option name against
the tables in `tests/tk_options.py`. It also parses every `bind` / `bind_all` /
`tag_bind` / `event_generate` pattern in the package against the Tk grammar in
`tests/tk_events.py`, which is the only way to notice a keysym typo hidden inside a
`try/except TclError` (those bindings simply never fire). **`tools/check_dist.py`** validates the result: an
exe of a plausible size, `_internal/` present, CustomTkinter's theme JSON collected, and
no "missing module named arduino_studio…" in PyInstaller's own warning file. If any of
that is wrong the script exits non-zero and `build_exe.bat` stops with
`dist\build_check.txt` explaining what to fix.

Both checks are also tests. `tests/test_ui_smoke.py` runs them over the whole package,
and `tests/tkstub.py` enforces the same rules at run time — it raises `TclError` for an
option name or a binding pattern the real interpreter would reject, so a typo fails in
the repository instead of on someone's desktop. That matters most for the sequences
bound inside `try/except TclError`, which otherwise stop working in silence. Run them
directly with
`python tests\test_ui_smoke.py` and `python tests\test_core_flow.py`.

`pyproject.toml` metadata (`pip install .`) and `run.py` are kept in sync with the
packaging spec; `arduino_studio.spec` ignores `tests/`, `docs/` and `build/`.

---

## How the code is organised

```
arduino_studio/
├── main.py                  argument parsing, logging, window launch
├── __main__.py              python -m arduino_studio
├── core/                    logic only - no Tk import anywhere in here
│   ├── utils.py             paths, atomic writes, names, ANSI/duration helpers, logging setup
│   ├── settings.py          Settings dataclass (76 fields) + SettingsStore with change listeners
│   ├── process.py           the one place subprocess is called: timeout, cancellation, env
│   ├── arduino_cli.py       ArduinoCLI wrapper: probe/version, compile, upload, burn,
│   │                        board/port/core/lib JSON, output parsing -> Diagnostics
│   ├── boards.py            built-in board presets, FQBN parsing, families, baud & memory info
│   ├── project.py           Project + ProjectManager: skeleton, templates, file ops, manifest
│   ├── library_manager.py   search/installed/updatable/install/remove/upgrade, ZIP & Git,
│   │                        project-local libraries, include->library resolution
│   ├── bootloader.py        BootloaderService: chip/programmer/fuse tables, plans for
│   │                        burn/read/backup/esptool, avrdude & esptool discovery, failure advice
│   ├── serial_service.py    pyserial wrapper on a reader thread + port enumeration helpers
│   ├── terminal_service.py  shell detection, command safety classification, quick commands
│   ├── runner.py            TaskRunner: two lanes (build/query), progress/cancel, TaskResult
│   └── examples.py          the bundled sketches, incl. EEPROM String Storage
├── ui/                      Tk / CustomTkinter - imports core, never the reverse
│   ├── app.py               the window: layout, menus, toolbar, actions, wiring, coordination
│   ├── theme.py             palettes, fonts, spacing, widget helpers
│   ├── code_editor.py       the editor widget (line numbers, indent, brackets, undo)
│   ├── editor_tabs.py       notebook of documents, dirty tracking, reopen history
│   ├── syntax.py            Arduino/C++ tokenizer + Tk tag painter (no third-party lexer)
│   ├── findbar.py           find/replace/find-all over the active document
│   ├── serial_monitor.py    the monitor tab
│   ├── terminal_panel.py    the shell tab (reader thread + output queue)
│   ├── libraries_panel.py   search / installed / versions / ZIP / Git
│   ├── bootloader_panel.py  the four-tab bootloader dialog
│   ├── settings_view.py     tabbed settings editor with validation
│   ├── setup_wizard.py      first-run arduino-cli + board-manager configuration
│   ├── icons.py             glyphs + the generated .ico
│   └── widgets/             dialogs, toolbar, explorer, status bar, console, text view, data table
├── tools/make_icon.py       icon generator (no binary assets in git)
├── tests/                   the two suites + fake_arduino_cli.py + the Tk stub/grammar
#                            (tkstub.py, tk_options.py, tk_events.py) the checks lean on
├── requirements.txt         customtkinter, pyserial (+ send2trash optional)
├── requirements-dev.txt     pyinstaller, ruff, mypy, pytest
├── pyproject.toml           package metadata + `arduino-studio` console script
├── arduino_studio.spec      PyInstaller build script
└── build_exe.bat            one-click Windows build
```

### Design rules the code follows

* **`core` never imports Tk**, so all of the logic is testable headless (and the
  test suite proves it). The UI talks to `core` only through its methods.
* **No blocking calls on the UI thread.** Everything that can take more than a
  few milliseconds — `compile`, `upload`, `lib install`, `core update-index`,
  index parsing, board/port refresh, burns, esptool runs, reads from the serial
  ring buffer — goes through `TaskRunner` on one of two lanes: `build`
  (serialised, cancellable, one operation at a time) and `query` (small reads,
  several at once). Results are handed back with `widget.after(...)`, so no
  worker ever touches a Tk widget directly.
* **Subprocess hygiene.** One `process.py` builds every command: no
  `shell=True`, arguments passed as a list, stdout/stderr merged and streamed
  line by line, a bounded timeout, the process group killed on cancel, UTF-8
  with `errors="replace"`. `arduino-cli` runs without the Arduino IDE being open,
  and nothing writes to the CLI's data directory except the CLI itself.
* **Errors are translated, not shown raw.** `humanize_failure`/`explain_failure`
  map the ~30 common compiler/avrdude/esptool/serial messages onto actionable
  advice; the full output is always still there in the Console.
* **Modal dialogs only when a decision is genuinely required** (unsaved changes,
  burning fuses, deleting a project, destructive terminal commands, install the
  missing library). Everything else is inline: the find bar, the console, the
  status bar, the command palette.
* Type hints and docstrings on every public function/class; `logging` instead of
  `print`; broad `except` clauses always name the exception they catch.

---

## Licence & attribution

This repository is your own project ("Myhobby2026/Arduino_Studio_Full_1.0v");
add a `LICENSE` file if you intend to publish it. Arduino, the Arduino logo,
`arduino-cli`, avrdude and esptool belong to their respective owners —
Arduino Studio is an independent front-end and is not affiliated with or
endorsed by Arduino SA. Board definitions, cores, bootloaders and libraries are
downloaded by `arduino-cli` under their own licences.
