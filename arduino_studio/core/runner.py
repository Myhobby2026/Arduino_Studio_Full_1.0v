"""Background task execution with lane semantics and thread-safe UI updates.

Long running work (compile, upload, library search, serial I/O, terminal
commands, ``avrdude`` calls) never runs in the Tkinter mainloop.  It is handed
to :class:`TaskRunner`, which

* executes the work in a worker thread,
* serialises hardware access in the ``"build"`` lane so that compile/upload and
  ``avrdude``/``esptool`` cannot overlap,
* routes every callback back into the UI thread through ``ui_post``
  (``widget.after(0, fn)`` in the GUI), and
* supports cancellation that also kills attached subprocesses.
"""

from __future__ import annotations

import itertools
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .utils import get_logger, human_duration

__all__ = ["TaskContext", "TaskHandle", "TaskResult", "TaskRunner", "TaskCancelled"]

LOGGER = get_logger("runner")

LANE_BUILD = "build"
LANE_QUERY = "query"


class TaskCancelled(Exception):
    """Raised inside a worker when the user cancelled the task."""


@dataclass
class TaskResult:
    """Outcome of a finished task (always delivered in the UI thread)."""

    ok: bool
    payload: Any = None
    error: Optional[BaseException] = None
    returncode: Optional[int] = None
    cancelled: bool = False
    duration: float = 0.0
    name: str = ""

    @property
    def duration_text(self) -> str:
        return human_duration(self.duration)


