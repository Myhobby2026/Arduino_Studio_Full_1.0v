"""The subset of Tk's binding-sequence grammar that this project relies on.

Tk parses a binding pattern such as ``<Control-Shift-KeyPress-A>`` itself, which
means a mistake in one is *not* a Python error: ``bind`` raises
``TclError: bad event type or keysym "keypad"`` the moment the widget is built.
A typo in a binding that runs while the main window is being constructed is
therefore another way for the packaged ``Arduino Studio.exe`` to open and close
again without an obvious message - which is exactly what happened with
``<Control-keypad-plus>`` in the console, and what silently disabled the
numpad-zoom bindings written as ``<Control+KP_Add>`` (Tk joins modifiers with
``-``, never ``+``).

The tables here are deliberately *conservative*: they exist to catch names this
repository invented, not to describe every keysym in X11.  Anything unknown but
plausible (``XF86*``, ``KP_*``, ``ISO_*``, a single character, a numeric code)
is accepted so the guard cannot produce false alarms on platforms or builds we
do not have here.

Used by:

* ``tests/tkstub.py`` - validates at run time, so a bad sequence fails the UI
  smoke suite the way the real interpreter fails the app;
* ``tools/check_ctk_kwargs.py`` - validates the literal sequences in the source
  tree, which ``build_exe.bat`` runs before PyInstaller starts.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

__all__ = ["MODIFIERS", "EVENT_TYPES", "KEYSYMS", "HINTS", "split_sequence",
           "validate", "describe", "unknown_tokens"]


#: ``<Control-...>`` and friends.  ``double``/``triple`` are modifiers, not event
#: types; ``un*`` forms mean "must not be held".
MODIFIERS = {
    "control", "uncontrol", "shift", "unshift", "alt", "unalt", "meta", "unmeta",
    "win", "unwin", "command", "uncommand", "option", "extend", "lock",
    "double", "triple", "anymodifier",
    "mod1", "mod2", "mod3", "mod4", "mod5",
    "mod1mask", "controlmask", "shiftmask", "colormapchange",
}

#: event types (Tk also accepts the short aliases ``Key``, ``Button``, ``M``, ...)
EVENT_TYPES = {
    "activate", "circulate", "clientmessage", "colormap", "configure", "create",
    "destroy", "enter", "enternotify", "expose", "focus", "focusin", "focusout",
    "gravity", "key", "keypress", "keyrelease", "keymap", "kbutton", "kpress",
    "krelease", "leave", "leavewindow", "map", "motion", "mousemotion",
    "mousewheel", "mpress", "mrelease", "nonexpose", "property", "reparent",
    "resizerequest", "selection", "selectionclear", "selectionrequest",
    "selectionnotify", "unmap", "visibility", "visibilitynotify",
    "button", "buttonpress", "buttonrelease", "buttonmotion", "button1", "button2",
    "button3", "button4", "button5",
}

#: keysyms we actually bind, plus the punctuation a text widget needs.  Single
#: characters never appear here: any one-character detail is valid Tk.
KEYSYMS = {
    # ASCII / Latin-1 names
    "space", "exclam", "quotedbl", "numbersign", "dollar", "percent", "ampersand",
    "apostrophe", "parenleft", "parenright", "asterisk", "plus", "comma", "minus",
    "period", "slash", "colon", "semicolon", "less", "equal", "greater",
    "question", "at", "bracketleft", "backslash", "bracketright", "asciicircum",
    "underscore", "grave", "braceleft", "bar", "braceright", "asciitilde",
    "brokenbar", "copyright", "ordfeminine", "notsign", "hyphen", "registered",
    "macron", "degree", "plusminus", "twosuperior", "threesuperior", "acute",
    "mu", "paragraph", "periodcentered", "cedilla", "onesuperior", "masculine",
    "guillemotleft", "guillemotright", "lessthanequal", "greatthanequal",
    "multiply", "divide", "nobreakspace", "yen",
    # editing / navigation
    "backspace", "tab", "linefeed", "clear", "return", "enter", "pause",
    "scroll_lock", "scrolllock", "sysreq", "sys_require", "escape", "convert",
    "nonconvert", "insert", "delete", "home", "end", "page_up", "page_down",
    "prior", "next", "begin", "up", "down", "left", "right", "undo", "redo",
    "find", "cancel", "help", "menu", "select", "execute", "break",
    # modifiers as keysyms (``<Shift_L>`` binds the key itself)
    "shift_l", "shift_r", "control_l", "control_r", "caps_lock", "shift_lock",
    "meta_l", "meta_r", "alt_l", "alt_r", "num_lock", "mode_switch", "lock",
    "win_l", "win_r", "multi_key", "compose",
    # function keys
    *(f"f{index}" for index in range(1, 36)),
}

#: keypad keys.  X11 spells them ``KP_Add``/``KP_Subtract``/``KP_Enter``; the
#: names people reach for first (``keypad-plus``, ``num_add``, ``kp_plus``) are
#: not keysyms at all, which is how the shipped console zoom crashed.
KEYSYMS |= {
    "kp_space", "kp_tab", "kp_enter", "kp_f1", "kp_f2", "kp_f3", "kp_f4",
    "kp_home", "kp_left", "kp_up", "kp_right", "kp_down", "kp_prior", "kp_next",
    "kp_begin", "kp_insert", "kp_delete", "kp_equal", "kp_multiply", "kp_add",
    "kp_separator", "kp_subtract", "kp_decimal", "kp_divide",
    *(f"kp_{index}" for index in range(0, 10)),
    "kp_page_up", "kp_page_down",
}

#: what to suggest when somebody invents a name
HINTS = {
    "keypad-plus": "KP_Add", "keypad_plus": "KP_Add", "keypadplus": "KP_Add",
    "kp_plus": "KP_Add", "numplus": "KP_Add", "numpad-plus": "KP_Add",
    "keypad-minus": "KP_Subtract", "keypad_minus": "KP_Subtract",
    "kp_minus": "KP_Subtract", "numpad-minus": "KP_Subtract",
    "keypad-enter": "KP_Enter", "keypadenter": "KP_Enter",
    "keypad": "KP_Add or one of the other keypad keysyms",
    "pageup": "Page_Up", "pagedown": "Page_Down", "ctrl": "Control",
    "altgr": "Mode_switch", "esc": "Escape", "del": "Delete", "returnkey": "Return",
    "plus-minus": "plus",
}


def split_sequence(sequence: str) -> list[str]:
    """Split ``<Control-Shift-A>`` into ``["Control", "Shift", "A"]``.

    A pattern without angle brackets (``"a"``, ``"<"``, ``"space"``) is a single
    detail and is returned as-is; virtual events (``<<Modified>>``) are opaque.
    """
    text = str(sequence or "")
    if not text:
        return []
    if text.startswith("<<") and text.endswith(">>") and len(text) > 4:
        return [text]
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    parts: list[str] = []
    chunk = ""
    depth = 0
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        if char == "-" and depth == 0:
            parts.append(chunk)
            chunk = ""
            continue
        chunk += char
    parts.append(chunk)
    return [part for part in parts if part != ""]


#: ``<B1-Motion>`` and ``<Button1-ButtonMotion>`` are ordinary Tk patterns
_BUTTON_TOKEN = re.compile(r"^(?:b|button)[1-5]$", re.IGNORECASE)


def _looks_like_keysym(token: str) -> bool:
    """True for anything Tk's ``XStringToKeysym`` would resolve."""
    if not token:
        return False
    if len(token) == 1:
        return True  # any single character is its own keysym
    lowered = token.lower()
    if lowered in KEYSYMS:
        return True
    if token.isdigit() or (token.startswith("0x") and len(token) > 2):
        return True  # numeric keysym code
    # families we do not enumerate: vendor keysyms and the keypad/Latin-1 sets
    return lowered.startswith(("kp_", "xf86", "iso_", "sun_", "mac_", "ole_"))


