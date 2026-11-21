"""Failure classes: who has to act when a run fails. Extend CLASS_BY_CODE to cover new error codes."""

ENVIRONMENT, AUTOMATION, PRODUCT = "environment", "automation", "product"

CLASS_BY_CODE = {
    **dict.fromkeys((
        "NO_DEVICE", "CAMERA_NOT_FOREGROUND", "CAMERA_APP_NOT_FOUND", "UI_UNAVAILABLE", "IO_ERROR",
        "RESULT_WRITE_FAILED", "VIDEO_COPY_INCOMPLETE", "INTERRUPTED",
    ), ENVIRONMENT),
    **dict.fromkeys((
        "MODE_NOT_FOUND", "MODE_NOT_READY", "INVALID_UI_BOUNDS", "UNSUPPORTED_CAMERA_VERSION",
        "INVALID_PROFILE_FILE", "INVALID_CHECKS_FILE", "INVALID_GOLDEN",
    ), AUTOMATION),
    **dict.fromkeys((
        "RECORD_START_UNVERIFIED", "RECORDING_ENDED_EARLY", "RECORD_STOP_UNVERIFIED", "VIDEO_NOT_SAVED",
        "AMBIGUOUS_VIDEO", "CAMERA_LAUNCH_FAILED", "CAMERA_PID_NOT_FOUND", "CLEANUP_UNVERIFIED",
    ), PRODUCT),
}
CLASS_BY_PREFIX = (("ADB_", ENVIRONMENT), ("DEVICE_", ENVIRONMENT), ("PRECHECK_", ENVIRONMENT))


def failure_class(code, diagnosis=None):
    """Classify an error code. A start that was never logged is automation when the log tag was seen but did not match."""
    if code == "RECORD_START_UNVERIFIED":
        stats = (diagnosis or {}).get("record_state_log", {})
        if stats.get("lines", 0) > 0 and stats.get("matched", 0) == 0:
            return AUTOMATION
    if code in CLASS_BY_CODE:
        return CLASS_BY_CODE[code]
    return next((value for prefix, value in CLASS_BY_PREFIX if code.startswith(prefix)), "unknown")
