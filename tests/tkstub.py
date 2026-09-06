"""Headless stand-in for ``tkinter`` / ``customtkinter`` used by the UI tests.

The sandbox (and CI) has no display, so the real toolkit cannot be started.
This module installs permissive fakes into :data:`sys.modules` *before* the
application is imported, which lets the tests:

* import every UI module,
* build the widget tree (all our ``_build()`` code actually runs),
* call event handlers, menu commands and callbacks,
* drive :meth:`after` scheduling deterministically via :func:`pump`.

The fakes model just enough behaviour to be useful: ``tk.Text`` keeps a real
string buffer with Tk-style ``line.column`` indexes, marks and tags, the
``ttk.Treeview`` keeps a parent/child node table, and ``Menu`` records its
entries so a test can invoke them by label.

Call :func:`install` once (idempotent) before importing anything from
``arduino_studio.ui``.
"""

from __future__ import annotations

import inspect
import itertools
import re
import os
import sys
import types
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = ["install", "installed", "pump", "reset", "ROOT", "next_dialog_answer", "DialogQueue",
           "DIALOGS", "CLIPBOARD", "patch_dialogs", "FakeEvent"]

_INSTALLED = False
_counter = itertools.count(1)

#: every key sequence bound on any stubbed widget (used to verify menu accelerators)
BOUND_SEQUENCES: list[str] = []

try:  # optional run-time validation of Tk option names (see tk_options.py)
    from tk_options import describe as _describe_options
except ImportError:  # pragma: no cover - imported as part of a package
    try:
        from .tk_options import describe as _describe_options  # type: ignore[attr-defined]
    except Exception:
        _describe_options = None  # type: ignore[assignment]


def _check_options(command: str, kwargs: dict[str, Any], receiver: str = "") -> None:
    """Raise ``TclError`` for options that the real Tk interpreter would reject.

    Without this the stub accepts anything, which is how ``Treeview.column(
    min_width=...)`` got as far as a packaged build and looked like "Arduino
    Studio.exe will not open".
    """
    if _describe_options is None or not kwargs:
        return
    message = _describe_options(command, [key for key in kwargs if key], receiver)
    if not message:
        return
    if os.environ.get("ARDUINO_STUDIO_TK_OPTION_WARNINGS"):  # debug mode: only report
        print(f"[tk-option] {receiver or '?'} {command}(): {message}", file=sys.stderr)
        return
    raise TclError(message)


def bound_sequences() -> set[str]:
    """All sequences bound so far, normalised (``<Control-Shift-Z>``)."""
    out: set[str] = set()
    for raw in BOUND_SEQUENCES:
        text = str(raw).strip()
        if text:
            out.add(text if text.startswith("<") else f"<{text}>")
    return out


# --------------------------------------------------------------------- helpers
class TextModel:
    """Index arithmetic for a ``tk.Text``-like buffer (Tk ``line.column`` rules)."""

    LINE_HEIGHT = 16

    def __init__(self, content: str, first_visible_line: int = 1) -> None:
        self.content = content
        self.first_visible_line = max(1, int(first_visible_line))

    # ---- geometry
    def line_starts(self) -> list[int]:
        starts = [0]
        for position, char in enumerate(self.content):
            if char == "\n":
                starts.append(position + 1)
        return starts

    def line_count(self) -> int:
        return self.content.count("\n") + 1

    def line_start(self, line: int) -> int:
        starts = self.line_starts()
        return starts[min(max(1, line), len(starts)) - 1]

    def line_end(self, line: int) -> int:
        starts = self.line_starts()
        line = min(max(1, line), len(starts))
        if line < len(starts):
            return starts[line] - 1
        return len(self.content)

    def offset(self, line: int, col: int) -> int:
        start = self.line_start(line)
        end = self.line_end(line)
        return start + max(0, min(col, end - start))

    def canonical(self, offset: int) -> str:
        offset = max(0, min(int(offset), len(self.content)))
        before = self.content[:offset]
        line = before.count("\n") + 1
        return f"{line}.{offset - (before.rfind(chr(10)) + 1)}"

    # ---- parsing
    def resolve(self, index: Any, marks: Optional[dict[str, str]] = None) -> int:
        marks = marks or {}
        text = re.sub(r"(?<=[\w)\]])([+-])(?=\d)", r" \1", str(index).strip())
        tokens = text.split()
        if not tokens:
            return 0
        base = tokens[0]
        rest = tokens[1:]
        if base in marks and marks[base] != base:
            position = self.resolve(marks[base], marks)
        elif base == "end":
            position = len(self.content)
        elif base.startswith("@"):
            position = self._from_pixel(base)
        elif re.match(r"^-?\d+\.\d+$", base):
            line, col = base.split(".")
            position = self.offset(int(line), int(col))
        elif re.match(r"^-?\d+\.\w+$", base):
            line_text, suffix = base.split(".", 1)
            line = int(line_text)
            if suffix in ("end", "lineend", "right"):
                position = self.line_end(line)
            elif suffix in ("start", "linestart", "left"):
                position = self.line_start(line)
            else:
                position = self.offset(line, 0)
        elif re.match(r"^-?\d+$", base):
            position = self.offset(int(base), 0)
        else:
            position = 0
        return self._apply_modifiers(position, rest)

    def _from_pixel(self, base: str) -> int:
        try:
            _x, y = base[1:].split(",")
            line = self.first_visible_line + int(float(y)) // self.LINE_HEIGHT
        except (ValueError, IndexError):  # pragma: no cover
            line = 1
        return self.offset(line, 0)

    def _apply_modifiers(self, position: int, tokens: list[str]) -> int:
        index = 0
        while index < len(tokens) and index < 12:
            token = tokens[index]
            if token in ("linestart", "display", "wordstart", "wordend", "lineend", "left", "right"):
                if token == "linestart" or (token == "display" and "linestart" in tokens[index + 1:index + 2]):
                    position = self.content.rfind("\n", 0, position) + 1
                    index += 2 if token == "display" else 1
                    continue
                if token == "lineend" or (token == "display" and "lineend" in tokens[index + 1:index + 2]):
                    nxt = self.content.find("\n", position)
                    position = len(self.content) if nxt < 0 else nxt
                    index += 2 if token == "display" else 1
                    continue
                if token == "left":
                    position = self.content.rfind("\n", 0, position) + 1
                    index += 1
                    continue
                if token == "right":
                    nxt = self.content.find("\n", position)
                    position = len(self.content) if nxt < 0 else nxt
                    index += 1
                    continue
                if token == "wordstart":
                    while position > 0 and (self.content[position - 1].isalnum() or self.content[position - 1] == "_"):
                        position -= 1
                elif token == "wordend":
                    while position < len(self.content) and (self.content[position].isalnum() or self.content[position] == "_"):
                        position += 1
                index += 1
                continue
            match = re.match(r"^([+-])\s*(\d+)\s*(c|chars?|l|lines?|indices|words|display\s*lines?)$",
                             token + (" " + tokens[index + 1] if index + 1 < len(tokens) else ""))
            if match:
                sign, amount, unit = match.group(1), int(match.group(2)), match.group(3).strip()
                step = amount if sign == "+" else -amount
                if unit in ("l", "lines", "display lines"):
                    line = self.content[:position].count("\n") + 1
                    col = position - self.line_start(line)
                    position = self.offset(line + step, col)
                elif unit in ("indices", "words"):
                    for _ in range(abs(step)):
                        position = self._apply_modifiers(position, ["wordstart" if step < 0 else "wordend"])
                else:
                    position += step
                index += 2 if " " in token + " " else 1
                index += 1 if len(match.group(3).split()) > 1 else 0
                continue
            index += 1
        return max(0, min(position, len(self.content)))


def _call_handler(handler: Callable[..., Any], event: Any) -> Any:
    """Call *handler* with or without the event argument, based on its signature."""
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):  # builtins / callables without signature
        return handler(event)
    required = [
        param for param in signature.parameters.values()
        if param.default is param.empty and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
    ]
    if not required:
        return handler()
    return handler(event)


class FakeEvent:
    """Minimal stand-in for a ``tk.Event``."""

    def __init__(self, widget: Any = None, **attrs: Any) -> None:
        self.widget = widget
        self.x = int(attrs.get("x", 0))
        self.y = int(attrs.get("y", 0))
        self.x_root = int(attrs.get("x_root", self.x))
        self.y_root = int(attrs.get("y_root", self.y))
        self.char = attrs.get("char", "")
        self.keysym = attrs.get("keysym", "")
        self.keycode = int(attrs.get("keycode", 0))
        self.delta = int(attrs.get("delta", 0))
        self.state = int(attrs.get("state", 0))
        self.num = int(attrs.get("num", 1))
        self.width = int(attrs.get("width", 800))
        self.height = int(attrs.get("height", 600))
        self.code = attrs.get("code", "")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"FakeEvent({self.keysym or self.char!r})"


# ------------------------------------------------------------------ after loop
_AFTER_QUEUE: list[tuple[int, int, Callable[[], Any], str]] = []


def _schedule(delay_ms: int, func: Callable[[], Any], ident: str) -> str:
    _AFTER_QUEUE.append((int(_counter.__next__()), max(0, int(delay_ms)), func, ident))
    return ident


def pump(limit: int = 200, *, max_passes: int = 40) -> int:
    """Run scheduled callbacks (shortest delay first); returns how many ran."""
    run = 0
    for _ in range(max_passes):
        if not _AFTER_QUEUE:
            break
        _AFTER_QUEUE.sort(key=lambda item: (item[1], item[0]))
        batch, _AFTER_QUEUE[:] = _AFTER_QUEUE[:limit], []
        for _seq, _delay, func, _ident in batch:
            run += 1
            func()
        if run >= limit:
            break
    return run


def reset_after_queue() -> None:
    _AFTER_QUEUE.clear()


