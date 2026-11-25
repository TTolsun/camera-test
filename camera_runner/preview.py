"""Run-scoped, read-only preview hierarchy capture for Android camera capture."""

from importlib.resources import as_file, files
from pathlib import PurePosixPath
import re
from xml.etree import ElementTree

from . import adb


class PreviewDevice(adb.Device):
    """Use a bundled UiAutomator test with no idle wait, and fall back to the standard dump."""

    def __init__(self, serial, run_id):
        super().__init__(serial, run_id)
        self.preview_jar = str(PurePosixPath(self.dump_path).with_suffix(".jar"))
        self.preview_counter = 0
        self.preview_attempted = False
        self.preview_started = False
        self.preview_disabled = False
        self.ui_backends = set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.preview_attempted:
            self.remove(self.preview_jar)

    def remove(self, *paths):
        try:
            self.shell("rm", "-f", *paths)
        except adb.RunError:
            pass  # A disconnected device cannot be cleaned up until it reconnects.

    def preview_ui(self):
        if not self.preview_attempted:
            self.preview_attempted = True
            with as_file(files("camera_runner").joinpath("assets").joinpath("preview.jar")) as jar:
                adb.adb("-s", self.serial, "push", str(jar), self.preview_jar)
            self.shell("chmod", "0444", self.preview_jar)
        self.preview_counter += 1
        stem = PurePosixPath(self.dump_path).stem
        name = f"{stem}-preview-{self.preview_counter}.xml"
        output = str(PurePosixPath(self.dump_path).with_name(name))
        # Some Galaxy firmware resolves getDataDirectory() to /data/local/tmp in this test process.
        nested_output = f"/data/local/tmp/local/tmp/{name}"
        paths = (output, nested_output)
        try:
            self.shell("rm", "-f", *paths)
            result = self.shell("uiautomator", "runtest", self.preview_jar, "-s", "-c",
                                "cameratest.PreviewSnapshot", "-e", "file", name, timeout=20)
            self.preview_started |= bool(re.search(r"(?m)^(OK \(|FAILURES!!!)", result))
            if not re.search(r"(?m)^OK \(1 test\)\s*$", result):
                raise adb.RunError("UI_UNAVAILABLE", "The preview snapshot helper did not complete.")
            data = self.shell("cat", output)
            try:
                root = ElementTree.fromstring(data)
            except ElementTree.ParseError as exc:
                raise adb.RunError("UI_UNAVAILABLE", "The preview snapshot was not valid XML.") from exc
            if root.tag != "hierarchy" or not any(node.get("package") for node in root.iter("node")):
                raise adb.RunError("UI_UNAVAILABLE", "The preview snapshot contained no UI nodes.")
            self.last_ui = data
            return root
        finally:
            self.remove(*paths)

    def ui(self):
        if not self.preview_disabled:
            try:
                root = self.preview_ui()
                self.ui_backends.add("preview")
                return root
            except (adb.RunError, OSError):
                if not self.preview_started:
                    # Keep the standard backend when this Android build cannot run the helper.
                    self.preview_disabled = True
        root = super().ui()
        self.ui_backends.add("standard")
        return root
