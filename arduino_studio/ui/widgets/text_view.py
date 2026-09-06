"""A themed, scrollable read/write text area built from ``tk.Text``.

CustomTkinter's own ``CTkTextbox`` is convenient but the console, serial
monitor and terminal all need marks, tags, per-line colours and fast appends, so
they share this widget instead.  It keeps a plain ``tk.Text`` (full Tk features)
inside a ``CTkFrame`` with a ``CTkScrollbar`` and exposes the small subset of
methods the panels use.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any, Callable, Iterable, Optional, Sequence

import customtkinter as ctk

from ...core.utils import get_logger
from ..theme import Palette

__all__ = ["ThemedTextView", "TagSpec"]

try:  # Windows uses Ctrl+C, everywhere else Command+C
    from tkinter.constants import CONTROL as _MODIFIER
except ImportError:  # pragma: no cover
    _MODIFIER = "Control"


class TagSpec:
    """Descriptor for a highlight tag (colour + optional font weight)."""

    __slots__ = ("name", "foreground", "background", "font_weight", "underline", "insertunfounded", "wraplength")

    def __init__(
        self,
        name: str,
        foreground: Optional[str] = None,
        background: Optional[str] = None,
        font_weight: Optional[str] = None,
        underline: bool = False,
        insertunfounded: bool = True,
        wraplength: int = 0,
    ) -> None:
        self.name = name
        self.foreground = foreground
        self.background = background
        self.font_weight = font_weight
        self.underline = underline
        self.insertunfounded = insertunfounded
        self.wraplength = wraplength

    def options(self, mono_font: Any) -> dict[str, Any]:
        """Translate the spec into ``tk.Text.tag_configure`` options.

        *mono_font* may be a font descriptor tuple (``("Consolas", 11)``) or a
        font object with ``configure``; both are handled so bold/italic tags work
        in either case.
        """
        opts: dict[str, Any] = {}
        if self.foreground:
            opts["foreground"] = self.foreground
        if self.background:
            opts["background"] = self.background
        if self.underline:
            opts["underline"] = True
        if not self.font_weight or mono_font is None:
            return opts
        if isinstance(mono_font, (tuple, list)):
            parts = list(mono_font)
            if len(parts) >= 3:
                parts[2] = self.font_weight
            elif len(parts) == 2:
                parts.append(self.font_weight)
            else:
                parts = [parts[0] if parts else "TkFixedFont", 10, self.font_weight]
            opts["font"] = tuple(parts)
            return opts
        if isinstance(mono_font, str):
            opts["font"] = f"{mono_font} {self.font_weight}"
            return opts
        try:
            import copy

            font = copy.copy(mono_font)
            font.configure(weight=self.font_weight)
            opts["font"] = font
        except (AttributeError, tk.TclError, TypeError):  # pragma: no cover
            opts["font"] = mono_font
        return opts


class ThemedTextView(tk.Text):
    """``tk.Text`` with integrated scrollbar, toolbar hooks and smart buffering.

    Designed for high-volume output: :meth:`append_lines` batches writes and the
    widget can throttle scrolling so a serial flood does not freeze the UI.
    """

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        font: Any = None,
        readonly: bool = True,
        wrap: str = "none",
        bg: Optional[str] = None,
        fg: Optional[str] = None,
        padx: int = 8,
        pady: int = 6,
        highlightthickness: int = 1,
        undo: bool = False,
        max_lines: int = 0,
        spacing1: int = 0,
        spacing3: int = 0,
    ) -> None:
        self.palette = palette
        self._max_lines = int(max_lines)
        self._trim_scheduled = False
        super().__init__(
            master,
            wrap=wrap,
            font=font or (palette.mono_family, palette.font_size),
            bg=bg or palette.console_bg,
            fg=fg or palette.console_fg,
            insertbackground=fg or palette.console_fg,
            selectbackground=palette.console_selection,
            selectforeground=fg or palette.console_fg,
            relief="flat",
            bd=0,
            highlightthickness=highlightthickness,
            highlightbackground=palette.border,
            highlightcolor=palette.accent,
            padx=padx,
            pady=pady,
            undo=undo,
            autoseparators=bool(undo),
            maxundo=-1 if undo else 0,
            spacing1=spacing1,
            spacing3=spacing3,
            state="normal" if not readonly else "disabled",
            takefocus=1,
            tabs=None,
        )
        self.readonly = readonly
        self.bind("<Key>", self._on_key, add=True)
        self.bind("<Button-3>", self._on_right_click, add=True)
        self.bind("<Control-c>", lambda e: (self.copy_selection(), "break")[1], add=True)
        self.bind("<Control-C>", lambda e: (self.copy_selection(), "break")[1], add=True)
        self.bind("<Control-a>", lambda e: self.select_all(), add=True)
        self.bind("<MouseWheel>", self._on_wheel, add=True)
        self.bind("<Button-4>", lambda e: (self.yview_scroll(-3, "units"), "break")[1], add=True)
        self.bind("<Button-5>", lambda e: (self.yview_scroll(3, "units"), "break")[1], add=True)

    # ------------------------------------------------------------------ keys
    def _on_key(self, event: "tk.Event") -> str:
        """Read-only areas must not accept text input but keep navigation."""
        if not self.readonly:
            return ""
        allowed = {
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
            "Control_L", "Shift_L", "Control_R", "Shift_R", "Tab", "Escape", "Return",
        }
        keysym = getattr(event, "keysym", "")
        if keysym in allowed:
            return ""
        character = getattr(event, "char", "")
        if character and character.isprintable() and not (event.state & 0x4):  # Ctrl held
            return "break"
        if character in ("\r", "\n") and not (event.state & 0x4):
            return "break"
        return ""

    def _on_right_click(self, event: "tk.Event") -> None:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=self.copy_selection, state="normal" if self.tag_has("sel") else "disabled")
        menu.add_command(label="Select all", command=self.select_all)
        if not self.readonly:
            menu.add_separator()
            menu.add_command(label="Cut", command=self.cut_selection)
            menu.add_command(label="Paste", command=self.paste_clipboard)
        menu.add_separator()
        menu.add_command(label="Clear", command=self.clear)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _on_wheel(self, event: "tk.Event") -> str:
        delta = getattr(event, "delta", 0)
        if not delta:  # Linux sends Button-4/5 instead
            return ""
        if getattr(event, "state", 0) & 0x4:  # Ctrl: font zoom handled by the owner
            return "break"
        self.yview_scroll(int(-delta / 120) * 3, "units")
        return "break"

    # ------------------------------------------------------------------ tags
    def configure_tags(self, specs: Sequence[TagSpec]) -> None:
        """Install the tag set used by the panel."""
        for spec in specs:
            options = spec.options(self.cget("font"))
            try:
                self.tag_configure(spec.name, **options)
            except tk.TclError:  # pragma: no cover
                self.tag_configure(spec.name, foreground=spec.foreground or self.cget("fg"))

    # ----------------------------------------------------------------- writes
    def append(self, text: str, tags: Iterable[str] = ()) -> None:
        """Append one block of text (keeping the caret/selection stable)."""
        tag_tuple = tuple(tags)
        readonly = self.readonly
        if readonly:
            self.configure(state="normal")
        try:
            self.insert("end", text, tag_tuple)
        finally:
            if readonly:
                self.configure(state="disabled")
        self._maybe_trim()

    def append_line(self, line: str, tags: Iterable[str] = ()) -> None:
        self.append(line.rstrip("\r\n") + "\n", tags)

    def append_lines(self, lines: Sequence[str], tags: Iterable[str] = ()) -> None:
        if not lines:
            return
        text = "".join(line.rstrip("\r\n") + "\n" for line in lines)
        self.append(text, tags)

    def insert_at(self, index: str, text: str, tags: Iterable[str] = ()) -> None:
        readonly = self.readonly
        if readonly:
            self.configure(state="normal")
        try:
            self.insert(index, text, tuple(tags))
        finally:
            if readonly:
                self.configure(state="disabled")

    def replace_range(self, start: str, end: str, text: str = "") -> None:
        readonly = self.readonly
        if readonly:
            self.configure(state="normal")
        try:
            self.delete(start, end)
            if text:
                self.insert(start, text)
        finally:
            if readonly:
                self.configure(state="disabled")

    def clear(self) -> None:
        readonly = self.readonly
        if readonly:
            self.configure(state="normal")
        try:
            self.delete("1.0", "end")
            self.edit_reset()
        finally:
            if readonly:
                self.configure(state="disabled")

    def _maybe_trim(self) -> None:
        """Drop the oldest lines when ``max_lines`` is exceeded."""
        if self._max_lines <= 0:
            return
        try:
            total = int(self.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            return
        if total <= self._max_lines:
            return
        readonly = self.readonly
        if readonly:
            self.configure(state="normal")
        try:
            self.delete("1.0", f"{total - self._max_lines + 1}.0")
        finally:
            if readonly:
                self.configure(state="disabled")

    # ------------------------------------------------------------- clipboard
    def copy_selection(self) -> str:
        try:
            text = self.get("sel.first", "sel.last")
        except tk.TclError:
            return ""
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:  # pragma: no cover
            get_logger("text").debug("clipboard unavailable")
        return text

    def cut_selection(self) -> str:
        text = self.copy_selection()
        if text:
            self.replace_range("sel.first", "sel.last")
        return text

    def paste_clipboard(self) -> str:
        try:
            return self.selection_get(selection="CLIPBOARD")
        except tk.TclError:
            return ""

    def select_all(self) -> str:
        self.tag_add("sel", "1.0", "end-1c")
        return "break"

    # ------------------------------------------------------------- scrolling
    def at_bottom(self, tolerance: int = 24) -> bool:
        try:
            first, last = self.yview()
        except (ValueError, tk.TclError):  # pragma: no cover - not mapped yet
            return True
        return last >= 1.0 or (1.0 - last) * 100 < tolerance

    def scroll_to_end(self) -> None:
        try:
            self.see("end")
        except tk.TclError:  # pragma: no cover
            pass

    def line_count(self) -> int:
        try:
            return int(self.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):
            return 0

    def text(self) -> str:
        """Full content without the trailing synthetic newline."""
        try:
            return self.get("1.0", "end-1c")
        except tk.TclError:  # pragma: no cover
            return ""

    def set_font(self, family: str, size: int) -> None:
        try:
            self.configure(font=(family, max(6, int(size))))
        except (tk.TclError, ValueError):  # pragma: no cover
            pass

    def on_link(self, tag: str, callback: Callable[[str], Any]) -> None:
        """Make a tag clickable (used for error lines and file links)."""
        self.tag_bind(tag, "<Button-1>", lambda event: callback(self.get(f"event.x", "event.x wordend")) or "break")
        self.tag_bind(tag, "<Enter>", lambda event: self.configure(cursor="hand2"))
        self.tag_bind(tag, "<Leave>", lambda event: self.configure(cursor=""))


class ScrolledTextView(ctk.CTkFrame):
    """Convenience wrapper: :class:`ThemedTextView` + scrollbar + optional header."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        readonly: bool = True,
        wrap: str = "none",
        max_lines: int = 0,
        font: Any = None,
        corner_radius: int = 8,
        tags: Sequence[TagSpec] = (),
        bg: Optional[str] = None,
        fg: Optional[str] = None,
        height: int = 0,
    ) -> None:
        super().__init__(master, fg_color=bg or palette.console_bg, corner_radius=corner_radius,
                         border_width=1, border_color=palette.border)
        self.palette = palette
        self.text = ThemedTextView(
            self, palette, readonly=readonly, wrap=wrap, max_lines=max_lines, font=font,
            bg=bg or palette.console_bg, fg=fg or palette.console_fg, highlightthickness=0,
        )
        if height:
            self.text.configure(height=int(height))
        if tags:
            self.text.configure_tags(tags)
        self.scrollbar = ctk.CTkScrollbar(self, command=self.text.yview)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

    # ------------------------------------------------------------------ API
    def append(self, text: str, tags: Iterable[str] = ()) -> None:
        self.text.append(text, tags)

    def append_line(self, line: str, tags: Iterable[str] = ()) -> None:
        self.text.append_line(line, tags)

    def append_lines(self, lines: Sequence[str], tags: Iterable[str] = ()) -> None:
        self.text.append_lines(lines, tags)

    def clear(self) -> None:
        self.text.clear()

    def configure_tags(self, specs: Sequence[TagSpec]) -> None:
        self.text.configure_tags(specs)

    def tag_config(self, name: str, **options: Any) -> None:
        self.text.tag_configure(name, **options)

    def see_end(self) -> None:
        self.text.scroll_to_end()

    def at_bottom(self) -> bool:
        return self.text.at_bottom()

    def focus_text(self) -> None:
        try:
            self.text.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def set_font(self, family: str, size: int) -> None:
        self.text.set_font(family, size)

    def content(self) -> str:
        return self.text.text()

    def set_readonly(self, value: bool) -> None:
        self.text.readonly = bool(value)
        self.text.configure(state="normal" if not value else "disabled")
