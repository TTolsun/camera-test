from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from camera_runner import adb
from camera_runner.preview import PreviewDevice


PACKAGE = "com.sec.android.app.camera"
VALID = f'<hierarchy><node package="{PACKAGE}" /></hierarchy>'
OUTPUT = "/data/local/tmp/camera-test-unit-preview-1.xml"
NESTED_OUTPUT = "/data/local/tmp/local/tmp/camera-test-unit-preview-1.xml"
JAR = "/data/local/tmp/camera-test-unit.jar"
UNRELATED = "/data/local/tmp/someone-elses.xml"


class PreviewTransport:
    def __init__(self):
        self.files = {OUTPUT: VALID, NESTED_OUTPUT: VALID, UNRELATED: VALID}
        self.outputs = [VALID]
        self.pushes = 0
        self.stock_reads = 0
        self.preview_reads = 0
        self.run_result = "OK (1 test)"

    def __call__(self, *args, **kwargs):
        if args[2] == "push":
            assert Path(args[3]).is_file()
            self.files[args[4]] = "jar"
            self.pushes += 1
            return ""
        remote = shlex.split(args[3])
        if remote == ["chmod", "0444", JAR]:
            assert JAR in self.files
            return ""
        if remote[:2] == ["rm", "-f"]:
            for path in remote[2:]:
                self.files.pop(path, None)
            return ""
        if remote[:2] == ["uiautomator", "runtest"]:
            assert remote[2:7] == [JAR, "-s", "-c", "cameratest.PreviewSnapshot", "-e"]
            assert remote[7] == "file"
            self.preview_reads += 1
            path = f"/data/local/tmp/{remote[8]}"
            assert path not in self.files and f"/data/local/tmp/local/tmp/{remote[8]}" not in self.files
            if (response := self.outputs.pop(0)) is not None:
                self.files[path] = response
            return self.run_result
        if remote[:1] == ["cat"]:
            if remote[1] not in self.files:
                raise adb.RunError("ADB_ERROR", "No such file")
            return self.files[remote[1]]
        if remote[:2] == ["sh", "-c"] and remote[2].startswith("uiautomator dump "):
            self.stock_reads += 1
            return VALID
        raise AssertionError(remote)


class PreviewTests(unittest.TestCase):
    def test_preview_uses_fresh_xml_and_cleans_up_only_its_own_files(self):
        transport = PreviewTransport()
        transport.outputs = [VALID, VALID]
        with patch.object(adb, "adb", side_effect=transport), PreviewDevice("phone", "unit") as device:
            self.assertEqual(device.ui().find("node").get("package"), PACKAGE)
            self.assertEqual(device.ui().find("node").get("package"), PACKAGE)
            self.assertEqual(device.last_ui, VALID)
            self.assertEqual(device.ui_backends, {"preview"})
            self.assertEqual(set(transport.files), {JAR, UNRELATED})
        self.assertEqual((transport.pushes, transport.preview_reads, transport.stock_reads), (1, 2, 0))
        self.assertEqual(set(transport.files), {UNRELATED})

    def test_failed_or_empty_preview_falls_back_and_leaves_no_artifacts(self):
        for result, output in (("OK (1 test)", None), ("OK (1 test)", "<hierarchy />"),
                               ("OK (1 test)", "malformed"), ("FAILURES!!!", VALID)):
            with self.subTest(result=result, output=output):
                transport = PreviewTransport()
                transport.outputs = [output]
                transport.run_result = result
                with patch.object(adb, "adb", side_effect=transport), PreviewDevice("phone", "unit") as device:
                    self.assertEqual(device.ui().find("node").get("package"), PACKAGE)
                    self.assertEqual(device.ui_backends, {"standard"})
                    self.assertEqual(device.last_ui, VALID)
                self.assertEqual((transport.preview_reads, transport.stock_reads), (1, 1))
                self.assertEqual(set(transport.files), {UNRELATED})

    def test_zipapp_contains_a_readable_dex_resource(self):
        root = Path(__file__).resolve().parent.parent
        with TemporaryDirectory() as output_dir:
            package = Path(output_dir) / "camera-test.pyz"
            subprocess.run([sys.executable, str(root / "build.py"), str(package)], check=True, capture_output=True)
            with ZipFile(package) as archive:
                actual = archive.read("camera_runner/assets/preview.jar")
                self.assertEqual(actual, (root / "camera_runner/assets/preview.jar").read_bytes())
            subprocess.run([
                sys.executable, "-c",
                'import sys; sys.path.insert(0, sys.argv[1]); '
                'from importlib.resources import as_file, files; '
                'from zipfile import ZipFile; '
                'resource = files("camera_runner").joinpath("assets").joinpath("preview.jar");\n'
                'with as_file(resource) as jar, ZipFile(jar) as archive:\n'
                '    assert archive.read("classes.dex").startswith(b"dex\\n")',
                str(package),
            ], check=True, cwd=output_dir, capture_output=True)