@dataclass
class TaskHandle:
    """Token returned by :meth:`TaskRunner.submit` (used to cancel/poll)."""

    task_id: int
    name: str
    lane: str
    started_at: float = field(default_factory=time.perf_counter)
    finished_at: Optional[float] = None
    cancelled: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    result: Optional[TaskResult] = None
    context: Optional["TaskContext"] = None

    @property
    def is_done(self) -> bool:
        return self.done.is_set()

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.perf_counter()
        return max(0.0, end - self.started_at)

    def cancel(self, reason: str = "cancelled by user") -> bool:
        """Request cancellation of this task (also kills attached processes)."""
        if self.done.is_set() or self.context is None:
            return False
        self.cancelled = True
        return self.context.cancel(reason)

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until the task finished (``True`` when it is done)."""
        return self.done.wait(timeout)


class TaskContext:
    """The only object a worker function receives.

    Workers use it for logging, progress reporting, cancellation checks and to
    register subprocesses so they can be killed on cancel.
    """

    def __init__(
        self,
        name: str,
        ui_post: Callable[[Callable[[], Any]], Any],
        on_log: Optional[Callable[[str, str], Any]] = None,
        on_progress: Optional[Callable[[Optional[float], str], Any]] = None,
        on_state: Optional[Callable[[str, Any], Any]] = None,
    ) -> None:
        self.name = name
        self._post = ui_post
        self._on_log = on_log
        self._on_progress = on_progress
        self._on_state = on_state
        self._cancel = threading.Event()
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self.returncode: Optional[int] = None
        self.cancel_reason = ""
        self._work: Optional[Callable[["TaskContext"], Any]] = None
        self._on_done: Optional[Callable[[TaskResult], Any]] = None

    def bind(self, work: Callable[["TaskContext"], Any], on_done: Optional[Callable[[TaskResult], Any]] = None) -> None:
        """Attach the worker callable (and its completion callback)."""
        self._work = work
        self._on_done = on_done

    # ------------------------------------------------------------- state
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancelled(self) -> None:
        """Raise :class:`TaskCancelled` when cancellation was requested."""
        if self._cancel.is_set():
            raise TaskCancelled(self.cancel_reason or f"{self.name} cancelled")

    def cancel(self, reason: str = "cancelled") -> bool:
        """Request cancellation; kills attached subprocesses. ``True`` if fresh."""
        if self._cancel.is_set():
            return False
        self.cancel_reason = reason
        self._cancel.set()
        with self._lock:
            procs = list(self._processes)
        for proc in procs:
            _terminate(proc)
        return True

    # ------------------------------------------------------------- output
    def log(self, text: str, level: str = "info") -> None:
        """Send one line of output to the UI (thread-safe, never raises)."""
        callback = self._on_log
        if callback is None or not text:
            return
        payload = str(text).rstrip("\r\n")
        self._post(lambda: _safe_call(callback, payload, level))

    def progress(self, fraction: Optional[float], message: str = "") -> None:
        """Report progress (``None`` fraction = indeterminate)."""
        callback = self._on_progress
        if callback is None:
            return
        self._post(lambda: _safe_call(callback, fraction, message))

    def publish(self, kind: str, value: Any) -> None:
        """Push an arbitrary typed event to the UI (used by serial/terminal)."""
        callback = self._on_state
        if callback is None:
            return
        self._post(lambda: _safe_call(callback, kind, value))

    def set_returncode(self, code: Optional[int]) -> None:
        self.returncode = code

    # ------------------------------------------------------ subprocess mgmt
    def attach(self, process: "subprocess.Popen[Any]") -> None:
        """Track *process* so :meth:`cancel` can kill it."""
        with self._lock:
            self._processes.add(process)

    def detach(self, process: "subprocess.Popen[Any]") -> None:
        with self._lock:
            self._processes.discard(process)

    def run(self, command: list[str], **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        """Convenience wrapper: run *command*, stream output, honour cancellation.

        Uses :func:`arduino_studio.core.process.run_streaming`.
        """
        from .process import run_streaming  # local import to avoid cycles

        return run_streaming(command, context=self, **kwargs)


def _safe_call(callback: Callable[..., Any], *args: Any) -> None:
    try:
        callback(*args)
    except Exception:  # pragma: no cover - UI callback must not kill the app
        LOGGER.exception("task callback failed while handling %r", (args[:1],))


def _terminate(process: "subprocess.Popen[Any]") -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
    except OSError:
        LOGGER.debug("could not terminate process", exc_info=True)


class TaskRunner:
    """Executes work functions off the UI thread and posts results back."""

    def __init__(
        self,
        ui_post: Optional[Callable[[Callable[[], Any]], Any]] = None,
        query_workers: int = 3,
    ) -> None:
        self._log = get_logger("runner")
        self._post = ui_post or (lambda fn: fn())
        self._ids = itertools.count(1)
        self._build_queue: "list[TaskHandle]" = []
        self._queue_cv = threading.Condition()
        self._build_worker: Optional[threading.Thread] = None
        self._build_lock = threading.RLock()
        self._query_pool = ThreadPoolExecutor(max_workers=max(1, query_workers), thread_name_prefix="ardu-query")
        self._active: dict[int, TaskHandle] = {}
        self._stopping = threading.Event()
        self._thread_lock = threading.Lock()

    # ------------------------------------------------------------- workers
    def _ensure_build_worker(self) -> None:
        with self._thread_lock:
            if self._build_worker is not None and self._build_worker.is_alive():
                return
            worker = threading.Thread(target=self._build_loop, name="ardu-build", daemon=True)
            self._build_worker = worker
            worker.start()

    def _build_loop(self) -> None:
        while not self._stopping.is_set():
            handle: Optional[TaskHandle] = None
            with self._queue_cv:
                while not self._build_queue and not self._stopping.is_set():
                    self._queue_cv.wait(0.25)
                if self._build_queue:
                    handle = self._build_queue.pop(0)
            if handle is None:
                continue
            self._execute(handle)

    # -------------------------------------------------------------- public
    def submit(
        self,
        name: str,
        work: Callable[[TaskContext], Any],
        *,
        lane: str = LANE_BUILD,
        on_done: Optional[Callable[[TaskResult], Any]] = None,
        on_log: Optional[Callable[[str, str], Any]] = None,
        on_progress: Optional[Callable[[Optional[float], str], Any]] = None,
        on_state: Optional[Callable[[str, Any], Any]] = None,
        on_start: Optional[Callable[[TaskHandle], Any]] = None,
    ) -> TaskHandle:
        """Queue *work* and return a :class:`TaskHandle` immediately.

        ``lane="build"`` serialises the task (hardware access);
        ``lane="query"`` runs it in the thread pool (CLI queries, searches).
        All callbacks are invoked in the UI thread.
        """
        context = TaskContext(name, self._post, on_log=on_log, on_progress=on_progress, on_state=on_state)
        context.bind(work, on_done)
        handle = TaskHandle(
            task_id=next(self._ids), name=name, lane=lane if lane in (LANE_BUILD, LANE_QUERY) else LANE_BUILD,
            context=context,
        )
        with self._thread_lock:
            self._active[handle.task_id] = handle
        if handle.lane == LANE_BUILD:
            self._ensure_build_worker()
            with self._queue_cv:
                self._build_queue.append(handle)
                self._queue_cv.notify_all()
        else:
            future = self._query_pool.submit(self._execute, handle)
            del future
        if on_start is not None:
            self._post(lambda: _safe_call(on_start, handle))
        return handle

    def cancel(self, handle: TaskHandle | int) -> bool:
        """Cancel a running or queued task."""
        task_id = handle.task_id if isinstance(handle, TaskHandle) else int(handle)
        with self._thread_lock:
            target = self._active.get(task_id)
        if target is None:
            return False
        with self._queue_cv:
            if target in self._build_queue:
                self._build_queue.remove(target)
                self._finish(target, TaskResult(ok=False, cancelled=True, name=target.name, error=TaskCancelled()))
                return True
        return target.cancel(f"{target.name} cancelled by user")

    def cancel_lane(self, lane: str = LANE_BUILD) -> int:
        """Cancel every active/queued task of *lane*; returns how many were hit."""
        count = 0
        with self._thread_lock:
            targets = [h for h in self._active.values() if h.lane == lane and not h.is_done]
        for target in targets:
            if self.cancel(target):
                count += 1
        return count

    def active_handles(self, lane: Optional[str] = None) -> "list[TaskHandle]":
        """The running/queued handles, optionally limited to one *lane*.

        Handy for diagnostics: the UI smoke test reports the names of whatever
        survived shutdown instead of only asserting that nothing did.
        """
        with self._thread_lock:
            active = [handle for handle in self._active.values() if not handle.is_done]
        if lane is None:
            return active
        return [handle for handle in active if handle.lane == lane]

    def busy(self, lane: Optional[str] = None) -> bool:
        """True while any (or any *lane*-restricted) task is running/queued."""
        return bool(self.active_handles(lane))

    def current(self, lane: str = LANE_BUILD) -> Optional[TaskHandle]:
        """The running/queued handle of *lane* (oldest first), if any."""
        with self._thread_lock:
            for handle in self._active.values():
                if handle.lane == lane and not handle.is_done:
                    return handle
        return None

    def wait_all(self, timeout: float = 2.0) -> bool:
        """Block until no task is active (used by tests and shutdown)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.busy():
                return True
            time.sleep(0.01)
        return not self.busy()

    def shutdown(self) -> None:
        """Cancel queued work and stop the pool (safe to call twice)."""
        self._stopping.set()
        try:
            with self._queue_cv:
                for handle in list(self._build_queue):
                    self._build_queue.remove(handle)
                    self._finish(handle, TaskResult(ok=False, cancelled=True, name=handle.name))
                self._queue_cv.notify_all()
            self.cancel_lane(LANE_BUILD)
        finally:
            self._query_pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------- internal
    def _execute(self, handle: TaskHandle) -> None:
        context = handle.context
        assert context is not None  # set in submit()
        result: TaskResult
        payload: Any = None
        error: Optional[BaseException] = None
        ok = False
        cancelled = False
        started = time.perf_counter()
        context.progress(None, f"starting {handle.name}")
        try:
            payload = context._work(context) if context._work else None
            ok = True
        except TaskCancelled as exc:
            cancelled, error = True, exc
        except Exception as exc:  # noqa: BLE001 - report every worker error to the UI
            error = exc
            self._log.exception("task %s failed", handle.name)
        duration = time.perf_counter() - started
        result = TaskResult(
            ok=ok and not cancelled,
            payload=payload,
            error=error,
            returncode=context.returncode,
            cancelled=cancelled,
            duration=duration,
            name=handle.name,
        )
        self._finish(handle, result)

    def _finish(self, handle: TaskHandle, result: TaskResult) -> None:
        handle.result = result
        handle.finished_at = time.perf_counter()
        handle.done.set()
        on_done = getattr(handle.context, "_on_done", None) if handle.context else None
        if handle.context is not None:
            handle.context.progress(1.0 if result.ok else 0.0, "done" if result.ok else "failed")
        if on_done is not None:
            self._post(lambda: _safe_call(on_done, result))
        with self._thread_lock:
            self._active.pop(handle.task_id, None)
