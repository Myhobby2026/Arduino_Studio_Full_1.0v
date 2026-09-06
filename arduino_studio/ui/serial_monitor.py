"""Serial monitor panel (open/close, receive view, transmit field, tools).

The panel owns its :class:`~arduino_studio.core.serial_service.SerialService`
but the service never touches widgets: data arrives on a reader thread and is
forwarded to the UI through the ``ui_post`` callback (``root.after``).

Features required by the specification and implemented here:

* configurable baud rate (common presets + custom), port picker with refresh,
* transmit field with line-ending selector (None / LF / CR / CRLF),
* receive window with timestamps, pause, autoscroll, clear, save log, filter,
* the port is released automatically before an upload and offered again after.
"""

from __future__ import annotations

import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.serial_service import (
    SerialConfig,
    SerialError,
    SerialPortInfo,
    SerialService,
    list_serial_ports,
)
from ..core.settings import COMMON_BAUD_RATES
from ..core.utils import get_logger
from .widgets.dialogs import ask_message, ask_text
from .widgets.text_view import ScrolledTextView, TagSpec
from .theme import Palette

__all__ = ["SerialMonitorPanel"]

#: menu label -> code understood by :meth:`SerialService.write`
ENDING_LABELS: dict[str, str] = {
    "None": "None",
    "Newline": "LF",
    "Carriage return": "CR",
    "Both NL & CR": "CRLF",
}
#: and the other way round (settings store the code)
CODE_TO_LABEL: dict[str, str] = {code: label for label, code in ENDING_LABELS.items()}

_MAX_LOG_LINES = 20000


