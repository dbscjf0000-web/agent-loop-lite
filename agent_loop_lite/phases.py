from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_loop_lite import git_ops
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


def _state_context(task_dir: TaskDir) -> dict[str, str]:
    """Build cycle-context fields injected into every worker prompt."""
    workspace = task_dir.workspace_path()
    state = task_dir.load_state()
    history = state.get("history") or []
    history_lines = []
    for entry in history[-5:][::-1]:
        history_lines.append(
            f"- cycle {entry.get('cycle')}: action={entry.get('action')} "
            f"passed={entry.get('passed')} hint={entry.get('hint', '')!r}"
        )
    history_text = "\n".join(history_lines) if history_lines else "(none)"

    git_log = git_ops.log_oneline(workspace, n=8) if git_ops.is_repo(workspace) else "(no git)"
    changed = []
    if git_ops.is_repo(workspace):
        last_pre = state.get("last_pre_cycle_sha")
        if last_pre:
            changed = git_ops.changed_files(workspace, since=last_pre)
    changed_text = "\n".join(f"- {f}" for f in changed) if changed else "(none)"
    return {
        "history": history_text,
        "git_log": git_log.strip() or "(empty)",
        "changed_files": changed_text,
    }


def run_planner(task_dir: TaskDir, config: Config) -> PlannerOutput:
    task = task_dir.task_path().read_text(encoding="utf-8")
    feedback = _previous_feedback(task_dir)
    ctx = _state_context(task_dir)
    prompt = PLANNER_PROMPT.format(
        task=task,
        feedback=feedback,
        history=ctx["history"],
        git_log=ctx["git_log"],
        changed_files=ctx["changed_files"],
    )
    workspace = task_dir.workspace_path()
    pre_sha = git_ops.current_sha(workspace) if git_ops.is_repo(workspace) else None
    resp = call_model(
        "planner",
        prompt,
        config.models.planner,
        workspace=workspace,
        timeout_s=config.model_timeout_s,
    )

    # Planner may write files via tools (cwd=workspace) OR emit `# file:`
    # blocks in stdout. Resolve all three channels:
    # 1) tool-written verify.sh/verify.py stays in workspace (run target).
    # 2) tool-written plan.md/r.md inside workspace → move to state dir.
    # 3) `# file:` blocks in stdout → write where appropriate.
    extracted = _extract_workspace_files(resp.text)
    for name, body in extracted.items():
        if name in {"verify.sh", "verify.py"}:
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            if name.endswith(".sh"):
                path.chmod(0o755)

    # If the worker wrote plan.md to workspace via tools, lift it out so the
    # rest of the loop (which reads state_dir/plan.md) sees it.
    ws_plan = workspace / "plan.md"
    if ws_plan.exists():
        task_dir.write_text("plan.md", ws_plan.read_text(encoding="utf-8"))
        ws_plan.unlink()
    ws_r = workspace / "r.md"
    if ws_r.exists():
        task_dir.write_text("r.md", ws_r.read_text(encoding="utf-8"))
        ws_r.unlink()

    # plan_text: prefer stdout content when present; otherwise read plan.md if
    # the worker wrote it via tools (now lifted into state dir).
    cleaned = _strip_file_blocks(resp.text)
    if cleaned.strip():
        plan_text = cleaned
    else:
        plan_text = task_dir.read_text("plan.md", "")

    r_notes, plan = _split_planner_response(plan_text)
    task_dir.write_text("r.md", r_notes)
    task_dir.write_text("plan.md", plan)

    # Snapshot Planner's writes (plan.md/verify.sh/r.md may be inside workspace
    # too if the worker put them there). Commit so Builder starts from a clean
    # baseline.
    if git_ops.is_repo(workspace) and git_ops.has_uncommitted_changes(workspace):
        git_ops.commit_all(workspace, f"planner-cycle")
    return PlannerOutput(r_notes=r_notes, plan=plan, response=resp)


def run_builder(
    task_dir: TaskDir,
    config: Config,
    *,
    subtask_context: str | None = None,
) -> BuilderOutput:
    workspace = task_dir.workspace_path()
    task = task_dir.task_path().read_text(encoding="utf-8")
    r_notes = task_dir.read_text("r.md", "(no R notes)")
    plan = task_dir.read_text("plan.md", "(no plan)")
    if subtask_context:
        plan = plan + "\n\n## Current subtask\n" + subtask_context + "\n"

    ctx = _state_context(task_dir)
    prompt = BUILDER_PROMPT.format(
        task=task,
        r_notes=r_notes,
        plan=plan,
        history=ctx["history"],
        git_log=ctx["git_log"],
        workspace=_workspace_listing(workspace),
    )

    pre_sha = git_ops.current_sha(workspace) if git_ops.is_repo(workspace) else None
    resp = call_model(
        "builder",
        prompt,
        config.models.builder,
        workspace=workspace,
        timeout_s=config.model_timeout_s,
    )

    # PRIMARY change-detection: git status. SECONDARY (legacy): parse `# file:`
    # blocks from stdout. We always run the legacy parser to support mock models
    # and CLIs without file-write tools — but git diff is the source of truth.
    legacy_files = _extract_workspace_files(resp.text)
    legacy_applied: list[str] = []
    for name, body in legacy_files.items():
        # Skip dangerous placeholder bodies that would wipe files
        if _looks_like_placeholder(body):
            continue
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        legacy_applied.append(name)

    git_changed: list[str] = []
    if git_ops.is_repo(workspace):
        # Diff against pre-Builder sha; also pick up untracked files
        git_changed = git_ops.changed_files(workspace, since=pre_sha) if pre_sha else []
        if not git_changed:
            # No commit yet — uncommitted changes are still "this builder's"
            for raw in git_ops.status_porcelain(workspace).splitlines():
                code = raw[:2]
                p = raw[3:].strip()
                if p:
                    git_changed.append(p)
        git_changed = sorted(set(git_changed))

    changed = sorted(set(legacy_applied) | set(git_changed))

    build_log = _strip_file_blocks(resp.text).strip()
    if changed:
        build_log += f"\n\nchanged_files: {', '.join(changed)}"
    if subtask_context:
        first_line = subtask_context.strip().splitlines()[0] if subtask_context.strip() else "subtask"
        existing = task_dir.read_text("build.md", "")
        task_dir.write_text(
            "build.md",
            (existing.rstrip("\n") + ("\n\n" if existing.strip() else "")
             + f"### subtask: {first_line}\n" + build_log.strip() + "\n"),
        )
    else:
        task_dir.write_text("build.md", build_log.strip() + "\n")
    return BuilderOutput(changed_files=sorted(changed), response=resp)


