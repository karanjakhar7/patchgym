"""Deterministic executable-solver example for the tiny task."""

import os
from pathlib import Path

workspace = Path(os.environ["TASK_WORKSPACE"])
source = workspace / "tinycalc.py"
contents = source.read_text(encoding="utf-8")
contents = contents.replace(
    "        return 0\n",
    '        raise ValueError("divisor must not be zero")\n',
)
source.write_text(contents, encoding="utf-8")

