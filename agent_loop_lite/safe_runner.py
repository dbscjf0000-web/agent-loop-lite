"""Robust subprocess runner for agent-loop-lite v2.8.

Centralises the subprocess invocation logic that ``model.py`` and
``verifier.py`` previously copy-pasted. Both Codex and Gemini reviews
of the v2.6/v2.7 failures identified the same root cause: ``subprocess.run``
with ``shell=True`` and ``text=True`` mis-handles three failure modes —

1. **Bad bytes in stdout/stderr** (e.g. codex CLI dumping raw path bytes
   from a non-ASCII workspace) crash decoding before the caller can
   recover.
2. **Orphan grand-children** — when the immediate child is a shell or
   wrapper that spawns the real CLI (cursor-agent → its internal tools),
   killing only the direct child leaves the grand-children running.
3. **Silent hangs** — a process that produces no output for the entire
   wall-clock budget cannot be distinguished from one that is still
   working; ``subprocess.run`` only enforces a single hard timeout.

``SafeRunner.run()`` addresses all three:

- Reads stdout/stderr as **bytes** and decodes with ``errors="replace"``.
- Starts the child in its **own process group** (``start_new_session=True``)
  and kills the whole group on timeout via ``os.killpg``.
- Supports an optional **idle timeout** in addition to the hard wall-clock
  timeout, watching stdout/stderr for any byte to refresh "alive".

The module deliberately has zero non-stdlib imports; it stays under a
hundred lines so the lite spirit of the project is preserved.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunOutcome:
    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float
    killed_by: str | None = None  # "hard_timeout" | "idle_timeout" | None
    pid: int | None = None
    pgid: int | None = None


class SafeRunnerTimeout(RuntimeError):
    """Raised when a SafeRunner invocation hits its budget."""


def _kill_group(pgid: int) -> None:
    """SIGTERM the process group; SIGKILL after a short grace period.

    macOS returns EPERM when the process group has already been reaped,
    so treat both EPERM and ESRCH as "already gone."
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(
    command: str,
    *,
    cwd: Path,
    stdin_bytes: bytes | None = None,
    hard_timeout_s: float = 1800,
    idle_timeout_s: float | None = None,
) -> RunOutcome:
    """Run ``command`` via a shell with robust process / encoding handling.

    Always uses ``shell=True`` (caller may pass a shell pipeline) but in a
    fresh session, so the entire process group can be torn down together.

    Returns a ``RunOutcome``. The caller is responsible for interpreting
    a non-zero ``returncode`` as success or failure.
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        stdin=subprocess.PIPE if stdin_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)

    if stdin_bytes is not None:
        try:
            proc.stdin.write(stdin_bytes)  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
        except (BrokenPipeError, OSError):
            pass

    stdout_buf: list[bytes] = []
    stderr_buf: list[bytes] = []
    last_activity = time.monotonic()
    lock = threading.Lock()

    def _drain(stream, buf: list[bytes]) -> None:
        nonlocal last_activity
        try:
            for chunk in iter(lambda: stream.read(4096), b""):
                if not chunk:
                    break
                with lock:
                    buf.append(chunk)
                    last_activity = time.monotonic()
        except (OSError, ValueError):
            pass

    t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_buf), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf), daemon=True)
    t_out.start()
    t_err.start()

    killed_by: str | None = None
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.monotonic()
        if now - started > hard_timeout_s:
            killed_by = "hard_timeout"
            _kill_group(pgid)
            break
        if idle_timeout_s is not None and (now - last_activity) > idle_timeout_s:
            killed_by = "idle_timeout"
            _kill_group(pgid)
            break
        time.sleep(0.2)

    proc.wait(timeout=10)
    t_out.join(timeout=2)
    t_err.join(timeout=2)

    elapsed = time.monotonic() - started
    return RunOutcome(
        returncode=proc.returncode,
        stdout=b"".join(stdout_buf).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_buf).decode("utf-8", errors="replace"),
        elapsed_s=round(elapsed, 3),
        killed_by=killed_by,
        pid=proc.pid,
        pgid=pgid,
    )