# ---------------------------------------------------------------- base widgets
class _Widget:
    """Anything that can be configured, packed, bound and destroyed."""

    _tk_kind = "frame"

    def __init__(self, master: Any = None, **options: Any) -> None:
        self.master = master
        self._options: dict[str, Any] = dict(options)
        self._children: list["_Widget"] = []
        self._bindings: dict[str, list[Callable[[Any], Any]]] = {}
        self._destroyed = False
        self._id = f"stub{next(_counter)}"
        if isinstance(master, _Widget):
            master._children.append(self)
        command = self._options.get("command")
        if callable(command):
            self._options["command"] = command

    # ---- config
    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if cnf is None:
            return {key: (key, key, "", "", value) for key, value in self._options.items()}
        if isinstance(cnf, dict):
            self._options.update(cnf)
            return None
        if not kwargs and cnf is not None and isinstance(cnf, str) and cnf in self._options:
            if callable(self._options[cnf]):
                return self._options[cnf]
            return (cnf, cnf, "", "", self._options[cnf])
        self._options.update(kwargs)
        if isinstance(cnf, str):
            self._options[cnf] = kwargs.get(cnf, None)
        return None

    config = configure

    def cget(self, key: str) -> Any:
        return self._options.get(key, "")

    def __setitem__(self, key: str, value: Any) -> None:
        self._options[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._options.get(key, "")

    def keys(self) -> list[str]:
        return list(self._options)

    # ---- geometry (no-ops)
    def pack(self, **kwargs: Any) -> None:
        _check_options("pack", kwargs, type(self).__name__.lower())
        self._options["_packed"] = kwargs

    def grid(self, **kwargs: Any) -> None:
        _check_options("grid", kwargs, type(self).__name__.lower())
        self._options["_gridded"] = kwargs

    def place(self, **kwargs: Any) -> None:
        _check_options("place", kwargs, type(self).__name__.lower())
        self._options["_placed"] = kwargs

    def pack_forget(self) -> None:
        self._options.pop("_packed", None)

    def grid_forget(self) -> None:
        self._options.pop("_gridded", None)

    def grid_remove(self) -> None:
        self._options.pop("_gridded", None)

    def place_forget(self) -> None:
        self._options.pop("_placed", None)

    def pack_propagate(self, flag: bool = False) -> None:
        pass

    def grid_propagate(self, flag: bool = False) -> None:
        pass

    def grid_rowconfigure(self, index: Any, **kwargs: Any) -> None:
        pass

    def grid_columnconfigure(self, index: Any, **kwargs: Any) -> None:
        pass

    def grid_size(self) -> tuple[int, int]:
        return (1, 1)

    def pack_info(self) -> dict[str, Any]:
        return dict(self._options.get("_packed") or {})

    def grid_info(self) -> dict[str, Any]:
        return dict(self._options.get("_gridded") or {})

    def lift(self, above: Any = None) -> None:
        pass

    def lower(self, below: Any = None) -> None:
        pass

    def update(self) -> None:
        pass

    def update_idletasks(self) -> None:
        pass

    def idletasks(self) -> None:
        pass

    def wait_variable(self, name: Any = None) -> None:
        pass

    def wait_visibility(self, window: Any = None) -> None:
        pass

    def wait_window(self, window: Any = None) -> None:
        pass

    # ---- geometry queries
    def winfo_id(self) -> int:
        return 12345

    def winfo_containing(self, *args: Any) -> Any:
        return None

    def winfo_atom(self, name: str, create: bool = False) -> int:
        return abs(hash(str(name))) % 100000

    def winfo_atomname(self, index: int) -> str:
        return "atom"

    def winfo_fpixels_or(self, *args: Any) -> float:  # pragma: no cover
        return 1.0

    def winfo_exists(self) -> bool:
        return not self._destroyed

    def winfo_ismapped(self) -> bool:
        return True

    def winfo_viewable(self) -> bool:
        return True

    def winfo_width(self) -> int:
        return int(self._options.get("width", 800) or 800)

    def winfo_height(self) -> int:
        return int(self._options.get("height", 600) or 600)

    def winfo_reqwidth(self) -> int:
        return self.winfo_width()

    def winfo_reqheight(self) -> int:
        return self.winfo_height()

    def winfo_x(self) -> int:
        return 0

    def winfo_y(self) -> int:
        return 0

    def winfo_rootx(self) -> int:
        return 0

    def winfo_rooty(self) -> int:
        return 0

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    def winfo_fpixels(self, value: Any) -> float:
        return float(re.sub(r"[^0-9.]", "", str(value)) or 0)

    def winfo_pixels(self, value: Any) -> int:
        return int(self.winfo_fpixels(value))

    def winfo_toplevel(self) -> Any:
        return ROOT

    def winfo_children(self) -> list[Any]:
        return list(self._children)

    def winfo_interactive(self) -> int:
        return 0

    def winfo_manager(self) -> str:
        if "_gridded" in self._options:
            return "grid"
        if "_packed" in self._options:
            return "pack"
        if "_placed" in self._options:
            return "place"
        return ""

    def winfo_class(self) -> str:
        return self._tk_kind.title()

    # ---- events
    def bind(self, sequence: str = "", func: Optional[Callable[[Any], Any]] = None,
             add: Any = None) -> str:
        if func is None:
            return ""
        self._bindings.setdefault(sequence, []).append(func)
        BOUND_SEQUENCES.append(str(sequence))
        return f"bind#{next(_counter)}"

    def bind_all(self, sequence: str = "", func: Optional[Callable[[Any], Any]] = None,
                 add: Any = None) -> str:
        return self.bind(sequence, func, add)

    def unbind(self, sequence: str, funcid: Any = None) -> None:
        self._bindings.pop(sequence, None)

    def bind_class(self, className: str, sequence: str = "", func: Any = None, add: Any = None) -> str:
        return ""

    def event_add(self, sequence: str = "", *args: Any, **kwargs: Any) -> None:
        pass

    def event_generate(self, sequence: str = None, **kwargs: Any) -> int:  # type: ignore[assignment]
        return self.dispatch(str(sequence), kwargs)

    def dispatch(self, sequence: str, attrs: Optional[dict[str, Any]] = None) -> int:
        """Fire the handlers bound to *sequence* (used by the tests)."""
        handlers = list(self._bindings.get(sequence, ()))
        if not handlers:
            for key, funcs in self._bindings.items():
                if key.replace(">", "").lower() == str(sequence).replace(">", "").lower():
                    handlers.extend(funcs)
        event = FakeEvent(self, **(attrs or {}))
        for handler in handlers:
            result = _call_handler(handler, event)
            if result == "break":
                return 0
        return len(handlers)

    def invoke(self, sequence: Optional[str] = None) -> Any:
        """Call the widget's command (or a bound handler) directly."""
        if sequence is not None:
            self.dispatch(sequence, {})
            return None
        command = self._options.get("command")
        if callable(command):
            return command()
        return None

    # ---- scheduling
    def after(self, delay: int, func: Optional[Callable[..., Any]] = None, *args: Any, **kwargs: Any) -> str:
        if func is None:
            return ""
        if kwargs:
            base = func

            def wrapped() -> Any:
                return base(*args, **kwargs)
        elif args:
            base2 = func

            def wrapped() -> Any:  # type: ignore[no-redef]
                return base2(*args)
        else:
            wrapped = func  # type: ignore[assignment]
        return _schedule(int(delay), wrapped, f"after#{next(_counter)}")

    def after_idle(self, func: Optional[Callable[..., Any]] = None, *args: Any) -> str:
        if func is None:
            return ""
        return self.after(0, func, *args)

    def after_cancel(self, ident: Any) -> None:
        _AFTER_QUEUE[:] = [item for item in _AFTER_QUEUE if item[3] != ident]

    def after_info(self, ident: Any) -> None:
        return None

    # ---- misc
    def focus_set(self) -> None:
        pass

    def focus_force(self) -> None:
        pass

    def focus(self) -> None:
        pass

    def focus_displayof(self) -> Any:
        return None

    def tk_focusNext(self) -> Any:
        return self

    def grab_set(self) -> None:
        pass

    def grab_release(self) -> None:
        pass

    def grab_current(self) -> Any:
        return None

    def clipboard_clear(self) -> None:
        CLIPBOARD["value"] = ""

    def clipboard_append(self, value: str, **kwargs: Any) -> None:
        CLIPBOARD["value"] = str(value)

    def clipboard_get(self) -> str:
        return str(CLIPBOARD.get("value", ""))

    def selection_clear(self) -> None:
        pass

    def nametowidget(self, path: str) -> Any:
        return self

    def children(self, name: Any = None) -> Any:
        return {}

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        for child in list(self._children):
            try:
                child.destroy()
            except Exception:  # pragma: no cover - cascade best effort
                pass
        self._children.clear()
        parent = self.master
        if isinstance(parent, _Widget) and self in parent._children:
            parent._children.remove(self)

    def __delattr__(self, name: str) -> None:
        object.__delattr__(self, name)


CLIPBOARD: dict[str, str] = {"value": ""}


class _TopLevel(_Widget):
    _tk_kind = "toplevel"

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._wm: dict[str, Any] = {}

    def title(self, value: str = None) -> Any:  # type: ignore[assignment]
        if value is None:
            return self._wm.get("title", "")
        self._wm["title"] = value

    wm_title = title

    def geometry(self, value: str = None) -> Any:  # type: ignore[assignment]
        if value is None:
            return "1200x800+0+0"
        self._wm["geometry"] = value

    def minsize(self, width: Any = None, height: Any = None) -> None:
        self._wm["minsize"] = (width, height)

    def maxsize(self, width: Any = None, height: Any = None) -> None:
        pass

    def resizable(self, width: Any = None, height: Any = None) -> None:
        pass

    def iconbitmap(self, *args: Any, **kwargs: Any) -> None:
        pass

    def iconphoto(self, *args: Any, **kwargs: Any) -> None:
        pass

    def icondefault(self, *args: Any, **kwargs: Any) -> None:
        pass

    def attributes(self, name: str = None, value: Any = None) -> Any:
        if name is None:
            return {}
        if value is None:
            return self._wm.get(f"attr:{name}", False)
        self._wm[f"attr:{name}"] = value

    def overrideredirect(self, flag: bool = False) -> None:
        self._wm["overrideredirect"] = flag

    def transient(self, master: Any = None) -> None:
        pass

    def protocol(self, name: str = None, func: Any = None) -> Any:  # type: ignore[assignment]
        if name is not None and func is not None:
            self._wm[f"protocol:{name}"] = func
        return self._wm.get(f"protocol:{name}") if name else None

    def deiconify(self) -> None:
        pass

    def withdraw(self) -> None:
        pass

    def iconify(self) -> None:
        pass

    def maxsize_or(self, *args: Any) -> None:  # pragma: no cover
        pass

    def lift(self, above: Any = None) -> None:
        pass

    def lower(self, below: Any = None) -> None:
        pass

    def state(self, value: str = None) -> Any:  # type: ignore[assignment]
        if value is None:
            return "normal"
        self._wm["state"] = value

    def tk_setPalette(self, *args: Any, **kwargs: Any) -> None:
        pass

    def report_callback_exception(self, *args: Any) -> None:
        pass


ROOT: Any = None


class Tk(_TopLevel):
    _tk_kind = "tk"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        global ROOT
        super().__init__(None, **kwargs)
        self._options_db: dict[str, Any] = {}
        self.call_log: list[tuple[Any, ...]] = []
        ROOT = self

    def option_add(self, pattern: str = "", value: Any = None, priority: Any = None) -> Any:
        if not pattern:
            return {}
        self._options_db[pattern] = value

    def option_get(self, what: Any) -> Any:
        return self._options_db.get(str(what), "")

    def option_clear(self) -> None:
        self._options_db.clear()

    def call(self, *args: Any) -> Any:
        self.call_log.append(args)
        if args and str(args[0]) in {"tk:: scaling", "winfo_scaling"}:
            return 1.0
        if args and str(args[0]) in {"tk_windowingscale", "tk scaling"}:
            return 1.3333333333
        if args and str(args[0]) == "set" and len(args) > 2:
            return args[2]
        if args and str(args[0]) == "font":
            return ("TkDefaultFont",)
        return ""

    def eval(self, script: str) -> str:
        self.call_log.append(("eval", script))
        if script.strip().startswith("set ") and " " in script.strip():
            return script.strip().split(None, 2)[-1]
        return ""

    def tk_setappstyle(self, *args: Any) -> None:
        pass

    def tk_bell(self) -> None:
        pass

    def mainloop(self, n: int = 0) -> None:
        pass

    def quit(self) -> None:
        pass

    def destroy(self) -> None:
        super().destroy()

    def update_idletasks(self) -> None:
        pass

    def wm_protocol(self, *args: Any) -> None:
        pass

    def image_create(self, *args: Any, **kwargs: Any) -> str:
        return f"image{next(_counter)}"

    def rgb_to_string(self, r: int, g: int, b: int) -> str:
        return f"#{r:02x}{g:02x}{b:02x}"

    def winfo_rgb(self, colour: Any) -> tuple[int, int, int]:
        text = str(colour or "")
        match = re.match(r"#([0-9a-f]{6})", text, re.I)
        if match:
            value = int(match.group(1), 16)
            return ((value >> 16) & 0xFF) * 257, ((value >> 8) & 0xFF) * 257, (value & 0xFF) * 257
        named = {"black": (0, 0, 0), "white": (65535, 65535, 65535)}
        return named.get(text.lower(), (0, 0, 0))

    def bind_all(self, sequence: str = "", func: Any = None, add: Any = None) -> str:
        return self.bind(sequence, func, add)


class Frame(_Widget):
    _tk_kind = "frame"


class Label(_Widget):
    _tk_kind = "label"

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)

    def cget(self, key: str) -> Any:
        return self._options.get(key, "")


