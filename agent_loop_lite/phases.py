from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_loop_lite.config import Config
from agent_loop_lite.jsonutil import extract_json
from agent_loop_lite.model import ModelResponse, call_model
from agent_loop_lite.prompts import BUILDER_PROMPT, CRITIC_PROMPT, PLANNER_PROMPT
from agent_loop_lite.state import TaskDir
from agent_loop_lite.verifier import CheckResult, validate_workspace


@dataclass(frozen=True)
class PlannerOutput:
    r_notes: str
    plan: str
    response: ModelResponse


@dataclass(frozen=True)
class BuilderOutput:
    changed_files: list[str]
    response: ModelResponse


@dataclass(frozen=True)
class CriticOutput:
    check: CheckResult
    judge: dict[str, Any]
    response: ModelResponse | None


def run_planner(task_dir: TaskDir, config: Config) -> PlannerOutput:
    task = task_dir.task_path().read_text(encoding="utf-8")
    feedback = _previous_feedback(task_dir)
    previous_r = task_dir.read_text("r.md", "(none)")
    prompt = PLANNER_PROMPT.format(task=task, feedback=feedback, previous_r=previous_r)
    resp = call_model(
        "planner",
        prompt,
        config.models.planner,
        workspace=task_dir.workspace_path(),
        timeout_s=config.model_timeout_s,
    )
    r_notes, plan = _split_planner_response(resp.text)
    task_dir.write_text("r.md", r_notes)
    task_dir.write_text("plan.md", plan)
    return PlannerOutput(r_notes=r_notes, plan=plan, response=resp)


def run_builder(task_dir: TaskDir, config: Config) -> BuilderOutput:
    task = task_dir.task_path().read_text(encoding="utf-8")
    r_notes = task_dir.read_text("r.md", "(no R notes)")
    plan = task_dir.read_text("plan.md", "(no plan)")
    prompt = BUILDER_PROMPT.format(
        task=task,
        r_notes=r_notes,
        plan=plan,
        workspace=_workspace_listing(task_dir.workspace_path()),
    )
    resp = call_model(
        "builder",
        prompt,
        config.models.builder,
        workspace=task_dir.workspace_path(),
        timeout_s=config.model_timeout_s,
    )

    files = _extract_workspace_files(resp.text)
    changed: list[str] = []
    for name, body in files.items():
        path = task_dir.workspace_path() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        changed.append(name)

    build_log = _strip_fences(resp.text).strip()
    if changed:
        build_log += f"\n\nchanged_files: {', '.join(sorted(changed))}"
    task_dir.write_text("build.md", build_log.strip() + "\n")
    return BuilderOutput(changed_files=sorted(changed), response=resp)


def run_critic(task_dir: TaskDir, config: Config) -> CriticOutput:
    plan = task_dir.read_text("plan.md", "(no plan)")
    check = validate_workspace(task_dir.workspace_path(), config.verify, plan_text=plan)
    task_dir.write_json("check.json", check.as_dict())

    state = task_dir.load_state()
    best_exists = bool(state.get("best_exists", False))
    if config.models.critic == "rule":
        judge = _rule_judge(check)
        task_dir.write_json("judge.json", judge)
        return CriticOutput(check=check, judge=judge, response=None)

    prompt = CRITIC_PROMPT.format(
        task=task_dir.task_path().read_text(encoding="utf-8"),
        plan=plan,
        check=json.dumps(check.as_dict(), indent=2),
        workspace_snapshot=_workspace_snapshot(task_dir.workspace_path()),
        best_exists=best_exists,
    )
    resp = call_model(
        "critic",
        prompt,
        config.models.critic,
        workspace=task_dir.workspace_path(),
        timeout_s=config.model_timeout_s,
    )
    try:
        parsed = extract_json(resp.text)
    except ValueError:
        parsed = _rule_judge(check)
        parsed["reason"] = "critic JSON was invalid; used rule fallback"
    judge = _normalize_judge(parsed, check)
    task_dir.write_json("judge.json", judge)
    return CriticOutput(check=check, judge=judge, response=resp)


def _rule_judge(check: CheckResult) -> dict[str, Any]:
    action = "stop" if check.passed else "redo_P"
    hint = "" if check.passed else _detailed_failure_hint(check)
    return {
        "passed": check.passed,
        "better": check.passed,
        "action": action,
        "hint": hint,
        "reason": check.summary,
    }


def _normalize_judge(
    raw: dict[str, Any],
    check: CheckResult,
) -> dict[str, Any]:
    passed = bool(raw.get("passed", check.passed))
    action = raw.get("action")
    if action not in {"stop", "redo_P", "redo_R"}:
        action = "stop" if passed else "redo_P"
    if "better" in raw:
        better = bool(raw["better"])
    else:
        better = passed
    hint = str(raw.get("hint") or "")
    if action != "stop" and not hint:
        hint = _detailed_failure_hint(check)
    return {
        "passed": passed,
        "better": better,
        "action": action,
        "hint": hint,
        "reason": str(raw.get("reason") or ""),
    }


