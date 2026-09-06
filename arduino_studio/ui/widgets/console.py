"""Compiler / upload console panel.

Features: colour coded severity tags, clickable error lines (jump to the
offending file and line), a progress bar with elapsed time, word wrap,
autoscroll lock, level filtering, save-log, and a *Cancel* button that is wired
to the active background task.
"""

from __future__ import annotations

import datetime as _datetime
import re
import time as _time
import tkinter as tk
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import customtkinter as ctk

from ...core.utils import get_logger, human_duration
from ..icons import icon_text
from ..theme import Palette
from .text_view import TagSpec, ThemedTextView

__all__ = ["ConsolePanel", "CONSOLE_TAGS"]

CONSOLE_TAGS: tuple[TagSpec, ...] = (
    TagSpec("cmd", foreground="#7ee787", font_weight="bold"),
    TagSpec("info", foreground=None),
    TagSpec("dim", foreground="#8b949e"),
    TagSpec("ok", foreground="#3fb950", font_weight="bold"),
    TagSpec("warn", foreground="#d29922"),
    TagSpec("error", foreground="#ff7b72", font_weight="bold"),
    TagSpec("note", foreground="#8b949e"),
    TagSpec("step", foreground="#58a6ff"),
    TagSpec("head", foreground="#d2a8ff", font_weight="bold"),
    TagSpec("time", foreground="#5c6672"),
    TagSpec("jump", foreground="#58a6ff", underline=True),
    TagSpec("errline", background="#3a1d20"),
)

_LEVEL_TAG = {
    "cmd": "cmd", "command": "cmd", "info": "info", "dim": "dim", "ok": "ok", "success": "ok",
    "warn": "warn", "warning": "warn", "error": "error", "note": "note", "step": "step",
    "head": "head", "progress": "dim",
}
_DIAG_LINE_RE = re.compile(r"(?P<file>[\w./\\+\- ]+\.(?:ino|cpp|c|h|hpp|S)):(?P<line>\d+)(?::\d+)?:\s*(?P<kind>error|warning|note|fatal error)")
ANSI_RESET = "\x1b[0m"


