"""Bootloader / programmer screen (Tools \u2192 Burn Bootloaders, chip tools).

Three jobs, all of which can brick a chip when done wrong, so every action runs
through :meth:`PlanDialog` first (full command + fuse table + warnings + an
" I understand" checkbox) and then in the serial-safe task lane:

* burn a bootloader to a blank AVR through an ISP programmer
  (``arduino-cli burn-bootloader``),
* read signature / fuses / lock bits back from the chip (``avrdude -U``),
* dump the flash of a working board to a file (documented as a *backup of the
  current firmware*, not a way to recover source code),
* flash a ``.bin`` to ESP32 / ESP8266 with ``esptool`` (erase-first option).

Buttons stay disabled until the selections are consistent (avrdude present, a
programmer chosen, a port chosen when the programmer needs one, a real board
family selected).  The help pane shows the ISP wiring for the chosen programmer.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.boards import family_for_fqbn, is_esp_fqbn
from ..core.bootloader import (
    BootloaderService,
    ChipInfo,
    FlashPlan,
    FuseSet,
    Programmer,
    build_fqbn_with_menus,
)
from ..core.runner import LANE_BUILD, LANE_QUERY, TaskResult
from ..core.utils import get_logger
from .theme import Palette
from .widgets.data_table import DataTable
from .widgets.dialogs import ask_message, ask_plan, ask_text, ask_yes_no
from .widgets.text_view import ScrolledTextView, TagSpec

__all__ = ["BootloaderPanel"]

_FUSE_COLUMNS = (
    ("name", "Setting", 150, "w"),
    ("value", "Value", 110, "w"),
    ("meaning", "Meaning", 330, "w"),
)


class BootloaderPanel(ctk.CTkFrame):
    """Bootloader manager UI (AVR ISP + ESP flashing)."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        service: BootloaderService,
        runner: Any,
        console: Any = None,
        settings: Any = None,
        get_fqbn: Optional[Callable[[], str]] = None,
        get_port: Optional[Callable[[], str]] = None,
        get_ports: Optional[Callable[[], list[str]]] = None,
        get_project: Optional[Callable[[], Any]] = None,
        get_build_dir: Optional[Callable[[], Optional[Path]]] = None,
        on_status: Optional[Callable[[str], Any]] = None,
        on_fqbn_changed: Optional[Callable[[str], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=0)
        self.palette = palette
        self.service = service
        self._runner = runner
        self._console = console
        self._settings = settings
        self._get_fqbn = get_fqbn
        self._get_port = get_port
        self._get_ports = get_ports
        self._get_project = get_project
        self._get_build_dir = get_build_dir
        self._on_status = on_status
        self._on_fqbn_changed = on_fqbn_changed
        self._log = get_logger("ui.bootloader")

        self._programmers: list[Programmer] = []
        self._fuses: dict[str, FuseSet] = {}
        self._menus: dict[str, dict[str, Any]] = {}
        self._menu_vars: dict[str, ctk.CTkOptionMenu] = {}
        self._task = None
        self._busy = False
        self._last_chip_info: Optional[ChipInfo] = None

        self._build()
        self.reload_capabilities()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(self, fg_color=palette.window_bg, corner_radius=8,
                                   segmented_button_fg_color=palette.panel_bg,
                                   segmented_button_selected_color=palette.accent,
                                   segmented_button_unselected_color=palette.panel_bg,
                                   segmented_button_selected_hover_color=palette.accent_hover,
                                   text_color=palette.text)
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=10, pady=(10, 8))
        self.tabs.add("Burn bootloader (AVR / ISP)")
        self.tabs.add("Flash binary (ESP32 / ESP8266)")
        self.tabs.set("Burn bootloader (AVR / ISP)")
        self.avr_frame = self.tabs.tab("Burn bootloader (AVR / ISP)")
        self.esp_frame = self.tabs.tab("Flash binary (ESP32 / ESP8266)")
        self.tabs.bind("<<CTkTabview-OnChange>>", lambda event: self._sync_state(), add=True)

        self._build_avr(self.avr_frame)
        self._build_esp(self.esp_frame)
        self._build_footer()

    # -------------------------------------------------------------- AVR panel
    def _build_avr(self, parent: Any) -> None:
        palette = self.palette
        parent.grid_rowconfigure(2, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        intro = ctk.CTkLabel(
            parent,
            text="Burn a bootloader to a blank or revived AVR (ATmega328P/328PB/ATmega2560, plus ATtiny "
                 "profiles shipped by the core) through an ISP programmer. The exact avrdude command and every "
                 "fuse byte are shown before anything runs.",
            font=(palette.font_family, 10), text_color=palette.text_dim, anchor="w", justify="left",
            wraplength=880,
        )
        intro.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))

        self.avr_warning = ctk.CTkLabel(parent, text="", font=(palette.font_family, 10), anchor="w",
                                        justify="left", wraplength=880, text_color=palette.warning)
        self.avr_warning.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 4))

        form = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=8)
        form.grid(row=2, column=0, sticky="nsew", padx=12, pady=(2, 4))
        form.grid_columnconfigure(1, weight=1)
        form.grid_columnconfigure(3, weight=1)
        form.grid_rowconfigure(8, weight=1)

        row = 0
        ctk.CTkLabel(form, text="Board", font=(palette.font_family, 10, "bold"),
                     text_color=palette.text_dim, anchor="w").grid(row=row, column=0, sticky="w", padx=(14, 6), pady=6)
        self.board_label = ctk.CTkLabel(form, text="arduino:avr:uno", anchor="w",
                                        font=(palette.mono_family, 10), text_color=palette.text)
        self.board_label.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(0, 14), pady=6)
        row += 1

        ctk.CTkLabel(form, text="Target chip", anchor="w",
                     font=(palette.font_family, 10)).grid(row=row, column=0, sticky="w", padx=(14, 6), pady=6)
        self.chip_menu = ctk.CTkOptionMenu(form, values=["ATmega328P"], height=28, command=self._chip_chosen)
        self.chip_menu.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=6)
        ctk.CTkLabel(form, text="Processor / clock", anchor="w",
                     font=(palette.font_family, 10)).grid(row=row, column=2, sticky="w", padx=(0, 6), pady=6)
        self.clock_menu = ctk.CTkOptionMenu(form, values=["16 MHz external"], height=28, command=self._clock_chosen)
        self.clock_menu.grid(row=row, column=3, sticky="ew", padx=(0, 14), pady=6)
        row += 1

        ctk.CTkLabel(form, text="Programmer", anchor="w",
                     font=(palette.font_family, 10)).grid(row=row, column=0, sticky="w", padx=(14, 6), pady=6)
        self.programmer_menu = ctk.CTkOptionMenu(form, values=["arduino"], height=28,
                                                 command=self._programmer_chosen)
        self.programmer_menu.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=6)
        ctk.CTkLabel(form, text="Port", anchor="w",
                     font=(palette.font_family, 10)).grid(row=row, column=2, sticky="w", padx=(0, 6), pady=6)
        self.port_menu = ctk.CTkOptionMenu(form, values=["No ports"], height=28, command=lambda value: None)
        self.port_menu.grid(row=row, column=3, sticky="ew", padx=(0, 14), pady=6)
        row += 1

        ctk.CTkLabel(form, text="Board options", anchor="w",
                     font=(palette.font_family, 10)).grid(row=row, column=0, sticky="nw", padx=(14, 6), pady=6)
        self.menu_frame = ctk.CTkFrame(form, fg_color="transparent")
        self.menu_frame.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(0, 14), pady=4)
        row += 1

        ctk.CTkLabel(form, text="Fuses", anchor="nw",
                     font=(palette.font_family, 10, "bold"),
                     text_color=palette.text_dim).grid(row=row, column=0, sticky="nw", padx=(14, 6), pady=(8, 4))
        self.fuse_table = DataTable(form, palette, _FUSE_COLUMNS, height=6, empty_text="No fuse profile selected",
                                   show_scrollbar=False)
        self.fuse_table.grid(row=row + 1, column=0, columnspan=4, sticky="nsew", padx=(14, 14), pady=(0, 6))
        row += 2

        custom = ctk.CTkFrame(form, fg_color=palette.surface, corner_radius=8)
        custom.grid(row=row, column=0, columnspan=4, sticky="ew", padx=14, pady=(2, 6))
        custom.grid_columnconfigure((1, 3, 5, 7), weight=1)
        ctk.CTkLabel(custom, text="Override", font=(palette.font_family, 9, "bold"),
                     text_color=palette.text_muted).grid(row=0, column=0, sticky="w", padx=(10, 6), pady=6)
        self.fuse_vars: dict[str, ctk.CTkEntry] = {}
        for index, (key, label, hint) in enumerate((
            ("low", "low", "0xFF"), ("high", "high", "0xDE"),
            ("extended", "ext", "0x05"), ("lock", "lock", "0x0F"),
        )):
            ctk.CTkLabel(custom, text=label, font=(palette.mono_family, 9), text_color=palette.text_muted,
                         anchor="e").grid(row=0, column=index * 2 + 1 - (1 if index else 0), sticky="e",
                                        padx=(6, 2))
            entry = ctk.CTkEntry(custom, placeholder_text=hint, width=76, height=26,
                                 font=(palette.mono_family, 10))
            entry.grid(row=0, column=index * 2 + 2, sticky="ew", padx=(0, 4))
            entry.bind("<KeyRelease>", lambda event: self._sync_state(), add=True)
            self.fuse_vars[key] = entry
        self.use_custom_fuses = ctk.CTkCheckBox(custom, text="use these instead",
                                                font=(palette.font_family, 9), checkbox_width=15,
                                                checkbox_height=15)
        self.use_custom_fuses.grid(row=0, column=8, sticky="e", padx=(6, 10))
        row += 1

        actions = ctk.CTkFrame(form, fg_color="transparent")
        actions.grid(row=row, column=0, columnspan=4, sticky="ew", padx=14, pady=(4, 12))
        self.burn_button = ctk.CTkButton(actions, text="\u26a1  Burn bootloader", width=170, height=32,
                                         corner_radius=8, fg_color=palette.error, hover_color=palette.hover,
                                         text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                         command=self.burn_bootloader)
        self.burn_button.pack(side="left", padx=(0, 6))
        self.read_button = ctk.CTkButton(actions, text="Read chip information", width=170, height=32,
                                         corner_radius=8, fg_color="transparent", border_width=1,
                                         border_color=palette.border, hover_color=palette.hover,
                                         text_color=palette.text, command=self.read_chip_info)
        self.read_button.pack(side="left", padx=(0, 6))
        self.backup_button = ctk.CTkButton(actions, text="Backup firmware\u2026", width=150, height=32,
                                           corner_radius=8, fg_color="transparent", border_width=1,
                                           border_color=palette.border, hover_color=palette.hover,
                                           text_color=palette.text, command=self.backup_firmware)
        self.backup_button.pack(side="left", padx=(0, 6))
        self.help_button = ctk.CTkButton(actions, text="Wiring help", width=110, height=32, corner_radius=8,
                                         fg_color="transparent", border_width=1, border_color=palette.border,
                                         hover_color=palette.hover, text_color=palette.text_dim,
                                         command=self.toggle_help)
        self.help_button.pack(side="right")

        side = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=8)
        side.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        side.grid_columnconfigure(0, weight=1)
        self.info_view = ScrolledTextView(side, palette, readonly=True, wrap="word", height=8,
                                          bg=palette.surface, fg=palette.text,
                                          font=(palette.mono_family, 10),
                                          tags=(TagSpec("head", foreground=palette.text, font_weight="bold"),
                                                TagSpec("key", foreground=palette.text_muted),
                                                TagSpec("pin", foreground=palette.accent),
                                                TagSpec("warn", foreground=palette.warning),
                                                TagSpec("value", foreground=palette.text)))
        self.info_view.grid(row=0, column=0, sticky="nsew", padx=10, pady=8)
        self._show_wiring_help()

    # -------------------------------------------------------------- ESP panel
    def _build_esp(self, parent: Any) -> None:
        palette = self.palette
        parent.grid_rowconfigure(4, weight=1)
        parent.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            parent,
            text="ESP32 / ESP8266 modules have no bootloader to burn: the sketch is flashed over the serial "
                 "bootloader that is already in ROM. Use this tab to flash a compiled .bin, an OTA image or a "
                 "vendor binary, optionally erasing the flash first.",
            font=(palette.font_family, 10), text_color=palette.text_dim, anchor="w", justify="left",
            wraplength=880,
        ).grid(row=0, column=0, columnspan=3, sticky="ew", padx=14, pady=(12, 4))

        self.esp_warning = ctk.CTkLabel(parent, text="", font=(palette.font_family, 10), anchor="w",
                                        justify="left", wraplength=880, text_color=palette.warning)
        self.esp_warning.grid(row=1, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 4))

        form = ctk.CTkFrame(parent, fg_color=palette.panel_bg, corner_radius=8)
        form.grid(row=2, column=0, columnspan=3, sticky="ew", padx=12, pady=(2, 4))
        form.grid_columnconfigure(1, weight=1)
        form.grid_columnconfigure(3, weight=1)

        self.bin_entry = ctk.CTkEntry(form, placeholder_text="path to firmware.bin", height=28,
                                      font=(palette.mono_family, 10))
        self.bin_entry.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(14, 8), pady=8)
        browse = ctk.CTkButton(form, text="Browse\u2026", width=92, height=28, corner_radius=6,
                               fg_color=palette.surface, hover_color=palette.hover, text_color=palette.text,
                               command=self.choose_binary)
        browse.grid(row=0, column=2, sticky="e", padx=(0, 14), pady=8)

        left = ctk.CTkFrame(form, fg_color="transparent")
        left.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(14, 8), pady=(0, 4))
        left.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(left, text="Flash address", font=(palette.font_family, 10), anchor="w",
                     text_color=palette.text_dim).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.address_entry = ctk.CTkEntry(left, height=28, width=120, font=(palette.mono_family, 10),
                                          fg_color=palette.surface, text_color=palette.text,
                                          border_color=palette.border)
        self.address_entry.grid(row=0, column=1, sticky="ew")

        right = ctk.CTkFrame(form, fg_color="transparent")
        right.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(0, 14), pady=(0, 4))
        right.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(right, text="Baud rate", font=(palette.font_family, 10), anchor="w",
                     text_color=palette.text_dim).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.baud_entry = ctk.CTkEntry(right, height=28, width=120, font=(palette.mono_family, 10),
                                       fg_color=palette.surface, text_color=palette.text,
                                       border_color=palette.border)
        self.baud_entry.grid(row=0, column=1, sticky="ew")
        self.baud_menu = ctk.CTkOptionMenu(right, values=["115200", "230400", "460800", "921600", "1500000"],
                                           width=60, height=28, command=lambda value: self._baud_chosen(value))
        self.baud_menu.grid(row=0, column=2, sticky="e", padx=(6, 0))

        note = ctk.CTkLabel(
            form,
            text="Address must match the image type - a sketch .bin normally starts at 0x1000 on ESP32 "
                 "(0x0 on ESP8266 legacy layouts). The board's own flash mode/size are used by default.",
            font=(palette.font_family, 9), text_color=palette.text_muted, anchor="w", justify="left",
            wraplength=840,
        )
        note.grid(row=2, column=0, columnspan=3, sticky="ew", padx=(14, 14), pady=(0, 2))

        self.address_entry.insert(0, "0x0")
        self.baud_entry.insert(0, "921600")

        options = ctk.CTkFrame(form, fg_color="transparent")
        options.grid(row=4, column=0, columnspan=3, sticky="ew", padx=(14, 14), pady=(0, 8))
        options.grid_columnconfigure(6, weight=1)
        self.erase_check = ctk.CTkCheckBox(options, text="Erase flash first", font=(palette.font_family, 10),
                                           checkbox_width=16, checkbox_height=16)
        self.erase_check.grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.verify_check = ctk.CTkCheckBox(options, text="Verify after write", font=(palette.font_family, 10),
                                            checkbox_width=16, checkbox_height=16)
        self.verify_check.select()
        self.verify_check.grid(row=0, column=1, sticky="w", padx=(0, 14))
        self.use_build_check = ctk.CTkCheckBox(options, text="Use .bin from the last Verify (build folder)",
                                               font=(palette.font_family, 10), checkbox_width=16,
                                               checkbox_height=16, command=self._sync_state)
        self.use_build_check.grid(row=0, column=2, sticky="w", padx=(0, 14))
        ctk.CTkLabel(options, text="mode", font=(palette.font_family, 9), text_color=palette.text_muted)\
            .grid(row=0, column=3, sticky="e", padx=(0, 4))
        self.mode_menu = ctk.CTkOptionMenu(options, values=["dio", "dout", "qio", "qout"], width=80, height=26,
                                           command=lambda value: None)
        self.mode_menu.set("dio")
        self.mode_menu.grid(row=0, column=4, sticky="e", padx=(0, 8))
        ctk.CTkLabel(options, text="size", font=(palette.font_family, 9), text_color=palette.text_muted)\
            .grid(row=0, column=5, sticky="e", padx=(0, 4))
        self.size_menu = ctk.CTkOptionMenu(options, values=["detect", "512KB", "1MB", "2MB", "4MB", "8MB", "16MB"],
                                           width=90, height=26, command=lambda value: None)
        self.size_menu.set("detect")
        self.size_menu.grid(row=0, column=6, sticky="e", padx=(0, 8))
        ctk.CTkLabel(options, text="freq", font=(palette.font_family, 9), text_color=palette.text_muted)\
            .grid(row=0, column=7, sticky="e", padx=(0, 4))
        self.freq_menu = ctk.CTkOptionMenu(options, values=["40m", "26m", "80m", "30m"], width=76, height=26,
                                           command=lambda value: None)
        self.freq_menu.set("40m")
        self.freq_menu.grid(row=0, column=8, sticky="e", padx=(0, 0))

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=3, sticky="ew", padx=14, pady=(2, 4))
        self.flash_button = ctk.CTkButton(actions, text="\u26a1  Flash binary", width=150, height=32,
                                          corner_radius=8, fg_color=palette.error, hover_color=palette.hover,
                                          text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                          command=self.flash_binary)
        self.flash_button.pack(side="left", padx=(0, 6))
        self.erase_button = ctk.CTkButton(actions, text="Erase whole flash", width=150, height=32,
                                          corner_radius=8, fg_color="transparent", border_width=1,
                                          border_color=palette.border, hover_color=palette.hover,
                                          text_color=palette.text, command=self.erase_flash)
        self.erase_button.pack(side="left", padx=(0, 6))
        self.flash_info_button = ctk.CTkButton(actions, text="Chip info (esptool)", width=150, height=32,
                                               corner_radius=8, fg_color="transparent", border_width=1,
                                               border_color=palette.border, hover_color=palette.hover,
                                               text_color=palette.text, command=self.esp_chip_info)
        self.flash_info_button.pack(side="left")

        self.esp_info = ScrolledTextView(parent, palette, readonly=True, wrap="word", height=8,
                                         bg=palette.surface, fg=palette.text,
                                         font=(palette.mono_family, 10),
                                         tags=(TagSpec("head", foreground=palette.text, font_weight="bold"),
                                               TagSpec("warn", foreground=palette.warning),
                                               TagSpec("value", foreground=palette.text)))
        self.esp_info.grid(row=4, column=0, columnspan=3, sticky="nsew", padx=12, pady=(0, 8))
        self._show_esp_help()

    # ----------------------------------------------------------------- footer
    def _build_footer(self) -> None:
        palette = self.palette
        footer = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8, height=36)
        footer.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        footer.grid_propagate(False)
        footer.grid_columnconfigure(2, weight=1)
        self.avrdude_label = ctk.CTkLabel(footer, text="avrdude: checking\u2026", font=(palette.font_family, 10),
                                          text_color=palette.text_muted, anchor="w")
        self.avrdude_label.grid(row=0, column=0, sticky="w", padx=(12, 12))
        self.esptool_label = ctk.CTkLabel(footer, text="esptool: checking\u2026", font=(palette.font_family, 10),
                                          text_color=palette.text_muted, anchor="w")
        self.esptool_label.grid(row=0, column=1, sticky="w", padx=(0, 12))
        self.status_label = ctk.CTkLabel(footer, text="", font=(palette.font_family, 10),
                                         text_color=palette.text_dim, anchor="e")
        self.status_label.grid(row=0, column=2, sticky="e", padx=(0, 12))
        self.cancel_button = ctk.CTkButton(footer, text="Cancel", width=74, height=26, corner_radius=6,
                                          fg_color="transparent", border_width=1, border_color=palette.border,
                                          hover_color=palette.hover, text_color=palette.text_dim,
                                          command=self._cancel)
        self.cancel_button.grid(row=0, column=3, sticky="e", padx=(0, 12))

    # ------------------------------------------------------------- capabilities
    def reload_capabilities(self) -> None:
        """Re-read programmers, chips and tool availability (core may have changed)."""
        self._programmers = self.service.programmers()
        labels = [programmer.label for programmer in self._programmers] or ["no programmers found"]
        try:
            self.programmer_menu.configure(values=labels)
            remembered = str(getattr(self._settings, "bootloader_programmer", "") or "arduino")
            match = next((item for item in self._programmers if item.id == remembered), None)
            self.programmer_menu.set(match.label if match else labels[0])
        except tk.TclError:  # pragma: no cover
            pass
        chips = self.service.supported_chips() or ["ATmega328P", "ATmega328PB", "ATmega2560"]
        try:
            self.chip_menu.configure(values=chips)
        except tk.TclError:  # pragma: no cover
            pass
        self._chip_chosen(self.chip_menu.get() if self.chip_menu else "ATmega328P", reload_ports=False)
        self._sync_state()

    def refresh_from_app(self) -> None:
        """Called whenever the panel is shown or the board / port changes."""
        board = self._fqbn()
        try:
            self.board_label.configure(text=board or "no board selected")
        except tk.TclError:  # pragma: no cover
            pass
        ports = self._ports()
        try:
            self.port_menu.configure(values=ports or ["No ports found"])
            current = self._port()
            if current:
                self.port_menu.set(current)
            elif ports:
                self.port_menu.set(ports[0])
        except tk.TclError:  # pragma: no cover
            pass
        chip = ""
        if board:
            try:
                chip = self.service.chip_for_board(board) or ""
            except Exception:  # pragma: no cover
                chip = ""
        if chip:
            try:
                if chip in list(self.chip_menu.cget("values") or []):
                    self.chip_menu.set(chip)
            except tk.TclError:  # pragma: no cover
                pass
            self._chip_chosen(chip)
        self._build_board_menus(board)
        self._sync_state()

    def _build_board_menus(self, fqbn: str) -> None:
        """Offer the core's menu options (cpu/flash/variant) for this board."""
        for widget in self._menu_vars.values():
            try:
                widget.destroy()
            except tk.TclError:  # pragma: no cover
                pass
        self._menu_vars.clear()
        self._menus.clear()
        if not fqbn:
            return
        try:
            options = self.service.board_menu_options(fqbn)
        except Exception as exc:  # pragma: no cover
            self._log.debug("board menu options failed: %s", exc)
            return
        if not options:
            return
        self._menus = dict(options)
        for column, (key, spec) in enumerate(sorted(options.items())[:6]):
            choices = list(spec.get("options") or []) if isinstance(spec, dict) else list(spec)
            values = [str(item.get("label", item.get("value", "")) if isinstance(item, dict) else item)
                      for item in choices] or ["default"]
            menu = ctk.CTkOptionMenu(self.menu_frame, values=values, height=26, width=170,
                                      command=lambda value, name=key: self._menu_chosen(name, value))
            menu.grid(row=0, column=column * 2, sticky="w", padx=(0, 8))
            ctk.CTkLabel(self.menu_frame, text=str(spec.get("label", key) if isinstance(spec, dict) else key),
                         font=(palette_font(self.palette), 9), text_color=self.palette.text_muted)\
                .grid(row=1, column=column * 2, sticky="w", padx=(0, 8))
            self._menu_vars[key] = menu
        self._sync_state()

    def _programmer_chosen(self, label: Any) -> None:
        """The ISP programmer changed: refresh wiring help, port need and buttons."""
        programmer = self._programmer()
        try:
            self.port_menu.configure(state="normal" if (programmer and programmer.needs_port) else "disabled")
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        if self._settings is not None and programmer is not None:
            try:
                self._settings.bootloader_programmer = programmer.id
            except AttributeError:  # pragma: no cover
                pass
        self._show_wiring_help()
        self._sync_state()

    def _menu_chosen(self, key: str, value: str) -> None:
        """A board menu option changed: the effective FQBN and preview follow."""
        self._sync_state()
        if self._on_fqbn_changed is not None:
            self._on_fqbn_changed(self.effective_fqbn())

    def effective_fqbn(self) -> str:
        """Board FQBN with the chosen menu options appended (``a=b,c=d``)."""
        base = self._fqbn()
        if not base or not self._menus:
            return base
        selections: dict[str, str] = {}
        for key, menu in self._menu_vars.items():
            try:
                selections[key] = str(menu.get())
            except tk.TclError:  # pragma: no cover
                continue
        try:
            return build_fqbn_with_menus(base, selections)
        except Exception:  # pragma: no cover
            return base

    # ------------------------------------------------------------------- chips
    def _chip_chosen(self, chip: Any, reload_ports: bool = True) -> None:
        chip = str(chip or "").strip()
        if not chip:
            return
        presets = self.service.fuse_presets(chip)
        labels = [str(_read_attr(preset, "summary", preset.clock)) for preset in presets] \
            or ["default (from board)"]
        self._fuses = {label: preset for label, preset in zip(labels, presets)}
        try:
            self.clock_menu.configure(values=labels)
            remembered = str(getattr(self._settings, "bootloader_last_clock", "") or "")
            chosen = next((label for label in labels if remembered and remembered in label), labels[0])
            self.clock_menu.set(chosen)
        except tk.TclError:  # pragma: no cover
            pass
        self._show_fuses()
        if reload_ports:
            self.refresh_from_app()

    def _clock_chosen(self, label: Any) -> None:
        self._show_fuses()

    def _current_fuse(self) -> Optional[FuseSet]:
        try:
            label = str(self.clock_menu.get())
        except tk.TclError:  # pragma: no cover
            return None
        return self._fuses.get(label)

    def _show_fuses(self) -> None:
        fuse = self._current_fuse()
        rows: list[tuple[str, list[str]]] = []
        tags: dict[str, str] = {}
        if fuse is not None:
            def add(key: str, value: str, meaning: str, tone: str = "normal") -> None:
                rows.append((key, [key, value or "-", meaning or ""]))
                tags[key] = tone

            add("low", fuse.low, "clock source, BOD, SPIEN, watchdog")
            add("high", fuse.high, "boot size, reset enable")
            add("extended", fuse.extended, "brown-out voltage, temp range")
            add("lock", fuse.lock, "locks ISP / read protection", "warn" if fuse.lock not in ("0x3F", "") else "normal")
            add("bootloader", fuse.bootloader, "bootloader address / size")
            add("source", fuse.source, "where this profile came from", "dim")
        self.fuse_table.set_rows(rows, tags)
        if fuse is not None:
            warnings = list(_read_attr(fuse, "warnings", []) or [])
            text = "  \n".join(warnings) if warnings else ""
            try:
                self.avr_warning.configure(text=text)
            except tk.TclError:  # pragma: no cover
                pass

    def _fuse_override(self) -> Optional[FuseSet]:
        """Fuse set from the manual override boxes (when enabled and complete)."""
        try:
            if not bool(self.use_custom_fuses.get()):
                return None
            values = {key: str(entry.get()).strip() for key, entry in self.fuse_vars.items()}
        except (tk.TclError, AttributeError):  # pragma: no cover
            return None
        if not any(values.values()):
            return None
        base = self._current_fuse()
        return FuseSet(chip=str(self.chip_menu.get()), clock="custom",
                       low=values.get("low", "") or (base.low if base else ""),
                       high=values.get("high", "") or (base.high if base else ""),
                       extended=values.get("extended", "") or (base.extended if base else ""),
                       lock=values.get("lock", "") or (base.lock if base else ""),
                       bootloader=base.bootloader if base else "",
                       note="manual override", source="user")

    # ------------------------------------------------------------------ states
    def _sync_state(self) -> None:
        """Enable/disable the action buttons according to the selections."""
        board = self._fqbn()
        supported, reason = (False, "no board selected")
        try:
            supported, reason = self.service.supports_bootloader(board) if board else (False, "no board selected")
        except Exception as exc:  # pragma: no cover
            reason = str(exc)
        avrdude = ""
        try:
            avrdude = self.service.find_avrdude()
        except Exception:  # pragma: no cover
            avrdude = ""
        programmer = self._programmer()
        port = self._port()
        needs_port = bool(programmer and programmer.needs_port)
        can_burn = bool(supported and avrdude and programmer and (port or not needs_port) and not self._busy)
        can_read = bool(avrdude and programmer and (port or not needs_port) and not self._busy)
        for widget, enabled in ((self.burn_button, can_burn), (self.read_button, can_read),
                                (self.backup_button, can_read)):
            try:
                widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:  # pragma: no cover
                continue
        try:
            self.avrdude_label.configure(
                text=f"avrdude: {Path(avrdude).name if avrdude else 'not found'}",
                text_color=self.palette.success if avrdude else self.palette.error,
            )
            self.avr_warning.configure(text="" if supported else (reason or ""))
        except tk.TclError:  # pragma: no cover
            pass
        if not avrdude:
            try:
                self.avr_warning.configure(
                    text="avrdude was not found in the installed cores. Install the AVR core "
                         "(Board manager / Settings) or point arduino-cli at its data folder.",
                )
            except tk.TclError:  # pragma: no cover
                pass
        family = family_for_fqbn(board) if board else ""
        esp = is_esp_fqbn(board)
        esptool = ""
        try:
            esptool = self.service.find_esptool(family or ("esp32" if esp else "esp32"))
        except Exception:  # pragma: no cover
            esptool = ""
        try:
            self.esptool_label.configure(
                text=f"esptool: {Path(esptool).name if esptool else 'not found'}",
                text_color=self.palette.success if esptool else self.palette.text_muted,
            )
        except tk.TclError:  # pragma: no cover
            pass
        binary = self._binary_path()
        can_flash = bool(esptool and binary and port and not self._busy)
        try:
            self.flash_button.configure(state="normal" if can_flash else "disabled")
            self.erase_button.configure(state="normal" if (esptool and port and not self._busy) else "disabled")
            self.esp_warning.configure(text="" if esp else
                                       "The selected board is not an ESP32/ESP8266 - the ESP tab flashes with "
                                       "esptool, so pick an ESP board for the address/size defaults to match.")
        except tk.TclError:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- actions
    def burn_bootloader(self) -> None:
        """Confirm, then ``arduino-cli burn-bootloader`` (+ optional fuse writes)."""
        board = self.effective_fqbn()
        programmer = self._programmer()
        port = self._port()
        if programmer is None:
            ask_message(self, kind="warning", title="No programmer",
                        message="Choose an ISP programmer first (e.g. 'Arduino as ISP').", palette=self.palette)
            return
        fuse = self._fuse_override() or self._current_fuse()
        try:
            plan = self.service.plan_burn_bootloader(board, programmer.id, port, fuse=fuse,
                                                     sketch_dir=self._project_root())
        except Exception as exc:
            ask_message(self, kind="error", title="Cannot build the command", message=str(exc),
                        palette=self.palette)
            return
        if not self._confirm_plan(plan, title="Burn bootloader", accept="Burn it"):
            return
        self._execute(plan, done_label=f"bootloader burned on {port or 'the programmer port'}",
                      ok_summary="Bootloader burned successfully")

    def read_chip_info(self) -> None:
        """Read signature, fuses and lock bits (never writes anything)."""
        programmer = self._programmer()
        if programmer is None:
            return
        try:
            plan = self.service.plan_read_chip(str(self.chip_menu.get()), programmer.id, self._port())
        except Exception as exc:
            ask_message(self, kind="error", title="Cannot read the chip", message=str(exc), palette=self.palette)
            return
        if plan is None:
            ask_message(self, kind="warning", title="Not supported",
                        message="This core/chip combination has no documented fuse bytes to read.",
                        palette=self.palette)
            return
        self._execute(plan, done_label="chip information read", ok_summary="Chip read complete",
                      reader=True)

    def backup_firmware(self) -> None:
        """Dump flash + eeprom to files, with an explicit disclaimer."""
        programmer = self._programmer()
        if programmer is None:
            return
        if not ask_plan(
            self,
            title="Backup firmware",
            heading="Read the whole flash (and EEPROM) of the connected chip?",
            sections=[
                _section("What happens",
                         f"  avrdude reads -U flash and -U eeprom through {programmer.name}\n"
                         "  and writes them into two files you choose.", "info"),
                _section("Important",
                         "This copies the *binary* that is currently on the chip. It does NOT recover\n"
                         "your source code, and it does not restore anything - it is a snapshot only.\n"
                         "If the lock bits are set, the read may fail or return 0xFF everywhere.", "warn"),
            ],
            accept_label="Choose folder", require_ack=False, palette=self.palette,
        ):
            return
        folder = self._backup_folder()
        if folder is None:
            return
        chip = str(self.chip_menu.get())
        try:
            plan = self.service.plan_backup(chip, programmer.id, self._port(),
                                            self.service.default_backup_path(folder, f"{chip}_backup", "hex"))
        except Exception as exc:
            ask_message(self, kind="error", title="Cannot back up", message=str(exc), palette=self.palette)
            return
        def done(result: TaskResult) -> None:
            if result.ok:
                self._log_backup_hint(str(plan.output_file or ""))

        self._execute(plan, done_label="firmware backup written", ok_summary="Backup complete", after=done)

    def _log_backup_hint(self, path: str) -> None:
        message = f"Backup written to {path}" if path else "Backup written"
        self._status(message)
        self._log_line(message, "ok")

    def flash_binary(self) -> None:
        """Flash a ``.bin`` (or the last build output) to an ESP module."""
        binary = self._binary_path()
        if not binary:
            ask_message(self, kind="warning", title="No binary",
                        message="Choose a .bin file first, or tick 'Use .bin from the last Verify'.",
                        palette=self.palette)
            return
        plan = self._esp_plan(action="write")
        if plan is None:
            return
        if not self._confirm_plan(plan, title="Flash binary", accept="Flash it"):
            return
        self._execute(plan, done_label=f"flashed {Path(binary).name}", ok_summary="Flash complete")

    def erase_flash(self) -> None:
        """Erase the whole ESP flash (destructive, no binary needed)."""
        plan = self._esp_plan(action="erase")
        if plan is None:
            return
        if not self._confirm_plan(plan, title="Erase flash", accept="Erase everything"):
            return
        self._execute(plan, done_label="flash erased", ok_summary="Erase complete")

    def esp_chip_info(self) -> None:
        """``esptool flash_id`` style read (safe, no writes)."""
        plan = self._esp_plan(action="id")
        if plan is None:
            return
        self._execute(plan, done_label="chip id read", ok_summary="Chip id read", reader=True)

    def _esp_plan(self, *, action: str) -> Optional[FlashPlan]:
        board = self._fqbn()
        family = family_for_fqbn(board) or ("esp8266" if "esp8266" in board else "esp32")
        port = self._port()
        if not port:
            ask_message(self, kind="warning", title="No port",
                        message="Select the serial port of the module in the toolbar first.",
                        palette=self.palette)
            return None
        baud = _int_or(str(self.baud_entry.get()), 921600)
        binary = self._binary_path()
        if action == "write" and not binary:
            return None
        try:
            if self.use_build_check.get() and action == "write":
                build_dir = self._build_dir()
                if build_dir is None:
                    ask_message(self, kind="warning", title="No build folder",
                                message="Run Verify first - the panel flashes the .bin from the last build.",
                                palette=self.palette)
                    return None
                return self.service.plan_flash_from_project(family, port, build_dir, baud=baud)
            return self.service.plan_esptool(
                family=family, port=port, baud=baud, address=str(self.address_entry.get()).strip() or "0x0",
                binary=binary or None, erase_first=bool(self.erase_check.get()),
                flash_mode=str(self.mode_menu.get()), flash_size=str(self.size_menu.get()),
                flash_freq=str(self.freq_menu.get()), action=action,
            )
        except Exception as exc:
            ask_message(self, kind="error", title="Cannot build the command", message=str(exc),
                        palette=self.palette)
            return None

    # ------------------------------------------------------------- execution
    def _confirm_plan(self, plan: FlashPlan, *, title: str, accept: str) -> bool:
        sections = [
            _section("Command", " ".join(plan.argv), "mono"),
            _section("Target", plan.human or plan.target or "-", "info"),
        ]
        for warning in plan.warnings:
            sections.append(_section("Warning", warning, "warn"))
        for note in plan.notes:
            sections.append(_section("Note", note, "dim"))
        dangerous = bool(_read_attr(plan, "is_dangerous", False))
        want_confirm = bool(getattr(self._settings, "bootloader_confirm_required", True))
        # destructive plans (erase, fuse writes) always need the acknowledgement tick
        require_ack = dangerous or want_confirm
        return bool(ask_plan(self, title=title, heading=title, sections=sections,
                             accept_label=accept, require_ack=require_ack, palette=self.palette))

    def _execute(self, plan: FlashPlan, *, done_label: str, ok_summary: str,
                 reader: bool = False, after: Optional[Callable[[TaskResult], Any]] = None) -> None:
        if self._busy:
            self._status("another chip operation is running")
            return
        self._set_busy(True, f"{plan.kind} running\u2026")
        self._log_command(plan.argv)

        def work(context: Any) -> Any:
            result = self.service.run(plan, on_line=context.log, context=context,
                                      progress=context.progress)
            payload = {"result": result}
            if reader:
                payload["chip"] = self.service.parse_chip_info(result.output)
            return payload

        def done(result: TaskResult) -> None:
            self._set_busy(False)
            payload = result.payload or {}
            command_result = payload.get("result")
            chip = payload.get("chip")
            if isinstance(chip, ChipInfo):
                self._show_chip_info(chip)
            if not result.ok:
                explanation = ""
                try:
                    explanation = self.service.explain_failure(command_result.output if command_result else
                                                               (result.error or ""), plan.kind)
                except Exception:  # pragma: no cover
                    explanation = ""
                self._status(f"{plan.kind} failed")
                self._log_line(explanation or result.error or "the tool reported an error", "error")
                ask_message(self, kind="error", title=f"{plan.kind} failed",
                            message=explanation or result.error or "The command returned an error.",
                            detail="The full tool output is in the console below. Nothing was verified.",
                            palette=self.palette)
                return
            if reader and isinstance(chip, ChipInfo) and not chip.ok:
                self._log_line(chip.error or "could not parse the chip response", "warn")
            self._status(done_label)
            self._log_line(f"{ok_summary} ({result.duration})", "ok")
            if after is not None:
                after(result)

        self._task = self._runner.submit(f"bootloader:{plan.kind}", work, lane=LANE_BUILD, on_done=done)

    def _show_chip_info(self, info: ChipInfo) -> None:
        self._last_chip_info = info
        try:
            self.info_view.clear()
            for line in info.formatted().splitlines():
                tag = "head" if line.endswith(":") else ("warn" if "mismatch" in line.lower() else "value")
                self.info_view.append_line(line, (tag,))
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        self._log_line(info.formatted(), "info")

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self._busy = bool(busy)
        try:
            self.status_label.configure(text=label or ("running\u2026" if busy else ""))
            self.cancel_button.configure(state="normal" if busy else "disabled")
        except tk.TclError:  # pragma: no cover
            pass
        self._sync_state()

    def _cancel(self) -> None:
        if self._task is not None and not self._task.is_done:
            self._task.cancel("cancelled from the bootloader panel")
            self._status("cancelling\u2026")

    # ------------------------------------------------------------------ help
    def toggle_help(self) -> None:
        """Show / hide the wiring + explanation pane."""
        try:
            if self.info_view.winfo_manager():
                self.info_view.grid_forget()
                self.help_button.configure(text="Wiring help")
            else:
                self.info_view.grid()
                self.help_button.configure(text="Hide help")
                self._show_wiring_help()
        except tk.TclError:  # pragma: no cover
            self._show_wiring_help()

    def _show_wiring_help(self) -> None:
        programmer = self._programmer()
        try:
            self.info_view.clear()
        except (tk.TclError, AttributeError):  # pragma: no cover
            return
        lines: list[tuple[str, str]] = [
            ("head", "ISP / ICSP wiring"),
            ("value", ""),
            ("key", "Board 6-pin ICSP"),
            ("pin", "  1 MISO (out from target)  ->  programmer MISO"),
            ("pin", "  2 VCC   (+5 V)             ->  programmer VCC"),
            ("pin", "  3 SCK   (clock)            ->  programmer SCK"),
            ("pin", "  4 MOSI (into target)       ->  programmer MOSI"),
            ("pin", "  5 RST                      ->  programmer RST"),
            ("pin", "  6 GND                      ->  programmer GND"),
            ("value", ""),
        ]
        if programmer is not None:
            lines += [("head", f"Programmer: {programmer.name}  (id {programmer.id})"),
                      ("key", "protocol"), ("value", f"  {programmer.protocol} @ {programmer.speed} Hz"),
                      ("key", "description"), ("value", f"  {programmer.description or '-'}")]
            for name, value in (getattr(programmer, "pins", None) or {}).items():
                lines.append(("pin", f"  {name}  {value}"))
            if programmer.needs_port:
                lines.append(("warn", "  this programmer needs a serial port (the 'Port' box)"))
            else:
                lines.append(("key", "  no port needed (the programmer is USB / uses itself)"))
            help_text = getattr(programmer, "wiring_help", "")
            if help_text:
                lines += [("value", ""), ("head", "Notes for this programmer")]
                for line in str(help_text).splitlines():
                    lines.append(("value", "  " + line if line.strip() else ""))
        lines += [
            ("value", ""),
            ("head", "Arduino as ISP"),
            ("value", "  1. Upload the 'Arduino as ISP' sketch to the helper board (Tools > Programmer),"),
            ("value", "     then use programmer id 'arduino' here. Use the 19200 baud wiring from that sketch's"),
            ("value", "     comments: helper 10 -> RST, 11 -> MOSI, 12 -> MISO, 13 -> SCK, 5V -> VCC, GND -> GND."),
            ("value", "  2. Keep the helper's reset pin free (some clones need a 10 uF capacitor between reset"),
            ("value", "     and GND so it does not restart while burning)."),
            ("value", ""),
            ("head", "Blank ATmega328P"),
            ("value", "  A fresh chip has no bootloader and no clock: set the fuse low byte to enable the"),
            ("value", "  external crystal (0xFF for 16 MHz), set the high byte for the boot section and SPIEN"),
            ("value", "  (0xDE), extended 0x05. A 100 nF capacitor between reset and ground and 10 kOhm from"),
            ("value", "  reset to +5 V make ISP reliable. Then burn the bootloader, upload the sketch, and the"),
            ("value", "  board enumerates as a normal Arduino."),
            ("value", ""),
            ("warn", "Wrong fuse bytes can stop the chip from being reachable over ISP. Double-check the"),
            ("warn", "values above - the confirmation dialog repeats them exactly as they will be written."),
        ]
        for tag, text in lines:
            self.info_view.append_line(text, (tag,))

    def _show_esp_help(self) -> None:
        lines = [
            ("head", "How flashing an ESP module works"),
            ("value", ""),
            ("value", "  ESP32 / ESP8266 boards keep their first-stage bootloader in ROM, so there is no"),
            ("value", "  bootloader to burn. Instead the chip enters the serial bootloader (GPIO0 held low on"),
            ("value", "  reset for ESP8266, or auto via DTR/RTS on most dev boards) and esptool writes the image."),
            ("value", ""),
            ("key", "Typical addresses"),
            ("value", "  ESP32 (4 MB): bootloader 0x1000, partition table 0x8000, boot app 0xE000, sketch 0x10000"),
            ("value", "  ESP8266:      sketch starts at 0x00000 (older 512 kB) or 0x1000 (newer SDK layout)"),
            ("value", ""),
            ("key", "Trouble"),
            ("value", "  'Failed to connect to ESP32: No sync packet' - hold BOOT/GPIO0 while resetting, lower"),
            ("value", "  the baud rate (115200), and check the port is not held by the Serial Monitor."),
            ("value", "  'Chip detect gives garbled output' - decoupling cap missing or the board's auto-reset"),
            ("value", "  circuit is not wired; try 'arduino:avrdude' free flow with DTR/RTS toggles."),
            ("value", ""),
            ("warn", "Erase wipes everything, including Wi-Fi credentials and any OTA sketch."),
        ]
        try:
            self.esp_info.clear()
            for tag, text in lines:
                self.esp_info.append_line(text, (tag,))
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    # ------------------------------------------------------------------ values
    def _baud_chosen(self, value: Any) -> None:
        """Sync the baud picker with the editable entry."""
        try:
            self.baud_entry.delete(0, "end")
            self.baud_entry.insert(0, str(value))
        except tk.TclError:  # pragma: no cover
            pass

    def _programmer(self) -> Optional[Programmer]:
        """The selected programmer (falls back to a custom id from Settings)."""
        try:
            label = str(self.programmer_menu.get())
        except tk.TclError:  # pragma: no cover
            return None
        for programmer in self._programmers:
            if programmer.label == label or programmer.id == label:
                return programmer
        # custom programmer id typed in Settings ("avrisp_mkII (my_custom)" style)
        candidate = label.split("(")[-1].strip().rstrip(")") if "(" in label else label
        return self.service.programmer_by_id(candidate or "arduino")

    def _port(self) -> str:
        try:
            label = str(self.port_menu.get())
        except tk.TclError:  # pragma: no cover
            return ""
        device = label.split(" - ")[0].strip()
        if device.startswith("No ports"):
            return ""
        return device or (self._get_port() if self._get_port else "")

    def _ports(self) -> list[str]:
        if self._get_ports is None:
            return []
        try:
            return list(self._get_ports() or [])
        except Exception:  # pragma: no cover
            return []

    def _fqbn(self) -> str:
        if self._get_fqbn is None:
            return ""
        try:
            return self._get_fqbn() or ""
        except Exception:  # pragma: no cover
            return ""

    def _project_root(self) -> Optional[Path]:
        if self._get_project is None:
            return None
        try:
            project = self._get_project()
        except Exception:  # pragma: no cover
            return None
        return Path(project.root) if project is not None else None

    def _build_dir(self) -> Optional[Path]:
        if self._get_build_dir is None:
            return None
        try:
            value = self._get_build_dir()
        except Exception:  # pragma: no cover
            return None
        return Path(value) if value else None

    def _binary_path(self) -> Optional[Path]:
        try:
            if self.use_build_check.get():
                build_dir = self._build_dir()
                if build_dir is not None and build_dir.is_dir():
                    for pattern in ("*.bin", "**/*.bin"):
                        found = sorted(build_dir.glob(pattern))
                        if found:
                            return found[0]
                return None
            text = str(self.bin_entry.get()).strip().strip('"')
        except (tk.TclError, AttributeError):  # pragma: no cover
            return None
        if not text:
            return None
        path = Path(text).expanduser()
        return path if path.is_file() else None

    def _backup_folder(self) -> Optional[Path]:
        from tkinter import filedialog

        initial = str(getattr(self._settings, "bootloader_backup_dir", "") or "") or str(Path.home())
        try:
            chosen = filedialog.askdirectory(parent=self, title="Where should the backup go?", initialdir=initial)
        except tk.TclError:  # pragma: no cover
            return None
        if not chosen:
            return None
        if self._settings is not None:
            try:
                self._settings.bootloader_backup_dir = chosen
            except AttributeError:  # pragma: no cover
                pass
        return Path(chosen)

    def choose_binary(self) -> None:
        """File picker for the ESP binary (also fills the path entry)."""
        from tkinter import filedialog

        build_dir = self._build_dir()
        initial = str(build_dir) if build_dir and build_dir.is_dir() else str(self._project_root() or Path.home())
        try:
            chosen = filedialog.askopenfilename(
                parent=self, title="Choose a firmware binary", initialdir=initial,
                filetypes=[("Binary firmware", "*.bin"), ("Flash image", "*.elf *.hex"), ("All files", "*.*")],
            )
        except tk.TclError:  # pragma: no cover
            return
        if not chosen:
            return
        try:
            self.bin_entry.delete(0, "end")
            self.bin_entry.insert(0, chosen)
        except tk.TclError:  # pragma: no cover
            pass
        size = ""
        try:
            size = self.service.flash_size_text(chosen)
        except Exception:  # pragma: no cover
            size = ""
        if size:
            self._status(f"{Path(chosen).name} - {size}")
        self._sync_state()

    # ------------------------------------------------------------------- misc
    def _log_command(self, argv: list[str]) -> None:
        if self._console is None:
            return
        try:
            self._console.show_command(argv)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def _log_line(self, text: str, level: str = "info") -> None:
        if self._console is None:
            return
        for line in str(text).splitlines() or [str(text)]:
            try:
                self._console.write(line, level)
            except (tk.TclError, AttributeError):  # pragma: no cover
                return

    def _status(self, message: str) -> None:
        try:
            self.status_label.configure(text=message[:110])
        except tk.TclError:  # pragma: no cover
            pass
        if self._on_status is not None:
            self._on_status(message)

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin the panel after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.window_bg)
        except tk.TclError:  # pragma: no cover
            pass
        self.fuse_table.refresh_palette(palette)

    def destroy(self) -> None:
        """Cancel a running chip operation with the panel."""
        task = self._task
        if task is not None and not task.is_done:
            try:
                task.cancel("panel closed")
            except Exception:  # pragma: no cover
                pass
        super().destroy()


# --------------------------------------------------------------------- helpers
def palette_font(palette: Palette) -> str:
    """Font family helper (kept local so the label code stays readable)."""
    return palette.font_family


def _section(title: str, content: str, tone: str) -> Any:
    from .widgets.dialogs import PlanSection

    return PlanSection(title, content, tone)


def _read_attr(obj: Any, name: str, default: Any) -> Any:
    """Read *name* whether it is a plain attribute, a property or a method."""
    value = getattr(obj, name, default)
    if callable(value):
        try:
            return value()
        except Exception:  # pragma: no cover - defensive
            return default
    return value


def _int_or(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