class Button(_Widget):
    _tk_kind = "button"

    def invoke(self) -> Any:
        command = self._options.get("command")
        return command() if callable(command) else None


class Checkbutton(Button):
    pass


class Radiobutton(Button):
    pass


class Scrollbar(_Widget):
    _tk_kind = "scrollbar"

    def set(self, *args: Any) -> None:
        self._options["_range"] = args

    def get(self) -> tuple[float, float]:
        values = self._options.get("_range") or (0.0, 1.0)
        try:
            return float(values[0]), float(values[1])
        except (TypeError, ValueError):  # pragma: no cover
            return 0.0, 1.0


class PanedWindow(_Widget):
    def add(self, child: Any = None, **kwargs: Any) -> None:
        pass

    def pane(self, index: Any, option: str = None, value: Any = None) -> Any:  # type: ignore[assignment]
        if option is None:
            return {}
        return value if value is not None else 200

    def sash_place(self, index: int, x: int, y: int) -> None:
        pass

    def forget(self, child: Any) -> None:
        pass


# --------------------------------------------------------------------- Text
class Text(_Widget):
    """Small but real text model: enough for editing, tags and marks."""

    _tk_kind = "text"

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._buffer = ""
        self._marks: dict[str, str] = {"insert": "1.0"}
        self._gravity: dict[str, str] = {}
        self._tag_ranges: dict[str, list[list[int]]] = {}
        self._tag_options: dict[str, dict[str, Any]] = {}
        self._undo: list[tuple[str, str]] = []
        self._redo: list[tuple[str, str]] = []
        self._checkpoint = ""
        self._modified = False
        self._options.setdefault("wrap", "none")

    # ---- helpers
    def _model(self) -> TextModel:
        return TextModel(self._buffer, self._first_visible_line())

    def _offset(self, index: Any) -> int:
        return self._model().resolve(str(index), self._marks)

    def _canonical(self, offset: int) -> str:
        return self._model().canonical(max(0, min(int(offset), len(self._buffer))))

    def _notify_modified(self) -> None:
        if not self._modified:
            self._modified = True
            self.dispatch("<<Modified>>", {})

    def _shift(self, position: int, delta: int, *, inserting: bool = False) -> None:
        """Move tag ranges and marks past an insertion at *position*."""
        for ranges in self._tag_ranges.values():
            for pair in ranges:
                if pair[0] >= position:
                    pair[0] += delta
                if pair[1] >= position:
                    pair[1] += delta
            ranges[:] = [pair for pair in ranges if pair[1] > pair[0]]
        for name in list(self._marks):
            if name in ("insert",):
                continue
            offset = self._offset(self._marks[name])
            if offset >= position:
                self._marks[name] = self._canonical(offset + delta)
        if self._tag_ranges.get("sel"):
            lo, hi = self._tag_ranges["sel"][0]
            self._marks["sel.first"] = self._canonical(lo)
            self._marks["sel.last"] = self._canonical(hi)
        else:
            self._marks.pop("sel.first", None)
            self._marks.pop("sel.last", None)

    # ---- content
    def get(self, index1: Any = "1.0", index2: Any = None) -> str:
        start = self._offset(index1)
        if index2 is None:
            return self._buffer[start:start + 1]
        end = self._offset(index2)
        return self._buffer[start:end] if end >= start else self._buffer[end:start]

    def insert(self, index: Any, chars: Any = "", tags: Any = (), **kwargs: Any) -> None:
        position = self._offset(index)
        payload = str(chars)
        self._buffer = self._buffer[:position] + payload + self._buffer[position:]
        names = ((tags,) if isinstance(tags, str) else tuple(tags or ())) if tags else ()
        self._shift(position, len(payload), inserting=True)
        for name in names:
            if name:
                self._tag_ranges.setdefault(str(name), []).append([position, position + len(payload)])
        key = str(index)
        if key in self._marks or key in ("insert", "current", "anchor"):
            gravity = self._gravity.get(key, "right")
            self._marks[key] = self._canonical(position if gravity == "left" else position + len(payload))
        else:
            # inserting at an absolute index moves the caret in real Tk as well
            self._marks["insert"] = self._canonical(position + len(payload))
        self._notify_modified()

    def delete(self, index1: Any, index2: Any = None) -> None:
        start = self._offset(index1)
        end = self._offset(index2) if index2 is not None else start + 1
        if end < start:
            start, end = end, start
        removed = end - start
        if removed <= 0:
            return
        self._buffer = self._buffer[:start] + self._buffer[end:]
        for ranges in self._tag_ranges.values():
            survivors: list[list[int]] = []
            for lo, hi in ranges:
                if hi <= start or lo >= end:
                    survivors.append([lo if hi <= start else lo - removed, hi if hi <= start else hi - removed])
                else:
                    survivors.append([min(lo, start), max(min(hi, start), min(lo, start))])
            ranges[:] = [pair for pair in survivors if pair[1] > pair[0]]
        anchor = self._canonical(min(start, len(self._buffer)))
        self._marks["insert"] = anchor
        self._marks.pop("sel.first", None)
        self._marks.pop("sel.last", None)
        self._notify_modified()

    def replace(self, index1: Any, index2: Any, content: str = "") -> None:
        self.delete(index1, index2)
        self.insert(index1, content)

    def index(self, index: Any) -> str:
        return self._canonical(self._offset(index))

    def compare(self, index1: Any, op: str, index2: Any) -> int:
        a, b = self._offset(index1), self._offset(index2)
        return int({"<": a < b, "<=": a <= b, "==": a == b, "!=": a != b,
                    ">": a > b, ">=": a >= b}.get(str(op), False))

    def search(self, pattern: Any, index: Any, stopindex: Any = None, forwards: Any = None,
               backwards: Any = None, regexp: Any = None, nocase: Any = None,
               count: Any = None) -> str:
        needle = str(pattern)
        start = self._offset(index)
        stop = self._offset(stopindex) if stopindex is not None else len(self._buffer)
        backwards = bool(backwards)
        flags = (re.IGNORECASE if nocase else 0)
        if regexp:
            if backwards:
                matches = [m for m in re.finditer(needle, self._buffer[:stop], flags) if m.start() >= start]
                match = matches[-1] if matches else None
                position = match.start() if match else -1
            else:
                match = re.search(needle, self._buffer[start:stop], flags)
                position = start + match.start() if match else -1
        else:
            haystack = self._buffer[:stop] if backwards else self._buffer[start:stop]
            needle_cmp = needle.lower() if nocase else needle
            hay_cmp = haystack.lower() if nocase else haystack
            position = (haystack.rfind(needle_cmp) if backwards else haystack.find(needle_cmp))
            if position >= 0 and not backwards:
                position += start
        if position < 0:
            return ""
        if count is not None and hasattr(count, "set"):
            count.set(position + len(needle))
        return self._canonical(position)

    def count(self, index1: Any, index2: Any = None, *options: Any) -> tuple[int, ...]:
        start = self._offset(index1)
        end = self._offset(index2) if index2 is not None else start
        if end < start:
            start, end = end, start
        segment = self._buffer[start:end]
        results: list[int] = []
        for option in options or ("chars",):
            name = str(option).lstrip("-").lower()
            if name in ("chars", "c", "display chars"):
                results.append(len(segment))
            elif name in ("lines", "l", "display lines", "wrapped lines"):
                results.append(segment.count("\n") + (1 if segment else 0))
            elif name in ("words", "indices"):
                results.append(len(re.findall(r"\S+", segment)))
            else:
                results.append(len(segment))
        return tuple(results)

    # ---- marks
    def mark_set(self, name: str, index: Any) -> None:
        self._marks[str(name)] = self.index(index)

    def mark_gravity(self, name: str, direction: str = "") -> str:
        if direction:
            self._gravity[str(name)] = str(direction)
        return self._gravity.get(str(name), "right")

    def mark_unset(self, *names: Any) -> None:
        for name in names:
            self._marks.pop(str(name), None)

    def mark_next(self, index: Any) -> str:
        return ""

    def mark_previous(self, index: Any) -> str:
        return ""

    # ---- tags
    def tag_add(self, tagName: Any, index1: Any = None, index2: Any = None) -> None:
        name = str(tagName)
        if index1 is None:
            return
        lo = self._offset(index1)
        hi = self._offset(index2) if index2 is not None else lo + 1
        if hi < lo:
            lo, hi = hi, lo
        self._tag_ranges.setdefault(name, []).append([lo, max(hi, lo + 1)])
        if name == "sel":
            self._marks["sel.first"] = self._canonical(lo)
            self._marks["sel.last"] = self._canonical(hi)

    def tag_remove(self, tagName: Any = None, index1: Any = "1.0", index2: Any = "end") -> None:
        if tagName is None:
            self._tag_ranges.clear()
            return
        name = str(tagName)
        if index1 in (None, "1.0") and index2 in (None, "end"):
            self._tag_ranges.pop(name, None)
            if name == "sel":
                self._marks.pop("sel.first", None)
                self._marks.pop("sel.last", None)
            return
        start, stop = sorted((self._offset(index1), self._offset(index2)))
        kept = []
        for lo, hi in self._tag_ranges.get(name, []):
            if hi <= start or lo >= stop:
                kept.append([lo, hi])
            elif lo < start and hi > stop:
                kept.append([lo, start])
                kept.append([stop, hi])
        if kept:
            self._tag_ranges[name] = kept
        else:
            self._tag_ranges.pop(name, None)

    def tag_ranges(self, tagName: str) -> tuple[str, ...]:
        out: list[str] = []
        for lo, hi in self._tag_ranges.get(str(tagName), []):
            out.append(self._canonical(lo))
            out.append(self._canonical(hi))
        return tuple(out)

    def tag_configure(self, tagName: Any = None, cnf: Any = None, **kwargs: Any) -> Any:
        _check_options("tag_configure", {**(cnf or {}), **kwargs},
                       type(self).__name__.lower() or "text")
        if tagName is None:
            return {key: dict(value) for key, value in self._tag_options.items()}
        if cnf is None and not kwargs:
            return dict(self._tag_options.get(str(tagName), {}))
        merged = dict(cnf or {})
        merged.update(kwargs)
        self._tag_options.setdefault(str(tagName), {}).update(merged)
        return dict(self._tag_options[str(tagName)])

    tag_config = tag_configure

    def tag_names(self, index: Any = None) -> tuple[str, ...]:
        if index is None:
            return tuple(self._tag_ranges)
        position = self._offset(index)
        return tuple(name for name, ranges in self._tag_ranges.items()
                     if any(lo <= position < hi for lo, hi in ranges))

    def tag_cget(self, tagName: str, option: str) -> Any:
        return self._tag_options.get(str(tagName), {}).get(str(option), "")

    def tag_raise(self, tagName: Any = None, aboveThis: Any = None) -> None:
        pass

    def tag_lower(self, tagName: Any = None, belowThis: Any = None) -> None:
        pass

    def tag_bind(self, tagName: str, sequence: str, func: Any, add: Any = None) -> str:
        return self.bind(sequence, func, add)

    def tag_nextrange(self, tagName: str, index1: Any, index2: Any = None) -> tuple[str, str]:
        start = self._offset(index1)
        for lo, hi in self._tag_ranges.get(str(tagName), []):
            if lo >= start:
                return (self._canonical(lo), self._canonical(hi))
        return ("", "")

    def tag_prevrange(self, tagName: str, index1: Any, index2: Any = None) -> tuple[str, str]:
        stop = self._offset(index1)
        for lo, hi in reversed(self._tag_ranges.get(str(tagName), [])):
            if hi <= stop:
                return (self._canonical(lo), self._canonical(hi))
        return ("", "")

    # ---- scrolling / layout
    def see(self, index: Any) -> None:
        self._options["_seen"] = str(index)

    def _first_visible_line(self) -> int:
        return int(str(self._options.get("_topline", "1.0")).split(".")[0] or 1)

    def dlineinfo(self, index: Any) -> Optional[dict[str, int]]:
        model = self._model()
        try:
            offset = model.resolve(str(index), self._marks)
        except Exception:  # pragma: no cover - bad index
            return None
        line = model.content[:offset].count("\n") + 1
        if line < self._first_visible_line():
            return None
        return {"x": 4, "y": (line - self._first_visible_line()) * 16, "width": 700,
                "height": 16, "ascent": 12, "descent": 4}

    def bbox(self, index: Any = None) -> Optional[tuple[int, int, int, int]]:
        info = self.dlineinfo(index or "1.0")
        if info is None:
            return None
        return (info["x"], info["y"], 8, info["height"])

    def yview(self, *args: Any) -> tuple[float, float]:
        return (0.0, 0.4)

    def yview_scroll(self, number: Any, what: str) -> None:
        top = self._first_visible_line() + int(number)
        self._options["_topline"] = f"{max(1, top)}.0"

    def yview_moveto(self, fraction: Any) -> None:
        try:
            self._options["_topline"] = f"{max(1, int(float(fraction) * self._model().line_count()))}.0"
        except (TypeError, ValueError):  # pragma: no cover
            pass

    def yview_pickplace(self, *args: Any) -> None:
        pass

    def xview(self, *args: Any) -> tuple[float, float]:
        return (0.0, 1.0)

    def xview_scroll(self, number: Any, what: str) -> None:
        pass

    def xview_moveto(self, fraction: Any) -> None:
        pass

    def scan_mark(self, x: int, y: int) -> None:
        pass

    def scan_dragto(self, x: int, y: int) -> None:
        pass

    # ---- undo (checkpoint based, like Tk's edit_separator model)
    def edit_separator(self) -> None:
        if self._checkpoint != self._buffer:
            self._undo.append((self._checkpoint, self._buffer))
            self._redo.clear()
            self._checkpoint = self._buffer

    def edit_undo(self) -> str:
        if not self._undo:
            raise TclError("nothing to undo")
        before, after = self._undo.pop()
        self._redo.append((before, after))
        self._buffer = before
        self._checkpoint = before
        return ""

    def edit_redo(self) -> str:
        if not self._redo:
            raise TclError("nothing to redo")
        before, after = self._redo.pop()
        self._undo.append((before, after))
        self._buffer = after
        self._checkpoint = after
        return ""

    def edit_reset(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._checkpoint = self._buffer

    def edit_modified(self, value: Any = None) -> Any:
        if value is None:
            return int(bool(self._modified))
        self._modified = bool(int(value))
        return int(self._modified)

    def edit_redoable(self) -> int:
        return int(bool(self._redo))

    def edit_undoable(self) -> int:
        return int(bool(self._undo))

    def edit_maxsize(self, chars: Any = None) -> Any:
        return 0

    # ---- misc
    def debug(self, flag: Any = None) -> int:
        return 0

    def peer(self, name: str) -> Any:
        return None

    def content(self) -> str:  # convenience for tests
        return self._buffer

    def set_content(self, value: str) -> None:  # convenience for tests
        self._buffer = str(value)
        self._marks["insert"] = "1.0"
        self._checkpoint = self._buffer
        self._tag_ranges.clear()
        self._undo.clear()
        self._redo.clear()
        self._modified = False


class Canvas(_Widget):
    _tk_kind = "canvas"

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._items: dict[int, dict[str, Any]] = {}
        self._scroll = (0.0, 1.0)

    def _create(self, kind: str, *args: Any, **kwargs: Any) -> int:
        item_id = next(_counter)
        self._items[item_id] = {"kind": kind, "coords": args, "options": kwargs}
        return item_id

    def create_text(self, *args: Any, **kwargs: Any) -> int:
        return self._create("text", *args, **kwargs)

    def create_rectangle(self, *args: Any, **kwargs: Any) -> int:
        return self._create("rect", *args, **kwargs)

    def create_line(self, *args: Any, **kwargs: Any) -> None | int:
        return self._create("line", *args, **kwargs)

    def create_window(self, *args: Any, **kwargs: Any) -> int:
        return self._create("window", *args, **kwargs)

    def create_oval(self, *args: Any, **kwargs: Any) -> int:
        return self._create("oval", *args, **kwargs)

    def itemconfigure(self, tagOrId: Any, cnf: Any = None, **kwargs: Any) -> Any:
        _check_options("itemconfigure", {**(cnf or {}), **kwargs}, type(self).__name__.lower())
        if cnf is None and not kwargs:
            return {}
        item = self._items.get(int(tagOrId) if str(tagOrId).isdigit() else 0)
        if item is not None:
            item["options"].update(kwargs)
        return {}

    def itemconfig(self, tagOrId: Any, cnf: Any = None, **kwargs: Any) -> Any:
        return self.itemconfigure(tagOrId, cnf, **kwargs)

    def coords(self, tagOrId: Any, *coords: Any) -> Any:
        return None

    def delete(self, tagOrId: Any) -> None:
        if str(tagOrId) == "all":
            self._items.clear()
            return
        try:
            self._items.pop(int(tagOrId), None)
        except (TypeError, ValueError):  # pragma: no cover
            pass

    def bbox(self, *args: Any) -> Optional[tuple[int, int, int, int]]:
        if not self._items:
            return (0, 0, 0, 0)
        return (0, 0, 320, 30)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "scrollregion" in kwargs:
            self._options["scrollregion"] = kwargs["scrollregion"]
        return super().configure(cnf, **kwargs)

    def xview(self, *args: Any) -> tuple[float, float]:
        return self._scroll

    def yview(self, *args: Any) -> tuple[float, float]:
        return self._scroll

    def xview_scroll(self, number: Any, what: str) -> None:
        pass

    def xview_moveto(self, fraction: Any) -> None:
        pass

    def yview_scroll(self, number: Any, what: str) -> None:
        pass

    def yview_moveto(self, fraction: Any) -> None:
        pass

    def scale(self, *args: Any) -> None:
        pass

    def find_closest(self, *args: Any) -> tuple[()]:
        return ()

    def tag_bind(self, tagOrId: Any, sequence: str, func: Any, add: Any = None) -> str:
        return ""


class Listbox(_Widget):
    _tk_kind = "listbox"

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._items: list[str] = []
        self._selection: list[int] = []

    def insert(self, index: Any, *items: Any) -> None:
        position = len(self._items) if str(index) == "end" else max(0, int(index))
        for offset, item in enumerate(items):
            self._items.insert(position + offset, str(item))

    def delete(self, first: Any = 0, last: Any = None) -> None:
        if str(first) == "end":
            self._items.clear()
            return
        start = int(first)
        end = len(self._items) if last in (None, "end") else int(last) + 1
        del self._items[start:end]

    def size(self) -> int:
        return len(self._items)

    def get(self, first: Any = 0, last: Any = None) -> Any:
        if last is None:
            index = int(first)
            return self._items[index] if 0 <= index < len(self._items) else None
        return self._items[int(first):int(last) + 1]

    def curselection(self) -> tuple[int, ...]:
        return tuple(self._selection)

    def selection_set(self, first: Any, last: Any = None) -> None:
        self._selection = [int(first)]

    def selection_clear(self, first: Any = 0, last: Any = None) -> None:
        self._selection = []

    def see(self, index: Any) -> None:
        pass

    def activate(self, index: Any) -> None:
        pass

    def nearest(self, y: Any) -> int:
        return 0

    def yview(self, *args: Any) -> tuple[float, float]:
        return (0.0, 1.0)

    def exportselection(self, flag: Any = None) -> None:
        pass


class Menu(_Widget):
    _tk_kind = "menu"

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self.entries: list[dict[str, Any]] = []

    def add(self, kind: str, cnf: Optional[dict[str, Any]] = None, **kwargs: Any) -> int:
        merged = dict(cnf or {})
        merged.update(kwargs)
        _check_options(f"add_{kind}" if not str(kind).startswith("add_") else str(kind), merged, "menu")
        entry = {"kind": kind}
        entry.update(cnf or {})
        entry.update(kwargs)
        self.entries.append(entry)
        return len(self.entries) - 1

    def add_command(self, cnf: Optional[dict[str, Any]] = None, **kwargs: Any) -> int:
        return self.add("command", cnf, **kwargs)

    def add_cascade(self, cnf: Optional[dict[str, Any]] = None, **kwargs: Any) -> int:
        return self.add("cascade", cnf, **kwargs)

    def add_checkbutton(self, cnf: Optional[dict[str, Any]] = None, **kwargs: Any) -> int:
        return self.add("checkbutton", cnf, **kwargs)

    def add_radiobutton(self, cnf: Optional[dict[str, Any]] = None, **kwargs: Any) -> int:
        return self.add("radiobutton", cnf, **kwargs)

    def add_separator(self, cnf: Optional[dict[str, Any]] = None, **kwargs: Any) -> int:
        return self.add("separator", cnf, **kwargs)

    def entryconfigure(self, index: Any = None, cnf: Any = None, **kwargs: Any) -> Any:
        if isinstance(index, (int, str)) and str(index).isdigit() and index is not None and kwargs:
            entry = self.entries[int(index)]
            entry.update(kwargs)
            return entry
        return {}

    entryconfig = entryconfigure

    def delete(self, first: Any = 0, last: Any = None) -> None:
        self.entries.clear()

    def index(self, item: Any) -> Optional[int]:
        if isinstance(item, str) and item in ("last", "end"):
            return len(self.entries) - 1
        for position, entry in enumerate(self.entries):
            if str(entry.get("label", "")) == str(item):
                return position
        return None

    def type(self, index: Any) -> str:
        try:
            return str(self.entries[int(index)]["kind"])
        except (ValueError, IndexError, KeyError):  # pragma: no cover
            return ""

    def invoke(self, label_or_index: Any) -> Any:
        """Run a menu entry (by index or label) - used by the tests."""
        index: Optional[int] = None
        if isinstance(label_or_index, int):
            index = label_or_index
        else:
            index = self.index(label_or_index)
        if index is None or index >= len(self.entries):
            return None
        entry = self.entries[index]
        command = entry.get("command")
        if callable(command) and str(entry.get("state", "normal")) == "normal":
            return command()
        return None

    def labels(self) -> list[str]:
        return [str(entry.get("label", "")) for entry in self.entries]

    def post(self, x: int, y: int) -> None:
        pass

    def unpost(self) -> None:
        pass

    def tk_popup(self, x: int, y: int, *args: Any) -> None:
        pass

    def insert_cascade(self, index: Any, **kwargs: Any) -> None:
        pass


class Toplevel(_TopLevel):
    pass


class ToplevelFrame(Frame):
    pass


class Misc(_Widget):
    pass


class Variable(_Widget):
    def __init__(self, master: Any = None, name: str = None, value: Any = None, **kwargs: Any) -> None:  # type: ignore[assignment]
        super().__init__(master, **kwargs)
        self._value = value
        self._traces: dict[str, Callable[..., Any]] = {}

    def get(self) -> Any:
        return self._value

    def set(self, value: Any) -> None:
        old = self._value
        self._value = value
        if old != value:
            for callback in list(self._traces.values()):
                try:
                    callback(self._id, old, "write")
                except (TypeError, Exception):  # pragma: no cover
                    pass

    def initialize(self, value: Any) -> None:
        self._value = value

    def trace_add(self, mode: str, callback: Callable[..., Any]) -> str:
        ident = f"trace{next(_counter)}"
        self._traces[ident] = callback
        return ident

    def trace(self, mode: str = "write", callback: Any = None) -> str:
        return self.trace_add(mode, callback) if callback else ""

    def trace_remove(self, mode: str, traceid: str) -> None:
        self._traces.pop(traceid, None)

    def trace_variable(self, mode: str, callback: Callable[..., Any]) -> str:
        return self.trace_add(mode, callback)

    def trace_vdelete(self, mode: str, traceid: str) -> None:
        self._traces.pop(traceid, None)

    def info(self) -> str:
        return "variable"


class StringVar(Variable):
    def __init__(self, master: Any = None, *, value: str = "", **kwargs: Any) -> None:
        super().__init__(master, value=value, **kwargs)

    def get(self) -> str:
        return "" if self._value is None else str(self._value)

    def set(self, value: Any) -> None:
        super().set("" if value is None else str(value))


class IntVar(Variable):
    def __init__(self, master: Any = None, *, value: int = 0, **kwargs: Any) -> None:
        super().__init__(master, value=int(value), **kwargs)

    def get(self) -> int:
        try:
            return int(self._value)
        except (TypeError, ValueError):  # pragma: no cover
            return 0

    def set(self, value: Any) -> None:
        try:
            super().set(int(value))
        except (TypeError, ValueError):  # pragma: no cover
            super().set(0)


class BooleanVar(Variable):
    def __init__(self, master: Any = None, *, value: bool = False, **kwargs: Any) -> None:
        super().__init__(master, value=bool(value), **kwargs)

    def get(self) -> bool:
        return bool(self._value)

    def set(self, value: Any) -> None:
        super().set(bool(value))


class DoubleVar(Variable):
    def __init__(self, master: Any = None, *, value: float = 0.0, **kwargs: Any) -> None:
        super().__init__(master, value=float(value), **kwargs)

    def get(self) -> float:
        try:
            return float(self._value)
        except (TypeError, ValueError):  # pragma: no cover
            return 0.0


class TclError(Exception):
    """Fake ``tkinter.TclError``."""


class _Font:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.options = dict(kwargs)
        self.options.setdefault("size", 10)
        self.options.setdefault("family", "TkDefaultFont")

    def actual(self, option: str = None, *args: Any, **kwargs: Any) -> Any:  # type: ignore[assignment]
        if option is None:
            return dict(self.options)
        return self.options.get(str(option), "")

    def cget(self, key: str) -> Any:
        return self.options.get(key, "")

    def config(self, **kwargs: Any) -> None:
        self.options.update(kwargs)

    configure = config

    def measure(self, text: str) -> int:
        return max(1, int(len(str(text)) * 7 * (abs(int(self.options.get("size", 10))) / 10.0)))

    def metrics(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        size = abs(int(self.options.get("size", 10)))
        return {"linespace": size + 4, "ascent": size + 1, "descent": 3, "fixed": 0}


# --------------------------------------------------------------------- ttk
class _TtkWidget(_Widget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)


class Style(_TtkWidget):
    def configure(self, style: Any = None, query: Any = None, **kwargs: Any) -> Any:
        if style is None:
            return super().configure(query, **kwargs) if query else None
        return {}

    def theme_use(self, name: str = None) -> Any:  # type: ignore[assignment]
        if name is None:
            return "alt"
        return None

    def theme_names(self) -> tuple[str, ...]:
        return ("alt", "default")

    def layout(self, style: str, theme: str = None) -> list[Any]:  # type: ignore[assignment]
        return []

    def element_names(self) -> tuple[str, ...]:
        return ()

    def element_options(self, element: str) -> tuple[str, ...]:
        return ()

    def map(self, style: str, **kwargs: Any) -> Any:
        return {}

    def lookup(self, style: str, option: str, *args: Any, **kwargs: Any) -> Any:
        return ""

    def element_create(self, *args: Any, **kwargs: Any) -> None:
        pass

    def layout(self, style: str, theme: str = None, *args: Any, **kwargs: Any) -> Any:  # type: ignore[assignment]
        return []

    def __getattr__(self, name: str) -> Any:
        # any other ttk.Style call is a no-op in the stub
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None
        return _noop


class Treeview(_TtkWidget):
    """Node table with the subset of the Treeview API used by the app."""

    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._nodes: dict[str, dict[str, Any]] = {
            "": {"text": "", "values": (), "tags": (), "children": [], "parent": "", "open": True},
        }
        self._ids = itertools.count(1)
        self._selection: list[str] = []
        self._columns = tuple(options.get("columns") or ())

    # ---- model
    def insert(self, parent: Any = "", index: Any = "end", iid: Any = None, **kwargs: Any) -> str:
        _check_options("insert", kwargs, "tree")
        parent = "" if parent is None else str(parent)
        if parent not in self._nodes:
            parent = ""
        node_id = str(iid) if iid else f"I{next(self._ids)}"
        node = {
            "text": kwargs.get("text", ""),
            "values": tuple(kwargs.get("values") or ()),
            "tags": tuple(kwargs.get("tags") or ()),
            "children": [],
            "parent": parent,
            "open": bool(kwargs.get("open", False)),
        }
        self._nodes[node_id] = node
        siblings = self._nodes[parent]["children"]
        if index == "end" or index is None or str(index) == "end":
            siblings.append(node_id)
        else:
            try:
                siblings.insert(int(index), node_id)
            except (ValueError, IndexError):  # pragma: no cover
                siblings.append(node_id)
        return node_id

    def delete(self, *items: Any) -> None:
        for item in items:
            node = self._nodes.pop(str(item), None)
            if node is None:
                continue
            parent = self._nodes.get(node["parent"])
            if parent is not None and str(item) in parent["children"]:
                parent["children"].remove(str(item))
            if str(item) in self._selection:
                self._selection.remove(str(item))

    def item(self, item: Any, option: Any = None, value: Any = None, **kwargs: Any) -> Any:
        _check_options("item", kwargs, "tree")
        node = self._nodes.get(str(item))
        if node is None:
            return {} if option is None else ""
        if option is None and not kwargs:
            return dict(node)
        updates = dict(kwargs)
        if option is not None and value is not None:
            updates[str(option)] = value
        elif isinstance(option, dict):
            updates.update(option)
        elif option is not None and value is None and not kwargs:
            return node.get(str(option), "")
        node.update(updates)
        return dict(node)

    def set(self, item: Any, column: Any = None, value: Any = None) -> Any:
        node = self._nodes.get(str(item))
        if node is None:
            return ""
        if value is None and column is None:
            return {"#0": node["text"], **dict(zip(self._columns, node["values"]))}
        if value is None:
            if str(column) in ("", "#0"):
                return node["text"]
            return dict(zip(self._columns, node["values"])).get(str(column), "")
        if str(column) in ("", "#0"):
            node["text"] = value
        else:
            values = list(node["values"])
            try:
                position = list(self._columns).index(str(column))
            except ValueError:  # pragma: no cover
                return ""
            while len(values) <= position:
                values.append("")
            values[position] = value
            node["values"] = tuple(values)
        return value

    def children(self, item: Any = None) -> tuple[str, ...]:
        node = self._nodes.get("" if item is None else str(item))
        return tuple(node["children"]) if node else ()

    def get_children(self, item: Any = "") -> tuple[str, ...]:
        return self.children(item if item is not None else "")

    def parent(self, item: Any) -> str:
        node = self._nodes.get(str(item))
        return node["parent"] if node else ""

    def index(self, item: Any) -> int:
        parent = self._nodes.get(self.parent(item))
        if parent is None:
            return 0
        try:
            return list(parent["children"]).index(str(item))
        except ValueError:  # pragma: no cover
            return 0

    def exists(self, item: Any) -> int:
        return int(str(item) in self._nodes)

    def detach(self, item: Any) -> None:
        node = self._nodes.get(str(item))
        if node is None:
            return
        parent = self._nodes.get(node["parent"])
        if parent is not None and str(item) in parent["children"]:
            parent["children"].remove(str(item))

    def reattach(self, item: Any, newParent: Any = "", index: Any = "end") -> None:
        node = self._nodes.get(str(item))
        if node is None:
            return
        old = self._nodes.get(node["parent"])
        if old is not None and str(item) in old["children"]:
            old["children"].remove(str(item))
        newParent = "" if newParent in (None, "") else str(newParent)
        target = self._nodes.setdefault(newParent, {"children": [], "parent": "", "text": "", "values": (),
                                                     "tags": (), "open": True})
        if index in ("end", None):
            target["children"].append(str(item))
        else:
            try:
                target["children"].insert(int(index), str(item))
            except (ValueError, IndexError):  # pragma: no cover
                target["children"].append(str(item))
        node["parent"] = newParent

    def move(self, item: Any, parent: Any, index: Any) -> None:
        self.reattach(item, parent, index)

    # ---- view
    def heading(self, column: Any, option: Any = None, **kwargs: Any) -> Any:
        _check_options("heading", kwargs, "tree")
        key = f"heading:{column}:{option}"
        if option is None and not kwargs:
            return {"text": self._options.get(f"heading:{column}", ""), "command": None}
        if option is not None and not kwargs:
            return self._options.get(key, "")
        for name, value in kwargs.items():
            self._options[f"heading:{column}:{name}"] = value
            if name == "text":
                self._options[f"heading:{column}"] = value
        return None

    def column(self, column: Any = None, option: Any = None, **kwargs: Any) -> Any:
        _check_options("column", kwargs, "tree")
        if column is None:
            return {}
        if option is None and not kwargs:
            return {"id": str(column), "width": 100, "anchor": "w", "minwidth": 0, "stretch": True}
        if option is not None and not kwargs:
            return self._options.get(f"column:{column}:{option}", 100)
        for name, value in kwargs.items():
            self._options[f"column:{column}:{name}"] = value
        return None

    def tag_configure(self, tag: str = None, option: Any = None, **kwargs: Any) -> Any:
        if tag is None:
            return {}
        if option is not None and not kwargs:
            return self._options.get(f"tag:{tag}:{option}", "")
        for name, value in kwargs.items():
            self._options[f"tag:{tag}:{name}"] = value
        return dict(self._options)

    tag_config = tag_configure

    def selection_set(self, items: Any) -> None:
        if isinstance(items, (list, tuple)):
            self._selection = [str(item) for item in items if str(item) in self._nodes]
        else:
            self._selection = [str(items)] if str(items) in self._nodes else []
        for callback in self._bindings.get("<<TreeviewSelect>>", []):
            callback(FakeEvent(self))

    def selection(self) -> tuple[str, ...]:
        return tuple(self._selection)

    def selection_add(self, items: Any) -> None:
        for item in items if isinstance(items, (list, tuple)) else [items]:
            if str(item) in self._nodes and str(item) not in self._selection:
                self._selection.append(str(item))

    def selection_remove(self, item: Any) -> None:
        if str(item) in self._selection:
            self._selection.remove(str(item))

    def see(self, item: Any) -> None:
        pass

    def identify_row(self, y: Any) -> str:
        visible = list(self._nodes[""]["children"])
        for node_id, node in self._nodes.items():
            visible.extend(node["children"])
        try:
            position = int(int(y) // 16)
        except (TypeError, ValueError):  # pragma: no cover
            return ""
        rows = [item for item in visible if item in self._nodes]
        return rows[position] if 0 <= position < len(rows) else ""

    def identify_column(self, x: Any) -> str:
        return "#0"

    def identify_region(self, x: Any, y: Any) -> str:
        return "heading" if int(y) < 24 else "tree"

    def identify(self, region: str, index_or_x: Any, y: Any = None) -> Any:
        if region == "children":
            return None
        return None

    def focus(self, item: Any = None) -> Any:
        if item is None:
            return self._selection[0] if self._selection else ""
        return None

    def yview(self, *args: Any) -> tuple[float, float]:
        return (0.0, 1.0)

    def yview_scroll(self, number: Any, what: str) -> None:
        pass

    def yview_moveto(self, fraction: Any) -> None:
        pass

    def xview(self, *args: Any) -> tuple[float, float]:
        return (0.0, 1.0)

    def xview_scroll(self, number: Any, what: str) -> None:
        pass

    def bbox(self, item: Any) -> Optional[tuple[int, int, int, int]]:
        return (0, 0, 200, 20)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "columns" in kwargs:
            self._columns = tuple(kwargs["columns"])
        return super().configure(cnf, **kwargs)


class Notebook(_TtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._tabs: list[tuple[str, Any, dict[str, Any]]] = []

    def add(self, child: Any = None, **kwargs: Any) -> None:
        title = str(kwargs.get("text") or kwargs.get("sticky") or f"tab{len(self._tabs) + 1}")
        self._tabs.append((title, child, kwargs))

    def forget(self, tab_id: Any) -> None:
        self._tabs = [entry for entry in self._tabs if entry[1] is not tab_id]

    def index(self, tab_id: Any = None) -> int:
        return 0

    def tabs(self) -> tuple[Any, ...]:
        return tuple(child for _title, child, _kwargs in self._tabs)

    def tab(self, tab_id: Any = None, option: str = None, value: Any = None, **kwargs: Any) -> Any:
        if option is None and not kwargs:
            return {"text": self._tabs[0][0] if self._tabs else "", "sticky": "nesw"}
        return value if value is not None else ""

    def select(self, tab_id: Any = None) -> Any:
        if tab_id is None:
            return str(self._tabs[0][1]) if self._tabs else ""
        return None

    def hide(self, tab_id: Any) -> None:
        pass

    def enable(self, tab_id: Any) -> None:
        pass

    def entryconfig(self, tab_id: Any, **kwargs: Any) -> Any:
        return {}

    def insert(self, pos: Any, child: Any = None, **kwargs: Any) -> None:
        self.add(child, **kwargs)


class Separator(_TtkWidget):
    pass


class Sizegrip(_TtkWidget):
    pass


class ScrollbarTtk(Scrollbar, _TtkWidget):
    pass


class Combobox(Listbox, _TtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._value = ""

    def set(self, value: Any) -> None:
        self._value = str(value)

    def get(self) -> str:
        return self._value

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "values" in kwargs:
            self._items = [str(v) for v in kwargs["values"]]
        return super().configure(cnf, **kwargs)

    def bind(self, sequence: str = "", func: Any = None, add: Any = None) -> str:
        return _Widget.bind(self, sequence, func, add)


class Progressbar(_TtkWidget):
    def start(self, interval: int = 50) -> None:
        pass

    def step(self, amount: int = 1) -> None:
        pass

    def stop(self) -> None:
        pass


# --------------------------------------------------------------- tk namespace
class _TkModule(types.ModuleType):
    """``tkinter`` replacement exposing only what the app needs."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - safety net
        raise AttributeError(f"tkinter stub: unsupported attribute {name!r}")


class _TTKModule(types.ModuleType):
    Style = Style
    Treeview = Treeview
    Notebook = Notebook
    Separator = Separator
    Sizegrip = Sizegrip
    Scrollbar = ScrollbarTtk
    Combobox = Combobox
    Progressbar = Progressbar
    Frame = _TtkWidget
    Label = _TtkWidget
    Button = _TtkWidget


class _FontModule(types.ModuleType):
    Font = _Font
    FontExists = _Font
    families = staticmethod(lambda *a, **k: ["TkDefaultFont"])
    actual = staticmethod(lambda *a, **k: {})
    names = staticmethod(lambda *a, **k: ["TkDefaultFont"])


class _FileDialogModule(types.ModuleType):
    """Returns scripted answers so tests can simulate the user picking files."""

    answers: list[Any] = []
    default_answer = ""

    @classmethod
    def _answer(cls) -> Any:
        if cls.answers:
            return cls.answers.pop(0)
        return cls.default_answer

    def askopenfilename(self, **kwargs: Any) -> str:
        return str(self._answer() or "")

    def askopenfilenames(self, **kwargs: Any) -> tuple[str, ...]:
        value = self._answer()
        if isinstance(value, (list, tuple)):
            return tuple(str(v) for v in value)
        return (str(value),) if value else ()

    def asksaveasfilename(self, **kwargs: Any) -> str:
        return str(self._answer() or "")

    def askdirectory(self, **kwargs: Any) -> str:
        value = self._answer()
        if isinstance(value, (list, tuple)):
            value = value[0]
        return str(value or "")

    def askopenfile(self, **kwargs: Any) -> Any:
        return None

    def asksaveasfile(self, **kwargs: Any) -> Any:
        return None


class _MessageBoxModule(types.ModuleType):
    """Scripted answers, keyed by function name; default 'ok'."""

    answers: dict[str, Any] = {}
    log: list[tuple[str, dict[str, Any]]] = []

    def _reply(self, name: str, kwargs: dict[str, Any]) -> Any:
        self.log.append((name, dict(kwargs)))
        if name in self.answers:
            value = self.answers[name]
            return value() if callable(value) else value
        if "*" in self.answers:
            value = self.answers["*"]
            return value() if callable(value) else value
        return True if name == "askyesno" else ("yes" if name == "askyesnocancel" else "ok")

    def showinfo(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("showinfo", kwargs or {"message": args})

    def showwarning(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("showwarning", kwargs or {"message": args})

    def showerror(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("showerror", kwargs or {"message": args})

    def askyesno(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("askyesno", kwargs or {"message": args})

    def askyesnocancel(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("askyesnocancel", kwargs or {"message": args})

    def askokcancel(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("askokcancel", kwargs or {"message": args})

    def askretrycancel(self, *args: Any, **kwargs: Any) -> Any:
        return self._reply("askretrycancel", kwargs or {"message": args})


class _ColorChooserModule(types.ModuleType):
    def askcolor(self, *args: Any, **kwargs: Any) -> tuple[None, None]:
        return (None, None)


# --------------------------------------------------------- customtkinter stub
class _CtkWidget(_Widget):
    """Base for every CustomTkinter widget in the stub."""

    def __init__(self, master: Any = None, **options: Any) -> None:
        options.setdefault("fg_color", "#242424")
        super().__init__(master, **options)
        self._text_value = str(options.get("text", "") or "")
        self._state = str(options.get("state", "normal"))

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        # CustomTkinter widgets are configured with keyword arguments only, so a
        # kwargs-only call must not be mistaken for the "query every option" form.
        if cnf is None and kwargs:
            cnf = dict(kwargs)
        if isinstance(cnf, dict):
            self._options.update(cnf)
            cnf = None
        if "text" in kwargs:
            self._text_value = str(kwargs["text"])
        if "state" in kwargs:
            self._state = str(kwargs["state"])
        if cnf is None:
            if not kwargs:
                return {key: (key, key, "", "", value) for key, value in self._options.items()}
            self._options.update(kwargs)
            return None
        return super().configure(cnf, **kwargs)

    config = configure

    def cget(self, key: str) -> Any:
        if key == "text":
            return self._text_value
        if key == "state":
            return self._state
        return self._options.get(key, "")

    def get(self) -> Any:
        return self._options.get("_value", self._text_value)

    def set(self, value: Any) -> None:
        self._options["_value"] = value
        if isinstance(self, (CTkButton, CTkLabel, CTkCheckBox, CTkSwitch)):
            self._text_value = str(value)

    def focus(self) -> None:  # pragma: no cover - parity with CTk
        pass


class CTk(Tk, _CtkWidget):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        Tk.__init__(self, *args, **kwargs)


class CTkToplevel(Toplevel, _CtkWidget):
    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        Toplevel.__init__(self, master, **kwargs)


class CTkFrame(_CtkWidget):
    pass


class CTkLabel(_CtkWidget):
    pass


class CTkButton(_CtkWidget):
    def invoke(self) -> Any:
        command = self._options.get("command")
        return command() if callable(command) else None


class CTkEntry(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._buffer = str(options.get("text", "") or "")
        self._insert = len(self._buffer)

    def get(self) -> str:
        return self._buffer

    def set(self, value: Any) -> None:
        self._buffer = "" if value is None else str(value)

    def insert(self, index: Any, text: Any) -> None:
        position = len(self._buffer) if str(index) == "end" else max(0, int(index))
        self._buffer = self._buffer[:position] + str(text) + self._buffer[position:]

    def delete(self, first: Any = 0, last: Any = None) -> None:
        start = int(first)
        end = len(self._buffer) if last in (None, "end") else int(last)
        self._buffer = self._buffer[:start] + self._buffer[end:]

    def select_range(self, start: Any, end: Any) -> None:
        pass

    def icursor(self, index: Any = None) -> None:
        pass

    def bbox(self, index: Any = None) -> tuple[int, int, int, int]:
        return (0, 0, 100, 20)

    def scan_mark(self, x: int, y: int) -> None:
        pass

    def scan_dragto(self, x: int, y: int) -> None:
        pass


    def __getattr__(self, name: str) -> Any:
        # CTkEntry proxies unknown methods to the underlying tk.Entry
        def _proxy(*args: Any, **kwargs: Any) -> Any:
            return None
        return _proxy


class CTkTextbox(Text, _CtkWidget):
    """CTkTextbox is Text-like; reuse the stub's text model."""

    def __init__(self, master: Any = None, **options: Any) -> None:
        Text.__init__(self, master, **options)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        return Text.configure(self, cnf, **kwargs)

    def cget(self, key: str) -> Any:
        return Text.cget(self, key)


class CTkCheckBox(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        self._variable = options.pop("variable", None)
        super().__init__(master, **options)
        if self._variable is None:
            self._variable = BooleanVar(value=False)

    def get(self) -> Any:
        return self._variable.get()

    def set(self, value: Any) -> None:
        self._variable.set(value)

    def select(self) -> None:
        self._variable.set(True)

    def deselect(self) -> None:
        self._variable.set(False)

    def toggle(self) -> None:
        self._variable.set(not bool(self._variable.get()))
        command = self._options.get("command")
        if callable(command):
            command()

    def invoke(self) -> Any:
        self.toggle()
        return None


class CTkSwitch(CTkCheckBox):
    pass


class CTkRadioButton(CTkCheckBox):
    pass


class CTkSlider(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._value = float(options.get("number_of_steps", 0) or 0)
        self._value = 0.5

    def get(self) -> float:
        return self._value

    def set(self, value: Any, from_user: bool = False) -> None:
        try:
            self._value = float(value)
        except (TypeError, ValueError):  # pragma: no cover
            pass


class CTkProgressBar(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._value = 0.0
        self._running = False

    def start(self, interval: int = 50) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def step(self, amount: int = 1) -> None:
        self._value = min(1.0, self._value + 0.05)

    def get(self) -> float:
        return self._value

    def set(self, value: Any) -> None:
        try:
            self._value = float(value)
        except (TypeError, ValueError):  # pragma: no cover
            pass

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "mode" in kwargs:
            self._options["mode"] = kwargs["mode"]
        if value := kwargs.get("progress"):
            self.set(value)
        return super().configure(cnf, **kwargs)


class CTkOptionMenu(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        values = list(options.get("values") or [])
        self._values = values
        self._value = values[0] if values else ""

    def get(self) -> Any:
        return self._value

    def set(self, value: Any) -> None:
        self._value = value

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "values" in kwargs:
            self._values = list(kwargs["values"])
            if not self._value and self._values:
                self._value = self._values[0]
        return super().configure(cnf, **kwargs)

    def cget(self, key: str) -> Any:
        if key == "values":
            return self._values
        return super().cget(key)

    def invoke(self, value: Any = None) -> Any:
        """Simulate choosing *value* (or the current one)."""
        chosen = self._value if value is None else value
        self._value = chosen
        command = self._options.get("command")
        if callable(command):
            return command(chosen)
        return None


class CTkComboBox(CTkOptionMenu):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._entry = CTkEntry(master, text=self._value)

    def get(self) -> Any:
        return self._entry.get() or self._value

    def set(self, value: Any) -> None:
        self._value = value
        self._entry.set(value)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "values" in kwargs:
            self._values = list(kwargs["values"])
        return super().configure(cnf, **kwargs)


class CTkScrollbar(_Widget):
    def set(self, *args: Any) -> None:
        self._options["_range"] = args

    def get(self) -> tuple[float, float]:
        return (0.0, 1.0)


class CTkScrollableFrame(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        options.pop("label_text", None)
        super().__init__(master, **options)
        self._canvas = Canvas(self)

    def _bind_mousewheel_event(self) -> None:
        pass

    def unbind_mousewheel_event(self) -> None:
        pass

    def get_scrollbar(self) -> Any:
        return CTkScrollbar(self)

    def see(self, widget: Any) -> None:
        pass


class CTkTabview(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._segments: dict[str, CTkFrame] = {}
        self._current = ""

    def add(self, name: str) -> CTkFrame:
        if name not in self._segments:
            self._segments[str(name)] = CTkFrame(self, fg_color="transparent")
            if not self._current:
                self._current = str(name)
        return self._segments[str(name)]

    def tab(self, name: str = None) -> Any:
        if name is None:
            return self._segments.get(self._current)
        return self._segments.get(str(name))

    def set(self, name: Any) -> None:
        key = str(name)
        if key in self._segments:
            self._current = key
            self._options["_value"] = key
            command = self._options.get("command")
            if callable(command):
                try:
                    command(key)
                except TypeError:  # pragma: no cover
                    pass

    def get(self) -> str:
        return self._current

    def show(self, name: Any) -> None:
        self.set(name)

    def insert(self, index: Any, name: str) -> CTkFrame:
        return self.add(name)

    def list(self) -> list[str]:
        return list(self._segments)

    def grid(self, **kwargs: Any) -> None:
        _Widget.grid(self, **kwargs)


class CTkSegmentedButton(_CtkWidget):
    def __init__(self, master: Any = None, **options: Any) -> None:
        super().__init__(master, **options)
        self._values = list(options.get("values") or [])
        self._selected = self._values[0] if self._values else ""

    def add(self, value: Any) -> None:
        if str(value) not in self._values:
            self._values.append(str(value))

    def remove(self, value: Any) -> None:
        if str(value) in self._values:
            self._values.remove(str(value))

    def set(self, value: Any) -> None:
        self._selected = str(value)

    def get(self) -> str:
        return self._selected

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "values" in kwargs:
            self._values = list(kwargs["values"])
        return super().configure(cnf, **kwargs)


class CTkFont(_Font):
    def __init__(self, size: int = 13, weight: str = "normal", slant: str = "roman",
                 family: str = "Segoe UI", underline: int = 0, **kwargs: Any) -> None:
        super().__init__(size=size, weight=weight, slant=slant, family=family,
                         underline=underline, **kwargs)


class CTkImage(_Widget):
    def __init__(self, light_image: Any = None, dark_image: Any = None,
                 size: tuple[int, int] = (32, 32), **kwargs: Any) -> None:
        super().__init__(None, **kwargs)
        self.light_image = light_image
        self.dark_image = dark_image
        self.size = size


class CTkInputDialog(_CtkWidget):
    result = ""

    def get_input(self) -> str:
        return str(type(self).result)


class CTkMenubar(_CtkWidget):
    pass


class _CtkModule(types.ModuleType):
    """``customtkinter`` replacement (all widgets are the stub classes above)."""

    CTk = CTk
    CTkToplevel = CTkToplevel
    CTkTk = CTk
    CTkFrame = CTkFrame
    CTkLabel = CTkLabel
    CTkButton = CTkButton
    CTkEntry = CTkEntry
    CTkTextbox = CTkTextbox
    CTkCheckBox = CTkCheckBox
    CTkSwitch = CTkSwitch
    CTkRadioButton = CTkRadioButton
    CTkSlider = CTkSlider
    CTkProgressBar = CTkProgressBar
    CTkOptionMenu = CTkOptionMenu
    CTkComboBox = CTkComboBox
    CTkScrollbar = CTkScrollbar
    CTkScrollableFrame = CTkScrollableFrame
    CTkTabview = CTkTabview
    CTkSegmentedButton = CTkSegmentedButton
    CTkFont = CTkFont
    CTkImage = CTkImage
    CTkInputDialog = CTkInputDialog
    CTkMenubar = CTkMenubar
    TclError = TclError

    appearance_mode = "Dark"
    color_theme = "blue"
    DPI_SCALING = 1.0

    def set_appearance_mode(self, mode: str) -> None:
        type(self).appearance_mode = str(mode)

    def set_default_color_theme(self, theme: Any) -> None:
        type(self).color_theme = str(theme)

    def deactivate_automatic_dpi_awareness(self) -> None:
        pass

    def set_widget_scaling(self, scaling_factor: float) -> None:
        pass

    def set_window_scaling(self, scaling_factor: float) -> None:
        pass

    def reset_widget_scaling(self) -> None:
        pass

    def set_widget_geometry_positions(self, mode: str) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(f"customtkinter stub: unsupported attribute {name!r}")


# --------------------------------------------------------------------- dialogs
class DialogQueue:
    """Script answers for the app's own modal dialogs (StudioDialog.run())."""

    def __init__(self) -> None:
        self.answers: list[Any] = []
        self.seen: list[dict[str, Any]] = []

    def push(self, *values: Any) -> None:
        self.answers.extend(values)

    def next_answer(self) -> Any:
        if self.answers:
            return self.answers.pop(0)
        return None

    def clear(self) -> None:
        self.answers.clear()
        self.seen.clear()


DIALOGS = DialogQueue()


def next_dialog_answer(value: Any = None) -> None:
    """Queue a reply for the next modal dialog."""
    DIALOGS.push(value)


# ------------------------------------------------------------------ install()
def install(*, force: bool = False) -> None:
    """Put the fakes into :data:`sys.modules` (idempotent)."""
    global _INSTALLED, ROOT
    if _INSTALLED and not force:
        return

    tkinter = _TkModule("tkinter")
    tkinter.Tk = Tk                                   # type: ignore[attr-defined]
    tkinter.Toplevel = Toplevel                       # type: ignore[attr-defined]
    tkinter.Frame = Frame                             # type: ignore[attr-defined]
    tkinter.Label = Label                             # type: ignore[attr-defined]
    tkinter.Button = Button                           # type: ignore[attr-defined]
    tkinter.Checkbutton = Checkbutton                 # type: ignore[attr-defined]
    tkinter.Radiobutton = Radiobutton                 # type: ignore[attr-defined]
    tkinter.Text = Text                               # type: ignore[attr-defined]
    tkinter.Canvas = Canvas                           # type: ignore[attr-defined]
    tkinter.Listbox = Listbox                         # type: ignore[attr-defined]
    tkinter.Menu = Menu                               # type: ignore[attr-defined]
    tkinter.Scrollbar = Scrollbar                     # type: ignore[attr-defined]
    tkinter.PanedWindow = PanedWindow                 # type: ignore[attr-defined]
    tkinter.Misc = Misc                               # type: ignore[attr-defined]
    tkinter.TclError = TclError                       # type: ignore[attr-defined]
    tkinter.StringVar = StringVar                     # type: ignore[attr-defined]
    tkinter.IntVar = IntVar                           # type: ignore[attr-defined]
    tkinter.BooleanVar = BooleanVar                   # type: ignore[attr-defined]
    tkinter.DoubleVar = DoubleVar                     # type: ignore[attr-defined]
    tkinter.Variable = Variable                       # type: ignore[attr-defined]
    tkinter.Event = FakeEvent                         # type: ignore[attr-defined]
    tkinter.Wm = _TopLevel                            # type: ignore[attr-defined]
    tkinter._defaultroot = lambda: ROOT               # type: ignore[attr-defined]
    tkinter.END = "end"                               # type: ignore[attr-defined]
    tkinter.INSERT = "insert"                         # type: ignore[attr-defined]
    tkinter.SEL = "sel"                               # type: ignore[attr-defined]
    tkinter.SEL_FIRST = "sel.first"                   # type: ignore[attr-defined]
    tkinter.SEL_LAST = "sel.last"                     # type: ignore[attr-defined]
    tkinter.ANCHOR = "anchor"                         # type: ignore[attr-defined]
    tkinter.LEFT = "left"                             # type: ignore[attr-defined]
    tkinter.RIGHT = "right"                           # type: ignore[attr-defined]
    tkinter.TOP = "top"                               # type: ignore[attr-defined]
    tkinter.BOTTOM = "bottom"                         # type: ignore[attr-defined]
    tkinter.CENTER = "center"                         # type: ignore[attr-defined]
    tkinter.NONE = "none"                             # type: ignore[attr-defined]
    tkinter.BOTH = "both"                             # type: ignore[attr-defined]
    tkinter.VERTICAL = "vertical"                     # type: ignore[attr-defined]
    tkinter.HORIZONTAL = "horizontal"                 # type: ignore[attr-defined]
    tkinter.WORD = "word"                             # type: ignore[attr-defined]
    tkinter.CHAR = "char"                             # type: ignore[attr-defined]
    tkinter.NORMAL = "normal"                         # type: ignore[attr-defined]
    tkinter.DISABLED = "disabled"                     # type: ignore[attr-defined]
    tkinter.SINGLE = "single"                         # type: ignore[attr-defined]
    tkinter.BROWSE = "browse"                         # type: ignore[attr-defined]
    tkinter.EXTENDED = "extended"                     # type: ignore[attr-defined]
    tkinter.MULTIPLE = "multiple"                     # type: ignore[attr-defined]
    tkinter.RAISED = "raised"                         # type: ignore[attr-defined]
    tkinter.SUNKEN = "sunken"                         # type: ignore[attr-defined]
    tkinter.FLAT = "flat"                             # type: ignore[attr-defined]
    tkinter.GROOVE = "groove"                         # type: ignore[attr-defined]
    tkinter.RIDGE = "ridge"                           # type: ignore[attr-defined]
    tkinter.SOLID = "solid"                           # type: ignore[attr-defined]
    tkinter.constants = types.ModuleType("tkinter.constants")
    for name in ("END", "INSERT", "SEL", "SEL_FIRST", "SEL_LAST", "ANCHOR", "LEFT", "RIGHT", "TOP",
                 "BOTTOM", "CENTER", "NONE", "BOTH", "VERTICAL", "HORIZONTAL", "WORD", "CHAR",
                 "NORMAL", "DISABLED", "SINGLE", "BROWSE", "EXTENDED", "MULTIPLE", "RAISED",
                 "SUNKEN", "FLAT", "GROOVE", "RIDGE", "SOLID"):
        setattr(tkinter.constants, name, getattr(tkinter, name))
    tkinter.ttk = _TTKModule("tkinter.ttk")           # type: ignore[attr-defined]
    tkinter.font = _FontModule("tkinter.font")        # type: ignore[attr-defined]
    tkinter.filedialog = _FileDialogModule("tkinter.filedialog")  # type: ignore[attr-defined]
    tkinter.messagebox = _MessageBoxModule("tkinter.messagebox")  # type: ignore[attr-defined]
    tkinter.colorchooser = _ColorChooserModule("tkinter.colorchooser")  # type: ignore[attr-defined]
    tkinter.simpledialog = types.ModuleType("tkinter.simpledialog")     # type: ignore[attr-defined]
    tkinter.simpledialog.askstring = lambda *a, **k: ""                 # type: ignore[attr-defined]

    sys.modules["tkinter"] = tkinter
    sys.modules["tkinter.ttk"] = tkinter.ttk
    sys.modules["tkinter.font"] = tkinter.font
    sys.modules["tkinter.filedialog"] = tkinter.filedialog
    sys.modules["tkinter.messagebox"] = tkinter.messagebox
    sys.modules["tkinter.colorchooser"] = tkinter.colorchooser
    sys.modules["tkinter.simpledialog"] = tkinter.simpledialog
    sys.modules["tkinter.constants"] = tkinter.constants

    ctk = _CtkModule("customtkinter")
    for name in ("set_appearance_mode", "set_default_color_theme", "deactivate_automatic_dpi_awareness",
                 "set_widget_scaling", "set_window_scaling", "reset_widget_scaling",
                 "set_widget_geometry_positions"):
        setattr(ctk, name, getattr(_CtkModule, name).__get__(ctk, _CtkModule))
    sys.modules["customtkinter"] = ctk

    # Dialog helpers used by the app's own modal windows.
    ctk.messagebox = tkinter.messagebox              # type: ignore[attr-defined]
    ctk.filedialog = tkinter.filedialog              # type: ignore[attr-defined]
    ctk.colorchooser = tkinter.colorchooser          # type: ignore[attr-defined]

    ROOT = Tk()
    _INSTALLED = True
    try:  # optional: make the app's modal dialogs answer themselves
        patch_dialogs()
    except Exception:  # pragma: no cover - app not importable yet
        pass


def installed() -> bool:
    """True once :func:`install` has run."""
    return _INSTALLED


def patch_dialogs() -> None:
    """Replace :meth:`StudioDialog.run` with a scripted answer.

    The dialog bodies still execute (that is what the smoke test wants), but no
    call blocks waiting for a user.  Push answers with :func:`next_dialog_answer`
    ; when the queue is empty a sensible per-class default is returned.
    """
    from arduino_studio.ui.widgets import dialogs

    if getattr(dialogs.StudioDialog, "_stubbed_run", False):
        return

    def run(self: Any, *_args: Any, **_kwargs: Any) -> Any:
        kind = type(self).__name__
        DIALOGS.seen.append(kind)
        if DIALOGS.answers:
            answer = DIALOGS.answers.pop(0)
            buttons = list(getattr(self, "buttons", ()) or ())
            if isinstance(answer, bool):
                # tests push True/False; translate to what the helper expects
                if kind == "MessageDialog" and buttons:
                    return buttons[-1] if answer else buttons[0]
                if kind == "InputDialog":
                    return getattr(self, "_initial", "") if answer else None
                if kind == "ChoiceDialog":
                    options = list(getattr(self, "_all_options", getattr(self, "options", [])) or [])
                    return options[0] if (options and answer) else None
            return answer
        if kind == "MessageDialog":
            buttons = list(getattr(self, "buttons", ("OK",)) or ("OK",))
            return getattr(self, "default_button", "") or buttons[-1]
        if kind == "InputDialog":
            return getattr(self, "_pending_value", getattr(self, "_initial", ""))
        if kind == "ChoiceDialog":
            options = list(getattr(self, "_all_options", getattr(self, "options", [])) or [])
            return options[0] if options else None
        if kind == "PlanDialog":
            return not bool(getattr(self, "_require_ack", False))
        return None

    dialogs.StudioDialog.run = run  # type: ignore[method-assign]
    dialogs.StudioDialog._stubbed_run = True


def reset() -> None:
    """Clear queued callbacks, dialogs and clipboard between tests."""
    reset_after_queue()
    BOUND_SEQUENCES.clear()
    DIALOGS.clear()
    CLIPBOARD["value"] = ""
    _FileDialogModule.answers = []
    _FileDialogModule.default_answer = ""
    _MessageBoxModule.answers = {}
    _MessageBoxModule.log = []
