"""Find / replace bar for the code editor.

Attached under the text area and toggled with Ctrl+F (find) and Ctrl+H
(find + replace).  Supports match case, whole word, regular expressions,
"highlight all", next/previous, replace, replace all and a live match counter.
"""

from __future__ import annotations

import re
import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

import customtkinter as ctk

from ..core.utils import get_logger
from .theme import Palette

__all__ = ["FindOptions", "FindReplaceBar"]


@dataclass
class FindOptions:
    """Search parameters."""

    pattern: str = ""
    case_sensitive: bool = False
    whole_word: bool = False
    regex: bool = False
    in_selection: bool = False
    wrap: bool = True

    def compile(self) -> Optional[re.Pattern[str]]:
        """Build a regex for the current options (``None`` when empty/invalid)."""
        text = self.pattern
        if not text:
            return None
        body = text if self.regex else re.escape(text)
        if self.whole_word:
            body = r"\b(?:" + body + r")\b"
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            return re.compile(body, flags)
        except re.error:
            return None

    def error(self) -> str:
        """Regex syntax error message, or ``""`` when the pattern is fine."""
        if not self.regex or not self.pattern:
            return ""
        try:
            re.compile(self.pattern)
        except re.error as exc:
            return f"invalid regex: {exc}"
        return ""


