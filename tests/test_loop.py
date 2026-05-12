import json

from agent_loop_lite.config import Config
from agent_loop_lite.loop import LoopRunner
from agent_loop_lite.state import TaskDir


def test_mock_loop_writes_rpivj_artifacts_and_best(tmp_path):
    td = TaskDir(tmp_path, "task1")
    result = LoopRunner(td, Config(root=tmp_path)).run("create a simple python file")

    assert result.status == "stop"
    assert result.best_exists is True
    assert (td.path / "r.md").exists()
    assert (td.path / "plan.md").exists()
    assert not (td.path / "rubric.json").exists()
    assert (td.path / "build.md").exists()
    assert (td.path / "check.json").exists()
    assert (td.path / "judge.json").exists()
    assert (td.workspace_path() / "solution.py").exists()
    assert (td.workspace_path() / "best" / "solution.py").exists()

    for phase in ("R", "P", "I", "V", "J"):
        assert (td.checkpoint_dir() / f"cycle_001_phase_{phase}.json").exists()


def test_config_can_select_worker_models(tmp_path):
    cfg_path = tmp_path / "agent-lite.toml"
    cfg_path.write_text(
        """
[models]
planner = "mock"
builder = "mock"
critic = "rule"
""",
        encoding="utf-8",
    )

    from agent_loop_lite.config import load_config

    cfg = load_config(cfg_path)
    assert cfg.models.planner == "mock"
    assert cfg.models.builder == "mock"
    assert cfg.models.critic == "rule"


def test_log_records_judge_decision(tmp_path):
    td = TaskDir(tmp_path, "task2")
    LoopRunner(td, Config(root=tmp_path)).run("create a file")

    rows = [
        json.loads(line)
        for line in (td.path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(row["event"] == "judge" and row["action"] == "stop" for row in rows)


def test_failed_rule_judge_writes_detailed_hint(tmp_path):
    from agent_loop_lite.config import VerifyConfig

    td = TaskDir(tmp_path, "task3")
    cfg = Config(
        root=tmp_path,
        verify=VerifyConfig(mode="shell", command="python missing.py"),
        max_cycles=1,
    )
    LoopRunner(td, cfg).run("create a file")

    judge = json.loads((td.path / "judge.json").read_text(encoding="utf-8"))
    assert judge["action"] == "redo_P"
    assert "What failed:" in judge["hint"]
    assert "Rerun:" in judge["hint"]
