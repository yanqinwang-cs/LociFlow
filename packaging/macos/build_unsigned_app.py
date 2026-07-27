from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGING_ROOT = PROJECT_ROOT / "packaging" / "macos"
SPEC_PATH = PACKAGING_ROOT / "loci_flow.spec"
BUILD_PATH = PACKAGING_ROOT / "build"
DIST_PATH = PACKAGING_ROOT / "dist"
APP_PATH = DIST_PATH / "Loci Flow.app"


def main() -> int:
    if sys.platform != "darwin":
        print("The unsigned Loci Flow application can only be built on macOS.")
        return 2
    if importlib.util.find_spec("PyInstaller") is None:
        print('Install the build extra with: uv sync --extra macos-build')
        return 2
    if not SPEC_PATH.is_file():
        print("The checked-in Loci Flow packaging specification is unavailable.")
        return 1

    for output in (BUILD_PATH, DIST_PATH):
        if output.exists():
            shutil.rmtree(output)

    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = "0"
    environment.setdefault("SOURCE_DATE_EPOCH", "946684800")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--workpath",
            str(BUILD_PATH),
            "--distpath",
            str(DIST_PATH),
            str(SPEC_PATH),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    if completed.returncode != 0 or not APP_PATH.is_dir():
        print("The unsigned Loci Flow application could not be built.")
        return 1
    print(APP_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
