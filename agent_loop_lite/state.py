from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def new_task_id(nbytes: int = 3) -> str:
    return secrets.token_hex(nbytes)


@dataclass(frozen=True)
class TaskInfo:
    task_id: str
    path: Path
    updated_at: float


class TaskDir:
    def __init__(self, root: Path, task_id: str) -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.path = self.root / task_id

    def init(self) -> None:
        for name in ("workspace", "checkpoints"):
            (self.path / name).mkdir(parents=True, exist_ok=True)
        for name in ("task.md", "log.jsonl", "metrics.jsonl"):
            p = self.path / name
            if not p.exists():
                p.touch()
        if not self.state_path().exists():
            self.save_state(
                {
                    "task_id": self.task_id,
                    "cycle": 1,
                    "next_phase": "R",
                    "redo_count": 0,
                    "best_exists": False,
                    "status": "new",
                }
            )

    def task_path(self) -> Path:
        return self.path / "task.md"

    def workspace_path(self) -> Path:
        return self.path / "workspace"

    def state_path(self) -> Path:
        return self.path / "state.json"

    def checkpoint_dir(self) -> Path:
        return self.path / "checkpoints"

    def read_text(self, name: str, default: str = "") -> str:
        p = self.path / name
        if not p.exists():
            return default
        return p.read_text(encoding="utf-8")

    def write_text(self, name: str, content: str) -> Path:
        p = self.path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def read_json(self, name: str, default: Any = None) -> Any:
        p = self.path / name
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8"))

    def write_json(self, name: str, content: dict[str, Any]) -> Path:
        return self.write_text(name, json.dumps(content, indent=2, ensure_ascii=False))

    def load_state(self) -> dict[str, Any]:
        return self.read_json("state.json", {})

    def save_state(self, state: dict[str, Any]) -> None:
        self.write_json("state.json", state)

    def checkpoint(self, cycle: int, phase: str, payload: dict[str, Any] | None = None) -> Path:
        body = {
            "task_id": self.task_id,
            "cycle": cycle,
            "phase": phase,
            "ts": time.time(),
            "payload": payload or {},
        }
        path = self.checkpoint_dir() / f"cycle_{cycle:03d}_phase_{phase}.json"
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def append_log(self, event: str, **fields: Any) -> None:
        self._append_jsonl("log.jsonl", {"ts": time.time(), "event": event, **fields})

    def append_metric(self, **fields: Any) -> None:
        self._append_jsonl("metrics.jsonl", {"ts": time.time(), **fields})

    def _append_jsonl(self, name: str, record: dict[str, Any]) -> None:
        with (self.path / name).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def snapshot_best(self, *, cycle: int) -> None:
        ws = self.workspace_path()
        tmp = ws / ".best.tmp"
        best = ws / "best"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)

        files: list[dict[str, Any]] = []
        for src in sorted(ws.iterdir()):
            if src.name in {"best", ".best.tmp", "__pycache__"}:
                continue
            if src.is_symlink():
                continue
            dst = tmp / src.name
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=False)
            elif src.is_file():
                shutil.copy2(src, dst)
            else:
                continue
            files.append(_file_meta(dst, src.name))

        manifest = {
            "passed": True,
            "cycle": cycle,
            "files": files,
            "created_at": time.time(),
        }
        (tmp / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if best.exists():
            shutil.rmtree(best)
        tmp.rename(best)

    def restore_best(self) -> bool:
        ws = self.workspace_path()
        best = ws / "best"
        if not best.is_dir():
            return False

        for entry in sorted(ws.iterdir()):
            if entry.name == "best":
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)

        for src in sorted(best.iterdir()):
            if src.name == "manifest.json":
                continue
            dst = ws / src.name
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=False)
            else:
                shutil.copy2(src, dst)
        return True


def list_tasks(root: Path) -> list[TaskInfo]:
    root = Path(root)
    if not root.exists():
        return []
    out: list[TaskInfo] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "state.json").exists():
            out.append(TaskInfo(child.name, child, child.stat().st_mtime))
    return out


def _file_meta(path: Path, name: str) -> dict[str, Any]:
    if path.is_dir():
        return {"name": name, "kind": "dir", "sha256": "", "size": 0}
    data = path.read_bytes()
    return {
        "name": name,
        "kind": "file",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