_STAGE_RE = re.compile(r"^###\s+stage\s+(\d+)\b", re.IGNORECASE | re.MULTILINE)
_SUBTASK_RE = re.compile(r"^[-*]\s*subtask\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_stages(plan_text: str) -> list[list[str]]:
    starts = [(m.start(), int(m.group(1))) for m in _STAGE_RE.finditer(plan_text)]
    if not starts:
        return []
    boundaries = [s for s, _ in starts] + [len(plan_text)]
    stages: list[list[str]] = []
    for i in range(len(starts)):
        section = plan_text[boundaries[i]:boundaries[i + 1]]
        subtasks = [m.group(1).strip() for m in _SUBTASK_RE.finditer(section)]
        stages.append(subtasks)
    return [s for s in stages if s]


def run_critic(task_dir: TaskDir, config: Config) -> CriticOutput:
    workspace = task_dir.workspace_path()
    plan = task_dir.read_text("plan.md", "(no plan)")
    check = validate_workspace(workspace, config.verify, plan_text=plan)
    task_dir.write_json("check.json", check.as_dict())

    state = task_dir.load_state()
    best_exists = bool(state.get("best_tag") or state.get("best_exists"))
    if config.models.critic == "rule":
        judge = _rule_judge(check)
        task_dir.write_json("judge.json", judge)
        return CriticOutput(check=check, judge=judge, response=None)

    pre_sha = state.get("last_pre_cycle_sha")
    if pre_sha and git_ops.is_repo(workspace):
        git_diff = git_ops.diff_text(workspace, since=pre_sha)
    else:
        git_diff = "(no diff available)"
    ctx = _state_context(task_dir)
    prompt = CRITIC_PROMPT.format(
        task=task_dir.task_path().read_text(encoding="utf-8"),
        plan=plan,
        check=json.dumps(check.as_dict(), indent=2),
        git_diff=git_diff or "(empty diff)",
        history=ctx["history"],
        best_exists=best_exists,
    )

    critic_pre_sha = git_ops.current_sha(workspace) if git_ops.is_repo(workspace) else None
    resp = call_model(
        "critic",
        prompt,
        config.models.critic,
        workspace=workspace,
        timeout_s=config.model_timeout_s,
    )

    # Critic is read-only — if it wrote anything, revert.
    if git_ops.is_repo(workspace) and critic_pre_sha and git_ops.has_uncommitted_changes(workspace):
        task_dir.append_log("critic_write_blocked", cycle=int(state.get("cycle", 0)))
        git_ops.reset_hard(workspace, critic_pre_sha)

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


def _normalize_judge(raw: dict[str, Any], check: CheckResult) -> dict[str, Any]:
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
    next_heading = re.search(r"^##\s+", markdown[match.end():], flags=re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(markdown)
    body = markdown[match.end():end].strip()
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

_PLACEHOLDER_PATTERNS = [
    re.compile(r"\.\.\.\s*\(.*?(생략|동일|elided|unchanged|truncated|keep previous).*?\)\s*\.\.\.", re.IGNORECASE),
    re.compile(r"<\s*(keep|preserve|unchanged|elided)[^>]*>", re.IGNORECASE),
    re.compile(r"^\s*\.{3,}\s*\[?(truncated|elided|unchanged)\]?\s*$", re.MULTILINE | re.IGNORECASE),
]


def _looks_like_placeholder(body: str) -> bool:
    """Detect placeholder-only bodies that would wipe a file.

    Triggers on:
    - Very short bodies (< 40 chars) that contain ellipsis markers
    - Any explicit `…(생략)…`, `<keep previous>`, `...elided...` markers
    """
    if not body or not body.strip():
        return True
    stripped = body.strip()
    if any(pat.search(stripped) for pat in _PLACEHOLDER_PATTERNS):
        return True
    if len(stripped) < 40 and ("..." in stripped or "…" in stripped):
        return True
    return False


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
        content = "\n".join(lines[idx + 1:]).rstrip() + "\n"
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


def _strip_file_blocks(text: str) -> str:
    """Remove only fenced blocks that contain `# file:` headers."""
    def replace(match: re.Match) -> str:
        body = match.group(1)
        lines = body.splitlines()
        idx = 0
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx < len(lines) and _FILE_HEADER_RE.match(lines[idx]):
            return ""
        return match.group(0)
    return _FENCE_RE.sub(replace, text or "")


def _workspace_listing(workspace: Path) -> str:
    rows: list[str] = []
    for path in sorted(workspace.iterdir()):
        if path.name in {"best", ".git"}:
            continue
        if path.is_file():
            rows.append(f"{path.name}\t{path.stat().st_size}B")
        elif path.is_dir():
            rows.append(f"{path.name}/")
    return "\n".join(rows) if rows else "(empty)"
