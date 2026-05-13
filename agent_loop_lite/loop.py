from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from agent_loop_lite.config import Config
from agent_loop_lite.model import ModelResponse
from concurrent.futures import ThreadPoolExecutor
from agent_loop_lite.phases import parse_stages, run_builder, run_critic, run_planner
from agent_loop_lite.state import TaskDir


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

        state = self.task_dir.load_state()
        cycle = int(state.get("cycle", 1))
        status = "running"

        while cycle <= self.config.max_cycles:
            next_phase = str(state.get("next_phase", "R"))
            self.task_dir.append_log("cycle_start", cycle=cycle, phase=next_phase)

            if next_phase in {"R", "P"}:
                planner = run_planner(self.task_dir, self.config)
                if next_phase == "R":
                    self._checkpoint("R", cycle, planner.response)
                self._checkpoint("P", cycle, planner.response)
                state = self._save_progress(state, cycle=cycle, next_phase="I")
                next_phase = "I"

            if next_phase == "I":
                plan_text = self.task_dir.read_text("plan.md", "")
                stages = parse_stages(plan_text)
                if stages:
                    # Clear build.md so subtasks append cleanly
                    self.task_dir.write_text("build.md", "")
                    all_changed: list[str] = []
                    last_resp = None
                    for stage_idx, subtasks in enumerate(stages, start=1):
                        self.task_dir.append_log(
                            "stage_start", cycle=cycle, stage=stage_idx, subtasks=len(subtasks),
                        )
                        with ThreadPoolExecutor(max_workers=max(1, len(subtasks))) as ex:
                            futures = [
                                ex.submit(run_builder, self.task_dir, self.config,
                                          subtask_context=s)
                                for s in subtasks
                            ]
                            for fut in futures:
                                out = fut.result()
                                all_changed.extend(out.changed_files)
                                last_resp = out.response
                        self.task_dir.append_log("stage_end", cycle=cycle, stage=stage_idx)
                    if last_resp is not None:
                        self._checkpoint(
                            "I", cycle, last_resp,
                            {"changed_files": sorted(set(all_changed)),
                             "stages": len(stages)},
                        )
                else:
                    builder = run_builder(self.task_dir, self.config)
                    self._checkpoint(
                        "I", cycle, builder.response,
                        {"changed_files": builder.changed_files},
                    )
                state = self._save_progress(state, cycle=cycle, next_phase="V")
                next_phase = "V"

            if next_phase in {"V", "J"}:
                critic = run_critic(self.task_dir, self.config)
                self.task_dir.checkpoint(cycle, "V", {"check": critic.check.as_dict()})
                if critic.response is not None:
                    self._metric("critic", "V/J", cycle, critic.response)
                self.task_dir.checkpoint(cycle, "J", {"judge": critic.judge})
                state, status = self._handle_judge(state, cycle, critic.judge)
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
            best_exists=bool(state.get("best_exists", False)),
        )

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

    def _handle_judge(
        self,
        state: dict[str, Any],
        cycle: int,
        judge: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        passed = bool(judge.get("passed", False))
        better = bool(judge.get("better"))
        action = str(judge.get("action", "stop"))
        redo_count = int(state.get("redo_count", 0))

        if better:
            self.task_dir.snapshot_best(cycle=cycle)
            state["best_exists"] = True
            redo_count = 0
            self.task_dir.append_log("promote_best", cycle=cycle, passed=passed)
        else:
            restored = self.task_dir.restore_best()
            redo_count += 1
            self.task_dir.append_log(
                "rollback",
                cycle=cycle,
                passed=passed,
                restored=restored,
            )

        self.task_dir.append_log(
            "judge",
            cycle=cycle,
            passed=passed,
            better=better,
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
