"""One reproducible, offline-capable test command for local and CI use."""

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command = [str(python if python.exists() else Path(sys.executable)), "-m", "pytest"]
    return subprocess.run(command, cwd=root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
