from agent_loop_lite.state import TaskDir


def test_best_snapshot_round_trip(tmp_path):
    td = TaskDir(tmp_path, "abc123")
    td.init()
    ws = td.workspace_path()
    (ws / "solution.py").write_text("print('v1')\n", encoding="utf-8")
    (ws / "notes.md").write_text("old\n", encoding="utf-8")

    td.snapshot_best(cycle=1)
    (ws / "solution.py").write_text("print('bad')\n", encoding="utf-8")
    (ws / "notes.md").unlink()
    (ws / "extra.txt").write_text("remove me\n", encoding="utf-8")

    assert td.restore_best() is True
    assert (ws / "solution.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert (ws / "notes.md").read_text(encoding="utf-8") == "old\n"
    assert not (ws / "extra.txt").exists()
    assert (ws / "best" / "manifest.json").exists()
