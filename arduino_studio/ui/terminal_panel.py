"""Integrated terminal panel (PowerShell / cmd, scoped to the project).

The panel drives :class:`~arduino_studio.core.terminal_service.TerminalService`,
which runs a real shell in a background thread and reports output plus exit
codes back through ``ui_post``.  Nothing here blocks the UI: sending a command
returns immediately, and long commands keep the window responsive (Ctrl+C is
offered as an interrupt).
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Any, Callable, Optional

import customtkinter as ctk

from ..core.terminal_service import (
    TerminalError,
    TerminalService,
    is_destructive,
    quick_commands,
)
from ..core.utils import get_logger
from .theme import Palette
from .widgets.dialogs import ask_message, ask_yes_no
from .widgets.text_view import ScrolledTextView, TagSpec

__all__ = ["TerminalPanel"]

_MAX_LINES = 6000


class TerminalPanel(ctk.CTkFrame):
    """Terminal tab: output view, command entry, shell picker, project shortcuts."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        settings: Any = None,
        ui_post: Optional[Callable[[Callable[[], Any]], Any]] = None,
        on_status: Optional[Callable[[str], Any]] = None,
        on_files_changed: Optional[Callable[[], Any]] = None,
        cli_path_provider: Optional[Callable[[], str]] = None,
        project_dir_provider: Optional[Callable[[], Optional[Path]]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=0)
        self.palette = palette
        self._settings = settings
        self._on_status = on_status
        self._on_files_changed = on_files_changed
        self._cli_path_provider = cli_path_provider
        self._project_dir_provider = project_dir_provider
        self._log = get_logger("ui.terminal")
        self._post = ui_post or (lambda func: self.after(0, func))
        self._history: list[str] = []
        self._history_index = 0
        self._started_for: Optional[Path] = None
        self._busy = False

        self.service = TerminalService(
            ui_post=self._post, on_output=self._on_output, on_prompt=self._on_prompt,
        )
        self._build()
        self._apply_shell_setting()

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        header.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(header, text="Shell", font=(palette.font_family, 10, "bold"),
                     text_color=palette.text_dim).grid(row=0, column=0, sticky="w", padx=(10, 4), pady=8)
        shells = TerminalService.available_shells()
        self._shell_labels = {label: key for key, label, _ok in shells}
        self.shell_menu = ctk.CTkOptionMenu(
            header, values=[label for _key, label, _ok in shells] or ["PowerShell"], width=210, height=28,
            fg_color=palette.surface, button_color=palette.border, button_hover_color=palette.hover,
            text_color=palette.text, command=self._shell_chosen,
        )
        self.shell_menu.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=8)

        self.cwd_label = ctk.CTkLabel(header, text="cwd: -", font=(palette.mono_family, 9),
                                      text_color=palette.text_muted, anchor="w")
        self.cwd_label.grid(row=0, column=2, sticky="ew", padx=(4, 8))
        self.exit_label = ctk.CTkLabel(header, text="exit: -", font=(palette.mono_family, 9),
                                       text_color=palette.text_muted, anchor="e", width=110)
        self.exit_label.grid(row=0, column=3, sticky="e", padx=(0, 8))

        self.start_button = ctk.CTkButton(header, text="\u25b6  Start", width=84, height=28, corner_radius=8,
                                          fg_color=palette.accent, hover_color=palette.accent_hover,
                                          text_color=palette.accent_text, font=(palette.font_family, 10, "bold"),
                                          command=self.start)
        self.start_button.grid(row=0, column=4, sticky="e", padx=(4, 4), pady=8)
        self.restart_button = ctk.CTkButton(header, text="\u21bb", width=32, height=28, corner_radius=8,
                                            fg_color="transparent", border_width=1, border_color=palette.border,
                                            hover_color=palette.hover, text_color=palette.text_dim,
                                            command=self.restart)
        self.restart_button.grid(row=0, column=5, sticky="e", padx=(0, 4), pady=8)
        self.stop_button = ctk.CTkButton(header, text="\u25a0", width=32, height=28, corner_radius=8,
                                         fg_color="transparent", border_width=1, border_color=palette.border,
                                         hover_color=palette.hover, text_color=palette.text_dim,
                                         command=self.stop)
        self.stop_button.grid(row=0, column=6, sticky="e", padx=(0, 10), pady=8)

        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 0))
        tools.grid_columnconfigure(4, weight=1)
        self.quick_menu = ctk.CTkOptionMenu(
            tools, values=["Insert project command\u2026"], width=300, height=26,
            fg_color=palette.surface, button_color=palette.border, button_hover_color=palette.hover,
            text_color=palette.text, command=self._quick_chosen,
        )
        self.quick_menu.grid(row=0, column=0, sticky="w", padx=(2, 6))
        self.history_menu = ctk.CTkOptionMenu(
            tools, values=["History"], width=230, height=26,
            fg_color=palette.surface, button_color=palette.border, button_hover_color=palette.hover,
            text_color=palette.text, command=self._history_chosen,
        )
        self.history_menu.grid(row=0, column=1, sticky="w", padx=(0, 6))
        for offset, (text, command, width) in enumerate((
            ("Copy", self.copy_selection, 62),
            ("Paste", self.paste_clipboard, 62),
            ("Clear", self.clear, 58),
            ("Ctrl+C", self.interrupt, 68),
        )):
            button = ctk.CTkButton(tools, text=text, width=width, height=26, corner_radius=6,
                                    fg_color="transparent", border_width=1, border_color=palette.border,
                                    hover_color=palette.hover, text_color=palette.text_dim,
                                    font=(palette.font_family, 10), command=command)
            button.grid(row=0, column=2 + offset, sticky="w", padx=(0, 4))
        for index in range(2, 6):
            tools.grid_columnconfigure(index, weight=0)
        self.save_button = ctk.CTkButton(tools, text="Save output", width=92, height=26, corner_radius=6,
                                         fg_color="transparent", border_width=1, border_color=palette.border,
                                         hover_color=palette.hover, text_color=palette.text_dim,
                                         font=(palette.font_family, 10), command=self.save_output)
        self.save_button.grid(row=0, column=5, sticky="e", padx=(0, 2))

        self.view = ScrolledTextView(
            self, palette, readonly=True, wrap="word", max_lines=_MAX_LINES, corner_radius=8,
            bg=palette.console_bg, fg=palette.console_fg,
            font=(palette.mono_family, palette.mono_size - 1),
            tags=(
                TagSpec("cmd", foreground=palette.accent, font_weight="bold"),
                TagSpec("out", foreground=palette.console_fg),
                TagSpec("dim", foreground=palette.console_dim),
                TagSpec("ok", foreground=palette.success),
                TagSpec("err", foreground=palette.error, font_weight="bold"),
                TagSpec("warn", foreground=palette.warning),
            ),
        )
        self.view.grid(row=2, column=0, sticky="nsew", padx=8, pady=(4, 4))

        prompt_row = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=8, height=42)
        prompt_row.grid(row=3, column=0, sticky="ew", padx=8, pady=(0, 8))
        prompt_row.grid_columnconfigure(1, weight=1)
        prompt_row.grid_propagate(False)
        self.prompt_label = ctk.CTkLabel(prompt_row, text="\u276f", font=(palette.mono_family, palette.mono_size),
                                         text_color=palette.accent, width=34, anchor="e")
        self.prompt_label.grid(row=0, column=0, sticky="e", padx=(10, 2), pady=7)
        self.entry = ctk.CTkEntry(prompt_row, placeholder_text="PowerShell command - Enter runs it, ↑/↓ history",
                                  height=30, font=(palette.mono_family, palette.mono_size - 1),
                                  fg_color=palette.surface, text_color=palette.text, border_color=palette.border)
        self.entry.grid(row=0, column=1, sticky="ew", padx=(2, 6), pady=7)
        self.entry.bind("<Return>", lambda event: self.run_entry(), add=True)
        self.entry.bind("<Up>", lambda event: self._history_step(-1), add=True)
        self.entry.bind("<Down>", lambda event: self._history_step(1), add=True)
        self.entry.bind("<Control-l>", lambda event: self.clear(), add=True)
        self.entry.bind("<Control-c>", self._entry_copy, add=True)
        self.entry.bind("<Escape>", lambda event: self._cancel_or_clear(), add=True)
        self.run_button = ctk.CTkButton(prompt_row, text="Run", width=70, height=30, corner_radius=8,
                                        fg_color=palette.accent, hover_color=palette.accent_hover,
                                        text_color=palette.accent_text, font=(palette.font_family, 11, "bold"),
                                        command=self.run_entry)
        self.run_button.grid(row=0, column=2, sticky="e", padx=(0, 10), pady=7)

    # ------------------------------------------------------------------ build
    def _apply_shell_setting(self) -> None:
        """Match the widgets to ``settings.terminal_shell`` (PowerShell on Windows)."""
        settings = self._settings
        if settings is None:
            return
        wanted = str(getattr(settings, "terminal_shell", "") or "")
        for label, key in self._shell_labels.items():
            if key == wanted:
                try:
                    self.shell_menu.set(label)
                except tk.TclError:  # pragma: no cover
                    pass
                break
        try:
            size = int(getattr(settings, "terminal_font_size", 0) or 0)
            if size:
                self.view.set_font(self.palette.mono_family, size)
        except (TypeError, ValueError):  # pragma: no cover
            pass
        self.refresh_quick_commands()

    def refresh_quick_commands(self) -> None:
        """Rebuild the 'common project commands' menu."""
        cli_path = ""
        if self._cli_path_provider is not None:
            try:
                cli_path = self._cli_path_provider() or ""
            except Exception:  # pragma: no cover
                cli_path = ""
        project_dir = None
        if self._project_dir_provider is not None:
            try:
                project_dir = self._project_dir_provider()
            except Exception:  # pragma: no cover
                project_dir = None
        commands = quick_commands(cli_path, project_dir or "")
        labels = [f"{title}" for title, _cmd in commands]
        self._quick_map = {title: command for title, command in commands}
        try:
            self.quick_menu.configure(values=labels or ["arduino-cli not configured"])
            self.quick_menu.set(labels[0] if labels else "arduino-cli not configured")
        except tk.TclError:  # pragma: no cover
            pass

    # ----------------------------------------------------------------- service
    def start(self, *, force: bool = False) -> bool:
        """Start (or reuse) the shell in the project directory."""
        if self.service.running and not force:
            self._note("shell already running", kind="dim")
            return True
        directory = self._project_dir()
        try:
            self.service.start(cwd=directory, quiet=False)
        except TerminalError as exc:
            ask_message(self, kind="error", title="Terminal unavailable", message=str(exc),
                        detail="Pick another shell (cmd.exe) in the dropdown, or install PowerShell 7.",
                        palette=self.palette)
            return False
        self._running_changed(True)
        try:
            self.entry.focus_set()
        except tk.TclError:  # pragma: no cover
            pass
        return True

    def stop(self) -> None:
        """Terminate the shell process (output stays visible)."""
        try:
            self.service.stop()
        except Exception as exc:  # pragma: no cover - defensive
            self._log.debug("terminal stop failed: %s", exc)
        self._running_changed(False)
        self._note("shell stopped", kind="dim")

    def restart(self) -> None:
        """Restart the shell (menu action / restart button)."""
        directory = self._project_dir()
        try:
            self.service.restart(cwd=directory)
        except TerminalError as exc:
            ask_message(self, kind="error", title="Terminal unavailable", message=str(exc), palette=self.palette)
            return
        self.view.clear()
        self._note("shell restarted", kind="dim")
        self._running_changed(self.service.running)

    def _project_dir(self) -> Optional[Path]:
        if self._project_dir_provider is None:
            return None
        try:
            return self._project_dir_provider()
        except Exception:  # pragma: no cover
            return None

    def set_project(self, directory: Optional[Path]) -> None:
        """Follow the active project: change dir and refresh shortcuts."""
        self.refresh_quick_commands()
        if directory is None:
            self._started_for = None
            return
        if not self.service.running:
            self._started_for = directory
            return
        if self._settings is not None and not bool(getattr(self._settings, "terminal_follow_project_dir", True)):
            return
        try:
            self.service.change_dir(directory)
            self._note(f"cd {directory}", kind="dim")
        except TerminalError as exc:  # pragma: no cover
            self._note(str(exc), kind="warn")
        self._started_for = directory

    def _shell_chosen(self, label: str) -> None:
        key = self._shell_labels.get(str(label))
        if key is None:
            return
        if self._settings is not None:
            try:
                self._settings.terminal_shell = key
            except AttributeError:  # pragma: no cover
                pass
        try:
            self.service.set_shell(key)
        except TerminalError as exc:
            self._note(str(exc), kind="error")
            return
        if self.service.running:
            self._note(f"shell switched to {label}", kind="dim")
            self.service.restart(cwd=self._project_dir())

    def _running_changed(self, running: bool) -> None:
        try:
            self.start_button.configure(text="\u25a0  Stop" if running else "\u25b6  Start")
            self.prompt_label.configure(text_color=self.palette.accent if running else self.palette.text_muted)
        except tk.TclError:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- commands
    def run_entry(self) -> str:
        """Run what is typed in the prompt (with destructive-command guard)."""
        try:
            command = self.entry.get().strip()
        except tk.TclError:  # pragma: no cover
            return "break"
        if not command:
            return "break"
        self.run_command(command)
        try:
            self.entry.delete(0, "end")
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    def run_command(self, command: str, *, skip_confirm: bool = False) -> bool:
        """Send *command* to the shell after the destructive-command check."""
        command = str(command).strip()
        if not command:
            return False
        if not skip_confirm:
            dangerous, reason = is_destructive(command)
            if dangerous and not self._confirm_destructive(command, reason):
                return False
        if not self.service.running and not self.start():
            return False
        if not self.service.send_command(command):
            self._note("shell did not accept the command - restarting it", kind="err")
            self.restart()
            return False
        self._history.append(command)
        self._history_index = len(self._history)
        self._refresh_history_menu()
        self._busy = True
        self._note(command, kind="cmd")
        return True

    def _confirm_destructive(self, command: str, reason: str) -> bool:
        """Ask before a destructive command; True = approved."""
        if self._settings is not None and not bool(getattr(self._settings, "terminal_confirm_destructive", True)):
            return True
        approved = ask_yes_no(
            self,
            f"Run this command?\n\n    {command}",
            detail=f"{reason}\n\nThis can permanently delete or overwrite files inside the project.",
            yes_label="Run it",
            no_label="Cancel",
            kind="warning",
            palette=self.palette,
        )
        if not approved:
            self._note("destructive command cancelled", kind="warn")
        return bool(approved)

    def _quick_chosen(self, label: str) -> None:
        """Insert a preset command in the prompt (editable before running)."""
        command = getattr(self, "_quick_map", {}).get(str(label), "")
        if not command:
            return
        try:
            self.entry.delete(0, "end")
            self.entry.insert(0, command)
            self.entry.focus_set()
            self.entry.icursor("end")
        except tk.TclError:  # pragma: no cover
            pass

    def _history_chosen(self, label: str) -> None:
        label = str(label)
        if label and label != "History":
            try:
                self.entry.delete(0, "end")
                self.entry.insert(0, label)
                self.entry.focus_set()
            except tk.TclError:  # pragma: no cover
                pass

    def _refresh_history_menu(self) -> None:
        recent = list(reversed(self._history[-25:]))
        try:
            self.history_menu.configure(values=recent or ["History"])
            self.history_menu.set(recent[0] if recent else "History")
        except tk.TclError:  # pragma: no cover
            pass

    def _history_step(self, direction: int) -> str:
        if not self._history:
            return "break"
        self._history_index = max(0, min(len(self._history), self._history_index + int(direction)))
        value = self._history[self._history_index] if self._history_index < len(self._history) else ""
        try:
            self.entry.delete(0, "end")
            self.entry.insert(0, value)
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    def interrupt(self) -> None:
        """Ctrl+C to the shell (breaks out of a running command)."""
        if not self.service.running:
            self._note("no shell running", kind="dim")
            return
        if self.service.interrupt():
            self._note("^C sent", kind="dim")
        else:  # pragma: no cover
            self._note("interrupt not supported for this shell - use Stop", kind="warn")

    def _cancel_or_clear(self) -> str:
        if self._busy:
            self.interrupt()
            return "break"
        try:
            if self.entry.get():
                self.entry.delete(0, "end")
            else:
                self.view.focus_text()
        except tk.TclError:  # pragma: no cover
            pass
        return "break"

    # ------------------------------------------------------------------- output
    #: :class:`~arduino_studio.core.terminal_service.TerminalService` emits
    #: ``on_output(kind, text)``; these are the kinds it uses.
    _OUTPUT_TAGS = {
        "output": "out", "out": "out", "command": "cmd", "cmd": "cmd",
        "error": "err", "err": "err", "info": "dim", "note": "dim", "prompt": "dim",
    }

    def _on_output(self, kind: str, text: str = "") -> None:
        """Append one service message (already marshalled onto the UI thread)."""
        if str(kind) not in self._OUTPUT_TAGS and str(text) in self._OUTPUT_TAGS:
            kind, text = text, kind  # tolerate a (text, kind) caller
        tag = self._OUTPUT_TAGS.get(str(kind), "out")
        for line in str(text).split("\n"):
            if line == "" and tag == "out":
                continue
            self.view.append_line(line, (tag,))
        self.view.see_end()

    def _on_prompt(self, cwd: str, exit_code: Optional[int] = None, ok: Optional[bool] = None) -> None:
        """Sentinel reached: the previous command finished."""
        self._busy = False
        try:
            self.cwd_label.configure(text=f"cwd: {cwd}")
        except tk.TclError:  # pragma: no cover
            pass
        good = (exit_code in (0, None)) if ok is None else bool(ok)
        try:
            self.exit_label.configure(
                text=f"exit: {0 if exit_code is None else exit_code}",
                text_color=self.palette.success if good else self.palette.error,
            )
        except tk.TclError:  # pragma: no cover
            pass
        if not good:
            self.view.append_line(f"command exited with code {exit_code}", ("err",))
        if self._on_files_changed is not None:
            self._on_files_changed()

    def _note(self, text: str, *, kind: str = "dim") -> None:
        for line in str(text).split("\n"):
            self.view.append_line(line, (kind,))

    def clear(self) -> str:
        """Clear the output view (does not touch the shell)."""
        self.view.clear()
        self._note("cleared - type 'cls' to reset the prompt banner", kind="dim")
        return "break"

    def copy_selection(self) -> None:
        try:
            self.view.text.copy_selection()
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def paste_clipboard(self) -> None:
        try:
            value = self.clipboard_get()
        except tk.TclError:  # pragma: no cover
            return
        try:
            self.entry.delete(0, "end")
            self.entry.insert(0, value.strip().split("\n")[0])
            self.entry.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def _entry_copy(self, event: "tk.Event") -> str:
        """Ctrl+C in the entry copies the selection; otherwise sends an interrupt."""
        try:
            if self.entry.get().strip():
                return ""     # let the normal clipboard binding handle it
        except tk.TclError:  # pragma: no cover
            pass
        self.interrupt()
        return "break"

    def save_output(self) -> None:
        """Write the transcript to a file."""
        from tkinter import filedialog
        import time

        path = filedialog.asksaveasfilename(
            parent=self, title="Save terminal output", defaultextension=".txt",
            initialfile=f"terminal_{time.strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(self.view.content(), encoding="utf-8")
        except OSError as exc:
            ask_message(self, kind="error", title="Could not save", message=str(exc), palette=self.palette)
            return
        if self._on_status is not None:
            self._on_status(f"terminal output saved: {Path(path).name}")

    # -------------------------------------------------------------------- misc
    def focus_command(self) -> None:
        """Menu action: focus the prompt, starting the shell if needed."""
        if not self.service.running:
            self.start()
        try:
            self.entry.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def run_captured_command(self, command: str) -> tuple[int, str]:
        """Run *command* outside the visible shell (one-shot, for tooling)."""
        try:
            return self.service.run_captured(command)
        except TerminalError as exc:
            return 127, str(exc)

    def refresh_palette(self, palette: Palette) -> None:
        """Re-skin after a theme change."""
        self.palette = palette
        try:
            self.configure(fg_color=palette.window_bg)
            self.view.text.configure(bg=palette.console_bg, fg=palette.console_fg)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass

    def destroy(self) -> None:
        """Kill the shell process with the panel."""
        try:
            self.service.stop(silent=True)
        except Exception:  # pragma: no cover
            pass
        super().destroy()
