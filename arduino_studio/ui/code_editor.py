"""The code editor widget.

A ``tkinter.Text`` (which gives full tag/mark/undo support) wrapped in a
CustomTkinter frame with:

* a line-number gutter canvas that also carries error/warning markers,
* incremental Arduino/C++ syntax highlighting (see :mod:`arduino_studio.ui.syntax`),
* auto indentation, bracket/quote auto-close, bracket pair matching,
* block indent/outdent with Tab / Shift+Tab, comment toggle with Ctrl+/,
* find/replace (``FindReplaceBar``), go-to-line, select-all, clipboard,
* an undo/redo stack (Tk's built-in one, with separators around grouped edits),
* clickable error markers, and
* a status strip (language, caret position, indentation, save state).
"""

from __future__ import annotations

import os
import re
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import customtkinter as ctk

from ..core.arduino_cli import Diagnostic
from ..core.utils import get_logger, natural_key
from .findbar import FindOptions, FindReplaceBar
from .syntax import (
    ArduinoKeywords,
    MAX_SCAN_CHARS,
    brace_balance,
    find_matching,
    is_open_block_comment,
    leading_whitespace,
    strip_comment_and_string_spans,
    tokenize,
)
from .theme import Palette

__all__ = ["EditorDocument", "CodeEditor", "LANGUAGE_BY_SUFFIX"]

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".ino": "Arduino Sketch",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".c": "C",
    ".h": "C Header",
    ".hpp": "C++ Header",
    ".hh": "C++ Header",
    ".json": "JSON",
    ".txt": "Plain Text",
    ".md": "Markdown",
    ".properties": "Properties",
}

#: Tags used for highlighting; registered on the Text widget at construction.
_HIGHLIGHT_TAGS: tuple[str, ...] = (
    "keyword", "type", "constant", "boolean", "arduino_api", "pin_constant", "string", "char",
    "number", "comment", "preprocessor", "function", "macro", "operator", "bracket", "define",
    "todo", "scope",
)

_MATCH_TAGS: tuple[str, ...] = (
    "current_line", "bracket_match", "bracket_error", "found", "found_current", "error_line",
    "warn_line", "marker_line", "selection_style", "diag_jump",
)

_PAIRS: dict[str, str] = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'", "<": ">"}
_CLOSERS: frozenset[str] = frozenset(")]}\"'")
_BRACKETS: str = "()[]{}"
_LINE_COMMENT = "//"


@dataclass
class EditorDocument:
    """One file on disk plus its in-editor state."""

    path: Path
    content: str = ""
    dirty: bool = False
    encoding: str = "utf-8"
    line_ending: str = "\n"
    read_only: bool = False
    error: str = ""
    mtime: float = 0.0
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()

    @property
    def language(self) -> str:
        return LANGUAGE_BY_SUFFIX.get(self.suffix, "Plain Text")

    @property
    def is_source(self) -> bool:
        return self.suffix in {".ino", ".cpp", ".c", ".h", ".hpp", ".cc", ".hh", ".cxx"}

    @property
    def is_json(self) -> bool:
        return self.suffix == ".json"

    def line_count(self) -> int:
        if not self.content:
            return 1
        return self.content.count("\n") + (0 if self.content.endswith("\n") else 1)

    def reload_from_disk(self) -> tuple[bool, str]:
        """Read the file; ``(changed, message)``."""
        try:
            stat = self.path.stat()
            raw = self.path.read_text(encoding=self.encoding, errors="replace")
        except OSError as exc:
            return False, f"Cannot read {self.path.name}: {exc}"
        ending = "\r\n" if "\r\n" in raw else "\n"
        text = raw.replace("\r\n", "\n")
        self.content = text
        self.line_ending = ending
        self.dirty = False
        self.mtime = stat.st_mtime
        self.error = ""
        return True, ""

    def save(self, content: str) -> tuple[bool, str]:
        """Write *content* atomically, restoring CRLF on Windows."""
        try:
            target = self.path
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = content.replace("\n", self.line_ending) if self.line_ending == "\r\n" else content
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(payload, encoding=self.encoding, newline="")
            os.replace(tmp, target)
        except OSError as exc:
            return False, f"Cannot save {self.path.name}: {exc}"
        self.content = content
        self.dirty = False
        try:
            self.mtime = self.path.stat().st_mtime
        except OSError:  # pragma: no cover
            pass
        return True, ""


