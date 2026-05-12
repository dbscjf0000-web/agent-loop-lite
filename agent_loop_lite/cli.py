from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_loop_lite import __version__
from agent_loop_lite.config import load_config
from agent_loop_lite.loop import LoopRunner
from agent_loop_lite.state import TaskDir, list_tasks, new_task_id


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command == "list":
        return _cmd_list(args)
    parser.print_help()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-lite")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Run a new RPIVJ task.")
    run.add_argument("task")
    _add_common_options(run)
    run.add_argument("--task-id", default=None)

    resume = sub.add_parser("resume", help="Resume a task from file state.")
    resume.add_argument("task_id")
    _add_common_options(resume)

    list_cmd = sub.add_parser("list", help="List task directories.")
    list_cmd.add_argument("--root", type=Path, default=Path(".agent_loop"))
    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--cycles", type=int, default=None)
    parser.add_argument("--max-redo", type=int, default=None)
    parser.add_argument("--planner-model", default=None)
    parser.add_argument("--builder-model", default=None)
    parser.add_argument("--critic-model", default=None)
    parser.add_argument("--verify-mode", choices=["none", "plan", "pytest", "shell"], default=None)
    parser.add_argument("--verify-command", default=None)


def _config_from_args(args: argparse.Namespace):
    cfg = load_config(args.config)
    return cfg.with_overrides(
        root=args.root,
        max_cycles=args.cycles,
        max_redo=args.max_redo,
        planner_model=args.planner_model,
        builder_model=args.builder_model,
        critic_model=args.critic_model,
        verify_mode=args.verify_mode,
        verify_command=args.verify_command,
    )


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    task_id = args.task_id or new_task_id()
    task_dir = TaskDir(cfg.root, task_id)
    result = LoopRunner(task_dir, cfg).run(args.task)
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    cfg = _config_from_args(args)
    task_dir = TaskDir(cfg.root, args.task_id)
    result = LoopRunner(task_dir, cfg).run()
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    for info in list_tasks(args.root):
        print(f"{info.task_id}\t{info.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
