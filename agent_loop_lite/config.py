from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Models:
    planner: str = "mock"
    builder: str = "mock"
    critic: str = "rule"


@dataclass(frozen=True)
class VerifyConfig:
    mode: str = "plan"
    command: str = ""
    timeout_s: int = 30


@dataclass(frozen=True)
class StopGateConfig:
    """v2.3: Optional second-opinion check before the loop declares stop.

    When ``enabled`` is true, a fresh-context blocker check runs after the
    main Critic returns ``action == "stop"``. If the gate finds a concrete
    submission blocker, the loop converts the stop into a ``redo_P`` and
    appends the gate's evidence to the judge hint.

    ``model`` takes priority over ``auto``. When both are empty and
    ``auto`` is true, the loop picks a vendor different from the main
    Critic (claude → codex, codex → claude, gemini → codex, …).
    """

    enabled: bool = False
    model: str = ""
    auto: bool = True


@dataclass(frozen=True)
class Config:
    root: Path = Path(".agent_loop")
    max_cycles: int = 3
    max_redo: int = 2
    model_timeout_s: int = 600
    models: Models = field(default_factory=Models)
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    stop_gate: StopGateConfig = field(default_factory=StopGateConfig)

    def with_overrides(
        self,
        *,
        root: Path | None = None,
        max_cycles: int | None = None,
        max_redo: int | None = None,
        planner_model: str | None = None,
        builder_model: str | None = None,
        critic_model: str | None = None,
        verify_mode: str | None = None,
        verify_command: str | None = None,
        stop_gate_model: str | None = None,
        stop_gate_disable: bool = False,
    ) -> "Config":
        models = self.models
        if planner_model is not None:
            models = replace(models, planner=planner_model)
        if builder_model is not None:
            models = replace(models, builder=builder_model)
        if critic_model is not None:
            models = replace(models, critic=critic_model)

        verify = self.verify
        if verify_mode is not None:
            verify = replace(verify, mode=verify_mode)
        if verify_command is not None:
            verify = replace(verify, command=verify_command)

        stop_gate = self.stop_gate
        if stop_gate_model is not None:
            stop_gate = replace(stop_gate, enabled=True, model=stop_gate_model)
        if stop_gate_disable:
            stop_gate = replace(stop_gate, enabled=False)

        return replace(
            self,
            root=root or self.root,
            max_cycles=max_cycles if max_cycles is not None else self.max_cycles,
            max_redo=max_redo if max_redo is not None else self.max_redo,
            models=models,
            verify=verify,
            stop_gate=stop_gate,
        )


def load_config(path: Path | None = None) -> Config:
    if path is None:
        local = Path("agent-lite.toml")
        path = local if local.exists() else None
    if path is None:
        return Config()

    data = _read_toml(path)
    models = data.get("models") or {}
    verify = data.get("verify") or {}
    stop_gate = data.get("stop_gate") or {}
    return Config(
        root=Path(str(data.get("root", ".agent_loop"))),
        max_cycles=int(data.get("max_cycles", 3)),
        max_redo=int(data.get("max_redo", 2)),
        model_timeout_s=int(data.get("model_timeout_s", 600)),
        models=Models(
            planner=str(models.get("planner", "mock")),
            builder=str(models.get("builder", "mock")),
            critic=str(models.get("critic", "rule")),
        ),
        verify=VerifyConfig(
            mode=str(verify.get("mode", "plan")),
            command=str(verify.get("command", "")),
            timeout_s=int(verify.get("timeout_s", 30)),
        ),
        stop_gate=StopGateConfig(
            enabled=bool(stop_gate.get("enabled", False)),
            model=str(stop_gate.get("model", "")),
            auto=bool(stop_gate.get("auto", True)),
        ),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as f:
        obj = tomllib.load(f)
    return obj if isinstance(obj, dict) else {}
