"""Build a single-file Camera Test runner with Python's standard library."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default="dist/camera-test.pyz", type=Path)
    output = parser.parse_args().output
    root = Path(__file__).resolve().parent
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("__main__.py", "from camera_test import main\nraise SystemExit(main())\n")
        for name in ("camera_test.py", "camera_checks.py", "camera_hal.py"):
            archive.write(root / name, name)
    print(output.resolve())


if __name__ == "__main__":
    main()