class SerialMonitorPanel(ctk.CTkFrame):
    """Serial monitor UI bound to a :class:`SerialService`."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        settings: Any = None,
        ui_post: Optional[Callable[[Callable[[], Any]], Any]] = None,
        on_status: Optional[Callable[[str], Any]] = None,
        on_open_state: Optional[Callable[[bool], Any]] = None,
        on_request_ports: Optional[Callable[[], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=0)
        self._settings = settings
        self.palette = palette
        self._on_status = on_status
        self._on_open_state = on_open_state
        self._on_request_ports = on_request_ports
        self._log = get_logger("ui.serial")
        self._post = ui_post or (lambda func: self.after(0, func))
        self._all_lines: list[tuple[str, str]] = []      # (text, kind)
        self._tx_history: list[str] = []
        self._tx_index = 0
        self._timestamps = tk.BooleanVar(value=True)
        self._autoscroll = tk.BooleanVar(value=True)
        self._paused = tk.BooleanVar(value=False)
        self._wrap = tk.BooleanVar(value=True)
        self._opened_by_us = False
        self._needs_reopen = False
        self._last_filter = ""
        self._bytes_job: Optional[str] = None

        self.service = SerialService(
            ui_post=self._post, on_data=self._on_data, on_state=self._on_state,
        )
        self._build()
        self._sync_settings()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # --- connection row -------------------------------------------------
        top = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8, height=44)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        top.grid_propagate(False)
        top.grid_columnconfigure(0, weight=1)

        self.port_menu = ctk.CTkOptionMenu(top, values=["No ports found"], width=250, height=30,
                                           command=self._port_chosen)
        self.port_menu.grid(row=0, column=0, sticky="w", padx=(12, 4), pady=7)
        self.refresh_ports_button = ctk.CTkButton(top, text="\u21bb", width=32, height=30, corner_radius=8,
                                                  fg_color=palette.surface, hover_color=palette.hover,
                                                  text_color=palette.text, command=self.refresh_ports)
        self.refresh_ports_button.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=7)

        self.baud_menu = ctk.CTkEntry(top, width=86, height=30, font=(palette.mono_family, palette.mono_size - 1),
                                      fg_color=palette.surface, text_color=palette.text,
                                      border_color=palette.border)
        self.baud_menu.insert(0, "9600")
        self.baud_menu.grid(row=0, column=2, sticky="w", padx=(0, 4), pady=7)
        self.baud_picker = ctk.CTkOptionMenu(top, values=[str(rate) for rate in COMMON_BAUD_RATES],
                                             width=42, height=30, corner_radius=8,
                                             fg_color=palette.surface, button_color=palette.border,
                                             button_hover_color=palette.hover, text_color=palette.text,
                                             command=lambda value: self.baud_menu.delete(0, "end")
                                             or self.baud_menu.insert(0, str(value)))
        self.baud_picker.grid(row=0, column=3, sticky="w", padx=(0, 8), pady=7)

        self.connect_button = ctk.CTkButton(top, text="\u25cf  Connect", width=120, height=30, corner_radius=8,
                                            fg_color=palette.accent, hover_color=palette.accent_hover,
                                            text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                            command=self.toggle_connection)
        self.connect_button.grid(row=0, column=4, sticky="w", padx=(4, 8), pady=7)

        self.dtr_var = tk.BooleanVar(value=True)
        self.rts_var = tk.BooleanVar(value=True)
        self.dtr_check = ctk.CTkCheckBox(top, text="DTR", variable=self.dtr_var, width=54,
                                         checkbox_width=16, checkbox_height=16,
                                         font=(palette.font_family, 10), text_color=palette.text_dim,
                                         command=lambda: self._flow_changed("dtr"))
        self.dtr_check.grid(row=0, column=5, sticky="w", padx=(2, 4), pady=7)
        self.rts_check = ctk.CTkCheckBox(top, text="RTS", variable=self.rts_var, width=54,
                                         checkbox_width=16, checkbox_height=16,
                                         font=(palette.font_family, 10), text_color=palette.text_dim,
                                         command=lambda: self._flow_changed("rts"))
        self.rts_check.grid(row=0, column=6, sticky="w", padx=(0, 6), pady=7)
        self.reset_button = ctk.CTkButton(top, text="Reset board", width=92, height=30, corner_radius=8,
                                          fg_color="transparent", border_width=1, border_color=palette.border,
                                          hover_color=palette.hover, text_color=palette.text_dim,
                                          font=(palette.font_family, 10), command=self.reset_board)
        self.reset_button.grid(row=0, column=7, sticky="w", padx=(0, 8), pady=7)

        # --- toolbar --------------------------------------------------------
        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 0))
        tools.grid_columnconfigure(6, weight=1)

        self.time_check = ctk.CTkCheckBox(tools, text="Add timestamp", variable=self._timestamps, width=130,
                                          checkbox_width=16, checkbox_height=16, font=(palette.font_family, 10),
                                          text_color=palette.text_dim, command=self._options_changed)
        self.time_check.grid(row=0, column=0, sticky="w", padx=(2, 10))
        self.autoscroll_check = ctk.CTkCheckBox(tools, text="Autoscroll", variable=self._autoscroll, width=100,
                                                checkbox_width=16, checkbox_height=16,
                                                font=(palette.font_family, 10), text_color=palette.text_dim)
        self.autoscroll_check.grid(row=0, column=1, sticky="w", padx=(0, 10))
        self.wrap_check = ctk.CTkCheckBox(tools, text="Wrap", variable=self._wrap, width=70,
                                          checkbox_width=16, checkbox_height=16, font=(palette.font_family, 10),
                                          text_color=palette.text_dim, command=self._options_changed)
        self.wrap_check.grid(row=0, column=2, sticky="w", padx=(0, 10))
        self.pause_button = ctk.CTkButton(tools, text="\u2016  Pause", width=86, height=26, corner_radius=6,
                                          fg_color="transparent", border_width=1, border_color=palette.border,
                                          hover_color=palette.hover, text_color=palette.text_dim,
                                          font=(palette.font_family, 10), command=self.toggle_pause)
        self.pause_button.grid(row=0, column=3, sticky="w", padx=(0, 4))
        self.clear_button = ctk.CTkButton(tools, text="Clear", width=64, height=26, corner_radius=6,
                                          fg_color="transparent", border_width=1, border_color=palette.border,
                                          hover_color=palette.hover, text_color=palette.text_dim,
                                          font=(palette.font_family, 10), command=self.clear)
        self.clear_button.grid(row=0, column=4, sticky="w")
        self.save_button = ctk.CTkButton(tools, text="Save log", width=84, height=26, corner_radius=6,
                                         fg_color="transparent", border_width=1, border_color=palette.border,
                                         hover_color=palette.hover, text_color=palette.text_dim,
                                         font=(palette.font_family, 10), command=self.save_log)
        self.save_button.grid(row=0, column=5, sticky="w", padx=(4, 0))

        self.filter_entry = ctk.CTkEntry(tools, placeholder_text="filter output\u2026", width=180, height=26,
                                         font=(palette.font_family, 10), fg_color=palette.surface,
                                         text_color=palette.text, border_color=palette.border)
        self.filter_entry.grid(row=0, column=6, sticky="ew", padx=(10, 6))
        self.filter_entry.bind("<KeyRelease>", lambda event: self._apply_filter(), add=True)
        self.filter_entry.bind("<Escape>", lambda event: self._clear_filter(), add=True)
        self.filter_entry.bind("<Return>", lambda event: self._apply_filter(), add=True)
        self.stats_label = ctk.CTkLabel(tools, text="closed", font=(palette.font_family, 10),
                                        text_color=palette.text_muted, width=230, anchor="e")
        self.stats_label.grid(row=0, column=7, sticky="e")

        # --- receive view ---------------------------------------------------
        self.view = ScrolledTextView(
            self, palette, readonly=True, wrap="word", max_lines=_MAX_LOG_LINES, corner_radius=8,
            bg=palette.console_bg, fg=palette.console_fg,
            font=(palette.mono_family, palette.mono_size - 1),
            tags=(
                TagSpec("rx", foreground=palette.console_fg),
                TagSpec("tx", foreground=palette.accent),
                TagSpec("time", foreground=palette.console_dim),
                TagSpec("note", foreground=palette.info, font_weight="bold"),
                TagSpec("error", foreground=palette.error, font_weight="bold"),
                TagSpec("match", background=palette.editor_find, foreground=palette.editor_fg),
            ),
        )
        self.view.grid(row=2, column=0, sticky="nsew", padx=8, pady=(6, 4))

        # --- transmit row ---------------------------------------------------
        bottom = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8, height=44)
        bottom.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        bottom.grid_propagate(False)
        bottom.grid_columnconfigure(0, weight=1)
        self.send_entry = ctk.CTkEntry(bottom, placeholder_text="Type a message and press Enter to send\u2026",
                                       height=30, font=(palette.mono_family, palette.mono_size - 1),
                                       fg_color=palette.surface, text_color=palette.text,
                                       border_color=palette.border)
        self.send_entry.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=7)
        self.send_entry.bind("<Return>", lambda event: self.send())
        self.send_entry.bind("<Up>", lambda event: self._history_step(-1))
        self.send_entry.bind("<Down>", lambda event: self._history_step(1))
        self.ending_menu = ctk.CTkOptionMenu(bottom, values=list(ENDING_LABELS), width=126, height=30,
                                             fg_color=palette.surface, button_color=palette.border,
                                             button_hover_color=palette.hover, text_color=palette.text,
                                             command=self._ending_changed)
        self.ending_menu.set("Newline")
        self.ending_menu.grid(row=0, column=1, sticky="e", padx=(0, 6), pady=7)
        self.send_button = ctk.CTkButton(bottom, text="Send", width=78, height=30, corner_radius=8,
                                         fg_color=palette.accent, hover_color=palette.accent_hover,
                                         text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                         command=self.send)
        self.send_button.grid(row=0, column=2, sticky="e", padx=(0, 10), pady=7)

    # ----------------------------------------------------------------- helpers
    def _sync_settings(self) -> None:
        """Adopt the serial-related settings."""
        settings = self._settings
        if settings is None:
            return
        try:
            self.baud_menu.delete(0, "end")
            self.baud_menu.insert(0, str(int(getattr(settings, "serial_baud", 9600) or 9600)))
            code = str(getattr(settings, "serial_line_ending", "LF") or "LF")
            self.ending_menu.set(CODE_TO_LABEL.get(code, "Newline"))
            self._timestamps.set(bool(getattr(settings, "serial_timestamps", True)))
            self._autoscroll.set(bool(getattr(settings, "serial_autoscroll", True)))
            self.dtr_var.set(bool(getattr(settings, "serial_toggle_dtr", True)))
            self.rts_var.set(bool(getattr(settings, "serial_toggle_rts", True)))
            port = str(getattr(settings, "port", "") or "")
            if port:
                self.port_menu.set(port)
            size = int(getattr(settings, "serial_font_size", 0) or 0)
            if size:
                self.view.set_font(self.palette.mono_family, size)
        except (tk.TclError, ValueError, TypeError):  # pragma: no cover
            pass

    def _ending_code(self) -> str:
        """Line-ending code for :meth:`SerialService.write` ('Newline' -> LF)."""
        try:
            label = str(self.ending_menu.get())
        except tk.TclError:  # pragma: no cover
            return "LF"
        return ENDING_LABELS.get(label, "LF")

    def _ending_changed(self, label: str) -> None:
        """Remember the chosen line ending in the settings file."""
        if self._settings is None:
            return
        try:
            self._settings.serial_line_ending = ENDING_LABELS.get(str(label), "LF")
        except AttributeError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------- ports
    def refresh_ports(self, ports: Optional[list[SerialPortInfo]] = None) -> None:
        """Republish available ports (queried by the app, or read directly)."""
        if ports is None:
            if self._on_request_ports is not None:
                self._on_request_ports()
                return
            ports = list_serial_ports()
        labels = [info.label for info in ports] if ports else ["No ports found"]
        selected = ""
        try:
            selected = str(self.port_menu.get())
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.port_menu.configure(values=labels)
            if selected in labels:
                self.port_menu.set(selected)
            elif ports:
                preferred = str(getattr(self._settings, "port", "") or "") if self._settings else ""
                match = next((info.label for info in ports if info.device == preferred), None)
                self.port_menu.set(match or ports[0].label)
        except tk.TclError:  # pragma: no cover
            pass
        if self._needs_reopen and ports:
            self._offer_reopen()

    def _port_device(self) -> str:
        """Extract ``COM7`` / ``/dev/ttyUSB0`` from the dropdown text."""
        try:
            text = str(self.port_menu.get())
        except tk.TclError:  # pragma: no cover
            return ""
        return text.split(" - ")[0].strip() or text.strip()

    def _port_chosen(self, value: str) -> None:
        """Persist the picked port and remember it for the upload flow."""
        device = str(value).split(" - ")[0].strip()
        if self._settings is not None:
            try:
                self._settings.port = device
            except AttributeError:  # pragma: no cover
                pass
        try:
            self.baud_menu.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def _baud(self) -> int:
        try:
            return int(str(self.baud_menu.get()).strip())
        except (ValueError, tk.TclError):
            return 9600

    # --------------------------------------------------------------- open/close
    def toggle_connection(self) -> None:
        """Connect or disconnect (button label follows the state)."""
        if self.service.is_open:
            self.close_port()
        else:
            self.open_port()

    def open_port(self, *, port: str = "", baud: Optional[int] = None) -> bool:
        """Open the selected port; shows a dialog on failure."""
        if self.service.is_open:
            self.close_port()
        device = port or self._port_device()
        if not device or device.startswith("No ports"):
            ask_message(self, kind="warning", title="No port selected",
                        message="No serial port is selected.",
                        detail="Connect the board and press the refresh button next to the port list.",
                        palette=self.palette)
            return False
        config = SerialConfig(port=device, baud=baud if baud is not None else self._baud(),
                              dtr=bool(self.dtr_var.get()), rts=bool(self.rts_var.get()))
        try:
            self.service.open(config)
        except SerialError as exc:
            self._note(f"Could not open {device}: {exc}", kind="error")
            ask_message(self, kind="error", title="Serial port error", message=str(exc),
                        detail=f"Port: {device} @ {config.baud} baud\n"
                               "Close the Arduino IDE Serial Monitor or any other program using this port.",
                        palette=self.palette)
            return False
        self._opened_by_us = True
        self._needs_reopen = False
        self._set_connected_ui(True, f"{device} @ {config.baud}")
        self._note(f"Connected to {device} at {config.baud} baud", kind="note")
        if self._settings is not None:
            try:
                self._settings.serial_baud = int(config.baud)
                self._settings.port = device
            except AttributeError:  # pragma: no cover
                pass
        return True

    def close_port(self, *, silent: bool = False) -> None:
        """Close the port (``silent`` skips the note, used by upload handling)."""
        was_open = self.service.is_open
        try:
            self.service.close()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.debug("serial close failed: %s", exc)
        if not silent and was_open:
            self._note("Disconnected", kind="note")
        self._set_connected_ui(False, "closed")

    def _set_connected_ui(self, connected: bool, detail: str) -> None:
        palette = self.palette
        try:
            self.connect_button.configure(
                text="\u25a0  Disconnect" if connected else "\u25cf  Connect",
                fg_color=palette.error if connected else palette.accent,
                hover_color=palette.hover,
                text_color=palette.accent_text,
            )
            for widget in (self.port_menu, self.baud_menu, self.baud_picker, self.refresh_ports_button):
                widget.configure(state="disabled" if connected else "normal")
            for widget in (self.send_entry, self.send_button, self.ending_menu, self.reset_button):
                widget.configure(state="normal" if connected else "disabled")
            self.stats_label.configure(text=detail)
        except tk.TclError:  # pragma: no cover
            pass
        if self._on_open_state is not None:
            self._on_open_state(bool(connected))
        if connected and self._bytes_job is None:
            self._poll_bytes()

    def _poll_bytes(self) -> None:
        """Refresh RX/TX counters once a second while connected."""
        if not self.service.is_open:
            self._bytes_job = None
            return
        try:
            self.stats_label.configure(text=self.service.status_line().replace("Serial: ", ""))
        except tk.TclError:  # pragma: no cover
            self._bytes_job = None
            return
        self._bytes_job = self.after(1000, self._poll_bytes)

    def _flow_changed(self, which: str) -> None:
        if not self.service.is_open:
            return
        try:
            if which == "dtr":
                self.service.set_flow_control(dtr=bool(self.dtr_var.get()))
            else:
                self.service.set_flow_control(rts=bool(self.rts_var.get()))
        except SerialError as exc:
            self._note(str(exc), kind="error")

    def reset_board(self) -> None:
        """Toggle DTR to reset the board (like the IDE's monitor does)."""
        if not self.service.is_open:
            return
        try:
            self.service.pulse_dtr()
            self._note("DTR pulse sent - board reset", kind="note")
        except SerialError as exc:
            self._note(str(exc), kind="error")

    # -------------------------------------------------------------------- data
    def send(self) -> None:
        """Send the transmit field with the selected line ending."""
        text = ""
        try:
            text = self.send_entry.get()
        except tk.TclError:  # pragma: no cover
            return
        if not text and not self.service.is_open:
            return
        if not self.service.is_open:
            self._note("Connect to a port first", kind="error")
            return
        try:
            self.service.write(text, line_ending=self._ending_code())
        except SerialError as exc:
            self._note(f"Send failed: {exc}", kind="error")
            return
        try:
            self.send_entry.delete(0, "end")
        except tk.TclError:  # pragma: no cover
            pass
        if text:
            self._tx_history.append(text)
            self._tx_index = len(self._tx_history)

    def send_text(self, text: str) -> bool:
        """Programmatic send (used by the command buttons of other panels)."""
        if not self.service.is_open:
            return False
        try:
            self.service.write(text, line_ending=self._ending_code())
            return True
        except SerialError as exc:
            self._note(str(exc), kind="error")
            return False

    def _history_step(self, direction: int) -> str:
        if not self._tx_history:
            return "break"
        self._tx_index = max(0, min(len(self._tx_history), self._tx_index + int(direction)))
        value = self._tx_history[self._tx_index] if self._tx_index < len(self._tx_history) else ""
        try:
            self.send_entry.delete(0, "end")
            self.send_entry.insert(0, value)
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    def _on_data(self, text: str, direction: str) -> None:
        """Reader-thread callback (already marshalled onto the UI thread)."""
        kind = "tx" if direction == "tx" else "rx"
        self._append(text, kind)

    def _on_state(self, state: str, message: str) -> None:
        if state == "error":
            self._note(f"Serial error: {message}", kind="error")
            self._set_connected_ui(False, "closed")
        elif state == "close":
            if self.service.is_open is False:
                self._set_connected_ui(False, "closed")
        elif state == "open":
            self._set_connected_ui(True, message)
        elif state == "rx-timeout":
            self._note("Timeout while reading", kind="error")
            self.close_port(silent=True)

    def _append(self, line: str, kind: str) -> None:
        """Add one line to the log model and (maybe) the view."""
        if self._filter_out(line):
            self._all_lines.append((line, kind))
            return
        prefix = ""
        if self._timestamps.get() and kind != "tx":
            prefix = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  "
        tags = (kind,) if not prefix else ("time", kind)
        self.view.append(prefix + line, tags)
        if self._autoscroll.get() or kind == "tx":
            self.view.see_end()
        self._all_lines.append((line, kind))

    def _note(self, line: str, *, kind: str = "note") -> None:
        self._append(line, kind)

    def write_line(self, line: str, kind: str = "note") -> None:
        """Public helper so the app can annotate the monitor output."""
        self._append(line, kind)

    # ------------------------------------------------------------------- tools
    def toggle_pause(self) -> None:
        """Pause/resume rendering (data keeps being buffered by the service)."""
        if self._paused.get():
            self.service.resume()
            self._paused.set(False)
            try:
                self.pause_button.configure(text="\u2016  Pause")
            except tk.TclError:  # pragma: no cover
                pass
            self._note("Resumed", kind="note")
        else:
            self.service.pause()
            self._paused.set(True)
            try:
                self.pause_button.configure(text="\u25b6  Resume")
            except tk.TclError:  # pragma: no cover
                pass
            self._note("Paused - incoming data is buffered", kind="note")

    def clear(self) -> None:
        """Clear the receive window and the internal log."""
        self._all_lines.clear()
        self.view.clear()

    def _apply_filter(self) -> None:
        needle = ""
        try:
            needle = self.filter_entry.get().strip().lower()
        except tk.TclError:  # pragma: no cover
            return
        if needle == self._last_filter:
            return
        self._last_filter = needle
        self.view.clear()
        for line, kind in self._all_lines:
            if needle and needle not in line.lower():
                continue
            self.view.append_line(line, (kind,))
        self.view.see_end()

    def _clear_filter(self) -> str:
        try:
            self.filter_entry.delete(0, "end")
        except tk.TclError:  # pragma: no cover
            pass
        self._apply_filter()
        return "break"

    def _filter_out(self, line: str) -> bool:
        needle = self._last_filter
        return bool(needle) and needle not in line.lower()

    def save_log(self) -> None:
        """Write the visible log to a text file."""
        try:
            from tkinter import filedialog

            path = filedialog.asksaveasfilename(
                parent=self, title="Save serial log", defaultextension=".txt",
                initialfile=f"serial_log_{time.strftime('%Y%m%d_%H%M%S')}.txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
        except (tk.TclError, RuntimeError):  # pragma: no cover
            path = ""
        if not path:
            return
        lines = [line for line, _kind in self._all_lines]
        try:
            saved = SerialService.save_log(lines, path, header=self.service.status_line())
        except OSError as exc:
            ask_message(self, kind="error", title="Could not save log", message=str(exc), palette=self.palette)
            return
        self._note(f"Log saved to {saved}", kind="note")
        if self._on_status is not None:
            self._on_status(f"serial log saved: {Path(saved).name}")

    def copy_all(self) -> str:
        """Copy the whole log to the clipboard (menu entry)."""
        text = "\n".join(line for line, _kind in self._all_lines)
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:  # pragma: no cover
            return ""
        return text

    # ------------------------------------------------------- upload handshake
    def release_port_for_upload(self) -> bool:
        """Close the port before an upload (returns True if it was open)."""
        if not self.service.is_open:
            return False
        port = self.service.active_port()
        released = self.service.close_for_upload(port)
        if released:
            self._needs_reopen = True
            self._note(f"Port {port} released for upload", kind="note")
            self._set_connected_ui(False, "released for upload")
        return bool(released)

    def upload_finished(self) -> None:
        """Offer to reopen the port after a successful upload."""
        if not self._needs_reopen:
            return
        self._needs_reopen = False
        if self.service.is_open:
            return
        auto = bool(getattr(self._settings, "serial_auto_reopen_after_upload", True)) if self._settings else True
        if auto:
            device = self._port_device()
            if device:
                if self.open_port(port=device):
                    self._note("Port reopened after upload", kind="note")
                return
        self._note("Upload finished - press Connect to resume monitoring", kind="note")

    def _offer_reopen(self) -> None:
        if not self._needs_reopen or self.service.is_open:
            return
        self.upload_finished()

    # -------------------------------------------------------------------- misc
    def persist_options(self) -> None:
        """Push the current monitor options back into the settings."""
        settings = self._settings
        if settings is None:
            return
        for name, value in (
            ("serial_baud", self._baud()),
            ("serial_timestamps", bool(self._timestamps.get())),
            ("serial_autoscroll", bool(self._autoscroll.get())),
            ("serial_toggle_dtr", bool(self.dtr_var.get())),
            ("serial_toggle_rts", bool(self.rts_var.get())),
        ):
            try:
                setattr(settings, name, value)
            except AttributeError:  # pragma: no cover
                continue

    def _options_changed(self) -> None:
        """Timestamp / wrap toggles."""
        try:
            self.view.text.configure(wrap="word" if self._wrap.get() else "none")
            self.view.text.configure(width=1)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def focus_send(self) -> None:
        try:
            self.send_entry.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def choose_custom_baud(self) -> None:
        """Menu action: type an arbitrary baud rate."""
        value = ask_text(self, "Custom baud rate", initial=str(self._baud()), title="Baud rate",
                         palette=self.palette)
        if value is None:
            return
        try:
            rate = int(str(value).strip())
        except ValueError:
            ask_message(self, kind="error", title="Invalid baud", message="'{}' is not a number.".format(value),
                        palette=self.palette)
            return
        self.baud_menu.delete(0, "end")
        self.baud_menu.insert(0, str(rate))
        if self.service.is_open:
            self.open_port()

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin the monitor after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.window_bg)
            self.view.text.configure(bg=palette.console_bg, fg=palette.console_fg,
                                     selectbackground=palette.console_selection)
            self.view.text.tag_configure("tx", foreground=palette.accent)
            self.view.text.tag_configure("time", foreground=palette.console_dim)
            self.view.text.tag_configure("error", foreground=palette.error)
            self.view.text.tag_configure("note", foreground=palette.info)
            self.connect_button.configure(fg_color=palette.error if self.service.is_open else palette.accent)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def destroy(self) -> None:
        """Stop the reader thread before the widget goes away."""
        if self._bytes_job is not None:
            try:
                self.after_cancel(self._bytes_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._bytes_job = None
        try:
            self.service.close()
        except Exception:  # pragma: no cover
            pass
        super().destroy()