class ConsolePanel(ctk.CTkFrame):
    """Bottom panel: build/upload output with progress + error navigation."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        on_jump: Optional[Callable[[str, int], Any]] = None,
        on_cancel: Optional[Callable[[], Any]] = None,
        on_save_log: Optional[Callable[[str], Any]] = None,
        settings: Any = None,
    ) -> None:
        super().__init__(master, fg_color=palette.window_bg, corner_radius=10, border_width=1,
                         border_color=palette.border)
        self.palette = palette
        self._on_jump = on_jump
        self._on_cancel = on_cancel
        self._on_save_log = on_save_log
        self._settings = settings
        self._log = get_logger("console")
        self._buffer: deque[str] = deque()
        self._pending: list[tuple[str, str]] = []
        self._flush_job: Optional[str] = None
        self._autoscroll = True
        self._show_timestamps = bool(getattr(settings, "console_timestamps", False))
        self._filter_errors_only = False
        self._wrap = False
        self._started_at: Optional[float] = None
        self._busy = False
        self._progress = 0.0
        self._elapsed_job: Optional[str] = None
        self._max_lines = int(getattr(settings, "console_max_lines", 5000) or 5000)
        self._font_size = int(getattr(settings, "console_font_size", 10) or 10)
        self._operations = 0
        self._errors = 0

        self._build()

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        palette = self.palette
        header = ctk.CTkFrame(self, fg_color=palette.panel_bg, corner_radius=0, height=34)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        self.title_label = ctk.CTkLabel(
            header, text="  Console", font=(palette.font_family, 12, "bold"),
            text_color=palette.text, anchor="w",
        )
        self.title_label.pack(side="left", padx=(4, 0))

        self.progress_label = ctk.CTkLabel(header, text="", font=(palette.font_family, 10),
                                           text_color=palette.text_muted, anchor="w", width=170)
        self.progress_label.pack(side="left", padx=(12, 0))

        self.progress = ctk.CTkProgressBar(header, width=140, height=8, progress_color=palette.accent,
                                           fg_color=palette.panel_alt)
        self.progress.set(0.0)
        self.progress.pack(side="left", padx=(8, 0))

        self.elapsed_label = ctk.CTkLabel(header, text="", font=(palette.mono_family, 10),
                                          text_color=palette.text_dim, width=90, anchor="w")
        self.elapsed_label.pack(side="left", padx=(8, 0))

        self.summary_label = ctk.CTkLabel(header, text="", font=(palette.font_family, 10),
                                          text_color=palette.text_muted, anchor="e")
        self.summary_label.pack(side="right", padx=(4, 4))

        def button(text_key: str, command: Callable[[], Any], width: int = 74) -> ctk.CTkButton:
            widget = ctk.CTkButton(
                header, text=icon_text(text_key), width=width, height=24, corner_radius=5,
                fg_color="transparent", hover_color=palette.hover, text_color=palette.text_dim,
                border_width=1, border_color=palette.border, command=command,
                font=(palette.font_family, 10), anchor="center",
            )
            widget.pack(side="right", padx=(4, 0))
            return widget

        self.cancel_button = button("cancel", self._cancel_clicked, 78)
        self.cancel_button.configure(state="disabled")
        self.save_button = button("log", self._save_log_clicked, 84)
        self.copy_button = button("copy", self._copy_clicked, 74)
        self.clear_button = button("clear", self.clear, 74)
        self.wrap_button = button("wrap", self.toggle_wrap, 66)
        self.scroll_button = button("pin", self.toggle_autoscroll, 86)
        self.scroll_button.configure(text="autoscroll: on")

        self.filter_switch = ctk.CTkSwitch(
            header, text="errors only", font=(palette.font_family, 10), text_color=palette.text_muted,
            progress_color=palette.warning, switch_width=30, switch_height=16, command=self._filter_toggled,
        )
        self.filter_switch.pack(side="right", padx=(6, 2))

        body = ctk.CTkFrame(self, fg_color=palette.console_bg, corner_radius=0)
        body.pack(fill="both", expand=True)
        self.text = ThemedTextView(
            body, palette, readonly=True, wrap="none", font=(palette.mono_family, self._font_size),
            max_lines=self._max_lines, padx=8, pady=6,
        )
        self.text.configure_tags(CONSOLE_TAGS)
        if palette.name == "Light":
            self._apply_light_tags()
        self.scrollbar = ctk.CTkScrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y", padx=(0, 2), pady=2)
        self.text.bind("<Double-Button-1>", self._maybe_jump, add=True)
        self.text.bind("<Button-1>", self._maybe_jump_cursor, add=True)
        self.text.bind("<Control-plus>", lambda e: self._zoom_font(1), add=True)
        # Tk spells the numpad keys KP_Add / KP_Subtract; "keypad-plus" is not a
        # keysym at all, and an unknown one makes bind() raise TclError.
        self.text.bind("<Control-KP_Add>", lambda e: self._zoom_font(1), add=True)
        self.text.bind("<Control-minus>", lambda e: self._zoom_font(-1), add=True)
        self.text.bind("<Control-KP_Subtract>", lambda e: self._zoom_font(-1), add=True)
        self.text.bind("<Control-equal>", lambda e: self._zoom_font(1), add=True)
        self._apply_jump_tag()

    def _apply_light_tags(self) -> None:
        """Tune the syntax-independent console colours for the light theme."""
        tweaks = {"cmd": "#1a7f37", "ok": "#1a7f37", "warn": "#9a6700", "error": "#cf222e",
                  "dim": "#6a737d", "time": "#8c959f", "jump": "#0969da", "step": "#0969da",
                  "head": "#8250df", "errline": "#ffebe9"}
        for name, colour in tweaks.items():
            option = "background" if name == "errline" else "foreground"
            try:
                self.text.tag_config(name, **{option: colour})
            except tk.TclError:  # pragma: no cover
                pass

    def _apply_jump_tag(self) -> None:
        try:
            self.text.tag_config("jump", underline=True)
        except tk.TclError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ writes
    def write(self, text: str, level: str = "info") -> None:
        """Queue one line of output (buffered and flushed on a timer)."""
        if text is None:
            return
        for line in str(text).split("\n"):
            if line == "" and not text.endswith("\n"):
                continue
            self._queue(line, level)
        self._schedule_flush()

    def write_block(self, text: str, level: str = "info") -> None:
        """Queue multi-line output verbatim."""
        if not text:
            return
        self._queue(text, level)
        self._schedule_flush()

    def show_command(self, argv: Sequence[str]) -> None:
        self._queue("$ " + " ".join(str(part) for part in argv), "cmd")
        self._schedule_flush()

    def _queue(self, line: str, level: str) -> None:
        tag = _LEVEL_TAG.get(str(level).lower(), "info")
        if self._filter_errors_only and tag not in {"error", "warn", "cmd"}:
            return
        stamp = ""
        if self._show_timestamps:
            now = _datetime.datetime.now()
            stamp = f"{now:%H:%M:%S}  "
        self._pending.append((f"{stamp}{line}", tag))
        self._buffer.append(f"{stamp}{line}")
        if len(self._buffer) > self._max_lines * 2:
            for _ in range(max(1, len(self._buffer) - self._max_lines)):
                self._buffer.popleft()

    def _schedule_flush(self) -> None:
        if self._flush_job is not None:
            return
        try:
            self._flush_job = self.after(50, self._flush)
        except (tk.TclError, RuntimeError):  # pragma: no cover - widget gone
            self._flush_job = None

    def _flush(self) -> None:
        self._flush_job = None
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        for line, tag in batch:
            self.text.append(line + "\n", (tag,))
            if tag == "error":
                self.text.tag_add("errline", "end-2l", "end-1l")
                self._errors += 1
        if self._autoscroll:
            self.text.scroll_to_end()
        self.summary_label.configure(
            text=f"{self.text.line_count()} lines - {self._operations} operations - {self._errors} errors"
        )

    def _apply_diag_links(self) -> None:
        """Tag diagnostics so a click jumps to the file/line."""
        try:
            total = self.text.line_count()
        except (tk.TclError, ValueError):  # pragma: no cover
            return
        for index in range(total, max(0, total - 400), -1):
            try:
                line = self.text.get(f"{index}.0", f"{index}.end")
            except tk.TclError:  # pragma: no cover
                break
            if _DIAG_LINE_RE.search(line):
                self.text.tag_add("jump", f"{index}.0", f"{index}.end")

    # -------------------------------------------------------------- operation
    def start_operation(self, label: str, detail: str = "") -> None:
        """Mark the beginning of a long operation (spinner + progress bar)."""
        self._busy = True
        self._started_at = _time.perf_counter()
        self._operations += 1
        self._progress = 0.0
        self.title_label.configure(text=f"  {label}")
        self.progress_label.configure(text=detail or "working...")
        self.progress.configure(mode="indeterminate")
        try:
            self.progress.start(14)
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        self.cancel_button.configure(state="normal")
        self.write(f"--- {label} ---", "head")
        if detail:
            self.write(detail, "dim")
        self._tick_elapsed()

    def set_progress(self, fraction: Optional[float], message: str = "") -> None:
        """Update the progress bar (``None`` keeps it indeterminate)."""
        if message:
            self.progress_label.configure(text=message[:120])
        try:
            if fraction is None:
                if self.progress.cget("mode") != "indeterminate":
                    self.progress.configure(mode="indeterminate")
                    self.progress.start(14)
            else:
                value = max(0.0, min(1.0, float(fraction)))
                if self.progress.cget("mode") != "determinate":
                    try:
                        self.progress.stop()
                    except (tk.TclError, AttributeError):
                        pass
                    self.progress.configure(mode="determinate")
                self._progress = value
                self.progress.set(value)
                self.progress_label.configure(text=f"{message[:70]}  {value * 100:3.0f}%".strip())
        except (tk.TclError, ValueError):  # pragma: no cover
            pass

    def finish_operation(self, ok: bool, summary: str = "", elapsed: Optional[float] = None) -> None:
        """Report the outcome (success/failure + elapsed time)."""
        self._busy = False
        duration = elapsed
        if duration is None and self._started_at is not None:
            duration = _time.perf_counter() - self._started_at
        self._stop_progress()
        self.cancel_button.configure(state="disabled")
        if duration is not None:
            self.elapsed_label.configure(text=human_duration(duration))
        if ok:
            self.progress_label.configure(text="done")
            self.set_progress(1.0, "done")
        else:
            self.set_progress(0.0, "failed")
        if summary:
            self.write(summary, "ok" if ok else "error")
        self._apply_diag_links()
        if self._autoscroll:
            self.text.scroll_to_end()

    def _stop_progress(self) -> None:
        try:
            self.progress.stop()
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        if self._elapsed_job is not None:
            try:
                self.after_cancel(self._elapsed_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._elapsed_job = None

    def _tick_elapsed(self) -> None:
        if not self._busy:
            return
        if self._started_at is not None:
            self.elapsed_label.configure(text=human_duration(_time.perf_counter() - self._started_at))
        try:
            self._elapsed_job = self.after(200, self._tick_elapsed)
        except (tk.TclError, RuntimeError):  # pragma: no cover
            self._elapsed_job = None

    # ------------------------------------------------------------ timers/util
    @property
    def start_time(self) -> Optional[float]:
        return self._started_at

    def set_start_time(self, value: Optional[float]) -> None:
        self._started_at = value

    def _cancel_clicked(self) -> None:
        if self._on_cancel is not None:
            self._on_cancel()
            self.write("cancel requested...", "warn")

    def _copy_clicked(self) -> None:
        content = self.text.text()
        if not content:
            self.write("(nothing to copy)", "dim")
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(content)
            self.progress_label.configure(text=f"copied {len(content.splitlines())} lines")
        except tk.TclError:  # pragma: no cover
            pass

    def _save_log_clicked(self) -> None:
        content = self.text.text()
        if not content:
            self.write("(nothing to save)", "dim")
            return
        if self._on_save_log is not None:
            self._on_save_log(content)
            return
        default_name = f"arduino-studio-{_datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            from tkinter import filedialog

            path = filedialog.asksaveasfilename(
                title="Save console log", defaultextension=".txt", initialfile=default_name,
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
        except tk.TclError:  # pragma: no cover
            path = ""
        if not path:
            return
        try:
            Path(path).write_text(content, encoding="utf-8")
            self.write(f"log saved to {path}", "ok")
        except OSError as exc:
            self.write(f"could not save log: {exc}", "error")

    def clear(self) -> None:
        """Empty the console (also resets the pending buffer)."""
        self._pending.clear()
        self._buffer.clear()
        self._errors = 0
        self.text.clear()
        self.summary_label.configure(text="")
        self.progress_label.configure(text="")
        self.elapsed_label.configure(text="")
        self.set_progress(0.0, "")

    def toggle_autoscroll(self) -> None:
        self._autoscroll = not self._autoscroll
        self.scroll_button.configure(text=f"autoscroll: {'on' if self._autoscroll else 'off'}")
        if self._autoscroll:
            self.text.scroll_to_end()

    def toggle_wrap(self) -> None:
        self._wrap = not self._wrap
        self.text.configure(wrap="word" if self._wrap else "none")
        self.wrap_button.configure(text=icon_text("wrap", label_override="wrap: on" if self._wrap else "wrap"))

    def _filter_toggled(self) -> None:
        try:
            self._filter_errors_only = bool(self.filter_switch.get())
        except (tk.TclError, ValueError):  # pragma: no cover
            self._filter_errors_only = not self._filter_errors_only
        self.write(f"filter: {'errors + warnings only' if self._filter_errors_only else 'everything'}", "dim")

    def toggle_timestamps(self) -> None:
        self._show_timestamps = not self._show_timestamps
        if self._settings is not None:
            try:
                self._settings.update(console_timestamps=self._show_timestamps)
            except Exception:  # pragma: no cover
                pass

    def _zoom_font(self, delta: int) -> str:
        self._font_size = max(7, min(24, self._font_size + int(delta)))
        self.text.set_font(self.palette.mono_family, self._font_size)
        if self._settings is not None:
            try:
                self._settings.update(console_font_size=self._font_size)
            except Exception:  # pragma: no cover
                pass
        return "break"

    # -------------------------------------------------------- error handling
    def _maybe_jump_cursor(self, event: "tk.Event") -> str:
        """Show a hand cursor over clickable diagnostics."""
        index = self.text.index(f"@{event.x},{event.y}")
        tags = self.text.tag_names(index)
        self.text.configure(cursor="hand2" if "jump" in tags else "")
        return ""

    def _maybe_jump(self, event: "tk.Event") -> str:
        index = self.text.index(f"@{event.x},{event.y}")
        if "jump" not in self.text.tag_names(index):
            return ""
        line_text = self.text.get(f"{index} linestart", f"{index} lineend")
        match = _DIAG_LINE_RE.search(line_text)
        if not match:
            return "break"
        if self._on_jump is not None:
            self._on_jump(match.group("file"), int(match.group("line")))
        return "break"

    def highlight_error_line(self, path: str, line: int) -> None:
        """Called by the app to mark a diagnostic (also used by the editor)."""
        self.write(f"jumping to {path}:{line}", "dim")

    def set_palette(self, palette: Palette) -> None:
        """Re-apply colours (theme switch)."""
        self.palette = palette
        self.configure(fg_color=palette.window_bg, border_color=palette.border)
        for child in self.winfo_children():
            try:
                if isinstance(child, ctk.CTkFrame):
                    child.configure(fg_color=palette.panel_bg if child is not self else palette.window_bg)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        try:
            self.text.configure(bg=palette.console_bg, fg=palette.console_fg,
                                selectbackground=palette.console_selection)
        except tk.TclError:  # pragma: no cover
            pass
        if palette.name == "Light":
            self._apply_light_tags()

    # -------------------------------------------------------------- shortcuts
    def focus_console(self) -> None:
        try:
            self.text.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def content(self) -> str:
        return self.text.text()

    def line_count(self) -> int:
        return self.text.line_count()

    def lines(self) -> list[str]:
        return list(self._buffer)

    def destroy(self) -> None:
        self._stop_progress()
        super().destroy()
