"""Git-backed workspace operations for agent-loop-lite v2.

Replaces the v1 ``best/`` folder snapshot/restore with a real git repo
inside the task workspace. This is the foundation of v2's "observe the
filesystem, don't parse stdout" approach.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


GIT_AUTHOR = ["-c", "user.email=agent-lite@local", "-c", "user.name=agent-lite"]


def _run(workspace: Path, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", *GIT_AUTHOR, *args]
    return subprocess.run(
        cmd,
        cwd=workspace,
        check=check,
        capture_output=capture,
        text=True,
    )


def is_repo(workspace: Path) -> bool:
    if not (workspace / ".git").exists():
        return False
    try:
        _run(workspace, "rev-parse", "--is-inside-work-tree")
        return True
    except subprocess.CalledProcessError:
        return False


def ensure_repo(workspace: Path) -> None:
    """Initialise a git repo in workspace and make an initial commit if empty."""
    workspace.mkdir(parents=True, exist_ok=True)
    if not is_repo(workspace):
        _run(workspace, "init", "-q", "-b", "main")
        gitignore = workspace / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(
                "best/\n__pycache__/\n*.pyc\n.DS_Store\nEMIT_*.txt\nLOOP_EMIT_*\n_*.emit_block.*\n",
                encoding="utf-8",
            )
    if current_sha(workspace) is None:
        _run(workspace, "add", "-A")
        _run(workspace, "commit", "--allow-empty", "-q", "-m", "init")


def current_sha(workspace: Path) -> str | None:
    try:
        res = _run(workspace, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        return None
    sha = res.stdout.strip()
    return sha or None


def status_porcelain(workspace: Path) -> str:
    res = _run(workspace, "status", "--porcelain")
    return res.stdout


def has_uncommitted_changes(workspace: Path) -> bool:
    return bool(status_porcelain(workspace).strip())


def commit_all(workspace: Path, message: str, *, allow_empty: bool = False) -> str | None:
    """Stage everything and create a commit. Returns new SHA or None if nothing to commit."""
    _run(workspace, "add", "-A")
    args = ["commit", "-q", "-m", message]
    if allow_empty:
        args.insert(1, "--allow-empty")
    try:
        _run(workspace, *args)
    except subprocess.CalledProcessError as exc:
        if not allow_empty and "nothing to commit" in (exc.stdout or "") + (exc.stderr or ""):
            return None
        raise
    return current_sha(workspace)


def changed_files(workspace: Path, since: str = "HEAD~1") -> list[str]:
    """Return list of changed file paths since the given ref."""
    try:
        res = _run(workspace, "diff", "--name-only", since, "HEAD")
    except subprocess.CalledProcessError:
        return []
    out = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    # Also pick up untracked files relative to current HEAD via porcelain
    for raw in status_porcelain(workspace).splitlines():
        code = raw[:2]
        path = raw[3:].strip()
        if "?" in code and path and path not in out:
            out.append(path)
    return sorted(set(out))


def diff_text(workspace: Path, since: str = "HEAD~1", *, max_chars: int = 12000) -> str:
    """Return git diff text since ref, truncated for prompt safety."""
    try:
        res = _run(workspace, "diff", "--stat", since, "HEAD")
        stat = res.stdout
        res = _run(workspace, "diff", since, "HEAD")
        diff = res.stdout
    except subprocess.CalledProcessError:
        return ""
    body = (stat + "\n" + diff) if stat else diff
    if len(body) > max_chars:
        body = body[:max_chars] + "\n... [diff truncated]"
    return body


def log_oneline(workspace: Path, n: int = 5) -> str:
    try:
        res = _run(workspace, "log", "--oneline", f"-{n}")
    except subprocess.CalledProcessError:
        return ""
    return res.stdout


def tag(workspace: Path, name: str, *, force: bool = True) -> None:
    args = ["tag"]
    if force:
        args.append("-f")
    args.append(name)
    _run(workspace, *args)


def tag_exists(workspace: Path, name: str) -> bool:
    try:
        res = _run(workspace, "tag", "-l", name)
    except subprocess.CalledProcessError:
        return False
    return name in res.stdout.split()


def reset_hard(workspace: Path, ref: str) -> None:
    _run(workspace, "reset", "--hard", "-q", ref)
    _run(workspace, "clean", "-fdq")


def list_tags(workspace: Path, pattern: str = "*") -> list[str]:
    try:
        res = _run(workspace, "tag", "-l", pattern)
    except subprocess.CalledProcessError:
        return []
    return [t for t in res.stdout.split() if t]
