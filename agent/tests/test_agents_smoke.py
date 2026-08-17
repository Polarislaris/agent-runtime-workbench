from __future__ import annotations

from pathlib import Path
import py_compile
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
AGENT_FILES = sorted(
    path
    for path in ROOT.rglob("*.py")
    if "tests" not in path.relative_to(ROOT).parts
)
AGENT_IDS = [path.name for path in AGENT_FILES]


@pytest.mark.parametrize("agent_path", AGENT_FILES, ids=AGENT_IDS)
def test_agent_scripts_compile(agent_path: Path) -> None:
    # Write bytecode into a temporary file so the test never depends on the
    # user's global Python cache directory being writable.
    with tempfile.NamedTemporaryFile(suffix=".pyc") as pyc:
        _ = py_compile.compile(str(agent_path), cfile=pyc.name, doraise=True)


def test_agent_scripts_exist() -> None:
    assert AGENT_FILES, "expected at least one agent script"
