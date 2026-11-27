"""Run the prototype with its isolated interpreter, from any working directory."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    project = Path(__file__).resolve().parent
    interpreter = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.exists():
        print(
            "Prototype environment missing. Run uv sync --locked in " + str(project),
            file=sys.stderr,
        )
        return 1
    return subprocess.call(
        [str(interpreter), "-m", "office_spade", *(sys.argv[1:] or ["demo"])], cwd=project
    )


if __name__ == "__main__":
    raise SystemExit(main())