def unknown_tokens(sequence: str) -> list[str]:
    """The parts of *sequence* that Tk would reject (empty list means it is fine)."""
    tokens = split_sequence(sequence)
    if not tokens:
        return []
    if len(tokens) == 1 and tokens[0] == sequence:
        return []  # virtual event, or a bare single-detail pattern
    text = str(sequence or "")
    if text.startswith("<") != text.endswith(">"):
        return [text]  # unbalanced brackets - Tk refuses this too
    bad: list[str] = []
    for index, token in enumerate(tokens):
        is_last = index == len(tokens) - 1
        if token in ("*", ""):
            continue
        if _BUTTON_TOKEN.match(token):
            continue  # <B1-Motion>, <Button1-ButtonPress>
        if not is_last:
            # every field before the last one is a modifier or the event type
            if token.lower() in MODIFIERS or token.lower() in EVENT_TYPES:
                continue
            bad.append(token)
            continue
        if token.lower() in EVENT_TYPES:
            continue  # ``<KeyRelease>`` on its own is a valid pattern
        if token.isdigit():
            continue  # ``<Button-1>`` / ``<Double-1>``
        if _looks_like_keysym(token):
            continue
        bad.append(token)
    return bad


def validate(sequence: str) -> bool:
    """True when Tk would accept *sequence* as a binding pattern."""
    return not unknown_tokens(sequence)


def describe(sequence: str) -> str:
    """Tk's own complaint about *sequence*, with a hint where we have one."""
    bad = unknown_tokens(sequence)
    if not bad:
        return ""
    token = bad[0]
    hint = HINTS.get(token.lower().replace("-", "_").replace("_", "-")) \
        or HINTS.get(token.lower())
    suffix = f' (did you mean "{hint}"?)' if hint else ""
    if "-" in token or "+" in token:
        suffix += '  Tk separates modifiers with "-", never with "+"' if "+" in token else ""
    return f'bad event type or keysym "{token}"{suffix} for "{sequence}"'


def first_error(sequences: Iterable[str]) -> Optional[str]:
    """``describe()`` for the first sequence that is not valid, else ``None``."""
    for sequence in sequences:
        message = describe(sequence)
        if message:
            return message
    return None