def _previous_feedback(task_dir: TaskDir) -> str:
    judge = task_dir.read_json("judge.json", {})
    if isinstance(judge, dict):
        action = str(judge.get("action") or "")
        reason = str(judge.get("reason") or "")
        hint = str(judge.get("hint") or "")
        parts = []
        if action:
            parts.append(f"previous action: {action}")
        if reason:
            parts.append(f"previous reason: {reason}")
        if hint:
            parts.append(f"required next-cycle change: {hint}")
        return "\n".join(parts) if parts else "(none)"
    return "(none)"


def _split_planner_response(text: str) -> tuple[str, str]:
    plan = _normalize_plan(text)
    r_notes = "\n\n".join(
        part
        for part in (
            _markdown_section(plan, "Task Summary"),
            _markdown_section(plan, "Assumptions"),
            _markdown_section(plan, "Success Criteria"),
        )
        if part.strip()
    )
    if not r_notes.strip():
        r_notes = "No separate R notes returned."
    return r_notes.strip() + "\n", plan.strip() + "\n"


def _normalize_plan(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return "# Plan\n\n## Task Summary\n(no plan returned)\n"
    if re.search(r"^#\s+Plan\s*$", stripped, flags=re.IGNORECASE | re.MULTILINE):
        return stripped
    return "# Plan\n\n" + stripped


def _markdown_section(markdown: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(markdown or "")
    if not match:
        return ""
    next_heading = re.search(r"^##\s+", markdown[match.end() :], flags=re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(markdown)
    body = markdown[match.end() : end].strip()
    return f"## {heading}\n{body}" if body else ""


def _detailed_failure_hint(check: CheckResult) -> str:
    failed = next(
        (cmd for cmd in check.commands if int(cmd.get("returncode", 0)) != 0),
        None,
    )
    if failed:
        command = str(failed.get("command") or "")
        stderr = str(failed.get("stderr") or "").strip()
        stdout = str(failed.get("stdout") or "").strip()
        evidence = stderr or stdout or check.summary
        evidence = evidence.replace("\n", " ")[:500]
        return (
            f"What failed: `{command}` did not pass. "
            f"Likely cause: inspect the implementation against plan.md and the command output. "
            f"Next change: revise plan.md and implementation to address this failure directly. "
            f"Rerun: `{command}`. Evidence: {evidence}"
        )
    return (
        f"What failed: {check.summary}. "
        "Likely cause: no deterministic verification passed. "
        "Next change: revise plan.md Success Criteria and Verification Strategy, then update the implementation. "
        "Rerun the commands listed in plan.md."
    )


_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*\s*\n(.*?)```", re.DOTALL)
_FILE_HEADER_RE = re.compile(r"^\s*(?:#|//|;|--)\s*file\s*:\s*([A-Za-z0-9_./-]+)\s*$")
_SAFE_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _extract_workspace_files(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _FENCE_RE.finditer(text or ""):
        body = match.group(1)
        lines = body.splitlines()
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines):
            continue
        header = _FILE_HEADER_RE.match(lines[idx])
        if not header:
            continue
        name = header.group(1)
        if not _is_safe_filename(name):
            continue
        content = "\n".join(lines[idx + 1 :]).rstrip() + "\n"
        out[name] = content
    return out


def _is_safe_filename(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name:
        return False
    path = Path(name)
    if path.is_absolute():
        return False
    if any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts):
        return False
    return all(bool(_SAFE_PART_RE.fullmatch(part)) for part in path.parts)


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text or "")


def _workspace_listing(workspace: Path) -> str:
    rows: list[str] = []
    for path in sorted(workspace.iterdir()):
        if path.name == "best":
            continue
        if path.is_file():
            rows.append(f"{path.name}\t{path.stat().st_size}B")
        elif path.is_dir():
            rows.append(f"{path.name}/")
    return "\n".join(rows) if rows else "(empty)"


def _workspace_snapshot(workspace: Path, *, max_file_chars: int = 6000, max_total: int = 24000) -> str:
    blocks: list[str] = []
    total = 0
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(workspace)
        if rel.parts and rel.parts[0] in {"best", "__pycache__"}:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(body) > max_file_chars:
            body = body[:max_file_chars] + "\n... [truncated]"
        block = f"===== {rel} =====\n{body}"
        if total + len(block) > max_total:
            blocks.append("... [workspace snapshot truncated]")
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(blocks) if blocks else "(workspace empty)"
