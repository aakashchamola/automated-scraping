"""
web/runner.py — Run the pipeline tools as subprocesses and stream their logs.

Each tool already logs to stdout, so the dashboard does not reimplement any
pipeline logic: it launches the same command a developer would type, captures
the output line by line, and fans it out to every connected browser.

One run at a time. Every tool in this project writes to the same Google Sheet
tabs, so two concurrent runs would interleave writes and corrupt each other's
row colouring — the lock is a correctness guard, not a convenience.
"""

import itertools
import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Keep the tail of a run in memory so a browser that connects late (or
# reloads) still sees the whole run rather than an empty console.
_MAX_LINES = 4000


class Run:
    """One tool execution: its process, its captured output, its subscribers."""

    _ids = itertools.count(1)

    def __init__(self, task_key: str, label: str, command: list):
        self.id = f"run-{next(Run._ids)}-{int(time.time())}"
        self.task_key = task_key
        self.label = label
        self.command = command
        self.started_at = datetime.now()
        self.finished_at = None
        self.exit_code = None
        self.status = "starting"          # starting | running | done | failed | stopped
        self.lines = deque(maxlen=_MAX_LINES)
        self.line_count = 0
        self._process = None
        self._subscribers = []
        self._lock = threading.Lock()

    # ── Output fan-out ────────────────────────────────────────────────────────

    def subscribe(self) -> queue.Queue:
        """Register a listener and hand it the backlog so it sees the full run."""
        q = queue.Queue()
        with self._lock:
            for line in self.lines:
                q.put(line)
            if self.status in ("done", "failed", "stopped"):
                q.put(None)
            else:
                self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _emit(self, line: dict) -> None:
        with self._lock:
            self.lines.append(line)
            self.line_count += 1
            for q in self._subscribers:
                q.put(line)

    def _close(self) -> None:
        with self._lock:
            for q in self._subscribers:
                q.put(None)
            self._subscribers = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._emit(_line("info", f"$ {' '.join(shlex.quote(c) for c in self.command)}"))
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            self._process = subprocess.Popen(
                self.command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
                start_new_session=True,      # so stop() can kill the whole tree
            )
        except OSError as exc:
            self.status = "failed"
            self.exit_code = -1
            self.finished_at = datetime.now()
            self._emit(_line("error", f"failed to start: {exc}"))
            self._close()
            return

        self.status = "running"
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for raw in self._process.stdout:
            self._emit(_line(_severity(raw), raw.rstrip("\n")))
        self.exit_code = self._process.wait()
        self.finished_at = datetime.now()
        if self.status == "stopped":
            self._emit(_line("warning", "run stopped by user"))
        elif self.exit_code == 0:
            self.status = "done"
            self._emit(_line("success", f"finished in {self.duration_seconds:.0f}s"))
        else:
            self.status = "failed"
            self._emit(_line("error", f"exited with code {self.exit_code}"))
        self._close()

    def stop(self) -> bool:
        if self._process is None or self._process.poll() is not None:
            return False
        self.status = "stopped"
        try:
            os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
        except OSError:
            self._process.terminate()
        return True

    # ── Views ─────────────────────────────────────────────────────────────────

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def summary(self) -> dict:
        return {
            "id": self.id,
            "task": self.task_key,
            "label": self.label,
            "status": self.status,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": self.finished_at.isoformat(timespec="seconds") if self.finished_at else None,
            "duration_seconds": round(self.duration_seconds, 1),
            "exit_code": self.exit_code,
            "line_count": self.line_count,
            "command": " ".join(self.command),
        }


def _severity(text: str) -> str:
    """Classify a log line so the console can colour it without parsing rules
    living in the browser."""
    lowered = text.lower()
    if " error" in lowered or lowered.startswith("error") or "traceback" in lowered:
        return "error"
    if " warning" in lowered or lowered.startswith("warning"):
        return "warning"
    return "log"


def _line(level: str, text: str) -> dict:
    return {"t": datetime.now().strftime("%H:%M:%S"), "level": level, "text": text}


class RunManager:
    """Owns the single-run lock and the run history."""

    def __init__(self, history: int = 40):
        self._runs = {}
        self._order = deque(maxlen=history)
        self._current = None
        self._lock = threading.Lock()

    def start(self, task_key: str, label: str, command: list):
        """Start a run, or return (None, reason) when one is already going."""
        with self._lock:
            if self._current is not None and self._current.status in ("starting", "running"):
                return None, f"'{self._current.label}' is still running — stop it first"
            run = Run(task_key, label, command)
            self._runs[run.id] = run
            self._order.append(run.id)
            self._current = run
        run.start()
        return run, None

    def get(self, run_id: str):
        return self._runs.get(run_id)

    def current(self):
        run = self._current
        return run if run and run.status in ("starting", "running") else None

    def history(self) -> list:
        return [self._runs[r].summary() for r in reversed(self._order) if r in self._runs]
