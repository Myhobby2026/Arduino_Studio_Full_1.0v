"""Modal dialogs: message boxes, confirmations, text/choice prompts, plan review.

All dialogs are ``CTkToplevel`` windows centred on their parent, with keyboard
shortcuts (Enter = primary action, Escape = cancel) and a ``result`` attribute.
They are used sparingly - most actions in the app update inline instead.
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

import customtkinter as ctk

from ...core.utils import get_logger
from ..theme import Palette, apply_tk_theme, palette_for

__all__ = [
    "StudioDialog",
    "MessageDialog",
    "InputDialog",
    "ChoiceDialog",
    "PlanDialog",
    "ask_message",
    "ask_yes_no",
    "ask_text",
    "ask_choice",
    "ask_plan",
    "DIALOG_KINDS",
]

DIALOG_KINDS = {
    "info": ("\u2139", "Info"),
    "question": ("?", "Question"),
    "warning": ("\u26a0", "Warning"),
    "error": ("\u2715", "Error"),
    "success": ("\u2713", "Done"),
}


def _toplevel(parent: Any) -> Any:
    """Return the real Tk toplevel that owns *parent*."""
    try:
        return parent.winfo_toplevel()
    except (AttributeError, tk.TclError):  # pragma: no cover
        return parent


class StudioDialog(ctk.CTkToplevel):
    """Base modal dialog: title bar, header label, body area, button row."""

    def __init__(
        self,
        parent: Any,
        *,
        title: str = "Arduino Studio",
        width: int = 520,
        height: int = 320,
        palette: Optional[Palette] = None,
        resizable: bool = False,
        modal: bool = True,
    ) -> None:
        self._parent = parent
        self.palette = palette or palette_for(getattr(parent, "_appearance_mode", "Dark") or "Dark")
        try:
            super().__init__(master=parent)
        except (tk.TclError, RuntimeError):  # pragma: no cover - parent already gone
            raise
        self.title(title)
        self.result: Any = None
        self._closed = False
        self._modal = modal
        self._pending_grab = False
        try:
            self.resizable(resizable, resizable)
        except tk.TclError:  # pragma: no cover
            pass
        self.geometry_center(width, height)
        self.configure(fg_color=self.palette.window_bg)
        apply_tk_theme(self, self.palette)
        try:
            self.transient(_toplevel(parent))
        except tk.TclError:  # pragma: no cover
            pass
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda event: self._on_cancel(), add=True)
        self.bind("<Return>", self._on_return, add=True)
        self.bind("<Alt-F4>", lambda event: self._on_close(), add=True)
        self.bind("<FocusIn>", self._maybe_grab, add=True)
        self.after(1, self._finalize_open)

    # ------------------------------------------------------------------ shell
    def _finalize_open(self) -> None:
        try:
            self.lift()
            self.focus_force()
        except tk.TclError:  # pragma: no cover
            return
        self._maybe_grab()

    def _maybe_grab(self, event: Any = None) -> None:
        if not self._modal or self._closed:
            return
        try:
            if self.winfo_id() and not self._pending_grab:
                self._pending_grab = True
                self.after(10, self._grab)
        except (tk.TclError, RuntimeError):  # pragma: no cover
            pass

    def _grab(self) -> None:
        self._pending_grab = False
        if self._closed:
            return
        try:
            self.grab_set()
        except (tk.TclError, RuntimeError):  # pragma: no cover
            pass

    def geometry_center(self, width: int, height: int) -> None:
        """Place the dialog in the middle of the parent window (or screen)."""
        try:
            parent = _toplevel(self._parent)
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            if pw < 50 or ph < 50:
                pw, ph = parent.winfo_screenwidth(), parent.winfo_screenheight()
                px = py = 0
            x = max(0, px + (pw - width) // 2)
            y = max(0, py + (ph - height) // 3)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except (tk.TclError, ValueError):  # pragma: no cover
            self.geometry(f"{width}x{height}")

    # ------------------------------------------------------------- overrides
    def _on_return(self, event: Any) -> str:
        return ""

    def _on_cancel(self) -> None:
        self.result = None
        self.close()

    def _on_close(self) -> None:
        self.close()

    def close(self, result: Any = None) -> None:
        """Hide and destroy the dialog, optionally setting a result."""
        if self._closed:
            return
        self._closed = True
        if result is not None:
            self.result = result
        try:
            self.grab_release()
        except (tk.TclError, RuntimeError):  # pragma: no cover
            pass
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def run(self) -> Any:
        """Block (with ``wait_window``) until the dialog closes; return result."""
        try:
            self.wait_window(self)
        except (tk.TclError, RuntimeError):  # pragma: no cover - app closing
            return None
        return self.result


class MessageDialog(StudioDialog):
    """Icon + message + a configurable button row."""

    def __init__(
        self,
        parent: Any,
        *,
        kind: str = "info",
        title: str = "",
        message: str = "",
        detail: str = "",
        buttons: Sequence[str] = ("OK",),
        default_button: str = "",
        palette: Optional[Palette] = None,
        width: int = 520,
        show_copy: bool = False,
        modal: bool = True,
    ) -> None:
        self.kind = kind if kind in DIALOG_KINDS else "info"
        self.message = message or ""
        self.detail = detail or ""
        self.buttons = tuple(buttons) or ("OK",)
        self.default_button = default_button or self.buttons[0]
        self._show_copy = show_copy
        height = 190 + (0 if not self.detail else min(220, 18 + 13 * (self.detail.count("\n") + 1)))
        super().__init__(parent, title=title or DIALOG_KINDS[self.kind][1], width=width, height=height,
                         palette=palette, modal=modal)
        self._build()

    def _build(self) -> None:
        palette = self.palette
        symbol, label = DIALOG_KINDS[self.kind]
        accent = {
            "info": palette.info,
            "question": palette.info,
            "warning": palette.warning,
            "error": palette.error,
            "success": palette.success,
        }[self.kind]

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(16, 8))

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill="x")
        ctk.CTkLabel(
            header, text=f"{symbol}  {label}", font=(palette.font_family, 15, "bold"),
            text_color=accent, anchor="w", justify="left",
        ).pack(side="left")

        text_frame = ctk.CTkFrame(body, fg_color="transparent")
        text_frame.pack(fill="both", expand=True, pady=(8, 0))
        ctk.CTkLabel(
            text_frame, text=self.message, font=(palette.font_family, 12), text_color=palette.text,
            anchor="w", justify="left", wraplength=self._wrap_length(),
        ).pack(anchor="w", fill="x")
        if self.detail:
            detail_box = ctk.CTkTextbox(
                text_frame, font=(palette.mono_family, max(9, palette.font_size)),
                fg_color=palette.console_bg, text_color=palette.console_fg,
                corner_radius=8, wrap="word", activate_scrollbars=True,
            )
            lines = min(12, self.detail.count("\n") + 1)
            detail_box.configure(height=18 + lines * 15)
            detail_box.pack(fill="x", pady=(8, 0))
            detail_box.insert("1.0", self.detail)
            detail_box.configure(state="disabled")
            if self._show_copy:
                ctk.CTkButton(
                    text_frame, text="Copy message", width=110, height=24,
                    fg_color=palette.panel_alt, hover_color=palette.hover, text_color=palette.text_dim,
                    command=self._copy_detail,
                ).pack(anchor="w", pady=(6, 0))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", side="bottom", padx=18, pady=(6, 14))
        made: dict[str, ctk.CTkButton] = {}
        for name in reversed(self.buttons):
            is_primary = name == self.default_button
            button = ctk.CTkButton(
                row, text=name, width=104, height=30,
                corner_radius=6,
                fg_color=palette.accent if is_primary else palette.panel_alt,
                hover_color=palette.hover if not is_primary else _hover(palette.accent),
                text_color=palette.accent_text if is_primary else palette.text,
                border_width=0 if is_primary else 1,
                border_color=palette.border,
                command=lambda value=name: self.close(value),
            )
            button.pack(side="right", padx=(6, 0))
            made[name] = button
        if made:
            primary = made.get(self.default_button)
            if primary is not None:
                self.after(40, primary.focus_set)

    def _wrap_length(self) -> int:
        return max(320, int(self._current_width * 0.8)) if hasattr(self, "_current_width") else 420

    @property
    def _current_width(self) -> int:
        try:
            return int(self.cget("width"))
        except (tk.TclError, ValueError):  # pragma: no cover
            return 520

    def _copy_detail(self) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(self.detail or self.message)
        except tk.TclError:  # pragma: no cover
            pass

    def _on_return(self, event: Any) -> str:
        if isinstance(event.widget, ctk.CTkTextbox):
            return ""
        self.close(self.default_button)
        return "break"


class InputDialog(StudioDialog):
    """Single line text prompt with optional validation."""

    def __init__(
        self,
        parent: Any,
        *,
        title: str = "Input",
        label: str = "",
        initial: str = "",
        palette: Optional[Palette] = None,
        width: int = 470,
        validator: Optional[Callable[[str], tuple[bool, str]]] = None,
        confirm_label: str = "OK",
        hint: str = "",
    ) -> None:
        super().__init__(parent, title=title, width=width, height=180 if not label else 200, palette=palette)
        self._validator = validator
        palette = self.palette
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=(14, 6))
        if label:
            ctk.CTkLabel(body, text=label, font=(palette.font_family, 12), text_color=palette.text,
                         anchor="w").pack(anchor="w")
        self.entry = ctk.CTkEntry(
            body, font=(palette.mono_family, max(10, palette.font_size + 1)), height=34,
            fg_color=palette.surface, text_color=palette.text, border_color=palette.border,
            placeholder_text=hint,
        )
        self.entry.pack(fill="x", pady=(6, 0))
        self.entry.insert(0, initial)
        self.error_label = ctk.CTkLabel(body, text="", font=(palette.font_family, 10),
                                        text_color=palette.error, anchor="w", justify="left", wraplength=width - 60)
        self.error_label.pack(anchor="w", fill="x", pady=(4, 0))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", side="bottom", padx=18, pady=(4, 14))
        ctk.CTkButton(row, text="Cancel", width=94, height=30, fg_color=palette.panel_alt,
                      hover_color=palette.hover, text_color=palette.text,
                      command=self._on_cancel).pack(side="right", padx=(6, 0))
        self._ok = ctk.CTkButton(row, text=confirm_label, width=110, height=30, fg_color=palette.accent,
                                 hover_color=_hover(palette.accent), text_color=palette.accent_text,
                                 command=self._accept)
        self._ok.pack(side="right")
        self.entry.bind("<Return>", lambda event: self._accept(), add=True)
        self.entry.bind("<KP_Enter>", lambda event: self._accept(), add=True)
        self.after(40, lambda: (self.entry.focus_set(), self.entry.selection_range(0, "end")))

    def _accept(self) -> None:
        value = self.entry.get().strip()
        if self._validator is not None:
            try:
                ok, message = self._validator(value)
            except Exception as exc:  # pragma: no cover - validator must not break the dialog
                ok, message = False, f"Validation failed: {exc}"
            if not ok:
                self.error_label.configure(text=message or "Invalid value.")
                self.entry.focus_set()
                return
        self.close(value)

    def _on_return(self, event: Any) -> str:
        return "break"


class ChoiceDialog(StudioDialog):
    """Searchable single-selection list (boards, programmers, libraries...)."""

    def __init__(
        self,
        parent: Any,
        *,
        title: str = "Choose",
        label: str = "",
        options: Sequence[Any],
        display: Optional[Callable[[Any], str]] = None,
        palette: Optional[Palette] = None,
        width: int = 620,
        height: int = 460,
        confirm_label: str = "Select",
        empty_message: str = "Nothing to choose from.",
    ) -> None:
        super().__init__(parent, title=title, width=width, height=height, palette=palette, resizable=True)
        self.palette = palette or self.palette
        self._options: list[Any] = list(options)
        self._display = display or (lambda item: str(item))
        self._filter = ""
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(12, 4))
        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill="x")
        if label:
            ctk.CTkLabel(header, text=label, font=(self.palette.font_family, 12),
                         text_color=self.palette.text, anchor="w").pack(anchor="w")
        self.search = ctk.CTkEntry(header, placeholder_text="Type to filter...", height=30,
                                   fg_color=self.palette.surface, text_color=self.palette.text,
                                   border_color=self.palette.border)
        self.search.pack(fill="x", pady=(6, 6))
        self.search.bind("<KeyRelease>", lambda event: self._refresh(), add=True)
        self.listbox = tk.Listbox(
            body, selectmode="single", activestyle="dotbox", relief="flat", bd=0,
            highlightthickness=1, highlightbackground=self.palette.border,
            highlightcolor=self.palette.accent, font=(self.palette.mono_family, self.palette.font_size),
            bg=self.palette.panel_bg, fg=self.palette.text, selectbackground=self.palette.accent,
            selectforeground=self.palette.accent_text, exportselection=False,
        )
        scrollbar = ctk.CTkScrollbar(body, command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", padx=(6, 0))
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", lambda event: self._accept(), add=True)
        self.listbox.bind("<Return>", lambda event: self._accept(), add=True)
        self.info_label = ctk.CTkLabel(body, text=empty_message, font=(self.palette.font_family, 10),
                                       text_color=self.palette.text_muted, anchor="w")
        self.info_label.pack(anchor="w", fill="x", pady=(4, 0))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", side="bottom", padx=14, pady=(4, 12))
        ctk.CTkButton(row, text="Cancel", width=94, height=30, fg_color=self.palette.panel_alt,
                      hover_color=self.palette.hover, text_color=self.palette.text,
                      command=self._on_cancel).pack(side="right", padx=(6, 0))
        ctk.CTkButton(row, text=confirm_label, width=110, height=30, fg_color=self.palette.accent,
                      hover_color=_hover(self.palette.accent), text_color=self.palette.accent_text,
                      command=self._accept).pack(side="right")
        self._visible: list[Any] = []
        self._refresh()
        self.after(40, self.search.focus_set)

    def _refresh(self) -> None:
        self._filter = (self.search.get() if hasattr(self, "search") else "").strip().lower()
        self.listbox.delete(0, "end")
        self._visible = []
        for option in self._options:
            text = self._display(option)
            if self._filter and self._filter not in text.lower():
                continue
            self._visible.append(option)
            self.listbox.insert("end", text)
        if self._visible:
            self.listbox.selection_set(0)
            self.listbox.see(0)
        self.info_label.configure(
            text=f"{len(self._visible)} of {len(self._options)} items" + (f" matching '{self._filter}'" if self._filter else "")
        )

    def _accept(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            self.info_label.configure(text="Select an item first (or type to filter).")
            return
        index = int(selection[0])
        if 0 <= index < len(self._visible):
            self.close(self._visible[index])

    def _on_return(self, event: Any) -> str:
        return ""


@dataclass
class PlanSection:
    """One labelled block inside :class:`PlanDialog`."""

    title: str
    content: str
    tone: str = "info"          # info | warn | error | ok | mono


class PlanDialog(StudioDialog):
    """Confirmation dialog for destructive operations (bootloader, fuses, flash).

    Shows the full command, the resulting settings and the safety warnings, and
    requires the user to tick a checkbox before the primary button unlocks.
    """

    def __init__(
        self,
        parent: Any,
        *,
        title: str = "Confirm",
        heading: str = "",
        sections: Sequence[PlanSection] = (),
        confirm_text: str = "I understand the risks and want to continue",
        accept_label: str = "Run",
        palette: Optional[Palette] = None,
        width: int = 720,
        height: int = 540,
        require_ack: bool = True,
    ) -> None:
        super().__init__(parent, title=title, width=width, height=height, palette=palette, resizable=True)
        palette = self.palette
        self._sections = list(sections)
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(14, 6))
        if heading:
            ctk.CTkLabel(body, text=heading, font=(palette.font_family, 13, "bold"),
                         text_color=palette.warning, anchor="w", justify="left",
                         wraplength=width - 60).pack(anchor="w", fill="x", pady=(0, 8))
        for section in self._sections:
            frame = ctk.CTkFrame(body, fg_color=palette.panel_bg, corner_radius=8,
                                 border_width=1, border_color=palette.border)
            frame.pack(fill="x", pady=(0, 8))
            if section.title:
                ctk.CTkLabel(frame, text=section.title.upper(), font=(palette.font_family, 9, "bold"),
                             text_color=_tone_color(palette, section.tone), anchor="w",
                             ).pack(anchor="w", fill="x", padx=10, pady=(8, 0))
            text = ctk.CTkTextbox(
                frame, font=(palette.mono_family if section.tone in {"mono", "error"} else palette.font_family,
                             max(9, palette.font_size)),
                fg_color="transparent", text_color=_tone_color(palette, section.tone),
                wrap="word", corner_radius=0, activate_scrollbars=False,
            )
            lines = max(1, section.content.count("\n") + 1)
            text.configure(height=min(220, 16 + lines * 15))
            text.insert("1.0", section.content)
            text.configure(state="disabled")
            text.pack(fill="x", padx=8, pady=(0, 8))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", side="bottom", padx=16, pady=(4, 14))
        self._ack: Optional[ctk.CTkCheckBox] = None
        if require_ack:
            self._ack = ctk.CTkCheckBox(
                footer, text=confirm_text, font=(palette.font_family, 11),
                text_color=palette.text, checkbox_width=18, checkbox_height=18,
                fg_color=palette.warning, hover_color=palette.error, border_color=palette.border,
                command=self._update_state,
            )
            self._ack.pack(anchor="w", pady=(0, 8))
        buttons = ctk.CTkFrame(footer, fg_color="transparent")
        buttons.pack(anchor="e")
        ctk.CTkButton(buttons, text="Cancel", width=100, height=32, fg_color=palette.panel_alt,
                      hover_color=palette.hover, text_color=palette.text,
                      command=self._on_cancel).pack(side="right", padx=(8, 0))
        self._accept_button = ctk.CTkButton(
            buttons, text=accept_label, width=130, height=32, fg_color=palette.error,
            hover_color=_hover(palette.error), text_color="#ffffff", state="disabled" if require_ack else "normal",
            command=self._accept,
        )
        self._accept_button.pack(side="right")
        self._require_ack = require_ack
        self.after(40, self._update_state)

    def _update_state(self) -> None:
        if not self._require_ack or self._ack is None:
            self._accept_button.configure(state="normal")
            return
        try:
            checked = bool(self._ack.get())
        except (tk.TclError, ValueError):  # pragma: no cover
            checked = False
        self._accept_button.configure(state="normal" if checked else "disabled")

    def _accept(self) -> None:
        if self._accept_button.cget("state") == "disabled":
            return
        self.close(True)

    def _on_return(self, event: Any) -> str:
        return "break"


def _tone_color(palette: Palette, tone: str) -> str:
    return {
        "info": palette.text,
        "warn": palette.warning,
        "error": palette.error,
        "ok": palette.success,
        "mono": palette.console_fg,
        "dim": palette.text_muted,
    }.get(tone, palette.text)


def _hover(color: str) -> str:
    from ..theme import darken

    try:
        return darken(color, 0.16)
    except (TypeError, ValueError):  # pragma: no cover
        return color


# ---------------------------------------------------------------------------------- helpers
def ask_message(
    parent: Any,
    kind: str = "info",
    title: str = "",
    message: str = "",
    detail: str = "",
    buttons: Sequence[str] = ("OK",),
    modal: bool = True,
    palette: Optional[Palette] = None,
    width: int = 520,
) -> Optional[str]:
    """Show a modal message box; returns the clicked button label."""
    dialog = MessageDialog(
        parent, kind=kind, title=title, message=message, detail=detail, buttons=buttons,
        palette=palette, modal=modal, width=width, show_copy=bool(detail),
    )
    if not modal:
        return None
    return dialog.run()


def ask_yes_no(
    parent: Any,
    question: str,
    detail: str = "",
    *,
    yes_label: str = "Yes",
    no_label: str = "No",
    kind: str = "question",
    palette: Optional[Palette] = None,
) -> bool:
    """Yes/No (or OK/Cancel) confirmation; ``True`` when the primary button was used."""
    answer = ask_message(parent, kind=kind, message=question, detail=detail,
                         buttons=(no_label, yes_label), palette=palette)
    return answer == yes_label


def ask_text(
    parent: Any,
    label: str,
    initial: str = "",
    *,
    title: str = "Input",
    validator: Optional[Callable[[str], tuple[bool, str]]] = None,
    hint: str = "",
    palette: Optional[Palette] = None,
    confirm_label: str = "OK",
) -> Optional[str]:
    """Prompt for one line of text (``None`` when cancelled)."""
    dialog = InputDialog(parent, title=title, label=label, initial=initial, validator=validator,
                         palette=palette, hint=hint, confirm_label=confirm_label)
    return dialog.run()


def ask_choice(
    parent: Any,
    options: Sequence[Any],
    *,
    title: str = "Choose",
    label: str = "",
    display: Optional[Callable[[Any], str]] = None,
    palette: Optional[Palette] = None,
) -> Optional[Any]:
    """Choose one item from a searchable list (``None`` when cancelled)."""
    dialog = ChoiceDialog(parent, title=title, label=label, options=options, display=display, palette=palette)
    return dialog.run()


def ask_plan(
    parent: Any,
    *,
    heading: str,
    sections: Sequence[PlanSection],
    title: str = "Confirm",
    accept_label: str = "Run",
    require_ack: bool = True,
    palette: Optional[Palette] = None,
) -> bool:
    """Destructive-action confirmation; returns ``True`` when accepted."""
    dialog = PlanDialog(parent, title=title, heading=heading, sections=sections,
                        accept_label=accept_label, require_ack=require_ack, palette=palette)
    return bool(dialog.run())
