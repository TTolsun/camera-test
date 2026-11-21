"""Common ADB transport, device selection, UI and logcat operations."""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from xml.etree import ElementTree

from .logcat import LOG_CLOCK_PATTERN


class RunError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def adb_path():
    if os.environ.get("CAMERA_TEST_ADB"):
        return os.environ["CAMERA_TEST_ADB"]
    if on_path := shutil.which("adb"):
        return on_path
    sdk_roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    if local_app_data := os.environ.get("LOCALAPPDATA"):
        sdk_roots.append(Path(local_app_data) / "Android" / "Sdk")
    if sys.platform == "darwin":
        sdk_roots.append(Path.home() / "Library" / "Android" / "sdk")
    sdk_roots.append(Path.home() / "Android" / "Sdk")
    for root in filter(None, sdk_roots):
        executable = "adb.exe" if os.name == "nt" else "adb"
        candidate = Path(root) / "platform-tools" / executable
        if candidate.is_file():
            return str(candidate)
    raise RunError("ADB_NOT_FOUND", "Install Android SDK Platform-Tools or set CAMERA_TEST_ADB.")


def adb(*args, timeout=12, binary=False):
    try:
        result = subprocess.run(
            [adb_path(), *args], capture_output=True, timeout=timeout, check=True
        )
    except FileNotFoundError as exc:
        raise RunError("ADB_NOT_FOUND", "Check the CAMERA_TEST_ADB path.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RunError("ADB_TIMEOUT", f"ADB timed out while running {args[0]}.") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RunError("ADB_ERROR", detail[:240] or "ADB command failed.") from exc
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")


def select_device(serial):
    devices = {}
    for line in adb("devices").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2:
            devices[fields[0]] = fields[1]
    if serial:
        if devices.get(serial) == "device":
            return serial
        raise RunError("DEVICE_UNAVAILABLE", f"Requested device state: {devices.get(serial, 'not listed')}.")
    physical = [key for key, state in devices.items() if state == "device" and not key.startswith("emulator-")]
    if len(physical) == 1:
        return physical[0]
    if physical:
        raise RunError("DEVICE_REQUIRED", "More than one device is ready. Specify --device.")
    raise RunError("NO_DEVICE", "Connect and authorize a Galaxy phone with ADB.")


@contextmanager
def device_lock(serial):
    key = sha256(serial.encode()).hexdigest()[:24]
    with (Path(tempfile.gettempdir()) / f"camera-test-{key}.lock").open("ab+") as lock:
        try:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunError("DEVICE_BUSY", "Another camera-test process has locked this device.") from exc
        yield


class Device:
    def __init__(self, serial, run_id):
        self.serial = serial
        self.dump_path = f"/data/local/tmp/camera-test-{run_id}.xml"
        self.last_ui = None  # Raw XML of the latest UI dump, kept for failure diagnosis.

    def shell(self, *args, timeout=12):
        return adb("-s", self.serial, "shell", shlex.join(args), timeout=timeout).strip()

    def ui(self):
        path = shlex.quote(self.dump_path)
        script = f"uiautomator dump {path} >/dev/null && cat {path}; rm -f {path}"
        data = self.shell("sh", "-c", script, timeout=14)
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as exc:
            raise RunError("UI_UNAVAILABLE", "Camera UI hierarchy is unavailable. Check unlock state or active recording.") from exc
        self.last_ui = data
        return root

    def tap(self, point):
        x, y = point
        self.shell("input", "tap", str(x), str(y))

    def package_foreground(self, package):
        activities = self.shell("dumpsys", "activity", "activities")
        return bool(re.search(rf"topResumedActivity=[^\n]*\s{re.escape(package)}/", activities))

    def screenshot(self):
        return adb("-s", self.serial, "exec-out", "screencap", "-p", binary=True)

    def pull(self, remote, local, timeout=60):
        adb("-s", self.serial, "pull", remote, str(local), timeout=timeout)

    def clock(self):
        stamp = self.shell("date", "+%m-%d %H:%M:%S.%3N")
        if not LOG_CLOCK_PATTERN.fullmatch(stamp):
            raise RunError("DEVICE_CLOCK", "Could not read a logcat timestamp from the device.")
        return stamp

    def logs(self, since, tags):
        filters = [*(f"{tag}:V" for tag in tags), "*:S"] if tags else []
        return self.shell(
            "logcat", "-d", "-T", since, "-v", "threadtime", "-b", "main", "-b", "system",
            "-b", "crash", *filters
        )

    def metadata(self):
        properties = self.shell("getprop")
        props = dict(re.findall(r"^\[([^\]]+)\]: \[(.*)\]$", properties, re.M))
        return {
            "model": props.get("ro.product.model"),
            "manufacturer": props.get("ro.product.manufacturer"),
            "android": props.get("ro.build.version.release"),
            "board_platform": props.get("ro.board.platform"),
            "soc_model": props.get("ro.soc.model"),
            "soc_manufacturer": props.get("ro.soc.manufacturer"),
            "soc_vendor": soc_vendor(props),
        }

    def package_version(self, package):
        version = re.search(r"^\s*versionName=(\S+)", self.shell("dumpsys", "package", package), re.M)
        return version[1] if version else None


def soc_vendor(props):
    manufacturer = props.get("ro.soc.manufacturer", "").casefold()
    if manufacturer in {"qti", "qualcomm", "qualcomm technologies, inc.", "qcom"}:
        return "qualcomm"
    if manufacturer in {"exynos", "samsung exynos", "samsung lsi", "samsung system lsi"}:
        return "exynos"
    hardware = {props.get(key, "").casefold() for key in (
        "ro.hardware", "ro.boot.hardware", "ro.board.platform", "ro.soc.model"
    )}
    if "qcom" in hardware:
        return "qualcomm"
    if any(re.fullmatch(r"exynos(?:[ _-]?\d+)?|s5e\d{4}", name) for name in hardware):
        return "exynos"
    return "unknown"
