from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

from agent_loop_lite import git_ops
from agent_loop_lite.config import Config
from agent_loop_lite.model import ModelResponse
from agent_loop_lite.phases import (
    parse_stages,
    run_builder,
    run_critic,
    run_planner,
    run_stop_gate,
)
from agent_loop_lite.state import TaskDir

_HISTORY_MAX = 8
_MAX_BUILDER_WORKERS = 4


@dataclass(frozen=True)
class RunResult:
    task_id: str
    status: str
    cycles_run: int
    best_exists: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LoopRunner:
    def __init__(self, task_dir: TaskDir, config: Config) -> None:
        self.task_dir = task_dir
        self.config = config

    def run(self, task: str | None = None) -> RunResult:
        self.task_dir.init()
        if task is not None and not self.task_dir.task_path().read_text(encoding="utf-8").strip():
            self.task_dir.task_path().write_text(task, encoding="utf-8")

        # Ensure workspace is a git repo; commit any seeded files as the
        # initial "best-cycle-0" baseline.
        workspace = self.task_dir.workspace_path()
        git_ops.ensure_repo(workspace)
        if git_ops.has_uncommitted_changes(workspace):
            git_ops.commit_all(workspace, "seed")
        if not git_ops.tag_exists(workspace, "best-cycle-0"):
            git_ops.tag(workspace, "best-cycle-0")

        state = self.task_dir.load_state()
        if "best_tag" not in state:
            state["best_tag"] = "best-cycle-0"
            self.task_dir.save_state(state)

        cycle = int(state.get("cycle", 1))
        status = "running"

        while cycle <= self.config.max_cycles:
            next_phase = str(state.get("next_phase", "R"))
            self.task_dir.append_log("cycle_start", cycle=cycle, phase=next_phase)

            if next_phase in {"R", "P"}:
                pre_sha = git_ops.current_sha(workspace)
                state["last_pre_cycle_sha"] = pre_sha
                self.task_dir.save_state(state)

                planner = self._safe_call("planner", state, cycle, run_planner, self.task_dir, self.config)
                if planner is None:
                    status = "worker_error"
                    break
                if next_phase == "R":
                    self._checkpoint("R", cycle, planner.response)
                self._checkpoint("P", cycle, planner.response)
                state = self._save_progress(state, cycle=cycle, next_phase="I")
                next_phase = "I"

            if next_phase == "I":
                plan_text = self.task_dir.read_text("plan.md", "")
                stages = parse_stages(plan_text)
                if stages:
                    self.task_dir.write_text("build.md", "")
                    all_changed: list[str] = []
                    last_resp = None
                    for stage_idx, subtasks in enumerate(stages, start=1):
                        self.task_dir.append_log(
                            "stage_start", cycle=cycle, stage=stage_idx, subtasks=len(subtasks),
                        )
                        workers = min(max(1, len(subtasks)), _MAX_BUILDER_WORKERS)
                        stage_failed = False
                        with ThreadPoolExecutor(max_workers=workers) as ex:
                            futures = [
                                ex.submit(run_builder, self.task_dir, self.config,
                                          subtask_context=s)
                                for s in subtasks
                            ]
                            for fut in futures:
                                try:
                                    out = fut.result()
                                except Exception as exc:  # noqa: BLE001
                                    self.task_dir.append_log(
                                        "worker_error", worker="builder",
                                        cycle=cycle, stage=stage_idx,
                                        reason=str(exc)[:200],
                                    )
                                    stage_failed = True
                                    continue
                                all_changed.extend(out.changed_files)
                                last_resp = out.response
                        self.task_dir.append_log("stage_end", cycle=cycle, stage=stage_idx)
                        if stage_failed:
                            status = "worker_error"
                            break
                    if status == "worker_error":
                        break
                    if last_resp is not None:
                        self._checkpoint(
                            "I", cycle, last_resp,
                            {"changed_files": sorted(set(all_changed)),
                             "stages": len(stages)},
                        )
                else:
                    builder = self._safe_call(
                        "builder", state, cycle, run_builder, self.task_dir, self.config,
                    )
                    if builder is None:
                        status = "worker_error"
                        break
                    self._checkpoint(
                        "I", cycle, builder.response,
                        {"changed_files": builder.changed_files},
                    )

                # Commit Builder's output so the V/J phase can diff against
                # pre-cycle baseline.
                if git_ops.has_uncommitted_changes(workspace):
                    git_ops.commit_all(workspace, f"cycle-{cycle:03d}-I builder")
                state = self._save_progress(state, cycle=cycle, next_phase="V")
                next_phase = "V"

            if next_phase in {"V", "J"}:
                critic = self._safe_call("critic", state, cycle, run_critic, self.task_dir, self.config)
                if critic is None:
                    status = "worker_error"
                    break
                self.task_dir.checkpoint(cycle, "V", {"check": critic.check.as_dict()})
                if critic.response is not None:
                    self._metric("critic", "V/J", cycle, critic.response)
                # v2.3: Stop-Gate second-opinion when main Critic says "stop".
                judge = run_stop_gate(
                    self.task_dir,
                    self.config,
                    judge=critic.judge,
                    check=critic.check,
                )
                # Persist the final (possibly amended) judge so judge.json
                # matches what handle_judge actually sees.
                self.task_dir.write_json("judge.json", judge)
                self.task_dir.checkpoint(cycle, "J", {"judge": judge})
                state, status = self._handle_judge(state, cycle, judge)
                if status != "running":
                    break
                cycle = int(state["cycle"])
                continue

            cycle += 1

        if status == "running":
            status = "max_cycles"
            state["status"] = status
            self.task_dir.save_state(state)

        return RunResult(
            task_id=self.task_dir.task_id,
            status=status,
            cycles_run=int(state.get("cycle", cycle)),
            best_exists=bool(state.get("best_tag") and state["best_tag"] != "best-cycle-0"),
        )

    def _safe_call(
        self,
        worker: str,
        state: dict[str, Any],
        cycle: int,
        fn: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run a worker call, logging+graceful-exit on any exception.

        Returns the worker output on success or ``None`` on failure. On
        failure, state is marked ``worker_error`` and saved so the loop
        can be resumed cleanly.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.task_dir.append_log(
                "worker_error",
                worker=worker,
                cycle=cycle,
                reason=str(exc)[:300],
            )
            updated = {**state, "status": "worker_error"}
            self.task_dir.save_state(updated)
            return None

    def _checkpoint(
        self,
        phase: str,
        cycle: int,
        response: ModelResponse,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.task_dir.checkpoint(cycle, phase, payload or {})
        self._metric(_worker_for_phase(phase), phase, cycle, response)

    def _metric(self, worker: str, phase: str, cycle: int, response: ModelResponse) -> None:
        self.task_dir.append_metric(
            cycle=cycle,
            phase=phase,
            worker=worker,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            cost_usd=response.cost_usd,
            latency_s=response.latency_s,
        )

    def _save_progress(
        self,
        state: dict[str, Any],
        *,
        cycle: int,
        next_phase: str,
    ) -> dict[str, Any]:
        updated = {**state, "cycle": cycle, "next_phase": next_phase, "status": "running"}
        self.task_dir.save_state(updated)
        return updated

    def _append_history(self, state: dict[str, Any], entry: dict[str, Any]) -> None:
        history = list(state.get("history") or [])
        history.append(entry)
        if len(history) > _HISTORY_MAX:
            history = history[-_HISTORY_MAX:]
        state["history"] = history

    def _handle_judge(
        self,
        state: dict[str, Any],
        cycle: int,
        judge: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        workspace = self.task_dir.workspace_path()
        passed = bool(judge.get("passed", False))
        action = str(judge.get("action", "stop"))
        # v2.6: redo_count tracks CONSECUTIVE-same-hint streaks. Resets when
        # the judge hint differs from the prior cycle's hint. Rationale: when
        # Stop-Gate keeps finding *different* blockers each cycle, the loop is
        # making real progress and shouldn't be cut off; only repeated same-
        # hint failures indicate the loop is stuck.
        redo_count = int(state.get("redo_count", 0))
        new_hint = str(judge.get("hint") or "")[:300]
        prev_hint = str(state.get("previous_hint") or "")

        # v2.10: revive FAIL-but-better promotion. When the Critic reports
        # the cycle improved over the prior best (passed=false, better=true),
        # keep Builder's progress under a `better-cycle-N` tag instead of
        # rolling back. This lets the loop accumulate fixes across cycles
        # when no single cycle PASSes — the previous v2 simplification
        # ("PASS only promote") cost us convergence on the polish task.
        better = bool(judge.get("better", passed))
        if passed:
            tag_name = f"best-cycle-{cycle:03d}"
            git_ops.tag(workspace, tag_name)
            state["best_tag"] = tag_name
            state["best_exists"] = True
            redo_count = 0
            self.task_dir.append_log("promote_best", cycle=cycle, tag=tag_name)
        elif better:
            # FAIL but improved — promote so the next cycle starts from here,
            # not from the prior best. The hint still flows through to drive
            # the next plan.
            tag_name = f"better-cycle-{cycle:03d}"
            git_ops.tag(workspace, tag_name)
            state["best_tag"] = tag_name
            state["best_exists"] = True
            if new_hint and new_hint == prev_hint:
                redo_count += 1
                hint_streak = "same"
            else:
                redo_count = 1
                hint_streak = "new"
            self.task_dir.append_log(
                "promote_better",
                cycle=cycle,
                tag=tag_name,
                action=action,
                hint_streak=hint_streak,
            )
        else:
            best_tag = str(state.get("best_tag") or "best-cycle-0")
            try:
                git_ops.reset_hard(workspace, best_tag)
                restored = True
            except Exception:
                restored = False
            if new_hint and new_hint == prev_hint:
                redo_count += 1
                hint_streak = "same"
            else:
                redo_count = 1
                hint_streak = "new"
            self.task_dir.append_log(
                "rollback",
                cycle=cycle,
                action=action,
                restored=restored,
                target=best_tag,
                hint_streak=hint_streak,
            )

        self._append_history(state, {
            "cycle": cycle,
            "passed": passed,
            "action": action,
            "hint": str(judge.get("hint") or "")[:300],
        })
        state["previous_action"] = action
        state["previous_hint"] = str(judge.get("hint") or "")[:300]

        self.task_dir.append_log(
            "judge",
            cycle=cycle,
            passed=passed,
            action=action,
            redo_count=redo_count,
        )

        if action == "stop":
            state.update({"status": "stop", "redo_count": redo_count, "next_phase": "done"})
            self.task_dir.save_state(state)
            return state, "stop"
        if redo_count >= self.config.max_redo:
            state.update({"status": "max_redo", "redo_count": redo_count, "next_phase": "done"})
            self.task_dir.save_state(state)
            return state, "max_redo"

        next_phase = "R" if action == "redo_R" else "P"
        state.update(
            {
                "cycle": cycle + 1,
                "next_phase": next_phase,
                "redo_count": redo_count,
                "status": "running",
            }
        )
        self.task_dir.save_state(state)
        return state, "running"


def _worker_for_phase(phase: str) -> str:
    if phase in {"R", "P"}:
        return "planner"
    if phase == "I":
        return "builder"
    return "critic"
