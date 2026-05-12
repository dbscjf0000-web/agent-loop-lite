from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
import re

from agent_loop_lite.config import VerifyConfig


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    mode: str
    summary: str
    commands: list[dict[str, object]]
    stdout: str = ""
    stderr: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_workspace(
    workspace: Path,
    config: VerifyConfig,
    *,
    plan_text: str = "",
) -> CheckResult:
    mode = config.mode.strip().lower()
    if mode == "none":
        files = [p for p in workspace.iterdir() if p.name != "best"]
        passed = any(p.is_file() or p.is_dir() for p in files)
        return CheckResult(
            passed=passed,
            mode="none",
            summary="workspace has output" if passed else "workspace is empty",
            commands=[],
        )
    if mode == "plan":
        commands = extract_verification_commands(plan_text)
        if not commands:
            files = [p for p in workspace.iterdir() if p.name != "best"]
            passed = any(p.is_file() or p.is_dir() for p in files)
            return CheckResult(
                passed=passed,
                mode="plan",
                summary=(
                    "no runnable verification command; workspace has output"
                    if passed
                    else "no runnable verification command and workspace is empty"
                ),
                commands=[],
            )
        return _run_commands(workspace, commands, config.timeout_s, "plan")
    if mode == "pytest":
        return _run_commands(workspace, ["python -m pytest -q"], config.timeout_s, "pytest")
    if mode == "shell":
        if not config.command:
            return CheckResult(False, "shell", "verify.command is empty", [])
        return _run_commands(workspace, [config.command], config.timeout_s, "shell")
    raise ValueError(f"unsupported verify mode: {config.mode}")


def extract_verification_commands(plan_text: str) -> list[str]:
    section = _section(plan_text, "Verification Strategy")
    commands: list[str] = []
    for block in re.findall(r"```(?:bash|sh|shell)?\s*\n(.*?)```", section, flags=re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                commands.append(line)
    for line in section.splitlines():
        for cmd in re.findall(r"`([^`]+)`", line):
            cmd = cmd.strip()
            if cmd and cmd != "manual-inspect":
                commands.append(cmd)
    deduped: list[str] = []
    for cmd in commands:
        if cmd not in deduped:
            deduped.append(cmd)
    return deduped


def _section(markdown: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(markdown or "")
    if not match:
        return ""
    next_heading = re.search(r"^##\s+", markdown[match.end() :], flags=re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(markdown)
    return markdown[match.end() : end]


def _run_commands(
    workspace: Path,
    commands: list[str],
    timeout_s: int,
    mode: str,
) -> CheckResult:
    records: list[dict[str, object]] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for command in commands:
        result = _run_command(workspace, command, timeout_s)
        records.append(result)
        stdout_parts.append(str(result.get("stdout") or ""))
        stderr_parts.append(str(result.get("stderr") or ""))
        if result["returncode"] != 0:
            return CheckResult(
                passed=False,
                mode=mode,
                summary=f"verification failed: {command}",
                commands=records,
                stdout="\n".join(stdout_parts)[-4000:],
                stderr="\n".join(stderr_parts)[-4000:],
            )
    return CheckResult(
        passed=True,
        mode=mode,
        summary=f"verification passed ({len(commands)} command(s))",
        commands=records,
        stdout="\n".join(stdout_parts)[-4000:],
        stderr="\n".join(stderr_parts)[-4000:],
    )


def _run_command(workspace: Path, command: str, timeout_s: int) -> dict[str, object]:
    try:
        proc = subprocess.run(
            command,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timed out after {timeout_s}s",
        }

    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[-4000:],
        "stderr": (proc.stderr or "")[-4000:],
    }
