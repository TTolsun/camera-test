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
        archive.write(root / "camera_test.py", "camera_test.py")
        for module in sorted((root / "camera_runner").rglob("*.py")):
            archive.write(module, module.relative_to(root).as_posix())
        preview = root / "camera_runner" / "assets" / "preview.jar"
        archive.write(preview, preview.relative_to(root).as_posix())
    print(output.resolve())


if __name__ == "__main__":
    main()
