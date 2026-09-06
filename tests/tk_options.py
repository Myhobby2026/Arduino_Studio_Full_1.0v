"""Tk option-name tables, shared by the test stub (run time) and
``tools/check_ctk_kwargs.py`` (build time).

Why this exists: a misspelled Tk option is not a Python error, it is a Tcl one -
``TclError: unknown option "-min_width"`` - raised while a window is being built.
Inside a packaged, windowed Arduino Studio that looks exactly like "the exe opens
and closes again", and a Tkinter test double that accepts anything cannot see it.
So both the test suite and the build script check the option names we pass to a
handful of closed-set commands against what Tk really accepts.

Reference: Tk 8.6 manual pages for grid/pack/place, ``ttk::treeview`` (column,
heading, insert, item), Text tags and ``menu`` entries, plus the canvas item
options.  Commands whose options depend on the widget (``item``, ``itemconfigure``,
``tag_configure``) are resolved from the receiver's name, and left unpoliced when
that is ambiguous.
"""

from __future__ import annotations

from typing import Iterable, Optional

#: ``widget.grid(...)``
GRID = {"column", "columnspan", "in", "ipadx", "ipady", "padx", "pady", "row", "rowspan", "sticky"}

#: ``widget.pack(...)``
PACK = {"after", "anchor", "before", "expand", "fill", "in", "ipadx", "ipady", "padx", "pady", "side"}

#: ``widget.place(...)``
PLACE = {"anchor", "bordermode", "height", "in", "relheight", "relwidth", "relx", "rely", "width", "x", "y"}

#: ``ttk.Treeview.column(...)`` - the option is ``minwidth``, never ``min_width``
TREEVIEW_COLUMN = {"id", "anchor", "extension", "justify", "minwidth", "option", "stretch", "width"}

#: ``ttk.Treeview.heading(...)``
TREEVIEW_HEADING = {"anchor", "command", "image", "position", "text", "justify"}

#: ``ttk.Treeview.insert(...)``
TREEVIEW_INSERT = {"parent", "index", "iid", "values", "text", "tags"}

#: ``ttk.Treeview.item(...)`` / ``ttk.Treeview.tag_configure(...)``
TREEVIEW_ITEM = {"text", "values", "tags", "open"}
TREEVIEW_TAG = {"background", "foreground", "font", "borderwidth", "relief", "image"}

#: ``tkinter.Text.tag_configure(...)``
TEXT_TAG = {
    "background", "borderwidth", "elide", "fg", "bg", "font", "justify", "lmargin1", "lmargin2", "offset",
    "overstrike", "relief", "rmargin", "rmargin2", "spacing1", "spacing2", "spacing3", "tabs", "underline",
    "wrap", "foreground",
}

#: ``canvas.create_*`` / ``canvas.itemconfigure(...)``
CANVAS_ITEM = {
    "anchor", "arrow", "arrowshape", "capstyle", "dash", "disableddash", "extent", "fill", "font", "height",
    "image", "joinstyle", "offset", "outline", "outlinewidth", "quality", "smooth", "spreadover", "start",
    "state", "stipple", "tags", "text", "width", "x", "y", "window", "coords",
}

#: ``menu.add_command`` / ``add_cascade`` / ``add_checkbutton`` / ``add_radiobutton``
MENU_ENTRY = {
    "accelerator", "activebackground", "activeforeground", "background", "columnbreak", "command", "compound",
    "font", "foreground", "hidemargin", "image", "indicatoron", "label", "menu", "offvalue", "onvalue",
    "padx", "pady", "relief", "row", "selectcolor", "separator", "state", "underline", "value",
}

#: commands policed by name alone
BY_COMMAND: dict[str, set[str]] = {
    "grid": GRID,
    "pack": PACK,
    "place": PLACE,
    "column": TREEVIEW_COLUMN,
    "heading": TREEVIEW_HEADING,
    "add_command": MENU_ENTRY,
    "add_cascade": MENU_ENTRY,
    "add_checkbutton": MENU_ENTRY,
    "add_radiobutton": MENU_ENTRY,
    "add_separator": {"padx"},
}

#: commands whose options depend on the widget they are called on
BY_RECEIVER: dict[str, dict[str, set[str]]] = {
    "item": {"tree": TREEVIEW_ITEM, "canvas": CANVAS_ITEM},
    "itemconfigure": {"tree": TREEVIEW_ITEM, "canvas": CANVAS_ITEM},
    "itemconfig": {"tree": TREEVIEW_ITEM, "canvas": CANVAS_ITEM},
    "tag_configure": {"tree": TREEVIEW_TAG | TEXT_TAG, "text": TEXT_TAG, "canvas": CANVAS_ITEM},
    "tag_config": {"tree": TREEVIEW_TAG | TEXT_TAG, "text": TEXT_TAG, "canvas": CANVAS_ITEM},
    # Text/Listbox/Menu all have insert() with different meanings - only the
    # Treeview one is policed, so nothing else can be flagged by mistake.
    "insert": {"tree": TREEVIEW_INSERT},
}


def _receiver_kind(receiver: str, command: str) -> Optional[str]:
    """Map ``self.tree`` / ``self.tab_canvas`` / ``Treeview`` to a widget kind."""
    if not receiver:
        return None
    text = str(receiver).lower()
    table = BY_RECEIVER.get(command, {})
    for kind in table:
        if kind in text:
            return kind
    # a Text widget's tag options are the safe superset
    if command.startswith("tag_") and "text" in text:
        return "text"
    return None


def command_options(command: str, receiver: str = "") -> Optional[set[str]]:
    """The accepted option names for *command*, or None when it is not policed."""
    name = str(command or "")
    table = BY_RECEIVER.get(name)
    if table:
        kind = _receiver_kind(receiver, name)
        return table.get(kind) if kind else None
    return BY_COMMAND.get(name)


def unsupported(command: str, names: Iterable[str], receiver: str = "") -> list[str]:
    """Option names *command* does not know (empty list means everything is fine)."""
    allowed = command_options(command, receiver)
    if allowed is None:
        return []
    return sorted(str(option) for option in names if option and option not in allowed)


def describe(command: str, names: Iterable[str], receiver: str = "") -> str:
    """A complaint in Tk's own words, with the supported names listed."""
    bad = unsupported(command, names, receiver)
    if not bad:
        return ""
    allowed = ", ".join(sorted(command_options(command, receiver) or set()))
    where = f"{receiver}.{command}()" if receiver else f"{command}()"
    return f'unknown option "-{bad[0]}" for {where} (supported options: {allowed})'