class CodeEditor(ctk.CTkFrame):
    """A single file's editor (one per open tab)."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        document: EditorDocument,
        settings: Any = None,
        on_dirty_changed: Optional[Callable[[bool], Any]] = None,
        on_cursor_moved: Optional[Callable[[int, int], Any]] = None,
        on_save_requested: Optional[Callable[[], Any]] = None,
        on_jump_to_error: Optional[Callable[[str, int], Any]] = None,
        on_content_changed: Optional[Callable[[str], Any]] = None,
    ) -> None:
        super().__init__(master, fg_color=palette.editor_bg, corner_radius=0)
        self.palette = palette
        self.document = document
        self._settings = settings
        self._on_dirty_changed = on_dirty_changed
        self._on_cursor_moved = on_cursor_moved
        self._on_save_requested = on_save_requested
        self._on_jump_to_error = on_jump_to_error
        self._on_content_changed = on_content_changed
        self._log = get_logger("editor")

        self._tab_size = int(getattr(settings, "tab_size", 4) or 4)
        self._insert_spaces = bool(getattr(settings, "insert_spaces", True))
        self._auto_indent = bool(getattr(settings, "auto_indent", True))
        self._auto_close_brackets = bool(getattr(settings, "auto_close_brackets", True))
        self._auto_close_quotes = bool(getattr(settings, "auto_close_quotes", True))
        self._highlight_enabled = True
        self._highlight_job: Optional[str] = None
        self._cursor_job: Optional[str] = None
        self._bracket_job: Optional[str] = None
        self._keywords = ArduinoKeywords()
        self._current_error_index = -1
        self._visible_start_line = 0
        self._visible_end_line = 0
        self._suppress_track = False
        self._completions_open = False
        self._completion_widget: Optional[tk.Toplevel] = None
        self._undo_depth = 0
        self._history_before: list[str] = []
        self._history_index = -1
        self._auto_saved = False
        self._completion_job: Optional[str] = None

        self._build_widgets()
        self._configure_tags()
        self.load_document()
        self._bind_keys()
        self.text.bind("<<Modified>>", self._on_modified, add=True)
        # ``load_document`` ran before the binding existed, so Tk still has the
        # modified flag set; clear it, otherwise the very first user edit would
        # not raise <<Modified>> and the unsaved marker would stay hidden.
        try:
            self.text.edit_modified(False)
        except tk.TclError:  # pragma: no cover - defensive
            pass
        self.text.bind("<KeyRelease>", self._on_key_release, add=True)
        self.text.bind("<ButtonRelease-1>", lambda event: self._schedule_cursor_work(), add=True)
        self.text.bind("<MouseWheel>", self._on_mouse_wheel, add=True)
        self.text.bind("<Button-4>", lambda event: self._on_scroll_event(event, -1), add=True)
        self.text.bind("<Button-5>", lambda event: self._on_scroll_event(event, 1), add=True)
        self.text.bind("<Configure>", lambda event: self._schedule_highlight(30), add=True)
        self.text.bind("<FocusIn>", lambda event: self._schedule_cursor_work(), add=True)
        self.gutter.bind("<Button-1>", self._on_gutter_click, add=True)

    # ------------------------------------------------------------------ build
    def _build_widgets(self) -> None:
        palette = self.palette
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        wrapper = tk.Frame(self, bg=palette.editor_bg, highlightthickness=0, bd=0)
        wrapper.grid(row=0, column=0, columnspan=3, sticky="nsew")
        wrapper.grid_rowconfigure(0, weight=1)
        wrapper.grid_columnconfigure(1, weight=1)

        self.gutter = tk.Canvas(
            wrapper, width=64, bg=palette.editor_gutter_bg, highlightthickness=0, bd=0,
            takefocus=0, cursor="arrow",
        )
        self.gutter.grid(row=0, column=0, sticky="ns")

        self.text = tk.Text(
            wrapper,
            wrap="none",
            undo=True,
            maxundo=-1,
            autoseparators=True,
            padx=8,
            pady=6,
            spacing1=0,
            spacing3=0,
            bd=0,
            highlightthickness=0,
            selectborderwidth=0,
            font=(palette.mono_family, max(8, self._font_size())),
            bg=palette.editor_bg,
            fg=palette.editor_fg,
            insertbackground=palette.editor_fg,
            insertwidth=2,
            selectbackground=palette.editor_selection,
            selectforeground=palette.editor_fg,
            inactiveselectbackground=palette.editor_selection,
            tabs=(f"{self._tab_size}c",),
            tabstyle="wordprocessor",
            takefocus=1,
            cursor="xterm",
            exportselection=True,
        )
        self.text.grid(row=0, column=1, sticky="nsew")

        self.vbar = ctk.CTkScrollbar(wrapper, command=self.text.yview)
        self.vbar.grid(row=0, column=2, sticky="ns", padx=(0, 1))
        self.hbar = ctk.CTkScrollbar(wrapper, orientation="horizontal", command=self.text.xview)
        self.hbar.grid(row=1, column=1, sticky="ew", pady=(0, 1))
        self.text.configure(yscrollcommand=self._on_scroll, xscrollcommand=self.hbar.set)

        status = ctk.CTkFrame(self, fg_color=palette.panel_bg, height=24, corner_radius=0)
        status.grid(row=1, column=0, columnspan=3, sticky="ew")
        status.grid_propagate(False)
        self.lang_label = ctk.CTkLabel(status, text=self.document.language, font=(palette.font_family, 9),
                                       text_color=palette.text_muted)
        self.lang_label.pack(side="left", padx=(8, 0))
        self.pos_label = ctk.CTkLabel(status, text="Ln 1, Col 1", font=(palette.mono_family, 9),
                                      text_color=palette.text_dim, width=130, anchor="w")
        self.pos_label.pack(side="left", padx=(12, 0))
        self.indent_label = ctk.CTkLabel(status, text=f"{'spaces' if self._insert_spaces else 'tab'} {self._tab_size}",
                                         font=(palette.font_family, 9), text_color=palette.text_muted, width=70, anchor="w")
        self.indent_label.pack(side="left")
        self.eol_label = ctk.CTkLabel(status, text="LF", font=(palette.font_family, 9),
                                      text_color=palette.text_muted, width=36, anchor="w")
        self.eol_label.pack(side="left")
        self.state_label = ctk.CTkLabel(status, text="", font=(palette.font_family, 9),
                                        text_color=palette.text_muted, width=110, anchor="e")
        self.state_label.pack(side="right", padx=(0, 8))
        self.diag_label = ctk.CTkLabel(status, text="", font=(palette.font_family, 9),
                                       text_color=palette.error, anchor="e")
        self.diag_label.pack(side="right", padx=(0, 10))

        self.find_bar = FindReplaceBar(
            self, palette,
            on_find_next=self.find_next,
            on_find_prev=self.find_previous,
            on_highlight_all=self.highlight_all,
            on_replace=self.replace_current,
            on_replace_all=self.replace_all,
            on_close=self._on_find_closed,
        )

    def _font_size(self) -> int:
        """Editor font size from the settings (12 by default)."""
        try:
            return max(8, int(getattr(self._settings, "editor_font_size", 12) or 12))
        except (TypeError, ValueError):  # pragma: no cover
            return 12

    # ------------------------------------------------------------------- tags
    def _configure_tags(self) -> None:
        colours = _syntax_colours(self.palette)
        for name in _HIGHLIGHT_TAGS:
            foreground = colours.get(name)
            if foreground:
                self.text.tag_configure(name, foreground=foreground)
        self.text.tag_configure("comment", foreground=colours.get("comment"), underline=False)
        self.text.tag_raise("comment")
        for name in ("keyword", "type", "function", "preprocessor", "macro"):
            self.text.tag_raise(name)
        self.text.tag_configure("current_line", background=self.palette.editor_current_line)
        self.text.tag_configure("bracket_match", background=self.palette.editor_bracket,
                               foreground=self.palette.editor_fg)
        self.text.tag_configure("bracket_error", background="#5c2b2b", foreground="#ffd7d7")
        self.text.tag_configure("found", background=self.palette.editor_find)
        self.text.tag_configure("found_current", background=self.palette.editor_find_current)
        self.text.tag_configure("error_line", background="#3a1d20")
        self.text.tag_configure("warn_line", background="#3a3218")
        self.text.tag_configure("marker_line", background="#233047")
        self.text.tag_configure("diag_jump", foreground=self.palette.info, underline=True)
        self.text.tag_raise("current_line")
        self.text.tag_raise("sel")
        self.text.tag_raise("found")
        self.text.tag_raise("found_current")
        self.text.tag_raise("bracket_match")

    # ------------------------------------------------------------- document io
    def load_document(self) -> None:
        """Fill the widget from ``self.document`` (no dirty flag)."""
        text = self.document.content
        self._suppress_track = True
        try:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", text)
            self.text.edit_reset()
            self.text.mark_set("insert", "1.0")
        finally:
            self._suppress_track = False
        self.document.dirty = False
        self._update_state_label()
        self._schedule_highlight(0)
        self._schedule_cursor_work()
        if self.document.read_only:
            self.text.configure(state="disabled")
            self.state_label.configure(text="read only")

    def content(self) -> str:
        """Full buffer text without the implicit trailing newline."""
        try:
            return self.text.get("1.0", "end-1c")
        except tk.TclError:  # pragma: no cover
            return self.document.content

    def mark_saved(self) -> bool:
        """Refresh the widget's saved state (after ``EditorDocument.save``)."""
        self._update_state_label()
        self._schedule_highlight(0)
        return True

    # ------------------------------------------------------------------ keys
    def _bind_keys(self) -> None:
        text = self.text
        for sequence, handler in (
            ("<Return>", self._on_return),
            ("<KP_Enter>", self._on_return),
            ("<Tab>", self._on_tab),
            ("<Shift-Tab>", self._on_shift_tab),
            ("<BackSpace>", self._on_backspace),
            ("<Delete>", self._on_delete),
            ("<Control-slash>", self.toggle_comment),
            ("<Control-keyslash>", self.toggle_comment),
            ("<Control-a>", self.select_all),
            ("<Control-z>", self.undo),
            ("<Control-Z>", self.undo),
            ("<Control-y>", self.redo),
            ("<Control-Shift-Z>", self.redo),
            ("<Control-s>", lambda event: self._request_save()),
            ("<Control-S>", lambda event: self._request_save()),
            ("<Control-d>", self.duplicate_line),
            ("<Control-Shift-d>", self.delete_line),
            ("<Control-Shift-K>", self.delete_line),
            ("<Control-k>", self.cut_line),
            ("<Control-g>", self._prompt_goto_line),
            ("<Control-f>", self.show_find),
            ("<Control-h>", self.show_replace),
            ("<Control-l>", self.select_current_line),
            ("<Control-Home>", lambda: (self.text.mark_set("insert", "1.0"), self._schedule_cursor_work(), "break")[2]),
            ("<Control-End>", lambda: (self.text.mark_set("insert", "end-1c"), self._schedule_cursor_work(), "break")[2]),
            ("<Control-bracketleft>", self.jump_matching_bracket),
            ("<Control-bracketright>", self.jump_matching_bracket),
            ("<Alt-Up>", lambda: self.move_line(-1)),
            ("<Alt-Down>", lambda: self.move_line(1)),
            ("<Shift-Alt-Up>", lambda: self.duplicate_line(direction=-1)),
            ("<Shift-Alt-Down>", lambda: self.duplicate_line(direction=1)),
            ("<Control-space>", self.show_completions),
            ("<Key>", self._completion_key),
            ("<Escape>", self._on_escape),
            ("<Prior>", lambda: self._page(-1)),
            ("<Next>", lambda: self._page(1)),
            ("<F2>", lambda: "break" if self._goto_next_error(1) else ""),
            ("<Shift-F2>", lambda: "break" if self._goto_next_error(-1) else ""),
            ("<Control+KP_Add>", lambda: self._zoom_font(1)),
            ("<Control-plus>", lambda: self._zoom_font(1)),
            ("<Control+KP_Subtract>", lambda: self._zoom_font(-1)),
            ("<Control-minus>", lambda: self._zoom_font(-1)),
            ("<Control-equal>", lambda: self._zoom_font(1)),
            ("<MouseWheel>", self._on_mouse_wheel),
            ("<Control-MouseWheel>", self._on_mouse_wheel),
        ):
            try:
                text.bind(sequence, handler, add=True)
            except tk.TclError:  # pragma: no cover - some sequences are platform specific
                continue
        for char in _PAIRS:
            try:
                text.bind(char, (lambda event, ch=char: self._on_open_char(ch)), add=True)
            except tk.TclError:  # pragma: no cover
                continue
        for closer in ")]}>":
            try:
                text.bind(closer, (lambda event, ch=closer: self._on_close_char(ch)), add=True)
            except tk.TclError:  # pragma: no cover
                continue
        for quote in ("\"", "'"):
            try:
                text.bind(quote, (lambda event, ch=quote: self._on_quote_char(ch)), add=True)
            except tk.TclError:  # pragma: no cover
                continue

    # ------------------------------------------------------------- edit hooks
    def _on_modified(self, event: Any = None) -> str:
        """``<<Modified>>`` handler - keeps the dirty flag and timers in sync."""
        widget: tk.Text = getattr(event, "widget", None) or self.text
        if not widget.edit_modified():
            return ""
        widget.edit_modified(False)
        if self._suppress_track:
            return ""
        was_dirty = self.document.dirty
        self.document.dirty = True
        if not was_dirty:
            self._update_state_label()
            self._notify_dirty(True)
        self._schedule_highlight(60)
        self._schedule_cursor_work()
        if self._on_content_changed is not None:
            self._on_content_changed(self.content())
        return ""

    def _notify_dirty(self, dirty: bool) -> None:
        if self._on_dirty_changed is not None:
            try:
                self._on_dirty_changed(dirty)
            except Exception:  # pragma: no cover
                self._log.exception("dirty callback failed")

    def set_dirty(self, dirty: bool) -> None:
        if self.document.dirty == dirty:
            return
        self.document.dirty = dirty
        self._update_state_label()
        self._notify_dirty(dirty)

    def _update_state_label(self) -> None:
        if self.document.read_only:
            self.state_label.configure(text="read only")
        elif self.document.dirty:
            self.state_label.configure(text="unsaved changes", text_color=self.palette.warning)
        else:
            self.state_label.configure(text="saved", text_color=self.palette.text_muted)

    # ------------------------------------------------------------ indentation
    def _indent_unit(self) -> str:
        return " " * self._tab_size if self._insert_spaces else "\t"

    def _on_return(self, event: "tk.Event") -> str:
        """Auto indentation: copy the current indent and open a level after ``{``."""
        if self.document.read_only:
            return ""
        text = self.text
        line_start = text.index("insert linestart")
        line_end = text.index("insert lineend")
        current_line = text.get(line_start, line_end)
        before = text.get(line_start, "insert")
        indent = leading_whitespace(before)
        stripped_before = before.strip()
        stripped_line = current_line.strip()

        extra = ""
        if self._auto_indent and stripped_before and (
            stripped_before.endswith("{") or stripped_before.endswith("(") or stripped_before.endswith("[")
            or stripped_before.endswith(":") and stripped_before.startswith("case")
        ):
            extra = self._indent_unit()
        # close a dangling pair on its own line: keep the closer aligned
        after = text.get("insert", line_end)
        trailing = after.strip()
        text.insert("insert", "\n" + indent + extra, "auto-newline")
        if extra and trailing and trailing[0] in ")]}":
            text.insert("insert", "\n" + indent)
            text.mark_set("insert", f"insert -{len(indent) + len(extra)}c linestart")
            text.mark_set("insert", f"{text.index('insert linestart')} +{len(indent) + len(extra)}c")
            text.see("insert")
            return "break"
        text.see("insert")
        self._schedule_highlight(0)
        return "break"

    def _on_tab(self, event: "tk.Event") -> str:
        text = self.text
        if text.tag_ranges("sel"):
            self.indent_selection()
            return "break"
        if self._accept_completion(event):
            return "break"
        if self.document.read_only:
            return "break"
        self.text.insert("insert", self._advance_to_tab_stop())
        return "break"

    def _advance_to_tab_stop(self) -> str:
        column = int(self.text.index("insert").split(".")[1])
        size = max(1, self._tab_size)
        pad = size - (column % size)
        if self._insert_spaces:
            return " " * pad
        return "\t"

    def _on_shift_tab(self, event: "tk.Event") -> str:
        if self.text.tag_ranges("sel"):
            self.outdent_selection()
            return "break"
        if self.document.read_only:
            return ""
        self.outdent_selection(single_line=True)
        return "break"

    def indent_selection(self) -> None:
        """Add one indent level to every selected (or current) line."""
        text = self.text
        unit = self._indent_unit()
        first, last = self._selection_lines()
        text.edit_separator()
        for line in range(first, last + 1):
            if not text.get(f"{line}.0", f"{line}.end").strip():
                continue
            text.insert(f"{line}.0", unit)
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", f"{first}.0", f"{last}.end")
        text.edit_separator()
        self._schedule_highlight(0)

    def outdent_selection(self, single_line: bool = False) -> None:
        """Remove one indent level from the selection (or caret line)."""
        text = self.text
        first, last = self._selection_lines()
        if single_line and first == last:
            pass
        text.edit_separator()
        for line in range(first, last + 1):
            content = text.get(f"{line}.0", f"{line}.end")
            if not content:
                continue
            removed = 0
            if content[0] == "\t":
                removed = 1
            else:
                while removed < self._tab_size and removed < len(content) and content[removed] == " ":
                    removed += 1
            if removed:
                text.delete(f"{line}.0", f"{line}.{removed}")
        text.edit_separator()
        self._schedule_highlight(0)

    def _selection_lines(self) -> tuple[int, int]:
        text = self.text
        try:
            ranges = text.tag_ranges("sel")
        except tk.TclError:  # pragma: no cover
            ranges = ()
        if ranges:
            first = int(text.index(ranges[0]).split(".")[0])
            last = int(text.index(ranges[1]).split(".")[0])
            # ignore a selection that ends exactly at the start of the next line
            if last > first and text.get(f"{last}.0", f"{last}.end") == "":
                last -= 1
            return first, last
        line = int(text.index("insert").split(".")[0])
        return line, line

    # ------------------------------------------------------- bracket handling
    def _on_open_char(self, char: str) -> str:
        text = self.text
        if self.document.read_only:
            return ""
        closing = _PAIRS.get(char)
        if not closing or not self._auto_close_brackets:
            return ""
        has_selection = bool(text.tag_ranges("sel"))
        text.edit_separator()
        if has_selection:
            start = str(text.index("sel.first"))
            selected = text.get("sel.first", "sel.last")
            text.delete("sel.first", "sel.last")
            text.insert(start, f"{char}{selected}{closing}")
            text.tag_remove("sel", "1.0", "end")
            text.tag_add("sel", f"{start} +1c", f"{start} +{1 + len(selected)}c")
            text.mark_set("insert", f"{start} +{1 + len(selected)}c")
        else:
            following = text.get("insert", "insert+1c")
            needs_space = char == "{" and self._needs_space_after_brace(following)
            insert_text = f"{char}{closing}"
            text.insert("insert", insert_text)
            if needs_space:
                text.insert("insert", " ")
                text.mark_set("insert", "insert-1c")
            else:
                text.mark_set("insert", "insert-1c")
        text.edit_separator()
        self._highlight_matching_bracket()
        self._schedule_highlight(0)
        return "break"

    @staticmethod
    def _needs_space_after_brace(following: str) -> bool:
        return bool(following) and following not in {"\n", "}", ";", " ", "\t"}

    def _on_close_char(self, char: str) -> str:
        text = self.text
        if self.document.read_only:
            return ""
        if text.get("insert", "insert+1c") == char and char in ")]}>":
            text.mark_set("insert", "insert+1c")
            self._highlight_matching_bracket()
            return "break"
        if char == ">" and text.get("insert", "insert+1c") != ">":
            return ""
        return ""

    def _on_quote_char(self, char: str) -> str:
        text = self.text
        if self.document.read_only:
            return ""
        if text.get("insert", "insert+1c") == char and self._inside_string_or_char(text.index("insert")):
            text.mark_set("insert", "insert+1c")
            return "break"
        if not (self._auto_close_quotes or self._auto_close_brackets):
            return ""
        if bool(text.tag_ranges("sel")):
            start = str(text.index("sel.first"))
            selected = text.get("sel.first", "sel.last")
            text.delete("sel.first", "sel.last")
            text.insert(start, f"{char}{selected}{char}")
            text.tag_remove("sel", "1.0", "end")
            text.tag_add("sel", f"{start} +1c", f"{start} +{1 + len(selected)}c")
            text.mark_set("insert", f"{start} +{1 + len(selected)}c")
            return "break"
        before = text.get("insert linestart", "insert")
        after = text.get("insert", "insert lineend")
        if _balanced_quote_context(before, after, char):
            text.insert("insert", f"{char}{char}")
            text.mark_set("insert", "insert-1c")
            self._schedule_highlight(0)
            return "break"
        return ""

    def _inside_string_or_char(self, index: str) -> bool:
        """True when *index* sits right before an auto-inserted closing quote."""
        tags = self.text.tag_names(index)
        return any(tag in {"string", "char"} for tag in tags)

    def _highlight_matching_bracket(self) -> None:
        """Colour the bracket at/behind the caret and its partner."""
        text = self.text
        text.tag_remove("bracket_match", "1.0", "end")
        text.tag_remove("bracket_error", "1.0", "end")
        caret = text.index("insert")
        offsets = (0, -1)
        source = text.get("1.0", "end-1c")
        masked = strip_comment_and_string_spans(source)
        for offset in offsets:
            index = _index_to_offset(text, caret, offset)
            if index is None or index < 0 or index >= len(source):
                continue
            if source[index] not in _BRACKETS:
                continue
            partner = find_matching(source, index, _BRACKETS, masked=masked)
            if partner is None:
                text.tag_add("bracket_error", f"{caret} {offset}c" if offset else caret,
                             f"{caret} +1c" if not offset else f"{caret} {offset + 1}c")
                return
            for spot in (index, partner):
                start = f"1.0+{spot}c"
                text.tag_add("bracket_match", start, f"{start} +1c")
            return
        # unbalanced pair under the caret: mark it red so the user sees it
        index = _index_to_offset(text, caret, 0)
        if index is not None and 0 <= index < len(source) and source[index] in _BRACKETS:
            if find_matching(source, index, _BRACKETS, masked=masked) is None:
                start = f"1.0+{index}c"
                text.tag_add("bracket_error", start, f"{start} +1c")

    def jump_matching_bracket(self, event: Any = None) -> str:
        """Ctrl+Shift+P style jump between a bracket and its partner."""
        text = self.text
        source = text.get("1.0", "end-1c")
        masked = strip_comment_and_string_spans(source)
        caret = text.index("insert")
        for offset in (0, -1):
            index = _index_to_offset(text, caret, offset)
            if index is None or index >= len(source) or index < 0 or source[index] not in _BRACKETS:
                continue
            partner = find_matching(source, index, _BRACKETS, masked=masked)
            if partner is not None:
                text.mark_set("insert", f"1.0+{partner}c")
                text.see("insert")
                self._highlight_matching_bracket()
                return "break"
        return ""

    # -------------------------------------------------------------- auto-close
    def _on_backspace(self, event: "tk.Event") -> str:
        text = self.text
        if self.document.read_only:
            return ""
        if text.tag_ranges("sel"):
            text.delete("sel.first", "sel.last")
            return "break"
        caret = text.index("insert")
        offset = _index_to_offset(text, caret, 0)
        source = text.get("1.0", "end-1c")
        if offset is None or offset <= 0 or offset > len(source):
            return ""
        before, after = source[offset - 1:offset], source[offset:offset + 1]
        if before in _PAIRS and _PAIRS[before] == after and not _inside_token(text, f"{caret} -1c"):
            text.delete(f"{caret} -1c", f"{caret} +1c")
            return "break"
        # remove one indentation step when the line is otherwise empty
        line = text.get(f"{caret} linestart", f"{caret} lineend")
        if line.strip() == "" and before in " \t" and self._auto_indent:
            column = int(caret.split(".")[1])
            size = max(1, self._tab_size)
            if before == "\t":
                text.delete(f"{caret} -1c", caret)
            else:
                delete_to = ((column - 1) // size) * size
                text.delete(f"{caret}.{delete_to}", caret)
            return "break"
        return ""

    def _on_delete(self, event: "tk.Event") -> str:
        text = self.text
        if self.document.read_only:
            return ""
        if text.tag_ranges("sel"):
            return ""
        caret = text.index("insert")
        source = text.get("1.0", "end-1c")
        offset = _index_to_offset(text, caret, 0)
        if offset is not None and 0 <= offset < len(source):
            char = source[offset]
            if char in ")]}>" and not _inside_token(text, caret):
                text.delete(caret, f"{caret} +1c")
                return "break"
        return ""

    def toggle_comment(self, event: Any = None) -> str:
        """Ctrl+/ - add or remove ``//`` on every line of the selection."""
        text = self.text
        if self.document.read_only:
            return ""
        first, last = self._selection_lines()
        block = text.get(f"{first}.0", f"{last}.end")
        code = strip_comment_and_string_spans(block)
        content_lines = [line for line in code.splitlines() if line.strip()]
        already_commented = bool(content_lines) and all(
            line.strip().startswith(_LINE_COMMENT)
            for line in text.get(f"{first}.0", f"{last}.end").splitlines() if line.strip()
        )
        json_like = self.document.suffix in {".json"}
        text.edit_separator()
        for line in range(first, last + 1):
            raw = text.get(f"{line}.0", f"{line}.end")
            if not raw.strip():
                continue
            if already_commented:
                index = raw.find(_LINE_COMMENT)
                if index != -1:
                    text.delete(f"{line}.{index}", f"{line}.{index + 2}")
            else:
                marker = "//" if not json_like else "/*"
                indent = leading_whitespace(raw)
                position = len(indent)
                text.insert(f"{line}.{position}", f"{marker} ")
                if json_like:
                    text.insert(f"{line}.end", " */")
        text.edit_separator()
        self._schedule_highlight(0)
        return "break"

    def duplicate_line(self, event: Any = None, direction: int = 1) -> str:
        """Ctrl+D - copy the current (or selected) lines below/above."""
        text = self.text
        if self.document.read_only:
            return ""
        first, last = self._selection_lines()
        block = text.get(f"{first}.0", f"{last}.end")
        text.edit_separator()
        target = f"{last}.end" if direction >= 0 else f"{first}.0"
        text.insert(target, "\n" + block if direction >= 0 else block + "\n")
        text.mark_set("insert", f"{last}.end" if direction >= 0 else f"{first}.0")
        text.edit_separator()
        self._schedule_highlight(0)
        return "break"

    def delete_line(self, event: Any = None) -> str:
        """Ctrl+Shift+K - remove the current/selected lines."""
        text = self.text
        if self.document.read_only:
            return ""
        first, last = self._selection_lines()
        text.edit_separator()
        if last + 1 <= int(text.index("end-1c").split(".")[0]):
            text.delete(f"{first}.0", f"{last + 1}.0")
        else:
            text.delete(f"{first}.lineend", f"{last}.end")
        text.edit_separator()
        self._schedule_highlight(0)
        return "break"

    def cut_line(self, event: Any = None) -> str:
        """Ctrl+K - cut the rest of the line (or the whole selection)."""
        text = self.text
        if self.document.read_only:
            return ""
        if text.tag_ranges("sel"):
            text.event_generate("<<CutSelected>>")
            return "break"
        cut = text.get("insert", "insert lineend")
        try:
            text.clipboard_clear()
            text.clipboard_append(cut)
        except tk.TclError:  # pragma: no cover
            pass
        text.delete("insert", "insert lineend")
        return "break"

    def move_line(self, direction: int) -> str:
        """Alt+Up / Alt+Down - swap the (selected) lines with the neighbour."""
        text = self.text
        if self.document.read_only:
            return ""
        first, last = self._selection_lines()
        try:
            total = int(text.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):  # pragma: no cover
            return "break"
        if direction < 0 and first <= 1:
            return "break"
        if direction > 0 and last >= total:
            return "break"
        if direction < 0:
            start, end = first - 1, last
        else:
            start, end = first, last + 1
        lines = text.get(f"{start}.0", f"{end}.end").split("\n")
        if direction < 0:
            moved = lines[1:] + lines[:1]          # block goes above its predecessor
            new_first, new_last = start, start + len(lines) - 2
        else:
            moved = lines[-1:] + lines[:-1]         # block goes below its successor
            new_first, new_last = start + 1, end
        text.edit_separator()
        text.delete(f"{start}.0", f"{end}.end")
        text.insert(f"{start}.0", "\n".join(moved))
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", f"{new_first}.0", f"{new_last}.end")
        text.mark_set("insert", f"{new_first}.0")
        text.see("insert")
        text.edit_separator()
        self._schedule_highlight(0)
        return "break"

    def select_all(self, event: Any = None) -> str:
        self.text.tag_add("sel", "1.0", "end-1c")
        return "break"

    def select_current_line(self, event: Any = None) -> str:
        text = self.text
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", "insert linestart", "insert lineend+1c")
        return "break"

    # -------------------------------------------------------------- completion
    def show_completions(self, event: Any = None) -> str:
        """Ctrl+Space - list identifiers from this buffer and the Arduino API."""
        if self.document.read_only:
            return ""
        prefix = self._word_before_caret()
        words = _collect_words(self.content())
        pool = sorted({w for w in words if len(w) >= 3} | set(self._keywords.keywords) | set(self._keywords.apis))
        options = [word for word in pool if word.lower().startswith(prefix.lower()) and word != prefix]
        if not options:
            self.state_label.configure(text="no completions")
            return "break"
        self._open_completion_list(options[:60], prefix)
        return "break"

    def _word_before_caret(self) -> str:
        """Identifier fragment between the previous non-word char and the caret."""
        text = self.text
        try:
            before = text.get("insert linestart", "insert")
        except tk.TclError:  # pragma: no cover
            return ""
        match = re.search(r"[\w$]*$", before)
        return match.group(0) if match else ""

    def _open_completion_list(self, options: Sequence[str], prefix: str) -> None:
        self._close_completions()
        try:
            bbox = self.text.bbox("insert") or (0, 0, 1, 1)
            x, y = self.text.winfo_rootx() + bbox[0], self.text.winfo_rooty() + bbox[1] + bbox[3]
            popup = tk.Toplevel(self)
            popup.wm_overrideredirect(True)
            popup.wm_geometry(f"+{x}+{y}")
            popup.configure(bg=self.palette.panel_alt)
            listbox = tk.Listbox(
                popup, exportselection=False, activestyle="none", relief="flat", bd=0,
                highlightthickness=1, highlightbackground=self.palette.border,
                font=(self.palette.mono_family, max(9, self._font_size())),
                bg=self.palette.panel_bg, fg=self.palette.text, selectbackground=self.palette.accent,
                selectforeground=self.palette.accent_text, width=max(18, min(40, max(len(o) for o in options) + 2)),
            )
            listbox.configure(height=min(10, len(options)))
            for option in options:
                listbox.insert("end", option)
            listbox.selection_set(0)
            listbox.pack(fill="both", expand=True)
            self._completion_widget = popup
            self._completion_list = listbox
            self._completion_prefix = prefix
            self._completions_open = True
            popup.bind("<FocusOut>", lambda event: self._close_completions(), add=True)
        except tk.TclError:  # pragma: no cover
            self._completions_open = False

    def _on_key_release(self, event: Any) -> str:
        """Post-keystroke work: dismiss or (re)open the completion popup.

        Bound to ``<KeyRelease>``.  Completion is offered only when the user
        asked for it (``Settings -> auto-complete``), only for identifier
        characters, and only for documents small enough that scanning the word
        list is cheap.  A small debounce keeps fast typing smooth.
        """
        char = getattr(event, "char", "") if event is not None else ""
        if self._completions_open:
            if char and (not char.isalnum() and char != "_"):
                self._close_completions()
            return ""
        if not bool(getattr(self._settings, "auto_complete", True)):
            return ""
        if not char or (not char.isalnum() and char != "_"):
            return ""
        if len(self.document.content) > 200_000 or not self.document.is_source:
            return ""
        if len(self._word_before_caret()) < 3:
            return ""
        job = getattr(self, "_completion_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        self._completion_job = self.after(180, self._maybe_show_completions)
        return ""

    def _maybe_show_completions(self) -> None:
        """Debounced completion trigger (only while the caret keeps its word)."""
        self._completion_job = None
        if self._completions_open or self.document.read_only:
            return
        try:
            if not self.text.compare("insert", "==", self.text.index("insert")):  # pragma: no cover
                return
        except (tk.TclError, AttributeError):  # pragma: no cover
            pass
        if len(self._word_before_caret()) >= 3:
            self.show_completions()

    def _completion_key(self, event: "tk.Event") -> str:
        if not self._completions_open:
            return ""
        keysym = getattr(event, "keysym", "")
        listbox = getattr(self, "_completion_list", None)
        if keysym in {"Down", "Up"}:
            if listbox is not None:
                current = listbox.curselection()
                index = int(current[0]) if current else -1
                index = index + (1 if keysym == "Down" else -1)
                index = max(0, min(listbox.size() - 1, index))
                listbox.selection_clear(0, "end")
                listbox.selection_set(index)
                listbox.see(index)
            return "break"
        if keysym in {"Return", "KP_Enter", "Tab"}:
            self._accept_completion(event)
            return "break"
        if keysym in {"Escape"}:
            self._close_completions()
            return "break"
        if keysym == "BackSpace":
            self._close_completions()
            return ""
        return ""

    def _accept_completion(self, event: Any = None) -> bool:
        listbox = getattr(self, "_completion_list", None)
        if not self._completions_open or listbox is None:
            return False
        selection = listbox.curselection()
        if not selection:
            self._close_completions()
            return False
        word = listbox.get(int(selection[0]))
        prefix = getattr(self, "_completion_prefix", "")
        self.text.edit_separator()
        self.text.delete(f"insert -{len(prefix)}c", "insert")
        self.text.insert("insert", word)
        self.text.edit_separator()
        self._close_completions()
        return True

    def _close_completions(self) -> None:
        self._completions_open = False
        widget = self._completion_widget
        self._completion_widget = None
        self._completion_list = None
        if widget is not None:
            try:
                widget.destroy()
            except tk.TclError:  # pragma: no cover
                pass

    def _on_escape(self, event: "tk.Event") -> str:
        if self._completions_open:
            self._close_completions()
            return "break"
        if self.find_bar.visible:
            self.find_bar.hide()
            return "break"
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_remove("found_current", "1.0", "end")
        self.text.tag_remove("marker_line", "1.0", "end")
        return ""

    # ------------------------------------------------------------- find/replace
    def show_find(self, event: Any = None) -> str:
        selection = ""
        if self.text.tag_ranges("sel"):
            selection = self.text.get("sel.first", "sel.last")
        self.find_bar.show(selection=selection, replace=False)
        return "break"

    def show_replace(self, event: Any = None) -> str:
        selection = ""
        if self.text.tag_ranges("sel"):
            selection = self.text.get("sel.first", "sel.last")
        self.find_bar.show(selection=selection, replace=True)
        return "break"

    def _on_find_closed(self) -> None:
        self.text.tag_remove("found", "1.0", "end")
        self.text.tag_remove("found_current", "1.0", "end")
        self.text.focus_set()

    def _search_bounds(self, options: FindOptions) -> tuple[str, str]:
        if options.in_selection and self.text.tag_ranges("sel"):
            return str(self.text.index("sel.first")), str(self.text.index("sel.last"))
        return "1.0", "end-1c"

    def find_next(self, options: Optional[FindOptions] = None) -> str:
        options = options or self.find_bar.options()
        regex = options.compile()
        if regex is None:
            return ""
        start, stop = self._search_bounds(options)
        text = self.text
        content = text.get(start, stop)
        base = _offset_of(text, start)
        matches = [match for match in regex.finditer(content)]
        if not matches:
            self.find_bar.set_counts(total=0)
            return "break"
        caret = _offset_of(text, text.index("insert")) - base
        index = next((i for i, match in enumerate(matches) if match.start() > caret), 0 if options.wrap else -1)
        if index < 0:
            self.find_bar.set_counts(total=len(matches), extra="no match after caret")
            return "break"
        self._apply_matches(matches, index, options, base)
        return "break"

    def find_previous(self, options: Optional[FindOptions] = None) -> str:
        options = options or self.find_bar.options()
        regex = options.compile()
        if regex is None:
            return ""
        start, stop = self._search_bounds(options)
        text = self.text
        content = text.get(start, stop)
        base = _offset_of(text, start)
        matches = list(regex.finditer(content))
        if not matches:
            self.find_bar.set_counts(total=0)
            return "break"
        caret = _offset_of(text, text.index("insert")) - base
        index = next((i for i in range(len(matches) - 1, -1, -1) if matches[i].start() < caret), len(matches) - 1)
        self._apply_matches(matches, index, options, base)
        return "break"

    def _apply_matches(self, matches: Sequence["re.Match[str]"], index: int, options: FindOptions, base: int) -> None:
        text = self.text
        text.tag_remove("found", "1.0", "end")
        text.tag_remove("found_current", "1.0", "end")
        for position, match in enumerate(matches):
            tag = "found_current" if position == index else "found"
            start_index = f"1.0+{base + match.start()}c"
            text.tag_add(tag, start_index, f"1.0+{base + match.end()}c")
        match = matches[index]
        caret_index = f"1.0+{base + match.start()}c"
        text.mark_set("insert", caret_index)
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", caret_index, f"1.0+{base + match.end()}c")
        text.see(caret_index)
        self.find_bar.set_counts(total=len(matches), current=index + 1)
        self._schedule_cursor_work()

    def highlight_all(self, options: FindOptions) -> None:
        """Live preview of every match while typing in the find bar."""
        text = self.text
        text.tag_remove("found", "1.0", "end")
        text.tag_remove("found_current", "1.0", "end")
        regex = options.compile()
        if regex is None:
            self.find_bar.set_counts(total=0)
            return
        start, stop = self._search_bounds(options)
        content = text.get(start, stop)
        base = _offset_of(text, start)
        matches = list(regex.finditer(content))
        for match in matches:
            text.tag_add("found", f"1.0+{base + match.start()}c", f"1.0+{base + match.end()}c")
        self.find_bar.set_counts(total=len(matches), current=0)

    def replace_current(self, replacement: str, options: FindOptions) -> str:
        regex = options.compile()
        if regex is None:
            return ""
        text = self.text
        if not text.tag_ranges("sel"):
            return self.find_next(options)
        start, stop = self._search_bounds(options)
        content = text.get(start, stop)
        base = _offset_of(text, start)
        caret = _offset_of(text, text.index("insert")) - base
        matches = list(regex.finditer(content))
        target = next((m for m in matches if m.start() >= caret), matches[0] if matches else None)
        if target is None:
            self.find_bar.set_counts(total=0)
            return ""
        value = _expand_replacement(replacement, target) if options.regex else replacement
        text.edit_separator()
        text.delete(f"1.0+{base + target.start()}c", f"1.0+{base + target.end()}c")
        text.insert(f"1.0+{base + target.start()}c", value)
        text.edit_separator()
        self.find_next(options)
        return ""

    def replace_all(self, replacement: str, options: FindOptions) -> int:
        """Replace every match; returns how many were replaced."""
        regex = options.compile()
        if regex is None:
            return 0
        text = self.text
        start, stop = self._search_bounds(options)
        content = text.get(start, stop)
        matches = list(regex.finditer(content))
        if not matches:
            return 0
        base = _offset_of(text, start)
        text.edit_separator()
        for match in reversed(matches):
            value = _expand_replacement(replacement, match) if options.regex else replacement
            text.delete(f"1.0+{base + match.start()}c", f"1.0+{base + match.end()}c")
            text.insert(f"1.0+{base + match.start()}c", value)
        text.edit_separator()
        self._schedule_highlight(0)
        self.find_bar.set_counts(extra=f"replaced {len(matches)}")
        return len(matches)

    # ------------------------------------------------------------- diagnostics
    def set_diagnostics(self, diagnostics: Sequence[Diagnostic]) -> None:
        """Mark error/warning lines and enable F2 navigation."""
        self.document.diagnostics = list(diagnostics)
        text = self.text
        for tag in ("error_line", "warn_line"):
            text.tag_remove(tag, "1.0", "end")
        errors = [d for d in self.document.diagnostics if d.is_error]
        warnings = [d for d in self.document.diagnostics if not d.is_error]
        for diag in errors:
            text.tag_add("error_line", f"{diag.line}.0", f"{diag.line}.end")
        for diag in warnings:
            text.tag_add("warn_line", f"{diag.line}.0", f"{diag.line}.end")
        self._current_error_index = -1
        total = len(errors) + len(warnings)
        if total:
            self.diag_label.configure(text=f"{len(errors)} error(s), {len(warnings)} warning(s) - F2 to jump")
        else:
            self.diag_label.configure(text="")
        self._schedule_highlight(0)

    def goto_diagnostic(self, index: int) -> bool:
        """Move the caret to diagnostic *index*; ``False`` when there are none."""
        if not self.document.diagnostics:
            return False
        position = max(0, min(len(self.document.diagnostics) - 1, index))
        diag = self.document.diagnostics[position]
        self._current_error_index = position
        return self.goto_line(diag.line or 1, column=max(0, diag.column - 1))

    def next_error(self, event: Any = None) -> str:
        """Jump the caret to the next diagnostic from the last build (F2)."""
        return "break" if self._goto_next_error(1) else ""

    def previous_error(self, event: Any = None) -> str:
        """Jump the caret to the previous diagnostic from the last build (Shift+F2)."""
        return "break" if self._goto_next_error(-1) else ""

    def _goto_next_error(self, step: int) -> bool:
        if not self.document.diagnostics:
            return False
        index = self._current_error_index + step
        if index >= len(self.document.diagnostics):
            index = 0
        if index < 0:
            index = len(self.document.diagnostics) - 1
        return self.goto_diagnostic(index)

    # ------------------------------------------------------------ caret / nav
    def goto_line(self, line: int, column: int = 0, select: bool = True) -> bool:
        """Scroll/caret to ``line:column`` (1-based)."""
        text = self.text
        total = int(text.index("end-1c").split(".")[0])
        if total <= 0:
            return False
        target = max(1, min(total, int(line)))
        index = f"{target}.{max(0, int(column))}c"
        try:
            text.mark_set("insert", index)
            text.see(index)
            if select:
                text.tag_remove("sel", "1.0", "end")
                text.tag_add("sel", f"{target}.0", f"{target}.end")
        except tk.TclError:  # pragma: no cover
            return False
        self._schedule_cursor_work()
        self._schedule_highlight(0)
        return True

    def _prompt_goto_line(self, event: Any = None) -> str:
        from .widgets.dialogs import ask_text

        total = int(self.text.index("end-1c").split(".")[0])
        value = ask_text(self, f"Go to line (1 - {total})", initial=str(total // 2), title="Go to Line")
        if value:
            try:
                self.goto_line(int(value))
            except ValueError:
                pass
        return "break"

    def _page(self, direction: int) -> str:
        self.text.yview_scroll(direction, "pages")
        self._schedule_highlight(0)
        return "break"

    # ------------------------------------------------------------ highlighting
    def _schedule_highlight(self, delay: int = 60) -> None:
        if not self._highlight_enabled:
            return
        if self._highlight_job is not None:
            try:
                self.after_cancel(self._highlight_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                self._highlight_job = None
        try:
            self._highlight_job = self.after(max(0, int(delay)), self._run_highlight)
        except tk.TclError:  # pragma: no cover
            self._highlight_job = None

    def _run_highlight(self) -> None:
        self._highlight_job = None
        if not self._highlight_enabled:
            return
        text = self.text
        try:
            source = text.get("1.0", "end-1c")
        except tk.TclError:  # pragma: no cover
            return
        for tag in _HIGHLIGHT_TAGS:
            text.tag_remove(tag, "1.0", "end")
        if not source:
            return
        if len(source) <= MAX_SCAN_CHARS:
            tokens = tokenize(source, self._keywords)
            for token in tokens:
                try:
                    text.tag_add(token.tag, f"1.0+{token.start}c", f"1.0+{token.end}c")
                except tk.TclError:  # pragma: no cover - document changed mid loop
                    return
            return
        # large document: only highlight the visible window
        first, last = self._visible_lines()
        start_offset = _offset_of(text, f"{first}.0")
        end_offset = _offset_of(text, f"{last}.lineend")
        window = source[start_offset:end_offset]
        open_comment = is_open_block_comment(source, start_offset)
        if open_comment:
            close = window.find("*/")
            if close == -1:
                text.tag_add("comment", f"{first}.0", f"{last}.lineend")
                return
            text.tag_add("comment", f"1.0+{start_offset}c", f"1.0+{start_offset + close + 2}c")
            window = window[close + 2:]
            start_offset += close + 2
        for token in tokenize(window, self._keywords, offset=start_offset):
            try:
                text.tag_add(token.tag, f"1.0+{token.start}c", f"1.0+{token.end}c")
            except tk.TclError:  # pragma: no cover
                return

    def set_highlight_enabled(self, enabled: bool) -> None:
        """Toggle highlighting (pasting huge files uses this to stay snappy)."""
        self._highlight_enabled = bool(enabled)
        if enabled:
            self._schedule_highlight(0)
        else:
            for tag in _HIGHLIGHT_TAGS:
                self.text.tag_remove(tag, "1.0", "end")

    def _visible_lines(self) -> tuple[int, int]:
        text = self.text
        try:
            first = int(text.index("@0,0").split(".")[0])
            height = max(1, text.winfo_height())
            last = int(text.index(f"@0,{height}").split(".")[0])
        except (tk.TclError, ValueError):  # pragma: no cover
            first, last = 1, 80
        return max(1, first - 1), last + 1

    # ------------------------------------------------------------- gutter
    def _on_scroll(self, *args: Any) -> None:
        try:
            self.vbar.set(*args)
        except (tk.TclError, ValueError):  # pragma: no cover
            pass
        self._update_gutter()
        if self._visible_lines() != (self._visible_start_line, self._visible_end_line):
            self._schedule_highlight(25)

    def _on_mouse_wheel(self, event: "tk.Event") -> str:
        if getattr(event, "state", 0) & 0x4:  # Ctrl: zoom the font
            self._zoom_font(1 if getattr(event, "delta", 0) > 0 else -1)
            return "break"
        delta = getattr(event, "delta", 0)
        if not delta:
            return ""
        self.text.yview_scroll(int(-delta / 120), "units")
        self._schedule_highlight(30)
        return "break"

    def _on_scroll_event(self, event: "tk.Event", direction: int) -> str:
        self.text.yview_scroll(direction * 3, "units")
        self._schedule_highlight(30)
        return "break"

    def _update_gutter(self) -> None:
        """Draw line numbers, the current-line marker and error dots."""
        canvas = getattr(self, "gutter", None)
        if canvas is None:
            return
        text = self.text
        try:
            if not bool(getattr(self._settings, "show_line_numbers", True)):
                canvas.delete("all")
                canvas.configure(width=16)
                return
            canvas.configure(width=self._gutter_width(), bg=self.palette.editor_gutter_bg)
            canvas.delete("all")
            first, last = self._visible_lines()
            total = int(text.index("end-1c").split(".")[0])
            self._visible_start_line, self._visible_end_line = first, min(last, total)
            caret_line = int(text.index("insert").split(".")[0])
            font_size = max(8, self._font_size())
            error_lines = {d.line for d in self.document.diagnostics if d.is_error}
            warn_lines = {d.line for d in self.document.diagnostics if not d.is_error}
            for line in range(first, last + 1):
                box = _line_box(text, f"{line}.0")
                if box is None:
                    continue
                _x, y, _width, height = box
                if y + height < 0 or y > canvas.winfo_height():
                    continue
                active = line == caret_line
                canvas.create_text(
                    self._gutter_width() - 10, y + height // 2, text=str(line), anchor="e",
                    fill=self.palette.editor_gutter_active if active else self.palette.editor_gutter_fg,
                    font=(self.palette.mono_family, max(8, font_size - 1)),
                )
                if line in error_lines:
                    canvas.create_oval(3, y + height // 2 - 3, 9, y + height // 2 + 3,
                                       fill=self.palette.error, outline=self.palette.error)
                elif line in warn_lines:
                    canvas.create_oval(3, y + height // 2 - 3, 9, y + height // 2 + 3,
                                       fill=self.palette.warning, outline=self.palette.warning)
        except tk.TclError:  # pragma: no cover - widget destroyed mid-update
            return

    def _gutter_width(self) -> int:
        try:
            total = int(self.text.index("end-1c").split(".")[0])
        except (tk.TclError, ValueError):  # pragma: no cover
            total = 1
        digits = max(2, len(str(total)))
        return int(18 + digits * max(6, self._font_size()) * 0.62)

    def _on_gutter_click(self, event: "tk.Event") -> str:
        """Click in the line-number gutter: select the whole line."""
        try:
            y_in_text = event.y_root - self.text.winfo_rooty()
            index = self.text.index(f"@0,{y_in_text}")
        except (tk.TclError, ValueError):  # pragma: no cover
            return ""
        line = int(index.split(".")[0])
        self.text.tag_remove("sel", "1.0", "end")
        self.text.tag_add("sel", f"{line}.0", f"{line}.end")
        self.text.mark_set("insert", f"{line}.0")
        self._schedule_cursor_work()
        return "break"

    # -------------------------------------------------------------- caret info
    def _schedule_cursor_work(self, delay: int = 0) -> None:
        if self._cursor_job is not None:
            try:
                self.after_cancel(self._cursor_job)
            except (tk.TclError, ValueError):  # pragma: no cover
                self._cursor_job = None
        try:
            self._cursor_job = self.after(max(0, delay), self._cursor_work)
        except tk.TclError:  # pragma: no cover
            self._cursor_job = None

    def _cursor_work(self) -> None:
        self._cursor_job = None
        text = self.text
        try:
            line, column = text.index("insert").split(".")
        except (tk.TclError, ValueError):  # pragma: no cover
            return
        line_number = int(line)
        column_number = int(column)
        try:
            lines = text.index("end-1c").split(".")[0]
        except tk.TclError:  # pragma: no cover
            lines = "1"
        text.tag_remove("current_line", "1.0", "end")
        if bool(getattr(self._settings, "highlight_current_line", True)):
            text.tag_add("current_line", f"{line_number}.0", f"{line_number}.end")
        sel = ""
        try:
            if text.tag_ranges("sel"):
                sel = text.get("sel.first", "sel.last")
        except tk.TclError:  # pragma: no cover
            sel = ""
        parts = [f"Ln {line_number}, Col {column_number + 1}"]
        if sel:
            parts.append(f"sel {len(sel)} ch")
        self.pos_label.configure(text="  -  ".join(parts))
        self._update_gutter()
        self._highlight_matching_bracket()
        if self._on_cursor_moved is not None:
            self._on_cursor_moved(line_number, column_number)

    # ------------------------------------------------------------------ theme
    def apply_settings(self) -> None:
        """Re-read editor options from the settings object."""
        settings = self._settings
        if settings is None:
            return
        self._tab_size = int(getattr(settings, "tab_size", self._tab_size) or 4)
        self._insert_spaces = bool(getattr(settings, "insert_spaces", self._insert_spaces))
        self._auto_indent = bool(getattr(settings, "auto_indent", self._auto_indent))
        self._auto_close_brackets = bool(getattr(settings, "auto_close_brackets", self._auto_close_brackets))
        self._auto_close_quotes = bool(getattr(settings, "auto_close_quotes", self._auto_close_quotes))
        self.text.configure(
            font=(getattr(settings, "editor_font_family", self.palette.mono_family),
                  max(8, int(getattr(settings, "editor_font_size", 12) or 12))),
            tabs=(f"{self._tab_size}c",),
            wrap="word" if getattr(settings, "word_wrap", False) else "none",
        )
        self.indent_label.configure(text=f"{'spaces' if self._insert_spaces else 'tab'} {self._tab_size}")
        self.palette = _merge_palette(palette=self.palette, settings=settings)
        self.refresh_palette(self.palette)
        self._schedule_highlight(0)

    def refresh_palette(self, palette: Palette) -> None:
        """Re-apply colours (dark/light switch or accent change)."""
        self.palette = palette
        self.configure(fg_color=palette.editor_bg)
        for child in self.winfo_children():
            try:
                if isinstance(child, (ctk.CTkFrame,)):
                    child.configure(fg_color=palette.panel_bg)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        try:
            self.text.configure(bg=palette.editor_bg, fg=palette.editor_fg, insertbackground=palette.editor_fg,
                                selectbackground=palette.editor_selection,
                                inactiveselectbackground=palette.editor_selection)
            self.gutter.configure(bg=palette.editor_gutter_bg)
            self.find_bar.configure_palette(palette)
        except tk.TclError:  # pragma: no cover
            pass
        self._configure_tags()
        self._update_gutter()

    def _zoom_font(self, delta: int) -> str:
        try:
            current = int(self.text.cget("font").split()[-1])
        except (ValueError, AttributeError, IndexError):  # pragma: no cover
            current = 12
        size = max(7, min(30, current + int(delta)))
        family = self.palette.mono_family
        try:
            family = " ".join(self.text.cget("font").split()[:-1]) or family
        except (ValueError, AttributeError):  # pragma: no cover
            pass
        self.text.configure(font=(family, size))
        if self._settings is not None:
            try:
                self._settings.update(editor_font_size=size)
            except Exception:  # pragma: no cover
                pass
        self._update_gutter()
        return "break"

    # --------------------------------------------------------------- helpers
    def insert_text(self, text: str, move_caret: bool = True) -> None:
        """Insert at the caret (grouped as one undo step)."""
        widget = self.text
        widget.edit_separator()
        widget.insert("insert", text)
        if move_caret:
            widget.mark_set("insert", f"insert +{len(text)}c")
        widget.see("insert")
        widget.edit_separator()
        self._schedule_highlight(0)

    def replace_all_text(self, text: str, keep_caret: bool = True) -> None:
        """Replace the entire buffer (used for external file reloads)."""
        widget = self.text
        caret = widget.index("insert")
        self._suppress_track = True
        try:
            widget.edit_separator()
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            if keep_caret:
                widget.mark_set("insert", caret)
                widget.see(caret)
        finally:
            self._suppress_track = False
        widget.edit_separator()
        self._schedule_highlight(0)

    def undo(self) -> str:
        """Ctrl+Z - one undo step (Tk maintains the stack)."""
        try:
            self.text.edit_separator()
            self.text.edit_undo()
        except tk.TclError:
            self._status("nothing left to undo")
        self._schedule_highlight(0)
        return "break"

    def redo(self) -> str:
        """Ctrl+Y / Ctrl+Shift+Z - one redo step."""
        try:
            self.text.edit_separator()
            self.text.edit_redo()
        except tk.TclError:
            self._status("nothing left to redo")
        self._schedule_highlight(0)
        return "break"

    def _status(self, message: str) -> None:
        if self._on_content_changed is None and self._settings is None:
            return
        try:
            self.state_label.configure(text=message[:48])
        except tk.TclError:  # pragma: no cover
            pass

    def focus_editor(self) -> None:
        try:
            self.text.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    def destroy(self) -> None:
        self._close_completions()
        super().destroy()


# --------------------------------------------------------------------------------- helpers
def _syntax_colours(palette: Palette) -> dict[str, str]:
    from .theme import SYNTAX_DARK, SYNTAX_LIGHT

    source = SYNTAX_LIGHT if palette.name == "Light" else SYNTAX_DARK
    return dict(source)


def _merge_palette(palette: Palette, settings: Any) -> Palette:
    """Re-derive the palette so font size/changes from settings take effect."""
    from .theme import palette_for

    appearance = getattr(settings, "appearance_mode", palette.name)
    accent = getattr(settings, "accent_color", None)
    return palette_for(appearance, accent)


def _line_box(text: tk.Text, index: str) -> Optional[tuple[int, int, int, int]]:
    """``(x, y, width, height)`` of *index*'s line.

    ``dlineinfo`` returns a dict on Tk >= 8.6.11 and a 4-element list on older
    Tk, so both shapes are normalised here.  ``None`` means "not visible".
    """
    try:
        info = text.dlineinfo(index)
    except (tk.TclError, ValueError, KeyError):  # pragma: no cover
        return None
    if info is None:
        return None
    if isinstance(info, dict):
        try:
            return (int(info.get("x", 0)), int(info.get("y", 0)),
                    int(info.get("width", 0)), int(info.get("height", 0)))
        except (TypeError, ValueError):  # pragma: no cover
            return None
    values = tuple(info)
    if len(values) >= 4:
        try:
            return (int(values[0]), int(values[1]), int(values[2]), int(values[3]))
        except (TypeError, ValueError):  # pragma: no cover
            return None
    return None


def _index_to_offset(text: tk.Text, index: str, char_offset: int = 0) -> Optional[int]:
    """Absolute character offset of ``index`` (+ *char_offset* chars)."""
    try:
        target = f"{index} {char_offset:+d}c" if char_offset else index
        return int(text.count("1.0", target, "chars")[0])
    except (tk.TclError, ValueError, TypeError):  # pragma: no cover
        return None


def _offset_of(text: tk.Text, index: str) -> int:
    try:
        return int(text.count("1.0", index, "chars")[0])
    except (tk.TclError, ValueError, TypeError):  # pragma: no cover
        return 0


def _inside_token(text: tk.Text, index: str) -> bool:
    """True when *index* is inside a string/char/comment tag."""
    try:
        tags = text.tag_names(index)
    except tk.TclError:  # pragma: no cover
        return False
    return any(tag in {"string", "char", "comment", "preprocessor"} for tag in tags)


def _balanced_quote_context(before: str, after: str, quote: str) -> bool:
    """Decide whether typing a quote should insert a closing partner.

    Rules: balanced quotes on the line, caret not inside a word, and either an
    empty line or a non-quote character directly after the caret.
    """
    if before.endswith("\\"):
        return False
    if before and (before[-1].isalnum() or before[-1] in "_.$>"):
        return False
    if quote == "'":
        tail = after[:1]
        head = before[-1:]
        if tail.isalpha() or head.isalpha():
            return False   # likely a prime / apostrophe in text
    if before.count(quote) % 2:
        return False
    if after[:1] == quote and after.count(quote) % 2 == 0:
        return False
    if after.count(quote) % 2:
        return False
    if "//" in before or "/*" in before.split("*/")[-1]:
        return False
    return True


def _expand_replacement(replacement: str, match: "re.Match[str]") -> str:
    """Expand ``\\1``/``$1`` groups in a replacement string."""
    try:
        return match.expand(replacement)
    except (re.error, IndexError):
        return replacement


_WORD_RE = re.compile(r"\b[A-Za-z_]\w{2,}\b")


def _collect_words(source: str, limit: int = 4000) -> set[str]:
    """Identifier candidates for auto-completion (buffer words)."""
    words: set[str] = set()
    for match in _WORD_RE.finditer(source, 0, MAX_SCAN_CHARS):
        words.add(match.group(0))
        if len(words) >= limit:
            break
    return words


def sort_file_names(names: Iterable[str]) -> list[str]:
    """Natural ordering helper for file lists (used by dialogs/tests)."""
    return sorted(names, key=natural_key)
