from __future__ import annotations

from agent.tooling import fs


def test_file_tools_respect_base_dir(tmp_path):
    base = tmp_path / "worktree-a"
    base.mkdir()

    assert "Wrote" in fs.run_write("src/app.txt", "hello", base_dir=base)
    assert (base / "src" / "app.txt").read_text(encoding="utf-8") == "hello"
    assert fs.run_read("src/app.txt", base_dir=base) == "hello"


def test_safe_path_rejects_base_dir_escape(tmp_path):
    base = tmp_path / "worktree-a"
    base.mkdir()

    assert fs.run_read("../outside.txt", base_dir=base).startswith("Error:")