class FindReplaceBar(ctk.CTkFrame):
    """Row of search widgets; all actions call back into the owning editor."""

    def __init__(
        self,
        master: Any,
        palette: Palette,
        *,
        on_find_next: Optional[Callable[[FindOptions], Any]] = None,
        on_find_prev: Optional[Callable[[FindOptions], Any]] = None,
        on_highlight_all: Optional[Callable[[FindOptions], Any]] = None,
        on_replace: Optional[Callable[[str, FindOptions], Any]] = None,
        on_replace_all: Optional[Callable[[str, FindOptions], Any]] = None,
        on_close: Optional[Callable[[], Any]] = None,
        show_replace: bool = False,
    ) -> None:
        super().__init__(master, fg_color=palette.panel_alt, corner_radius=0, height=40)
        self.palette = palette
        self._on_find_next = on_find_next
        self._on_find_prev = on_find_prev
        self._on_highlight_all = on_highlight_all
        self._on_replace = on_replace
        self._on_replace_all = on_replace_all
        self._on_close = on_close
        self._log = get_logger("find")
        self.pack_propagate(False)

        left = ctk.CTkFrame(self, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=(10, 0), pady=5)

        self.find_entry = ctk.CTkEntry(
            left, width=230, height=28, placeholder_text="Find",
            font=(palette.mono_family, max(10, palette.font_size)),
            fg_color=palette.surface, text_color=palette.text, border_color=palette.border,
        )
        self.find_entry.pack(side="left")
        self.find_entry.bind("<Return>", lambda event: self.find_next(), add=True)
        self.find_entry.bind("<KP_Enter>", lambda event: self.find_next(), add=True)
        self.find_entry.bind("<Shift-Return>", lambda event: self.find_previous(), add=True)
        self.find_entry.bind("<KeyRelease>", self._on_key_release, add=True)
        self.find_entry.bind("<Escape>", lambda event: self.hide(), add=True)

        self.replace_entry = ctk.CTkEntry(
            left, width=180, height=28, placeholder_text="Replace with",
            font=(palette.mono_family, max(10, palette.font_size)),
            fg_color=palette.surface, text_color=palette.text, border_color=palette.border,
        )
        if show_replace:
            self.replace_entry.pack(side="left", padx=(8, 0))
        self._show_replace = show_replace

        self._buttons: dict[str, ctk.CTkButton] = {}

        def add_button(name: str, text: str, command: Callable[[], Any], width: int = 62) -> ctk.CTkButton:
            button = ctk.CTkButton(
                left, text=text, width=width, height=26, corner_radius=5,
                fg_color=palette.surface, hover_color=palette.hover, text_color=palette.text,
                border_width=1, border_color=palette.border, font=(palette.font_family, 10),
                command=command,
            )
            button.pack(side="left", padx=(6, 0))
            self._buttons[name] = button
            return button

        add_button("next", "\u2193", lambda: self.find_next(), 34)
        add_button("prev", "\u2191", lambda: self.find_previous(), 34)
        add_button("replace", "replace", lambda: self._do_replace(False), 74)
        add_button("replace_all", "all", lambda: self._do_replace(True), 60)

        self._checks: dict[str, ctk.CTkCheckBox] = {}

        def add_check(name: str, text: str, default: bool = False) -> ctk.CTkCheckBox:
            check = ctk.CTkCheckBox(
                left, text=text, font=(palette.font_family, 10), text_color=palette.text_dim,
                checkbox_width=15, checkbox_height=15, corner_radius=4, border_width=1,
                border_color=palette.border, fg_color=palette.accent, hover_color=palette.hover,
                command=lambda: (self._refresh_state(), self._highlight()),
            )
            check.pack(side="left", padx=(10, 0))
            if default:
                check.select()
            self._checks[name] = check
            return check

        add_check("case", "Aa")
        add_check("word", "word")
        add_check("regex", ".*")
        add_check("selection", "in sel")

        self.count_label = ctk.CTkLabel(left, text="", font=(palette.font_family, 10),
                                       text_color=palette.text_muted, width=110, anchor="w")
        self.count_label.pack(side="left", padx=(10, 0))

        close = ctk.CTkButton(
            self, text="\u2715", width=30, height=26, corner_radius=5, fg_color="transparent",
            hover_color=palette.hover, text_color=palette.text_dim, command=self.hide,
        )
        close.pack(side="right", padx=(0, 8), pady=5)
        self.history: list[str] = []
        self.history_index = -1
        self._refresh_state()

    # ------------------------------------------------------------------ state
    def options(self) -> FindOptions:
        """Current search options."""
        return FindOptions(
            pattern=self.find_entry.get(),
            case_sensitive=bool(self._checks["case"].get()),
            whole_word=bool(self._checks["word"].get()),
            regex=bool(self._checks["regex"].get()),
            in_selection=bool(self._checks["selection"].get()),
        )

    def _on_key_release(self, event: "tk.Event") -> None:
        keysym = getattr(event, "keysym", "")
        if keysym in {"Up", "Down", "Escape", "Return", "KP_Enter"}:
            return
        self._highlight()
        self._refresh_state()

    def _refresh_state(self) -> None:
        has_text = bool(self.find_entry.get())
        error = self.options().error()
        state = "normal" if has_text and not error else "disabled"
        for key in ("next", "prev", "replace_all"):
            self._buttons[key].configure(state=state)
        self._buttons["replace"].configure(state="normal" if self._show_replace else "disabled")
        if error:
            self.count_label.configure(text=error[:34], text_color=self.palette.error)
        elif has_text:
            self.count_label.configure(text="", text_color=self.palette.text_muted)
        else:
            self.count_label.configure(text="", text_color=self.palette.text_muted)

    def set_replace_visible(self, visible: bool) -> None:
        """Show/hide the replacement entry (Ctrl+F vs Ctrl+H)."""
        self._show_replace = bool(visible)
        if visible:
            if not self.replace_entry.winfo_manager():
                self.replace_entry.pack(side="left", padx=(8, 0), before=self._buttons["next"])
        else:
            self.replace_entry.pack_forget()
        self._buttons["replace"].configure(state="normal" if visible else "disabled")
        self._buttons["replace_all"].configure(state="normal" if visible else "disabled")

    def show(self, selection: str = "", replace: bool = False) -> None:
        """Reveal the bar, pre-filling the selection when present."""
        self.pack(fill="x", side="bottom")
        if selection and len(selection) < 120 and "\n" not in selection:
            self.find_entry.delete(0, "end")
            self.find_entry.insert(0, selection)
        self.find_entry.focus_set()
        try:
            self.find_entry.selection_range(0, "end")
        except (tk.TclError, ValueError):  # pragma: no cover
            pass
        self.set_replace_visible(replace)
        self._highlight()
        self._refresh_state()

    def hide(self) -> None:
        """Hide the bar and clear highlights."""
        self.pack_forget()
        if self._on_close is not None:
            self._on_close()

    @property
    def visible(self) -> bool:
        try:
            return bool(self.winfo_manager())
        except tk.TclError:  # pragma: no cover
            return False

    # ---------------------------------------------------------------- actions
    def find_next(self) -> None:
        self._remember_history()
        if self._on_find_next is not None:
            self._on_find_next(self.options())
        self._refresh_state()

    def find_previous(self) -> None:
        self._remember_history()
        if self._on_find_prev is not None:
            self._on_find_prev(self.options())
        self._refresh_state()

    def _highlight(self) -> None:
        if self._on_highlight_all is not None:
            self._on_highlight_all(self.options())

    def _do_replace(self, all_matches: bool) -> None:
        options = self.options()
        if not options.pattern:
            return
        self._remember_history()
        replacement = self.replace_entry.get() if self._show_replace else ""
        if all_matches and self._on_replace_all is not None:
            count = self._on_replace_all(replacement, options)
            self.set_counts(total=0, current=0, extra=f"replaced {count}")
        elif self._on_replace is not None:
            self._on_replace(replacement, options)
        self._refresh_state()

    def _remember_history(self) -> None:
        text = self.find_entry.get()
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            if len(self.history) > 60:
                self.history.pop(0)
        self.history_index = len(self.history)

    def history_previous(self) -> None:
        """Up-arrow: walk back through the search history."""
        if not self.history:
            return
        self.history_index = max(0, self.history_index - 1)
        self._set_entry(self.history[self.history_index])

    def history_next(self) -> None:
        """Down-arrow: walk forward through the search history."""
        if not self.history:
            return
        self.history_index = min(len(self.history), self.history_index + 1)
        if self.history_index >= len(self.history):
            self._set_entry("")
        else:
            self._set_entry(self.history[self.history_index])

    def _set_entry(self, value: str) -> None:
        self.find_entry.delete(0, "end")
        self.find_entry.insert(0, value)
        self._highlight()
        self._refresh_state()

    # ------------------------------------------------------------------ count
    def set_counts(self, total: int = 0, current: int = 0, extra: str = "") -> None:
        """Display ``"3 of 12"`` style feedback (or an error/notice)."""
        if extra:
            self.count_label.configure(text=extra, text_color=self.palette.text_muted)
            return
        if total <= 0:
            text = "no matches" if self.find_entry.get() else ""
            self.count_label.configure(text=text, text_color=self.palette.warning if text else self.palette.text_muted)
            return
        label = f"{max(1, current)} of {total}"
        self.count_label.configure(text=label, text_color=self.palette.text_muted)

    def configure_palette(self, palette: Palette) -> None:
        """Re-skin after a theme switch."""
        self.palette = palette
        self.configure(fg_color=palette.panel_alt)
        for entry in (self.find_entry, self.replace_entry):
            entry.configure(fg_color=palette.surface, text_color=palette.text, border_color=palette.border)
        for button in self._buttons.values():
            button.configure(fg_color=palette.surface, hover_color=palette.hover,
                             text_color=palette.text, border_color=palette.border)
        for check in self._checks.values():
            check.configure(text_color=palette.text_dim, fg_color=palette.accent, hover_color=palette.hover,
                            border_color=palette.border)


def escape_for_regex(text: str) -> str:
    """Expose ``re.escape`` for tests/callers that build patterns manually."""
    return re.escape(text)
