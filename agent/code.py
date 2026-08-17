#!/usr/bin/env python3
"""Compatibility script entry point.

Run from the repository root with:

    python3 agent/code.py

or run the package directly with:

    python3 -m agent
"""

from __future__ import annotations

from pathlib import Path
import sys


if __package__ in (None, ""):
    # When this file is executed as a script, add the parent directory so
    # absolute package imports like agent.runtime.cli keep working.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.runtime.cli import main


if __name__ == "__main__":
    main()
