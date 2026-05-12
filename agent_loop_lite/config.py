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
class Config:
    root: Path = Path(".agent_loop")
    max_cycles: int = 3
    max_redo: int = 2
    model_timeout_s: int = 600
    models: Models = field(default_factory=Models)
    verify: VerifyConfig = field(default_factory=VerifyConfig)

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

        return replace(
            self,
            root=root or self.root,
            max_cycles=max_cycles if max_cycles is not None else self.max_cycles,
            max_redo=max_redo if max_redo is not None else self.max_redo,
            models=models,
            verify=verify,
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
    )


def _read_toml(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as f:
        obj = tomllib.load(f)
    return obj if isinstance(obj, dict) else {}
